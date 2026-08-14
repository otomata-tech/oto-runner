"""Client LLM « chat + tool calling » — le seam provider du worker.

Transplanté du prototype `server-agent` d'oto-backend (25/07), avec UNE
simplification structurelle : ici la clé vient de l'ENVIRONNEMENT, point. Le
worker est un client pur (ADR 0064-D1) — il ne connaît ni coffre, ni cascade,
ni org : l'org qui paie se choisit en amont, en posant la clé dans l'env de
l'unit du worker (V1 : un worker = une org). La cascade de credentials reste
côté backend, où elle a toujours vécu.

Le reste du contrat est inchangé, et c'est lui qui compte :
- `complete()` rend un `Turn` : texte visible + `tool_calls` demandés +
  `raw_content` — les blocs BRUTS de la réponse, à réinjecter INCHANGÉS dans le
  fil comme tour assistant (les blocs de pensée se rejouent verbatim, les
  reconstruire casse le tour suivant). C'est exactement ce que le fil du
  backend stocke en `provider_raw` (R1) : ce module et la table parlent la
  même langue.
- `stop_reason` se lit AVANT le contenu : un `refusal` revient en HTTP 200 avec
  un contenu vide — c'est un tour terminal, pas un crash.
- La profondeur se règle par `output_config.effort`, jamais par temperature.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from .llm_types import LlmUnavailable, ToolCall, Turn

# Sonnet par défaut — divergence ASSUMÉE avec le prototype (Opus) : un run hébergé
# tourne sans humain qui regarde le compteur, et la campagne réelle a montré qu'un
# modèle de milieu de gamme exécute correctement une procédure cadrée. Opus se
# choisit par flotte ou par env quand le jugement le justifie.
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_EFFORT = "medium"
DEFAULT_MAX_TOKENS = 8192

_ENV_KEY = "ANTHROPIC_API_KEY"


def model() -> str:
    return os.environ.get("OTO_RUNNER_MODEL") or DEFAULT_MODEL


def effort() -> str:
    return os.environ.get("OTO_RUNNER_EFFORT") or DEFAULT_EFFORT


def max_tokens() -> int:
    try:
        return int(os.environ.get("OTO_RUNNER_MAX_TOKENS", "") or DEFAULT_MAX_TOKENS)
    except ValueError:
        return DEFAULT_MAX_TOKENS


def resolve_key() -> str:
    """La clé, de l'env, ou une erreur ACTIONNABLE — jamais un repli muet."""
    key = os.environ.get(_ENV_KEY, "").strip()
    if not key:
        raise LlmUnavailable(
            f"{_ENV_KEY} absente de l'environnement du worker — c'est elle qui "
            "décide quelle org paie ses tours (V1 : un worker = une org)")
    return key


def _sdk():
    try:
        import anthropic  # noqa: PLC0415 — import guardé : sans la lib, pas de worker
        return anthropic
    except Exception:  # noqa: BLE001
        return None


def _block_type(block: Any) -> str:
    if isinstance(block, dict):
        return str(block.get("type") or "")
    return getattr(block, "type", "") or ""


def _block_to_dict(block: Any) -> dict:
    """Bloc de réponse → dict JSON-sérialisable, réinjectable TEL QUEL (et stockable
    tel quel en `provider_raw` du fil). On ne reconstruit jamais un bloc de pensée :
    on le transporte."""
    if isinstance(block, dict):
        return {k: v for k, v in block.items() if v is not None}
    dump = getattr(block, "model_dump", None)
    if dump is not None:
        try:
            return {k: v for k, v in dump(mode="json").items() if v is not None}
        except Exception:  # noqa: BLE001
            pass
    return {"type": _block_type(block) or "text",
            "text": str(getattr(block, "text", "") or "")}


# ── La FORME du fil, confinée ici (la boucle ne connaît que ces 4 hooks) ─────
def user_message(text: str) -> dict:
    return {"role": "user", "content": text}


def assistant_message(turn: Turn) -> dict:
    """Le tour assistant à réinjecter — blocs BRUTS, jamais reconstruits."""
    return {"role": "assistant", "content": turn.raw_content}


def tool_messages(results: list[dict]) -> list[dict]:
    """Les résultats d'outils d'UN tour → messages de fil. Anthropic : TOUS dans
    UN message user (les scinder apprend au modèle à cesser de paralléliser).
    `results` = [{id, text, is_error}]."""
    return [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": r["id"],
         "content": r["text"], "is_error": r["is_error"]} for r in results]}]


def format_tools(schemas: list[dict]) -> list[dict]:
    """Schémas NEUTRES ({name, description, input_schema}) → format Anthropic
    (identique, c'est lui qui a fixé la forme neutre)."""
    return list(schemas)


def complete(*, system: str, messages: list, tools: list[dict],
             api_key: Optional[str] = None) -> Turn:
    """UN tour de modèle — synchrone : le worker est un process dédié, pas un
    serveur mono-loop, il a le droit d'attendre.

    `messages` = le fil au format provider (les `provider_raw` du fil backend,
    rejoués dans l'ordre) ; `tools` = `[{name, description, input_schema}]`.
    Toute erreur réseau/quota remonte à la boucle, qui décide (backoff du job)."""
    anthropic = _sdk()
    if anthropic is None:
        raise LlmUnavailable("le paquet `anthropic` n'est pas installé")
    client = anthropic.Anthropic(api_key=api_key or resolve_key())
    kwargs: dict = {
        "model": model(),
        "max_tokens": max_tokens(),
        "system": system,
        "messages": messages,
        "output_config": {"effort": effort()},
    }
    if tools:
        kwargs["tools"] = tools
    resp = client.messages.create(**kwargs)

    stop = getattr(resp, "stop_reason", "") or "end_turn"
    usage = {}
    try:
        usage = {"input_tokens": resp.usage.input_tokens,
                 "output_tokens": resp.usage.output_tokens}
    except Exception:  # noqa: BLE001 — la télémétrie n'est jamais bloquante
        pass

    raw = [_block_to_dict(b) for b in getattr(resp, "content", []) or []]
    if stop == "refusal":
        return Turn(text="", tool_calls=(), stop_reason="refusal",
                    raw_content=raw, usage=usage)

    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in getattr(resp, "content", []) or []:
        kind = _block_type(block)
        if kind == "text":
            texts.append(getattr(block, "text", "") or "")
        elif kind == "tool_use":
            raw_args = getattr(block, "input", None)
            calls.append(ToolCall(
                id=getattr(block, "id", "") or "",
                name=getattr(block, "name", "") or "",
                arguments=raw_args if isinstance(raw_args, dict) else {}))
    return Turn(text="\n".join(t for t in texts if t.strip()).strip(),
                tool_calls=tuple(calls), stop_reason=stop,
                raw_content=raw, usage=usage)
