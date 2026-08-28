"""La boucle d'agent du worker — transplantée du prototype, allégée par le design.

Le prototype (`server-agent`, oto-backend 25/07) tournait DANS le process du
backend : il devait ré-appliquer lui-même la rédaction de champs et surveiller
ses chemins de credential, parce que `Tool.run` court-circuitait le middleware.
**Ici le worker est un client MCP pur (ADR 0064-D1), et tout ça disparaît** :
chaque appel d'outil traverse la face MCP du backend, qui applique credential,
RBAC, activation, rédaction et journal — comme pour n'importe quel client. La
boucle n'a plus que trois responsabilités : le tour de modèle, l'allowlist,
les bornes.

Ce qui est conservé du prototype, à l'identique :
- l'allowlist FAIL-CLOSED (un outil hors liste revient au modèle en tour
  d'erreur — le modèle se corrige — jamais en exception qui tuerait le job) ;
- la troncature MARQUÉE d'une sortie d'outil (le modèle doit SAVOIR qu'il
  manque quelque chose, sinon il conclut sur un extrait en croyant tout voir) ;
- les résultats d'un tour rendus GROUPÉS au modèle — leur forme dans le fil
  appartient au provider (un message user chez Anthropic, N messages role:tool
  chez OpenAI) ;
- le texte intermédiaire gardé comme repli si le budget de tours s'épuise.

Divergence ASSUMÉE : le plafond de tours par défaut passe de 6 à 24. Le 6 du
prototype bornait un chat public à petites questions ; un run de procédure est
un travail (la veille LinkedIn réelle = 15 tours). Le plafond effectif se règle
par job, borné dur à 64.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from .llm_types import ToolCall, Turn  # noqa: F401 — le contrat du provider

MAX_TOOL_OUTPUT_CHARS = 12_000
# Signatures d'erreurs TRANSITOIRES d'outil : rejouées UNE fois, silencieusement
# (le modèle ne voit que la seconde réponse). La politique de reprise est de la
# MÉCANIQUE, pas de la consigne : en prose elle coûte des caractères (payés en
# écritures perdues, mesuré) et invite l'agent à contourner l'outil capricieux
# plutôt qu'à le retenter. Une erreur MÉTIER (not_found, 400) n'est jamais
# rejouée — elle est une réponse.
_TRANSIENT_RE = None  # compilé au premier usage (module importable sans re)
DEFAULT_MAX_STEPS = 24
HARD_MAX_STEPS = 64
MAX_HISTORY_MESSAGES = 60   # tours provider transportés au modèle (le fil complet
                            # reste au backend — ici on borne le COÛT d'un tour)
# Les postes d'usage cumulés sur un run. Les deux derniers ne sont pas du
# décor : `input_tokens` ne compte QUE le reste non caché, donc sans eux le
# volume d'entrée réel d'un run caché est illisible.
USAGE_KEYS = ("input_tokens", "output_tokens",
              "cache_creation_input_tokens", "cache_read_input_tokens")


class ToolTransport(Protocol):
    """Ce que la boucle attend d'un transport d'outils : la face MCP du backend.

    `schemas(names)` → [{name, description, input_schema}] ; `call(name, args)` →
    (texte, is_error). Les gates et la rédaction sont CÔTÉ SERVEUR — le transport
    ne filtre rien, il transporte."""
    def schemas(self, names: frozenset) -> list[dict]: ...
    def call(self, name: str, arguments: dict) -> tuple[str, bool]: ...


@dataclass(frozen=True)
class AgentSpec:
    """Ce que l'agent est autorisé à être, pour UN run."""
    system: str
    tools: frozenset
    max_steps: int = DEFAULT_MAX_STEPS
    label: str = "run"


@dataclass
class AgentStep:
    tool: str
    ok: bool
    duration_ms: int
    error: Optional[str] = None
    # Un appel qui ABOUTIT sans rien rendre — une réservation de ligne qui ne
    # rend AUCUNE ligne, par exemple. `ok` ne le distingue pas d'un appel
    # fécond : la boucle ne connaît pas la sémantique des outils, elle pose ici
    # le verdict que l'appelant lui rend (`a_vide`).
    vide: bool = False
    # L'échec vient du TRANSPORT (session MCP perdue, réseau, protocole), pas de
    # l'outil : l'appel n'a jamais été exécuté. Un tel échec ne se lit pas comme
    # une réponse métier — un job qui en porte n'a pas « fait le travail ».
    transport_ko: bool = False

    def as_dict(self) -> dict:
        out = {"tool": self.tool, "ok": self.ok, "duration_ms": self.duration_ms}
        if self.error:
            out["error"] = self.error
        if self.vide:
            out["vide"] = True
        if self.transport_ko:
            out["transport_ko"] = True
        return out


@dataclass
class AgentResult:
    reply: str
    steps: list = field(default_factory=list)
    stopped: str = "end_turn"           # end_turn | max_steps | refusal | no_reply
    usage: dict = field(default_factory=dict)
    messages: list = field(default_factory=list)
    raw_outputs: Optional[list] = None   # les entrées BRUTES du fournisseur,
    # quand il en rend (chemin Conversations) — de quoi diagnostiquer un run
    # dont le fil ne garde qu'une synthèse. Les autres providers laissent None.
    model: Optional[str] = None          # la version CONCRÈTE qui a tourné,
    # quand le provider sait la résoudre (chemin Conversations) : un alias
    # flottant ne se date pas après coup. Les autres providers laissent None.


# `on_turn(role, content_neutre, provider_raw)` : le point d'ancrage du FIL (R1).
# La boucle appose chaque tour au fil du backend PENDANT le run — c'est ce qui rend
# le worker jetable entre deux tours (un kill se répare par re-claim + rechargement).
# None = pas de persistance (tests, dry-run).
OnTurn = Callable[[str, dict, dict], None]


def _cap(text: str) -> str:
    if len(text) <= MAX_TOOL_OUTPUT_CHARS:
        return text
    return (text[:MAX_TOOL_OUTPUT_CHARS]
            + f"\n…[sortie tronquée à {MAX_TOOL_OUTPUT_CHARS} caractères — "
              "affine la requête (filtre, limite) pour en voir moins à la fois]")


def _trim(messages: list) -> list:
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return list(messages)
    return list(messages[-MAX_HISTORY_MESSAGES:])


def execute_tool(spec: AgentSpec, transport: ToolTransport,
                 call: ToolCall) -> tuple[str, bool, bool]:
    """UN appel d'outil → (texte, is_error, panne de transport). Ne lève jamais :
    une erreur d'outil est un résultat que le modèle lit pour se corriger.
    Fail-closed sur l'allowlist AVANT tout transport.

    Le troisième terme sépare l'erreur MÉTIER (une réponse : not_found, 400) de
    la PANNE DE TRANSPORT — le transport a levé, l'appel n'a pas eu lieu. Le
    modèle lit les deux de la même façon (il n'y a rien d'autre à lui dire),
    mais le worker, lui, ne doit pas conclure « done » sur un travail que le
    transport a empêché."""
    if call.name not in spec.tools:
        return (f"Outil `{call.name}` indisponible pour ce run. "
                f"Outils autorisés : {', '.join(sorted(spec.tools)) or '(aucun)'}.",
                True, False)
    try:
        text, is_error = transport.call(call.name, call.arguments or {})
        if is_error and _est_transitoire(text):
            time.sleep(2)
            text, is_error = transport.call(call.name, call.arguments or {})
    except Exception as e:  # noqa: BLE001 — l'erreur de la cible EST un résultat
        return (f"Erreur de l'outil `{call.name}` : {e}", True, True)
    return (_cap(text), is_error, False)


def _est_transitoire(texte: str) -> bool:
    return bool(re.search(
        r"timeout|timed?\s*out|d\u00e9lai|429|too many requests|"
        r"50[234]|bad gateway|unavailable|connection|connexion",
        (texte or "")[:400], re.IGNORECASE))


def run(spec: AgentSpec, transport: ToolTransport, provider,
        prompt: Optional[str] = None,
        history: Optional[list] = None, on_turn: Optional[OnTurn] = None,
        api_key: Optional[str] = None,
        a_vide: Optional[Callable[[str, str], bool]] = None) -> AgentResult:
    """La boucle : tours de modèle et d'outils jusqu'à conclusion, plafond, ou refus.

    `history` = les `provider_raw` du fil, rejoués dans l'ordre (continuation d'un
    run) ; `prompt` = le nouveau tour user (None = reprendre sans rien ajouter,
    ex. après un kill en plein tour). `on_turn` appose chaque tour au fil backend.

    `a_vide(nom, sortie)` → « cet appel a abouti SANS RIEN RENDRE » : la boucle
    voit les sorties d'outils, mais leur SENS appartient au domaine (le worker).
    Elle lui pose la question et marque le pas ; absent, aucun pas n'est vide."""
    messages = _trim(history or [])
    if prompt is not None:
        um = provider.user_message(prompt)
        messages.append(um)
        if on_turn:
            on_turn("user", {"text": prompt}, um)

    schemas = provider.format_tools(transport.schemas(spec.tools))
    steps: list[AgentStep] = []
    usage = dict.fromkeys(USAGE_KEYS, 0)
    stopped = "end_turn"
    reply = ""
    plafond = max(1, min(spec.max_steps, HARD_MAX_STEPS))

    for _ in range(plafond + 1):
        turn = provider.complete(system=spec.system, messages=messages,
                                 tools=schemas, api_key=api_key)
        for k in USAGE_KEYS:
            usage[k] = usage.get(k, 0) + int(turn.usage.get(k) or 0)

        if turn.stop_reason == "refusal":
            stopped, reply = "refusal", ""
            break

        assistant_raw = provider.assistant_message(turn)
        messages.append(assistant_raw)
        if on_turn:
            on_turn("assistant",
                    {"text": turn.text,
                     "tool_calls": [{"name": c.name} for c in turn.tool_calls]},
                    assistant_raw)

        if not turn.wants_tools:
            reply, stopped = turn.text, "end_turn"
            break

        results = []
        neutre = []
        for call in turn.tool_calls:
            started = time.monotonic()
            text, is_error, transport_ko = execute_tool(spec, transport, call)
            ms = int((time.monotonic() - started) * 1000)
            steps.append(AgentStep(tool=call.name, ok=not is_error, duration_ms=ms,
                                   error=text[:200] if is_error else None,
                                   vide=bool(a_vide and not is_error
                                             and a_vide(call.name, text)),
                                   transport_ko=transport_ko))
            neutre.append({"name": call.name, "ok": not is_error, "duration_ms": ms})
            results.append({"id": call.id, "text": text, "is_error": is_error})
        # La FORME des résultats dans le fil appartient au provider (un message
        # user chez Anthropic, N messages role:tool chez OpenAI) — la boucle ne
        # la connaît pas, elle appose ce qu'on lui rend.
        for tool_raw in provider.tool_messages(results):
            messages.append(tool_raw)
            if on_turn:
                on_turn("tool", {"tool_calls": neutre}, tool_raw)
        if turn.text:
            reply = turn.text
    else:
        stopped = "max_steps"

    if not reply and stopped == "end_turn":
        stopped = "no_reply"
    return AgentResult(reply=reply, steps=steps, stopped=stopped, usage=usage,
                       messages=messages)


def serialize(payload) -> str:
    """Payload structuré → texte pour le fil (utilitaire des transports)."""
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(payload)
