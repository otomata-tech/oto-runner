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


def test_un_contenu_en_liste_de_blocs_est_normalise(monkeypatch):
    """Mistral rend parfois `content` en LISTE de blocs typés au lieu d'une
    chaîne (vécu, job 52 : AttributeError au .strip() — déterministe au rejeu
    tant que la réponse garde cette forme)."""
    import oto_runner.agent_llm_openai as A

    class _R:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": [
                        {"type": "text", "text": "première partie"},
                        {"type": "text", "text": "seconde"}]},
                     "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2}}

    monkeypatch.setattr(A, "_post_borne", lambda url, corps, entetes: _R)
    t = A.complete(system="s", messages=[A.user_message("go")], tools=[],
                   api_key="k")
    assert t.text == "première partie\nseconde"


# ── LE CACHE : une clé à poser, un compteur à lire ──────────────────────────

def test_la_cle_de_cache_est_posee_sur_chaque_appel(monkeypatch):
    """⚠️ Sans elle, le fournisseur ne met RIEN en cache — mesuré le 01/09 :
    deux appels identiques, zéro jeton caché ; avec elle, 96 % dès le second.
    Un passage de 33 fiches a coûté 0,108 $ la ligne faute de ce paramètre."""
    vus = {}

    class R:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "ok"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 5}}

    def faux_post(url, corps, entetes, **_):
        vus.update(corps)
        return R()

    monkeypatch.setattr(P, "_post_borne", faux_post)
    monkeypatch.setattr(P, "resolve_key", lambda: "k")
    P.complete(system="s", messages=[], tools=None)
    assert vus.get("prompt_cache_key"), "aucune clé de cache dans la requête"


def test_les_jetons_servis_par_le_cache_ne_comptent_pas_comme_neufs(monkeypatch):
    """⚠️ `prompt_tokens` INCLUT ce que le cache a servi. Les porter tels quels
    ferait payer au plein tarif, dans nos relevés, ce qui est facturé 10 %."""
    class R:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "ok"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 2812, "completion_tokens": 3,
                              "prompt_tokens_details": {"cached_tokens": 2688}}}

    monkeypatch.setattr(P, "_post_borne", lambda *a, **k: R())
    monkeypatch.setattr(P, "resolve_key", lambda: "k")
    t = P.complete(system="s", messages=[], tools=None)
    assert t.usage["input_tokens"] == 124, "le neuf, pas le total"
    assert t.usage["cache_read_input_tokens"] == 2688
