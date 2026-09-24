"""La voie `claude-subscription` : le travail tourne sur l'ABONNEMENT Claude de son porteur.

Famille `claude_subscription` (oto-backend#1043, modèles `sub:*`). Le travail ne
s'exécute pas ici : il s'exécute dans le BAC de la personne — un utilisateur Unix
de la box de la ferme, où elle a connecté elle-même son abonnement au CLI Claude
Code officiel (dépôt `otomata-tech/ferme-claude`). Ce module demande le run à
l'AGENT de la box, qui le lance dans le bac et en renvoie le flux.

Ce qui ne passe JAMAIS par ici : l'identifiant de l'abonnement. Il vit dans le
HOME du bac ; le worker ne reçoit du backend qu'un `sandbox_id`, et ne remet à
l'agent que ce que le travail porte déjà — la consigne, l'allowlist, le jeton
DÉLÉGUÉ (borné au bail) pour le relais MCP (`relais_mcp`).

Chemin ONE-SHOT : la boucle d'outils tourne dans le CLI, le worker reçoit le
résultat. Le bail se PROLONGE pendant le run, à chaque événement du flux, au plus
une fois par `_PROLONGER_S` — un déroulé plus long que son bail libérerait la
personne pendant que le CLI tourne encore (deux exécutions sur une session).

Ce que le worker rapporte au backend en plus : `abonnement` — l'état du forfait
tel que le fournisseur l'annonce (`rate_limit_event`), et `deconnecte` quand le
bac n'est plus connecté. C'est ce qui met la personne en attente au seuil, avant
le refus.

Environnement : `OTO_FERME_URL` (l'agent, sur le réseau privé de la box),
`OTO_FERME_TOKEN` (son jeton), `OTO_MCP_URL`.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Callable, Optional

import requests

from .agent_runtime import AgentResult, AgentStep

logger = logging.getLogger("oto_runner")

ONE_SHOT = True     # le worker choisit le chemin là-dessus
BAC = True          # … et remet à ce provider le contexte du bac (cf. worker._traiter)

FAMILLE = "claude_subscription"
DEFAULT_MODEL = "sub:sonnet"
_PREFIXE_MODELE = "sub:"
_PREFIXE_OUTIL = "mcp__oto__"     # le nom que le CLI donne aux outils du serveur `oto`
_ENV_AGENT = "OTO_FERME_URL"
_ENV_JETON = "OTO_FERME_TOKEN"
_PROLONGER_S = 60
_LECTURE_S = 600    # silence toléré entre deux événements (un outil lent)

# Les fins du CLI (`result.subtype`) → le vocabulaire de la boucle (`AgentResult.stopped`).
_FINS = {"success": "end_turn", "error_max_turns": "max_steps"}


class LlmUnavailable(RuntimeError):
    pass


def model() -> str:
    return os.environ.get("OTO_RUNNER_MODEL") or DEFAULT_MODEL


def depot() -> str:
    return FAMILLE


def resolve_key() -> str:
    """Aucune clé : ce worker ne paie rien. Ce qu'il lui faut, c'est l'agent de la box."""
    for nom in (_ENV_AGENT, _ENV_JETON):
        if not (os.environ.get(nom) or "").strip():
            raise LlmUnavailable(f"{nom} absent de l'environnement du worker")
    return ""


def _modele_du_cli(modele: str) -> str:
    """`sub:opus` → `opus` : le CLI connaît les alias, le catalogue les préfixe."""
    if not modele.startswith(_PREFIXE_MODELE):
        raise ValueError(f"modèle `{modele}` hors de la famille {FAMILLE} (préfixe `{_PREFIXE_MODELE}`)")
    return modele[len(_PREFIXE_MODELE):]


def _nom_outil(nom: str) -> str:
    return nom[len(_PREFIXE_OUTIL):] if nom.startswith(_PREFIXE_OUTIL) else nom


def lire_flux(evenements, on_event=None, prolonger: Optional[Callable[[], None]] = None,
              horloge=time.monotonic) -> AgentResult:
    """Le flux `stream-json` du CLI (plus le résumé de l'agent) → `AgentResult`.

    Séparé du transport pour se tester sur un flux enregistré."""
    steps: list[AgentStep] = []
    en_vol: dict = {}          # tool_use_id → (nom, instant du départ)
    init, resultat, forfait, resume = {}, None, None, {}
    dernier = horloge()
    for ev in evenements:
        maintenant = horloge()      # UNE lecture par événement
        if on_event:
            on_event("claude", ev)
        if prolonger and maintenant - dernier >= _PROLONGER_S:
            prolonger()
            dernier = maintenant
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            init = ev
        elif t == "rate_limit_event":
            forfait = ev.get("rate_limit_info")
        elif t == "assistant":
            for bloc in (ev.get("message") or {}).get("content") or []:
                if bloc.get("type") == "tool_use":
                    en_vol[bloc.get("id")] = (_nom_outil(bloc.get("name") or "?"), maintenant)
        elif t == "user":
            for bloc in (ev.get("message") or {}).get("content") or []:
                if isinstance(bloc, dict) and bloc.get("type") == "tool_result":
                    nom, depart = en_vol.pop(bloc.get("tool_use_id"), ("?", maintenant))
                    steps.append(AgentStep(tool=nom, ok=not bloc.get("is_error"),
                                           duration_ms=int((maintenant - depart) * 1000)))
        elif t == "result":
            resultat = ev
        elif t == "ferme_resume":
            resume = ev
    if resume.get("connecte") is False:
        # Pas une exception : c'est une CONCLUSION que le backend doit lire
        # (`deconnecte` → la personne passe `needs_login`, ses travaux attendent).
        return AgentResult(reply="", stopped="fin_anormale",
                           defaut={"finish_reason": "bac_deconnecte"},
                           abonnement={"deconnecte": True})
    if resultat is None:
        raise RuntimeError(f"le CLI n'a rendu aucun résultat ({resume.get('erreur') or 'flux coupé'})")
    source = init.get("apiKeySource")
    if source != "none":
        # Une clé d'API dans le bac ferait payer quelqu'un d'autre que l'abonnement.
        raise RuntimeError(f"le run n'a pas tourné sur l'abonnement (apiKeySource={source!r})")
    erreur_du_cli = bool(resultat.get("is_error"))
    return AgentResult(
        reply=resultat.get("result") or "",
        steps=steps,
        # Une erreur du CLI (fournisseur, plafond atteint en vol) est une fin ANORMALE,
        # nommée comme telle : conclue `done`, elle cacherait un travail non fait.
        stopped=("fin_anormale" if erreur_du_cli
                 else _FINS.get(resultat.get("subtype"), resultat.get("subtype") or "no_reply")),
        defaut=({"finish_reason": resultat.get("subtype") or "erreur_du_cli"}
                if erreur_du_cli else None),
        usage=resultat.get("usage") or {},
        model=init.get("model"),
        abonnement=({"etat": forfait.get("status"), "fenetres": forfait.get("unifiedWindows")}
                    if forfait else None))


def run_once(*, instructions: str, inputs: str, tools, api_key: Optional[str] = None,
             modele: Optional[str] = None, on_event=None, bac: Optional[str] = None,
             mcp=None, prolonger: Optional[Callable[[], None]] = None) -> AgentResult:
    """UN run complet dans le bac `bac`, outils compris (côté CLI), → AgentResult."""
    if api_key:
        raise RuntimeError("un travail d'abonnement ne porte jamais de clé de modèle")
    if not bac:
        raise RuntimeError("travail d'abonnement sans `sandbox_id` : aucun bac où le lancer")
    if mcp is None:
        raise RuntimeError("travail d'abonnement sans session MCP : le relais n'aurait aucun contexte")
    resolve_key()
    corps = {
        "prompt": inputs,
        "model": _modele_du_cli(modele or model()),
        "system": instructions,
        "mcp": {"url": mcp.url, "token": mcp.token, "org": mcp.org, "project": mcp.project,
                "run_id": mcp.run_id, "tools": sorted(tools or ())},
    }
    url = f"{os.environ[_ENV_AGENT].rstrip('/')}/api/bacs/{bac}/runs"
    entetes = {"Authorization": f"Bearer {os.environ[_ENV_JETON]}",
               "Accept": "application/x-ndjson"}
    with requests.post(url, json=corps, headers=entetes, stream=True,
                       timeout=(10, _LECTURE_S)) as r:
        if r.status_code != 200:
            raise RuntimeError(f"agent de la ferme : {r.status_code} {r.text[:300]}")
        lignes = (json.loads(l) for l in r.iter_lines(decode_unicode=True) if l)
        return lire_flux(lignes, on_event=on_event, prolonger=prolonger)
