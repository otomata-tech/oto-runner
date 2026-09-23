"""La boîte à outils d'une session de flotte suit l'org de la MISSION (#1058).

Le backend calcule la visibilité des outils à l'`initialize` MCP, avant tout
`_org=` d'appel : sans en-tête, elle dérive de l'org maison du compte porteur.
Vécu le 22/09/2026 : la maison a basculé 2 → 226 et les connecteurs de toutes
les flottes du compte ont disparu ~2h. Le runner porte donc `X-Oto-Org` sur la
requête `initialize` quand il connaît l'org — et seulement sur elle : ensuite,
chaque appel porte son `_org`, qui fait foi.
"""
from __future__ import annotations

from oto_runner import mcp as mcp_mod
from oto_runner.mcp import McpSession


class _Reponse:
    def __init__(self, entetes=None):
        self.headers = entetes or {}
        self.content = b"{}"


def _requetes(monkeypatch, **kw):
    vues = []

    def _post(url, json=None, headers=None, **_):
        vues.append((json.get("method"), dict(headers)))
        if json.get("method") == "initialize":
            return _Reponse({"mcp-session-id": "s1"})
        return _Reponse()

    monkeypatch.setattr(mcp_mod, "post_with_deadline", _post)
    s = McpSession(url="http://x", token="t", **kw)
    return s, vues


def test_l_initialize_porte_l_org_de_la_mission(monkeypatch):
    _, vues = _requetes(monkeypatch, org=226)
    methode, entetes = vues[0]
    assert methode == "initialize"
    assert entetes["X-Oto-Org"] == "226"


def test_sans_org_l_initialize_ne_porte_aucun_en_tete_d_org(monkeypatch):
    _, vues = _requetes(monkeypatch)
    methode, entetes = vues[0]
    assert methode == "initialize"
    assert "X-Oto-Org" not in entetes


def test_seul_l_initialize_porte_l_en_tete(monkeypatch):
    """Les requêtes suivantes portent leur org en `_org`, pas en en-tête."""
    _, vues = _requetes(monkeypatch, org=226)
    suivantes = [e for m, e in vues if m != "initialize"]
    assert suivantes, "la notification initialized suit l'initialize"
    assert all("X-Oto-Org" not in e for e in suivantes)


def test_une_session_rouverte_reporte_l_org(monkeypatch):
    """Une session perdue se rouvre par `_ouvrir` : même handshake, même org."""
    s, vues = _requetes(monkeypatch, org=226)
    vues.clear()
    s._ouvrir()
    assert vues[0][0] == "initialize"
    assert vues[0][1]["X-Oto-Org"] == "226"
