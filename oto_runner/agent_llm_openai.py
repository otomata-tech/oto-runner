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
    usage = {"input_tokens": int(u.get("prompt_tokens") or 0),
             "output_tokens": int(u.get("completion_tokens") or 0)}

    if fin == "content_filter":
        return Turn(text="", tool_calls=(), stop_reason="refusal",
                    raw_content=msg, usage=usage)

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
    return Turn(text=(msg.get("content") or "").strip(),
                tool_calls=tuple(calls),
                stop_reason="end_turn" if fin in ("stop", "tool_calls") else fin,
                raw_content=msg, usage=usage)
