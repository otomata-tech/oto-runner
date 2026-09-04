"""Adaptateur OpenAI-compatible — Scaleway Generative APIs d'abord, tout endpoint
compatible ensuite.

Même contrat que l'adaptateur Anthropic (`complete` + les 4 hooks de forme de
fil), implémenté en `requests` NU — pas de SDK : l'API OpenAI-compatible est un
POST JSON, et une dépendance de moins est une dépendance de moins. La cible par
défaut est Scaleway (`api.scaleway.ai/v1`) : modèles ouverts, hébergement
France, prix plancher — le tool calling y est PROUVÉ par l'appel (14/08, quatre
modèles, arguments corrects du premier coup).

La forme de fil OpenAI, confinée ici :
- le tour assistant se rejoue comme LE MESSAGE COMPLET rendu par l'API
  (`content` + `tool_calls` intacts — reconstruire les tool_calls casserait la
  corrélation par id) ;
- chaque résultat d'outil est UN message `role:"tool"` séparé, corrélé par
  `tool_call_id` — l'inverse exact d'Anthropic (un seul message user), et c'est
  précisément pour ça que la boucle ne connaît AUCUNE de ces deux formes.
"""
from __future__ import annotations

import json
import os
import signal
from typing import Optional

import requests

from .llm_types import LlmUnavailable, ToolCall, Turn

DEFAULT_BASE = "https://api.scaleway.ai/v1"
# gpt-oss-120b : le candidat du banc — 0,15/0,60 €/M, tool calling prouvé, et le
# même modèle que le spike Letta avait retenu. Surchargé par OTO_RUNNER_MODEL.
DEFAULT_MODEL = "gpt-oss-120b"
DEFAULT_MAX_TOKENS = 8192
_TIMEOUT = (10, 300)
# ⚠️ Le read timeout d'urllib3 se RÉARME à chaque octet reçu : un serveur qui
# goutte tient la connexion indéfiniment (vécu : un tour de modèle figé 35 min,
# pile bloquée dans ssl.read). Le plafond wall-clock coupe pour de vrai.
_WALL_TIMEOUT_S = 420


class _Deadline(Exception):
    pass


def _post_borne(url: str, corps: dict, entetes: dict):
    """POST avec un VRAI plafond de durée (SIGALRM — le worker est mono-thread).
    Lève LlmUnavailable au-delà : la boucle échoue proprement, le job se rejoue
    et REPREND son fil — jamais un process suspendu qu'il faut tuer à la main."""
    def _coupe(signum, frame):
        raise _Deadline()

    ancien = signal.signal(signal.SIGALRM, _coupe)
    signal.alarm(_WALL_TIMEOUT_S)
    try:
        return requests.post(url, json=corps, timeout=_TIMEOUT, headers=entetes)
    except _Deadline:
        raise LlmUnavailable(
            f"tour de modèle > {_WALL_TIMEOUT_S}s (deadline wall-clock) — "
            "le serveur gouttait sans conclure") from None
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, ancien)

_ENV_KEY = "OTO_RUNNER_OPENAI_API_KEY"
_ENV_BASE = "OTO_RUNNER_OPENAI_BASE"


def model() -> str:
    return os.environ.get("OTO_RUNNER_MODEL") or DEFAULT_MODEL


def base_url() -> str:
    return (os.environ.get(_ENV_BASE) or DEFAULT_BASE).rstrip("/")


def max_tokens() -> int:
    try:
        return int(os.environ.get("OTO_RUNNER_MAX_TOKENS", "") or DEFAULT_MAX_TOKENS)
    except ValueError:
        return DEFAULT_MAX_TOKENS


# ⚠️ Ce provider parle à un hôte CONFIGURABLE (Scaleway par défaut, mais aussi
# La Plateforme). Le dépôt de clé se lit donc de la base URL, jamais du nom du
# module ni de la variable d'environnement : `OTO_RUNNER_OPENAI_API_KEY` sert
# les deux, et croire qu'une clé « openai » appartient à Mistral demanderait à
# une org la clé d'un fournisseur chez qui elle ne tourne pas. Un hôte absent de
# cette table n'a PAS de dépôt : la plateforme paie, et ça se dit ainsi.
_DEPOTS_PAR_HOTE = {"api.mistral.ai": "mistral"}


def depot() -> str:
    from urllib.parse import urlparse
    return _DEPOTS_PAR_HOTE.get(urlparse(base_url()).netloc, "")


def resolve_key() -> str:
    key = os.environ.get(_ENV_KEY, "").strip()
    if not key:
        raise LlmUnavailable(
            f"{_ENV_KEY} absente de l'environnement du worker — pour Scaleway, "
            "une clé IAM du projet qui paie (SCW secret key)")
    return key


# ── La FORME du fil, confinée ici ────────────────────────────────────────────
def user_message(text: str) -> dict:
    return {"role": "user", "content": text}


def assistant_message(turn: Turn) -> dict:
    """Le message assistant COMPLET rendu par l'API, rejoué tel quel — les
    `tool_calls` doivent revenir intacts pour que les messages `role:tool`
    suivants se corrèlent par id."""
    return turn.raw_content if isinstance(turn.raw_content, dict) else {
        "role": "assistant", "content": turn.text}


def tool_messages(results: list[dict]) -> list[dict]:
    """UN message `role:tool` PAR résultat, corrélé par `tool_call_id`.
    `results` = [{id, text, is_error}] — l'erreur est un contenu comme un autre,
    le modèle la lit pour se corriger."""
    return [{"role": "tool", "tool_call_id": r["id"], "content": r["text"]}
            for r in results]


def format_tools(schemas: list[dict]) -> list[dict]:
    """Schémas neutres → format OpenAI (`type:function`, `parameters`)."""
    return [{"type": "function",
             "function": {"name": s["name"],
                          "description": s.get("description", ""),
                          "parameters": s.get("input_schema")
                          or {"type": "object", "properties": {}}}}
            for s in schemas]


def complete(*, system: str, messages: list, tools: list[dict],
             api_key: Optional[str] = None) -> Turn:
    """UN tour de modèle — synchrone, le worker a le droit d'attendre.

    Le `system` passe en premier message (la convention OpenAI) ; `messages` est
    le fil au format OpenAI (les `provider_raw` rejoués). Toute erreur HTTP
    remonte à la boucle avec le DIRE du serveur, tronqué — jamais avalée."""
    corps = {
        "model": model(),
        "max_tokens": max_tokens(),
        "messages": [{"role": "system", "content": system}, *messages],
        # ⚠️ SANS cette cle, le fournisseur ne met rien en cache — mesure du
        # 01/09 : deux appels identiques, zero jeton mis en cache ; avec elle,
        # 96 % des le second appel. Elle est STABLE par procedure : ce qui vaut
        # d'etre garde est le prefixe partage — consigne et outils — pas le fil
        # d'une fiche. Un identifiant par fiche ne partagerait rien.
        "prompt_cache_key": os.environ.get("OTO_RUNNER_CACHE_KEY")
        or "oto-runner-procedure",
    }
    if tools:
        corps["tools"] = tools
    r = _post_borne(base_url() + "/chat/completions", corps,
                    {"Authorization": f"Bearer {api_key or resolve_key()}"})
    if r.status_code >= 400:
        try:
            detail = r.json().get("message") or r.json().get("error") or r.text
        except Exception:  # noqa: BLE001
            detail = r.text
        raise RuntimeError(f"chat/completions → {r.status_code} : {str(detail)[:300]}")
    d = r.json()

    choix = (d.get("choices") or [{}])[0]
    msg = choix.get("message") or {}
    fin = choix.get("finish_reason") or "stop"
    u = d.get("usage") or {}
    # ⚠️ `prompt_tokens` COMPTE les jetons servis par le cache. Les porter tels
    # quels ferait payer au plein tarif, dans nos releves, ce qui est facture
    # 10 %. On separe donc, et `input_tokens` ne garde que ce qui est neuf.
    _caches = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    usage = {"input_tokens": max(0, int(u.get("prompt_tokens") or 0) - _caches),
             "output_tokens": int(u.get("completion_tokens") or 0),
             "cache_read_input_tokens": _caches}

    # Ce que le fournisseur DIT avoir servi, à défaut ce qu'on a demandé.
    servi = d.get("model") or model()

    if fin == "content_filter":
        return Turn(text="", tool_calls=(), stop_reason="refusal",
                    raw_content=msg, usage=usage, model=servi)

    calls = []
    for tc in (msg.get("tool_calls") or []):
        f = tc.get("function") or {}
        try:
            args = json.loads(f.get("arguments") or "{}")
        except Exception:  # noqa: BLE001 — des arguments malformés sont un appel
            # invalide, pas un crash : le modèle recevra l'erreur de l'outil
            args = {}
        calls.append(ToolCall(id=tc.get("id") or "", name=f.get("name") or "",
                              arguments=args if isinstance(args, dict) else {}))
    contenu = msg.get("content") or ""
    if isinstance(contenu, list):
        # Mistral rend parfois le contenu en LISTE de blocs typés au lieu
        # d'une chaîne (vécu, job 52 — AttributeError au .strip()).
        contenu = "\n".join(b.get("text", "") for b in contenu
                            if isinstance(b, dict) and b.get("type") == "text")
    return Turn(text=contenu.strip(),
                tool_calls=tuple(calls),
                stop_reason="end_turn" if fin in ("stop", "tool_calls") else fin,
                raw_content=msg, usage=usage, model=servi)
