"""L'adaptateur OpenAI-compatible — le parsing et les formes, sans réseau.

Ce qui se casse en silence si on ne le fige pas : les `arguments` qui arrivent
en CHAÎNE JSON (pas en dict comme chez Anthropic), le mapping d'usage
prompt/completion → input/output, le `content_filter` traduit en refus terminal,
et les deux formes de fil (message assistant complet rejoué tel quel, un
message `role:tool` par résultat).
"""
from __future__ import annotations

import json

import pytest

from oto_runner import agent_llm_openai as P
from oto_runner.llm_types import Turn


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _reponse(message, finish="stop", usage=None):
    return {"choices": [{"message": message, "finish_reason": finish}],
            "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20}}


def test_les_arguments_arrivent_en_chaine_json_et_sortent_en_dict(monkeypatch):
    msg = {"role": "assistant", "content": None,
           "tool_calls": [{"id": "c1", "type": "function",
                           "function": {"name": "data_claim_next",
                                        "arguments": '{"namespace": "vivier", "worker": "w1"}'}}]}
    monkeypatch.setattr(P.requests, "post",
                        lambda *a, **k: _Resp(_reponse(msg, finish="tool_calls")))
    turn = P.complete(system="s", messages=[], tools=[], api_key="k")
    assert turn.wants_tools
    c = turn.tool_calls[0]
    assert c.name == "data_claim_next" and c.arguments == {"namespace": "vivier",
                                                           "worker": "w1"}
    assert turn.stop_reason == "end_turn"
    assert turn.usage == {"input_tokens": 100, "output_tokens": 20}


def test_des_arguments_malformes_font_un_appel_vide_pas_un_crash(monkeypatch):
    msg = {"role": "assistant", "content": None,
           "tool_calls": [{"id": "c1", "function": {"name": "data_rows",
                                                    "arguments": "{pas du json"}}]}
    monkeypatch.setattr(P.requests, "post",
                        lambda *a, **k: _Resp(_reponse(msg, finish="tool_calls")))
    turn = P.complete(system="s", messages=[], tools=[], api_key="k")
    assert turn.tool_calls[0].arguments == {}, \
        "l'outil recevra {} et rendra son erreur — le modèle se corrigera"


def test_content_filter_est_un_refus_terminal(monkeypatch):
    monkeypatch.setattr(P.requests, "post", lambda *a, **k: _Resp(
        _reponse({"role": "assistant", "content": ""}, finish="content_filter")))
    turn = P.complete(system="s", messages=[], tools=[], api_key="k")
    assert turn.stop_reason == "refusal" and not turn.wants_tools


def test_une_erreur_http_remonte_avec_le_dire_du_serveur(monkeypatch):
    monkeypatch.setattr(P.requests, "post", lambda *a, **k: _Resp(
        {"message": "invalid model"}, status=400))
    with pytest.raises(RuntimeError) as e:
        P.complete(system="s", messages=[], tools=[], api_key="k")
    assert "invalid model" in str(e.value) and "400" in str(e.value)


def test_le_system_passe_en_premier_message(monkeypatch):
    vu = {}

    def _post(url, json=None, timeout=None, headers=None):
        vu.update(corps=json)
        return _Resp(_reponse({"role": "assistant", "content": "ok"}))
    monkeypatch.setattr(P.requests, "post", _post)
    P.complete(system="LE CADRE", messages=[{"role": "user", "content": "hi"}],
               tools=[], api_key="k")
    assert vu["corps"]["messages"][0] == {"role": "system", "content": "LE CADRE"}
    assert vu["corps"]["messages"][1]["role"] == "user"


def test_les_deux_formes_de_fil():
    msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]}
    turn = Turn(text="", raw_content=msg)
    assert P.assistant_message(turn) is msg, \
        "le message assistant se rejoue COMPLET (tool_calls intacts, corrélés par id)"
    outs = P.tool_messages([{"id": "c1", "text": "res1", "is_error": False},
                            {"id": "c2", "text": "res2", "is_error": True}])
    assert [o["role"] for o in outs] == ["tool", "tool"], "un message PAR résultat"
    assert outs[0]["tool_call_id"] == "c1" and outs[1]["content"] == "res2"


def test_format_tools_enveloppe_en_function():
    out = P.format_tools([{"name": "data_rows", "description": "lit",
                           "input_schema": {"type": "object"}}])
    assert out == [{"type": "function",
                    "function": {"name": "data_rows", "description": "lit",
                                 "parameters": {"type": "object"}}}]


def test_le_plafond_wall_clock_coupe_un_serveur_qui_goutte(monkeypatch):
    """Le read timeout d'urllib3 se réarme à chaque octet : un serveur qui
    goutte tient la connexion indéfiniment (vécu : 35 min, pile dans ssl.read).
    La deadline SIGALRM coupe pour de vrai et lève une erreur propre."""
    import time as _t

    from oto_runner.llm_types import LlmUnavailable

    monkeypatch.setattr(P, "_WALL_TIMEOUT_S", 1)
    monkeypatch.setattr(P.requests, "post", lambda *a, **k: _t.sleep(5))
    with pytest.raises(LlmUnavailable) as e:
        P.complete(system="s", messages=[], tools=[], api_key="k")
    assert "wall-clock" in str(e.value)
