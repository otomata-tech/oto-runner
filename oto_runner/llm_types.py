"""Les types partagés du seam provider — et rien d'autre.

`Turn.raw_content` est OPAQUE pour la boucle : sa forme appartient au provider
(liste de blocs chez Anthropic, message assistant complet chez OpenAI-compat) et
il se rejoue tel quel — c'est aussi ce que le fil du backend stocke en
`provider_raw`. Conséquence assumée en V1 : **un run se continue sur le provider
qui l'a commencé** (les formes ne se traduisent pas entre elles).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolCall:
    """Un appel d'outil demandé par le modèle."""
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class Turn:
    """Un tour de modèle. `raw_content` = la matière brute à réémettre telle quelle."""
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: str = "end_turn"       # end_turn | refusal | …
    raw_content: object = None
    usage: dict = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LlmUnavailable(RuntimeError):
    """Le substrat LLM n'est pas configuré (lib absente / clé absente)."""
