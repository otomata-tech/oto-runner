"""La boucle du worker — les invariants transplantés du prototype, prouvés ici.

Fakes purs (pas de réseau, pas de SDK) : un faux provider à tours scriptés et un
faux transport MCP. Ce qui se teste est la BOUCLE — allowlist fail-closed,
troncature marquée, plafond de tours, persistance du fil dans l'ordre.
"""
from __future__ import annotations

import pytest

from oto_runner import agent_llm, agent_runtime
from oto_runner.agent_runtime import AgentSpec


class FauxTransport:
    """Un transport MCP qui note tout et rend ce qu'on lui a scripté."""
    def __init__(self, reponses=None):
        self.reponses = dict(reponses or {})
        self.appels = []

    def schemas(self, names):
        return [{"name": n, "description": "", "input_schema": {"type": "object"}}
                for n in sorted(names)]

    def call(self, name, arguments):
        self.appels.append((name, arguments))
        return self.reponses.get(name, ("ok", False))


def _tours(monkeypatch, tours):
    """Scripte la suite de tours du modèle."""
    file = list(tours)

    def _complete(**kwargs):
        return file.pop(0)
    monkeypatch.setattr(agent_llm, "complete", _complete)


def _turn(text="", calls=(), stop="end_turn"):
    return agent_llm.Turn(
        text=text,
        tool_calls=tuple(agent_llm.ToolCall(id=f"t{i}", name=n, arguments=a)
                         for i, (n, a) in enumerate(calls)),
        stop_reason=stop,
        raw_content=[{"type": "text", "text": text}] if text else [])


SPEC = AgentSpec(system="s", tools=frozenset({"data_rows", "oto_procedure"}),
                 max_steps=3)


def test_un_outil_hors_allowlist_nest_jamais_transporte(monkeypatch):
    """Fail-closed : le refus revient au modèle en tour d'erreur — et le transport
    n'est même pas sollicité (un nom hors liste ne se cherche pas)."""
    t = FauxTransport()
    _tours(monkeypatch, [
        _turn(calls=[("email_send", {"to": "x"})]),
        _turn(text="compris, j'arrête"),
    ])
    res = agent_runtime.run(SPEC, t, prompt="go")
    assert t.appels == [], "l'outil interdit ne doit jamais atteindre le transport"
    assert res.steps[0].ok is False and "indisponible" in (res.steps[0].error or "")
    assert res.stopped == "end_turn"


def test_une_sortie_geante_est_tronquee_avec_la_marque(monkeypatch):
    """Le modèle doit SAVOIR qu'il manque quelque chose — la marque est le contrat."""
    t = FauxTransport({"data_rows": ("x" * 50_000, False)})
    _tours(monkeypatch, [
        _turn(calls=[("data_rows", {})]),
        _turn(text="fini"),
    ])
    res = agent_runtime.run(SPEC, t, prompt="go")
    tour_outils = res.messages[-2]          # le message user des tool_results
    contenu = tour_outils["content"][0]["content"]
    assert len(contenu) < 50_000 and "tronquée" in contenu
    assert res.stopped == "end_turn"


def test_le_plafond_de_tours_arrete_proprement(monkeypatch):
    """Un modèle qui boucle s'arrête à max_steps — avec le dernier texte comme repli."""
    t = FauxTransport()
    _tours(monkeypatch, [
        _turn(text="je continue", calls=[("data_rows", {})]) for _ in range(10)
    ])
    res = agent_runtime.run(SPEC, t, prompt="go")
    assert res.stopped == "max_steps"
    assert res.reply == "je continue", "le texte intermédiaire est le repli"
    assert len(t.appels) <= SPEC.max_steps + 1


def test_un_refus_est_terminal_jamais_un_crash(monkeypatch):
    t = FauxTransport()
    _tours(monkeypatch, [_turn(stop="refusal")])
    res = agent_runtime.run(SPEC, t, prompt="go")
    assert res.stopped == "refusal" and res.reply == ""
    assert t.appels == []


def test_le_fil_est_appose_dans_lordre_et_en_double_etage(monkeypatch):
    """on_turn reçoit (rôle, projection neutre, brut provider) pour CHAQUE tour —
    c'est le contrat du fil R1 : l'UI lit le neutre, la reprise rejoue le brut."""
    t = FauxTransport({"data_rows": ('{"rows": []}', False)})
    _tours(monkeypatch, [
        _turn(text="je regarde", calls=[("data_rows", {"limit": 1})]),
        _turn(text="terminé"),
    ])
    fil = []
    agent_runtime.run(SPEC, t, prompt="vas-y",
                      on_turn=lambda role, neutre, brut: fil.append((role, neutre, brut)))
    roles = [f[0] for f in fil]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert fil[1][1]["tool_calls"] == [{"name": "data_rows"}]
    assert fil[2][2]["role"] == "user" and fil[2][2]["content"][0]["type"] == "tool_result"
    assert fil[3][1]["text"] == "terminé"


def test_une_erreur_de_transport_est_un_resultat_pas_une_panne(monkeypatch):
    class Casse(FauxTransport):
        def call(self, name, arguments):
            raise RuntimeError("connexion perdue")
    t = Casse()
    _tours(monkeypatch, [
        _turn(calls=[("data_rows", {})]),
        _turn(text="je conclus sans"),
    ])
    res = agent_runtime.run(SPEC, t, prompt="go")
    assert res.stopped == "end_turn"
    assert res.steps[0].ok is False and "connexion perdue" in res.steps[0].error


def test_la_continuation_repart_de_lhistorique_sans_nouveau_prompt(monkeypatch):
    """Reprise après une mort en plein tour : history seul, prompt=None — le fil
    rejoué verbatim, aucun tour user parasite ajouté."""
    t = FauxTransport()
    _tours(monkeypatch, [_turn(text="je reprends et je conclus")])
    historique = [{"role": "user", "content": "démarre"},
                  {"role": "assistant", "content": [{"type": "text", "text": "début"}]}]
    res = agent_runtime.run(SPEC, t, prompt=None, history=historique)
    assert res.reply == "je reprends et je conclus"
    assert res.messages[0] == historique[0], "l'historique est rejoué tel quel"
    roles = [m["role"] for m in res.messages]
    assert roles.count("user") == 1, "aucun tour user n'est inventé à la reprise"
