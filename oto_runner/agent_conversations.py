"""Le chemin ONE-SHOT : l'API Conversations de Mistral, outils exécutés CHEZ EUX.

Pourquoi il existe : l'API stateless re-facture tout l'historique à chaque tour
de boucle — 58 % du coût d'un job mesuré au vol v86, la 5e fiche d'un fil coûte
6,6× la première. Conversations orchestre les appels d'outils CÔTÉ MISTRAL (le
connecteur MCP oto y est déclaré) dans UN appel facturé une fois : ~26 k jetons
par fiche mesurés sur 194 fiches réelles, plat au rang. Décision Alexis du
19/08 : la campagne bascule dessus avant tout lancement.

Ce que ce chemin CHANGE : la boucle ne tourne plus dans le worker — il ne voit
pas les tours, il reçoit le RÉSULTAT (les `outputs` : entrées typées, dont les
exécutions d'outils une à une). La surveillance est PRÉSERVÉE au grain job :
usage, motif d'arrêt, pas, `tool_counts` sont dérivés des outputs — ce qui se
PERD est le verbatim des tours intermédiaires (`store=False` : rien ne reste
chez Mistral — c'est une contrainte de CONFORMITÉ du contrat client, pas un
réglage de confort).

⚠️ En requests NU + deadline SIGALRM — JAMAIS le SDK mistralai : son
`timeout_ms` devient un timeout httpx dont le `read` se réarme à chaque octet
reçu (basesdk.py:227) — 20 heures de gel silencieux sur la boucle locale le
18/08. Le SDK ne rejoue pas non plus les transitoires sur cet appel : les
rejeux sont ICI, explicites.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

from .agent_runtime import AgentResult, AgentStep
from .deadline import DeadlineExceeded, post_with_deadline  # noqa: F401 — DeadlineExceeded
# fait partie du contrat d'erreur de run_once (remonte au job, jamais rejouée ici).

logger = logging.getLogger("oto_runner")

ONE_SHOT = True                      # le worker choisit le chemin là-dessus

_ENV_KEY = "OTO_RUNNER_OPENAI_API_KEY"   # même clé que le mode openai (La Plateforme)
_ENV_BASE = "OTO_RUNNER_OPENAI_BASE"
_DEFAULT_BASE = "https://api.mistral.ai"
DEFAULT_MODEL = "mistral-large-latest"

# Un one-shot porte un run ENTIER (les tours d'outils compris) : la deadline est
# large — mais TOUJOURS plus courte que le bail one-shot du worker (1800 s), sinon
# un pair re-claimerait un job dont la conversation tourne encore.
_WALL_S = 900


class LlmUnavailable(RuntimeError):
    pass


def model() -> str:
    return os.environ.get("OTO_RUNNER_MODEL") or DEFAULT_MODEL


def resolve_key() -> str:
    key = os.environ.get(_ENV_KEY, "").strip()
    if not key:
        raise LlmUnavailable(f"{_ENV_KEY} absente de l'environnement du worker")
    return key


def connector_id() -> str:
    cid = os.environ.get("OTO_RUNNER_CONNECTOR_ID", "").strip()
    if not cid:
        raise LlmUnavailable(
            "OTO_RUNNER_CONNECTOR_ID absent : le mode conversations exécute les "
            "outils chez Mistral via un connecteur MCP déclaré côté compte — sans "
            "son id, aucun outil. (Le connecteur porte son propre secret vers "
            "mcp.oto.cx ; le worker n'en voit jamais.)")
    return cid


_TRANSITOIRE = (429, 500, 502, 503, 504)


def run_once(*, instructions: str, inputs: str, tools,
             api_key: Optional[str] = None) -> AgentResult:
    """UNE conversation complète (outils compris, côté Mistral) → AgentResult.

    Rejeux : 2, espacés, sur les seuls TRANSITOIRES HTTP — un DeadlineExceeded
    (>15 min murales) remonte tel quel : le retry de JOB décide, re-payer
    aveuglément un run entier n'est pas une politique de rejeu."""
    corps = {
        "model": model(),
        "inputs": inputs,
        "instructions": instructions,
        "tools": [{"type": "connector", "connector_id": connector_id(),
                   "tool_configuration": {"include": list(tools or ())}}],
        "store": False,
        "stream": False,
    }
    entetes = {"Authorization": f"Bearer {api_key or resolve_key()}",
               "Content-Type": "application/json"}
    base = (os.environ.get(_ENV_BASE) or _DEFAULT_BASE).rstrip("/")
    if base.endswith("/v1"):
        # La base openai-compat porte déjà /v1 (l'env existante de la box) : la
        # réutiliser telle quelle donnait /v1/v1/conversations → 404 « no Route
        # matched » (vécu au premier appel réel de l'essai).
        base = base[:-3]
    url = f"{base}/v1/conversations"
    derniere = None
    for essai in range(3):
        # read 660 s : une conversation exécute ses outils côté Mistral SANS
        # streamer — un silence de >5 min y est normal, pas une panne (vécu :
        # un run légitime coupé à read=300 dès la 3e fiche de campagne). La
        # deadline MURALE (900 s) reste le seul vrai couperet.
        r = post_with_deadline(url, json=corps, headers=entetes,
                               timeout=(10, 660), wall_s=_WALL_S)
        if r.status_code in _TRANSITOIRE:
            derniere = f"{r.status_code} : {r.text[:200]}"
            logger.warning("conversations %s (essai %s/3)", derniere, essai + 1)
            time.sleep(5 * (essai + 1))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"conversations → {r.status_code} : {r.text[:300]}")
        return _parse(r.json(), tools)
    raise RuntimeError(f"conversations → transitoire persistant ({derniere})")


def _nom_outil(name: str, tools) -> str:
    """Le connecteur Mistral PRÉFIXE les noms d'outils de son propre nom
    (`oto-11aout_data_write`) : sans normalisation, les `tool_counts` du bilan
    ne matchent plus les noms de la plateforme — 13 jobs à « zéro data_write »
    alors que les fiches étaient écrites (vécu à l'essai des 20). On normalise
    par l'ALLOWLIST du job : exacte, courte, jamais une devinette de préfixe."""
    for t in tools or ():
        if name == t or name.endswith("_" + t):
            return t
    return name


def _parse(d: dict, tools=()) -> AgentResult:
    """Les `outputs` d'une conversation → le contrat AgentResult du worker.

    Défensif sur la forme (chunks texte ou chaîne nue) — le banc fige ce qui est
    parsé, l'essai réel des 20 fiches valide contre le service."""
    steps: list[AgentStep] = []
    textes: list[str] = []
    for e in d.get("outputs") or []:
        typ = (e or {}).get("type") or ""
        if typ == "tool.execution":
            steps.append(AgentStep(tool=_nom_outil(e.get("name") or "?", tools),
                                   ok=True, duration_ms=0))
        elif typ.startswith("message"):
            contenu = e.get("content")
            if isinstance(contenu, list):
                contenu = "".join(c.get("text", "") for c in contenu
                                  if isinstance(c, dict))
            if contenu:
                textes.append(str(contenu))
        elif typ == "function.call":
            # Un connecteur bien configuré exécute côté serveur ; un function.call
            # NU qui remonte signifierait « à toi de jouer » — ce chemin ne joue
            # pas : il le COMPTE comme un pas non exécuté, visible au bilan.
            steps.append(AgentStep(tool=_nom_outil(e.get("name") or "?", tools),
                                   ok=False,
                                   duration_ms=0, error="function.call non exécuté"))
    u = d.get("usage") or {}
    usage = {"input_tokens": int(u.get("prompt_tokens") or 0),
             "output_tokens": int(u.get("completion_tokens") or 0)}
    return AgentResult(reply="\n".join(textes).strip(), steps=steps,
                       stopped="end_turn", usage=usage)
