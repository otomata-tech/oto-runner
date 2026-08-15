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


def test_lorg_porte_les_tools_sans_project(monkeypatch):
    """`oto_procedure` déclare `_org` mais pas `_project` : sans l'org de la
    mission, la doctrine se cherche dans l'org MAISON du jeton et n'existe pas
    (vécu : 2 jobs de re-validation en échec avant une ligne). L'org ne se pose
    QUE quand le projet ne peut pas la porter — jamais les deux."""
    def _s(tools):
        return _session_org(monkeypatch, tools)

    s, vu = _s({"oto_procedure": ["op", "slug", "_org"]})
    s.call("oto_procedure", {"op": "get", "slug": "demo"})
    assert vu["appel"]["arguments"]["_org"] == 226
    assert "_project" not in vu["appel"]["arguments"]

    s, vu = _s({"data_claim_next": ["namespace", "_org", "_project", "_run_id"]})
    s.call("data_claim_next", {"namespace": "ns"})
    assert vu["appel"]["arguments"]["_project"] == 248
    assert "_org" not in vu["appel"]["arguments"], "le projet porte déjà l'org"


def _session_org(monkeypatch, tools):
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
    return McpSession(url="http://x", token="t", project=248, run_id="r-1",
                      org=226), vu


def test_un_initialize_muet_echoue_net_apres_trois_essais(monkeypatch):
    """Un 502 pendant l'initialize rendait une session MUETTE — puis chaque
    appel mourait en « Missing session ID » cryptique. Trois essais, échec NET."""
    import pytest

    import oto_runner.mcp as M

    essais = {"n": 0}

    def _post(self, corps, avec_entetes=False):
        essais["n"] += 1
        return ({}, {}) if avec_entetes else {}

    monkeypatch.setattr(McpSession, "_post", _post)
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(RuntimeError) as e:
        McpSession(url="http://x", token="t")
    assert "session id" in str(e.value) and essais["n"] == 3


def test_toute_requete_sortante_a_une_deadline_dure(monkeypatch):
    """Deux workers pendus UNE HEURE en SSL (le read timeout se réarme, le
    handshake pend) : chaque client HTTP du runner passe par la deadline
    SIGALRM — plus aucun requests nu sur le chemin des requêtes."""
    import time as _t

    import pytest

    from oto_runner import deadline as D

    monkeypatch.setattr(D, "_DEFAULT_WALL_S", 1)
    with pytest.raises(D.DeadlineExceeded):
        D._with_deadline(lambda url, **k: _t.sleep(5), "http://lent", wall_s=1)


def test_un_tools_list_vide_echoue_net(monkeypatch):
    """Un tools/list en erreur (502 en vol) laissait un cache VIDE — le
    fail-safe ne posait plus aucun jeton et le job mourait sur une erreur
    MÉTIER trompeuse jamais rejouée (« Aucune doctrine (scope org) », vécu
    job 27). Session dégradée = échec NET, le retry de job fait le reste."""
    import pytest

    def _post(self, corps, avec_entetes=False):
        if corps.get("method") == "initialize":
            return ({"mcp-session-id": "s1"}, {}) if avec_entetes else {}
        return {"_brut": "502 Bad Gateway"}

    monkeypatch.setattr(McpSession, "_post", _post)
    s = McpSession(url="http://x", token="t")
    with pytest.raises(RuntimeError, match="session dégradée"):
        s.schemas(frozenset())
