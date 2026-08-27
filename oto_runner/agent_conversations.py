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

## L'appel RENVOYÉ au client, et la relance du fil

En mode connecteur, aucun appel d'outil n'a à revenir au client : le
fournisseur les exécute. Un `function.call` en fin d'`outputs` est donc un fil
INTERROMPU — le fournisseur ne connaît pas l'outil demandé et rend la main. Sans
relance, le job conclut sans avoir rien écrit, tout en ayant payé le run entier.

`OTO_RUNNER_RELANCES_MAX` (entier, défaut 0 = jamais) autorise N relances. Une
relance est un NOUVEAU POST `/v1/conversations` : `store=False` interdit
`/restart`, qui repart d'une entrée STOCKÉE chez le fournisseur — rien n'est
stocké, le fil se rejoue donc entier par ses `inputs`. La référence de l'API
(docs.mistral.ai/api — `ConversationRequest.inputs`, type `ConversationInputs`)
accepte au choix une chaîne ou une liste d'entrées `InputEntries`, union de
`MessageInputEntry`, `MessageOutputEntry`, `FunctionResultEntry`,
`FunctionCallEntry`, `ToolExecutionEntry` et `AgentHandoffEntry` : les `outputs`
reçus y RETOURNENT tels quels, suivis d'une `FunctionResultEntry`
(`tool_call_id` + `result`, tous deux requis) par appel renvoyé.

## La version RÉSOLUE du modèle

Le modèle est nommé par un ALIAS (`mistral-large-latest`) : le fournisseur le
fait pointer ailleurs quand il le décide, sans préavis. Une anomalie de
campagne ne se date alors PAS — on ignore quel modèle a réellement tourné, et
sur quels jobs. `modele_resolu()` interroge `/v1/models` et rend la version
concrète derrière l'alias ; `run_once` la pose sur chaque `AgentResult`, et le
worker la déclare au job. C'est un relevé d'observabilité : il ne fait jamais
échouer un run que la campagne a payé.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

import requests

from .agent_runtime import AgentResult, AgentStep
from .deadline import DeadlineExceeded, post_with_deadline  # noqa: F401 — DeadlineExceeded
# fait partie du contrat d'erreur de run_once (remonte au job, jamais rejouée ici).

logger = logging.getLogger("oto_runner")

ONE_SHOT = True                      # le worker choisit le chemin là-dessus

_ENV_KEY = "OTO_RUNNER_OPENAI_API_KEY"   # même clé que le mode openai (La Plateforme)
_ENV_BASE = "OTO_RUNNER_OPENAI_BASE"
_ENV_RELANCES = "OTO_RUNNER_RELANCES_MAX"
_DEFAULT_BASE = "https://api.mistral.ai"
DEFAULT_MODEL = "mistral-large-latest"

# Un one-shot porte un run ENTIER (les tours d'outils compris) : la deadline est
# large — mais TOUJOURS plus courte que le bail one-shot du worker (1800 s), sinon
# un pair re-claimerait un job dont la conversation tourne encore.
_WALL_S = 900

_CONSIGNE_APPEL_RENVOYE = (
    "Cet outil n'existe pas. Les outils sont exécutés par le connecteur : "
    "appelle-les par leur nom exact, sans rédiger dans le nom de l'appel. "
    "Reprends là où tu en étais.")


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


def relances_max() -> int:
    """Combien de fois relancer un fil qui rend un appel au client. 0 = aucune."""
    brut = os.environ.get(_ENV_RELANCES, "").strip()
    if not brut:
        return 0
    if not brut.isdigit():
        raise LlmUnavailable(
            f"{_ENV_RELANCES} = {brut!r} : un entier positif ou nul est attendu")
    return int(brut)


_TRANSITOIRE = (429, 500, 502, 503, 504)

# La résolution d'alias est mise en cache AVEC UNE DURÉE : une bascule doit se
# voir dans l'heure de vol, pas au prochain redémarrage du worker.
_TTL_RESOLUTION_S = 600
_resolutions: dict = {}      # nom d'alias → (instant monotone, version concrète)


def base_api() -> str:
    """La base du service, SANS `/v1`.

    La base openai-compat de l'env de la box le porte déjà : la réutiliser
    telle quelle donnait `/v1/v1/conversations` → 404 « no Route matched »
    (vécu au premier appel réel de l'essai)."""
    base = (os.environ.get(_ENV_BASE) or _DEFAULT_BASE).rstrip("/")
    return base[:-3].rstrip("/") if base.endswith("/v1") else base


def _lister_modeles() -> list:
    """Le catalogue du fournisseur — `data`, une entrée par modèle."""
    r = requests.get(f"{base_api()}/v1/models",
                     headers={"Authorization": f"Bearer {resolve_key()}"},
                     timeout=(10, 30))
    if r.status_code >= 400:
        raise RuntimeError(f"models → {r.status_code} : {r.text[:200]}")
    return (r.json() or {}).get("data") or []


def _version_concrete(nom: str, data: list) -> Optional[str]:
    """`mistral-large-latest` → `mistral-large-2512`.

    Le catalogue porte les DEUX formes, et l'entrée d'un alias porte des
    `aliases` comme les autres : c'est donc l'ENSEMBLE des noms cités en alias
    qui dit lesquels flottent. Un `nom` absent de cet ensemble est déjà une
    version concrète — il se rend tel quel. Sinon la version est l'entrée qui
    le cite en alias et dont l'`id` ne flotte pas lui-même.

    Un nom que le catalogue ne connaît nulle part ne se devine pas : None."""
    flottants = {a for e in data for a in ((e or {}).get("aliases") or [])}
    ids = {(e or {}).get("id") for e in data}
    if nom not in flottants:
        return nom if nom in ids else None
    for e in data:
        eid = (e or {}).get("id")
        if eid and eid not in flottants and nom in ((e or {}).get("aliases") or []):
            return eid
    return None


def modele_resolu(nom: str) -> Optional[str]:
    """La version concrète derrière `nom`, ou None si elle ne s'établit pas.

    SEULE tolérance de ce chemin : une panne réseau (ou un catalogue muet) rend
    None, journalisé en warning — un relevé d'observabilité ne fait jamais
    échouer un job que la campagne a déjà payé. L'échec n'est PAS mis en cache :
    le job suivant retente."""
    fige = _resolutions.get(nom)
    if fige is not None and time.monotonic() - fige[0] < _TTL_RESOLUTION_S:
        return fige[1]
    try:
        data = _lister_modeles()
    except Exception as e:  # noqa: BLE001 — cf. docstring
        logger.warning("version de %s non résolue (%s) : le job ne la portera pas",
                       nom, e)
        return None
    resolu = _version_concrete(nom, data)
    if resolu is None:
        logger.warning("version de %s introuvable au catalogue (%s modèles) : "
                       "le job ne la portera pas", nom, len(data))
        return None
    _resolutions[nom] = (time.monotonic(), resolu)
    return resolu


def run_once(*, instructions: str, inputs: str, tools,
             api_key: Optional[str] = None) -> AgentResult:
    """UNE conversation complète (outils compris, côté Mistral) → AgentResult.

    Rejeux : 2, espacés, sur les seuls TRANSITOIRES HTTP — un DeadlineExceeded
    (>15 min murales) remonte tel quel : le retry de JOB décide, re-payer
    aveuglément un run entier n'est pas une politique de rejeu.

    Un fil qui rend un `function.call` au client est relancé au plus
    `OTO_RUNNER_RELANCES_MAX` fois ; les passes forment UN bilan (pas
    concaténés, jetons additionnés).

    Le bilan porte la version CONCRÈTE que l'alias résolvait au moment de
    l'appel : sans elle, une bascule d'alias ne se date pas après coup."""
    nom = model()
    corps = {
        "model": nom,
        "inputs": inputs,
        "instructions": instructions,
        "tools": [{"type": "connector", "connector_id": connector_id(),
                   "tool_configuration": {"include": list(tools or ())}}],
        "store": False,
        "stream": False,
    }
    entetes = {"Authorization": f"Bearer {api_key or resolve_key()}",
               "Content-Type": "application/json"}
    url = f"{base_api()}/v1/conversations"
    maxi = relances_max()
    # Relevé AVANT le premier POST : c'est la version en vigueur au moment de
    # l'appel qu'on enregistre, pas celle d'après un run d'un quart d'heure.
    resolu = modele_resolu(nom)
    cumul: Optional[AgentResult] = None
    for relance in range(maxi + 1):
        d = _poster(url, corps, entetes)
        cumul = _cumuler(cumul, _parse(d, tools))
        renvoyes = _appels_renvoyes(d)
        if not renvoyes or relance == maxi:
            break
        logger.info("conversation relancée (%s/%s) : function.call renvoyé « %s »",
                    relance + 1, maxi, str(renvoyes[0].get("name") or "?")[:60])
        corps = dict(corps, inputs=_entrees_de_relance(inputs, d, renvoyes))
    cumul.model = resolu
    return cumul


def _poster(url: str, corps: dict, entetes: dict) -> dict:
    """UN POST vers /v1/conversations, rejeux transitoires compris → son JSON."""
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
        return r.json()
    raise RuntimeError(f"conversations → transitoire persistant ({derniere})")


def _appels_renvoyes(d: dict) -> list:
    """La TRAÎNE de `function.call` qui termine les outputs — les appels rendus
    au client. En mode connecteur aucun n'a lieu d'être, quel qu'en soit le nom :
    tous se répondent, aucun ne se joue."""
    traine = []
    for e in reversed(d.get("outputs") or []):
        if ((e or {}).get("type") or "") != "function.call":
            break
        traine.append(e)
    traine.reverse()
    return traine


def _entree_initiale(inputs) -> list:
    if isinstance(inputs, str):
        return [{"object": "entry", "type": "message.input",
                 "role": "user", "content": inputs}]
    return list(inputs)


def _entrees_de_relance(inputs, d: dict, renvoyes: list) -> list:
    """Le fil rejoué : l'ordre initial, les outputs reçus tels quels, puis la
    réponse à chaque appel rendu."""
    entrees = _entree_initiale(inputs) + list(d.get("outputs") or [])
    for appel in renvoyes:
        tid = appel.get("tool_call_id")
        if not tid:
            raise RuntimeError(
                "conversations → function.call sans tool_call_id : la réponse "
                "ne permet pas de répondre à l'appel")
        entrees.append({"object": "entry", "type": "function.result",
                        "tool_call_id": tid,
                        "result": _CONSIGNE_APPEL_RENVOYE})
    return entrees


def _cumuler(cumul: Optional[AgentResult], passe: AgentResult) -> AgentResult:
    """Les passes d'un même job font UN bilan : le prix payé est leur somme, et
    le relevé des pas doit montrer l'appel rendu autant que ce qui l'a suivi."""
    if cumul is None:
        return passe
    textes = [t for t in (cumul.reply, passe.reply) if t]
    return AgentResult(
        reply="\n".join(textes),
        steps=list(cumul.steps) + list(passe.steps),
        stopped=passe.stopped,
        usage={c: int(cumul.usage.get(c) or 0) + int(passe.usage.get(c) or 0)
               for c in ("input_tokens", "output_tokens")},
        raw_outputs=passe.raw_outputs)


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
            # NU qui remonte signifie « à toi de jouer » — le pas est compté non
            # exécuté, visible au bilan, et la relance y répond quand elle est armée.
            steps.append(AgentStep(tool=_nom_outil(e.get("name") or "?", tools),
                                   ok=False,
                                   duration_ms=0, error="function.call non exécuté"))
    u = d.get("usage") or {}
    usage = {"input_tokens": int(u.get("prompt_tokens") or 0),
             "output_tokens": int(u.get("completion_tokens") or 0)}
    return AgentResult(reply="\n".join(textes).strip(), steps=steps,
                       stopped="end_turn", usage=usage,
                       raw_outputs=d.get("outputs"))
