"""Le flux SSE du serveur n'annonce PAS de charset — il se décode en UTF-8 quand même.

`text/event-stream` sans `charset` fait retomber requests sur le défaut HTTP des
`text/*`, ISO-8859-1 : « é » ressortait en « Ã© ». Sur le chemin stateless le
modèle RECOPIE ses résultats d'outils, donc la corruption finissait dans les
fiches produites (mesuré en production le 28/08/2026).
"""
from __future__ import annotations

import json

import pytest
import requests
from requests.utils import get_encoding_from_headers

import oto_runner.mcp as M
from oto_runner.mcp import McpSession

_INIT = b'data: {"jsonrpc":"2.0","id":1,"result":{}}'


def _reponse(corps: bytes, content_type: str, entetes: dict | None = None):
    """Une réponse requests RÉELLE, montée comme l'adaptateur HTTP la monte.

    Le point du test tient là : `Response.encoding` est DÉRIVÉ des en-têtes par
    `get_encoding_from_headers`, et c'est ce calcul-là qui rend ISO-8859-1 pour
    un `text/*` sans charset. Une Response nue (encoding None) retomberait sur
    la détection heuristique de `apparent_encoding` et masquerait le défaut.
    """
    r = requests.Response()
    r.status_code = 200
    r._content = corps
    r.headers["Content-Type"] = content_type
    for k, v in (entetes or {}).items():
        r.headers[k] = v
    r.encoding = get_encoding_from_headers(r.headers)
    return r


def _session(monkeypatch, reponse_appel):
    """Une session dont le transport est scripté — mais dont `_post` est le VRAI."""
    envois = []

    def _post(url, **kw):
        envois.append(kw)
        methode = (kw.get("json") or {}).get("method")
        if methode in ("initialize", "notifications/initialized"):
            return _reponse(_INIT, "text/event-stream", {"mcp-session-id": "s1"})
        return reponse_appel

    monkeypatch.setattr(M, "post_with_deadline", _post)
    return McpSession(url="http://x", token="t"), envois


def _appel(s):
    return s._post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "fr_search", "arguments": {}}})


_TEXTE = "Aucun résultat — société déjà écartée (critère d'éligibilité)"
# `ensure_ascii=False` : le serveur (starlette) sérialise ainsi — les accents
# voyagent en octets UTF-8 BRUTS sur le fil, pas en échappements `\uXXXX`. C'est
# ce qui rend la mauvaise détection d'encodage destructrice.
_CHARGE = ('data: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text",'
           '"text":' + json.dumps(_TEXTE, ensure_ascii=False) + '}]}}')


def test_le_flux_sse_sans_charset_se_decode_en_utf8(monkeypatch):
    """Le cas de production : aucun charset annoncé, du contenu accentué."""
    s, _ = _session(monkeypatch, _reponse(_CHARGE.encode("utf-8"),
                                          "text/event-stream"))
    texte = _appel(s)["result"]["content"][0]["text"]
    assert "Ã" not in texte, "double encodage : requests a lu du Latin-1"
    assert texte == _TEXTE


def test_un_charset_utf8_annonce_reste_correct(monkeypatch):
    """Le serveur peut se mettre à annoncer son charset : rien ne change."""
    s, _ = _session(monkeypatch, _reponse(_CHARGE.encode("utf-8"),
                                          "text/event-stream; charset=utf-8"))
    assert _appel(s)["result"]["content"][0]["text"] == _TEXTE


def test_une_reponse_non_utf8_leve(monkeypatch):
    """Pas de fallback silencieux : un corps qui n'est pas de l'UTF-8 est un
    échec NET, pas des remplacements muets au milieu d'une fiche."""
    latin1 = 'data: {"jsonrpc":"2.0","id":2,"result":{"t":"éligibilité"}}'.encode(
        "latin-1")   # 0xE9 nu : du Latin-1 valide, de l'UTF-8 impossible
    s, _ = _session(monkeypatch, _reponse(latin1, "text/event-stream"))
    with pytest.raises(RuntimeError, match="UTF-8"):
        _appel(s)


def test_le_corps_envoye_part_en_utf8(monkeypatch):
    """L'aller aussi : `json=` laisse requests sérialiser (échappement \\uXXXX,
    donc de l'ASCII) puis encoder en UTF-8 — un argument accentué round-trip."""
    s, envois = _session(monkeypatch, _reponse(_CHARGE.encode("utf-8"),
                                               "text/event-stream"))
    corps = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "t", "arguments": {"q": "clôture différée"}}}
    s._post(corps)
    assert envois[-1]["json"] == corps, "le corps part par `json=`, jamais en str"
    prepare = requests.Request("POST", "http://x", json=corps).prepare()
    assert json.loads(prepare.body.decode("utf-8")) == corps
