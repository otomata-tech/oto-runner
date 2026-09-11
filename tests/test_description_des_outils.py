"""La description d'un outil servie au modèle : bornée par un réglage, et toute coupe DITE.

⚠️ Ce fichier existe parce que le runner coupait EN SILENCE chaque description à 1 024
caractères, une borne reprise du prototype et jamais mesurée. Pour `data_write` (7 865
caractères), l'agent recevait l'exemple `"comment": "@keep"` et jamais la règle « seul dans
la couche », ni `@empty`, ni l'écriture par `id` : 14 cases « @keep — … » sur le lot 01 du
vivier, le 11/09/2026. Mesuré le même jour : l'API Mistral accepte au moins 65 536 caractères.
"""
from __future__ import annotations

import json

import pytest
import requests
from requests.utils import get_encoding_from_headers

import oto_runner.mcp as M
from oto_runner import agent_runtime
from oto_runner.agent_runtime import AgentSpec
from oto_runner.llm_types import Turn

_INIT = b'data: {"jsonrpc":"2.0","id":1,"result":{}}'


def _reponse(corps: bytes, entetes=None):
    r = requests.Response()
    r.status_code = 200
    r._content = corps
    r.headers["Content-Type"] = "text/event-stream"
    for k, v in (entetes or {}).items():
        r.headers[k] = v
    r.encoding = get_encoding_from_headers(r.headers)
    return r


def _servies(monkeypatch, descriptions, noms):
    """Une session dont le `tools/list` rend ces descriptions ; ce que `schemas` en sert."""
    outils = [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": {}}}
              for n, d in descriptions.items()]
    liste = ("data: " + json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": outils}})).encode()

    def _post(url, **kw):
        if (kw.get("json") or {}).get("method") in ("initialize", "notifications/initialized"):
            return _reponse(_INIT, {"mcp-session-id": "s1"})
        return _reponse(liste)

    monkeypatch.setattr(M, "post_with_deadline", _post)
    s = M.McpSession(url="http://x", token="t")
    return {x["name"]: x["description"] for x in s.schemas(frozenset(noms))}, s


def test_data_write_arrive_entier_par_defaut(monkeypatch):
    monkeypatch.delenv("OTO_RUNNER_TOOL_DESC_MAX", raising=False)
    servies, s = _servies(monkeypatch, {"data_write": "x" * 7865}, {"data_write"})
    assert len(servies["data_write"]) == 7865
    assert s.descriptions_servies == [{"outil": "data_write", "longueur": 7865, "servie": 7865}]


def test_une_description_trop_longue_est_coupee_et_relevee(monkeypatch):
    monkeypatch.delenv("OTO_RUNNER_TOOL_DESC_MAX", raising=False)
    servies, s = _servies(monkeypatch, {"oto_procedure": "y" * 20000}, {"oto_procedure"})
    assert len(servies["oto_procedure"]) == M.DEFAULT_DESC_MAX
    assert s.descriptions_servies == [
        {"outil": "oto_procedure", "longueur": 20000, "servie": M.DEFAULT_DESC_MAX}]


def test_la_borne_se_regle(monkeypatch):
    monkeypatch.setenv("OTO_RUNNER_TOOL_DESC_MAX", "1024")
    servies, _ = _servies(monkeypatch, {"data_write": "x" * 7865}, {"data_write"})
    assert len(servies["data_write"]) == 1024


@pytest.mark.parametrize("brut", ["abc", "0", "-5"])
def test_une_borne_invalide_leve(monkeypatch, brut):
    monkeypatch.setenv("OTO_RUNNER_TOOL_DESC_MAX", brut)
    with pytest.raises(ValueError):
        _servies(monkeypatch, {"data_write": "x"}, {"data_write"})


def test_seuls_les_outils_permis_sont_releves(monkeypatch):
    monkeypatch.delenv("OTO_RUNNER_TOOL_DESC_MAX", raising=False)
    _, s = _servies(monkeypatch, {"data_write": "x" * 10, "fr_search": "z" * 10}, {"data_write"})
    assert [d["outil"] for d in s.descriptions_servies] == ["data_write"]


class _Transport:
    descriptions_servies = [{"outil": "data_rows", "longueur": 100, "servie": 100},
                            {"outil": "oto_procedure", "longueur": 20000, "servie": 8192}]

    def schemas(self, names):
        return [{"name": n, "description": "", "input_schema": {"type": "object"}}
                for n in sorted(names)]

    def call(self, name, arguments):
        return ("ok", False)


class _Provider:
    def complete(self, **kwargs):
        return Turn(text="fini", tool_calls=(), stop_reason="end_turn",
                    raw_content=[{"type": "text", "text": "fini"}])

    def user_message(self, text):
        return {"role": "user", "content": text}

    def assistant_message(self, turn):
        return {"role": "assistant", "content": turn.raw_content}

    def tool_messages(self, results):
        return []

    def format_tools(self, schemas):
        return list(schemas)


def test_le_journal_du_travail_dit_la_coupe():
    evenements = []
    agent_runtime.run(AgentSpec(system="s", tools=frozenset({"data_rows", "oto_procedure"})),
                      _Transport(), _Provider(), prompt="go",
                      on_event=lambda ev, champs: evenements.append((ev, champs)))
    d = [c for ev, c in evenements if ev == "descriptions_outils"]
    assert d and d[0]["coupees"] == ["oto_procedure"] and len(d[0]["outils"]) == 2
