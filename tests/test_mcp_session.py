"""Les jetons de contexte se posent SÉLECTIVEMENT, d'après le schéma du tool.

Ils sont advertisés par tool (ADR 0038 côté serveur) et la face MCP valide
contre le schéma déclaré : poser `_project` sur un tool qui ne le déclare pas
fait refuser l'appel ENTIER. Vécu au premier vol de flotte — `oto_procedure`
ne déclare pas `_project`, 4 jobs en échec avant une seule ligne traitée.
"""
from __future__ import annotations

from oto_runner.mcp import McpSession


def _session(monkeypatch, tools):
    """Une session sans réseau : tools/list scripté, appels capturés."""
    vu = {}

    def _post(self, corps, avec_entetes=False):
        methode = corps.get("method")
        if methode == "initialize":
            return ({"mcp-session-id": "s1"}, {}) if avec_entetes else {}
        if methode == "tools/list":
            return {"result": {"tools": [
                {"name": n, "inputSchema": {"type": "object",
                                            "properties": {k: {} for k in props}}}
                for n, props in tools.items()]}}
        if methode == "tools/call":
            vu["appel"] = corps["params"]
            return {"result": {"content": [{"type": "text", "text": "{}"}]}}
        return {}

    monkeypatch.setattr(McpSession, "_post", _post)
    s = McpSession(url="http://x", token="t", project=248, run_id="r-1")
    return s, vu


def test_un_tool_qui_declare_les_jetons_les_recoit(monkeypatch):
    s, vu = _session(monkeypatch, {"data_claim_next":
                                   ["namespace", "worker", "_project", "_run_id"]})
    s.call("data_claim_next", {"namespace": "ns", "worker": "w"})
    assert vu["appel"]["arguments"]["_project"] == 248
    assert vu["appel"]["arguments"]["_run_id"] == "r-1"


def test_un_tool_qui_ne_declare_pas_project_ne_le_recoit_jamais(monkeypatch):
    s, vu = _session(monkeypatch, {"oto_procedure": ["op", "slug", "_org"]})
    s.call("oto_procedure", {"op": "get", "slug": "demo"})
    assert "_project" not in vu["appel"]["arguments"], \
        "poser un jeton non déclaré fait refuser l'appel ENTIER (vécu, 4 jobs)"
    assert "_run_id" not in vu["appel"]["arguments"]


def test_un_tool_inconnu_du_cache_ne_recoit_aucun_jeton(monkeypatch):
    s, vu = _session(monkeypatch, {"autre_tool": ["x"]})
    s.call("tool_inconnu", {"x": 1})
    assert "_project" not in vu["appel"]["arguments"], \
        "fail-safe : un appel sans contexte vaut mieux qu'un refus"
