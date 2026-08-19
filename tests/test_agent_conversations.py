"""Le chemin one-shot Conversations — les outils tournent chez Mistral.

Ce que le banc fige : le PARSE des outputs (le bilan de surveillance en dérive —
usage, pas, tool_counts), les rejeux transitoires explicites (le SDK ne rejoue
pas, et on ne l'utilise pas : son timeout est troué — basesdk.py:227, 20 h de
gel sur la boucle locale), la conformité `store=False`, et le chemin worker
complet (fil à deux tours, bilan déclaré). L'essai réel des 20 fiches valide
contre le service ce que le banc ne peut pas savoir.
"""
from __future__ import annotations

import pytest

import oto_runner.agent_conversations as C
from oto_runner import worker as W
from tests.test_worker_reprise import FauxBackend, _job


_REPONSE = {
    "outputs": [
        {"type": "tool.execution", "name": "data_claim_next"},
        {"type": "tool.execution", "name": "serper_search"},
        {"type": "tool.execution", "name": "serper_search"},
        {"type": "tool.execution", "name": "data_write"},
        {"type": "tool.execution", "name": "data_release"},
        {"type": "message.output", "content": [
            {"type": "text", "text": "Fiche enrichie, "},
            {"type": "text", "text": "ligne libérée."}]},
    ],
    "usage": {"prompt_tokens": 21000, "completion_tokens": 900, "total_tokens": 21900},
}


class _R:
    def __init__(self, status=200, corps=None, texte=""):
        self.status_code = status
        self._corps = corps
        self.text = texte

    def json(self):
        return self._corps


def _env(monkeypatch):
    monkeypatch.setenv("OTO_RUNNER_OPENAI_API_KEY", "k")
    monkeypatch.setenv("OTO_RUNNER_CONNECTOR_ID", "conn-1")


def test_le_bilan_de_surveillance_derive_des_outputs(monkeypatch):
    """La boucle tourne chez Mistral, mais le JOB garde son bilan : usage,
    pas, tool_counts — la page /automations reste vraie."""
    _env(monkeypatch)
    vu = {}

    def post(url, **kw):
        vu.update(url=url, corps=kw["json"])
        return _R(corps=_REPONSE)

    monkeypatch.setattr(C, "post_with_deadline", post)
    res = C.run_once(instructions="la procédure", inputs="vas-y",
                     tools=("data_claim_next", "serper_search"))
    assert res.reply == "Fiche enrichie, ligne libérée."
    assert res.usage == {"input_tokens": 21000, "output_tokens": 900}
    assert [s.tool for s in res.steps] == [
        "data_claim_next", "serper_search", "serper_search",
        "data_write", "data_release"]
    assert vu["url"].endswith("/v1/conversations")


def test_store_false_est_toujours_envoye(monkeypatch):
    """`store=False` est une contrainte de CONFORMITÉ du contrat client —
    rien ne reste chez Mistral. Pas un défaut implicite : ENVOYÉ, toujours."""
    _env(monkeypatch)
    vu = {}
    monkeypatch.setattr(C, "post_with_deadline",
                        lambda url, **kw: vu.update(kw["json"]) or _R(corps=_REPONSE))
    C.run_once(instructions="p", inputs="i", tools=())
    assert vu["store"] is False
    assert vu["tools"][0]["connector_id"] == "conn-1"


def test_un_transitoire_est_rejoue_puis_passe(monkeypatch):
    """Le SDK ne rejoue pas ; nous si — explicitement, sur les seuls
    transitoires HTTP, jamais sur une DeadlineExceeded (re-payer un run
    entier n'est pas une politique de rejeu)."""
    _env(monkeypatch)
    monkeypatch.setattr(C.time, "sleep", lambda s: None)
    essais = {"n": 0}

    def post(url, **kw):
        essais["n"] += 1
        return _R(status=502, texte="bad gateway") if essais["n"] < 3 \
            else _R(corps=_REPONSE)

    monkeypatch.setattr(C, "post_with_deadline", post)
    res = C.run_once(instructions="p", inputs="i", tools=())
    assert essais["n"] == 3 and res.reply


def test_un_400_echoue_net_sans_rejeu(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(C, "post_with_deadline",
                        lambda url, **kw: _R(status=400, texte="invalid connector"))
    with pytest.raises(RuntimeError, match="conversations → 400"):
        C.run_once(instructions="p", inputs="i", tools=())


def test_sans_connector_id_erreur_franche(monkeypatch):
    monkeypatch.setenv("OTO_RUNNER_OPENAI_API_KEY", "k")
    monkeypatch.delenv("OTO_RUNNER_CONNECTOR_ID", raising=False)
    with pytest.raises(C.LlmUnavailable, match="OTO_RUNNER_CONNECTOR_ID"):
        C.run_once(instructions="p", inputs="i", tools=())


def test_le_worker_one_shot_appose_ordre_et_synthese(monkeypatch):
    """Le chemin worker complet : fil à DEUX tours (l'ordre, la synthèse avec le
    relevé des exécutions), bilan déclaré (tool_counts au grain job) — la
    surveillance des vacances ne perd pas son poste."""
    _env(monkeypatch)

    class FauxConv:
        ONE_SHOT = True

        @staticmethod
        def run_once(*, instructions, inputs, tools):
            from oto_runner.agent_runtime import AgentResult, AgentStep
            assert "la procédure" in instructions
            return AgentResult(reply="fait", stopped="end_turn",
                               usage={"input_tokens": 20000, "output_tokens": 500},
                               steps=[AgentStep(tool="data_write", ok=True,
                                                duration_ms=0)])

    class McpSansBoucle:
        def __init__(self, **kw):
            self.run_id = None

        def outil(self, name, args=None):
            if name == "oto_procedure":
                return {"body_md": "la procédure"}
            if name == "run_start":
                return {"run_id": "r-C"}
            return {}

    monkeypatch.setattr(W, "McpSession", McpSansBoucle)
    b = FauxBackend()
    W._traiter(b, _job("start"), provider=FauxConv)
    roles = [a[1] for a in b.appels if a[0] == "append"]
    assert roles == ["user", "assistant"], "le fil garde l'ordre et la synthèse"
    result = next(a for a in b.appels if a[0] == "complete_result")[1]
    assert result["tool_counts"] == {"data_write": 1}
    assert result["usage_tokens"] == 20500


def test_une_base_openai_avec_v1_ne_double_pas_le_chemin(monkeypatch):
    """L'env de la box porte la base openai-compat AVEC /v1 : la réutiliser
    donnait /v1/v1/conversations → 404 « no Route matched » (vécu, job 274)."""
    _env(monkeypatch)
    monkeypatch.setenv("OTO_RUNNER_OPENAI_BASE", "https://api.mistral.ai/v1")
    vu = {}
    monkeypatch.setattr(C, "post_with_deadline",
                        lambda url, **kw: vu.update(url=url) or _R(corps=_REPONSE))
    C.run_once(instructions="p", inputs="i", tools=())
    assert vu["url"] == "https://api.mistral.ai/v1/conversations"


def test_les_noms_prefixes_par_le_connecteur_sont_normalises(monkeypatch):
    """Le connecteur Mistral préfixe les outils de son nom (oto-11aout_data_write) :
    sans normalisation, le bilan disait « zéro data_write » sur des fiches
    écrites (vécu). L'allowlist du job normalise — exacte, jamais une devinette."""
    _env(monkeypatch)
    rep = {"outputs": [
        {"type": "tool.execution", "name": "oto-11aout_data_claim_next"},
        {"type": "tool.execution", "name": "oto-11aout_data_write"},
        {"type": "message.output", "content": "ok"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    monkeypatch.setattr(C, "post_with_deadline", lambda url, **kw: _R(corps=rep))
    res = C.run_once(instructions="p", inputs="i",
                     tools=("data_claim_next", "data_write"))
    assert [s.tool for s in res.steps] == ["data_claim_next", "data_write"]
