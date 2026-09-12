"""Le modèle vient AVEC le travail — et le worker sert CE modèle, ou rien.

Jusqu'ici le modèle était épinglé par worker (`OTO_RUNNER_MODEL`) : un agent
pouvait le déclarer, le serveur le stockait et le validait, et le runner le
jetait. `tests/test_champs_servis_et_lus.py` l'inscrivait noir sur blanc dans
`_NON_LUS`. Un champ inerte est une dette ; **un champ qui PROMET une
attribution qui n'arrive pas est pire** — parce qu'on lit la promesse dans les
relevés de coût.

Ce que ces bancs protègent :

1. **Le modèle du travail est celui qui part au fournisseur**, sur les trois
   chemins (Anthropic, OpenAI-compatible, conversations).
2. **Sans modèle déclaré, rien ne change** — le worker garde le sien, à l'octet.
3. **Une famille étrangère ne s'exécute PAS.** Servir le modèle du worker à la
   place de celui qu'on demande facturerait le mauvais fournisseur et rendrait
   un modèle que personne n'a choisi : un mensonge qui ne se lit que sur une
   facture.
4. **L'estampille suit la demande**, pas l'environnement : un bilan qui nomme le
   modèle du worker sur un travail servi par un autre envoie chercher au mauvais
   endroit. (Le chemin `conversations` estampille la version concrète résolue au
   catalogue — son arbitrage à lui, tenu par `test_modele_resolu.py`.)
"""
from __future__ import annotations

import pytest

from oto_runner import agent_conversations, agent_llm, agent_llm_openai, worker
from oto_runner.agent_runtime import AgentSpec


def _job(**payload):
    base = {"tools": ["data_rows"], "max_steps": 5}
    base.update(payload)
    return {"id": 1, "payload": base}


# ── 1. le cadre porte le modèle ───────────────────────────────────────────────

def test_le_cadre_porte_le_modele_du_travail():
    assert worker._spec_du_job(_job(model="claude-opus-5")).model == "claude-opus-5"


def test_sans_modele_declare_le_cadre_n_en_porte_AUCUN():
    """`None`, et surtout pas le défaut du worker recopié ici : c'est le
    provider qui décide du repli, et lui seul sait lequel."""
    assert worker._spec_du_job(_job()).model is None


def test_un_modele_vide_vaut_une_absence():
    """Une chaîne vide traversant le payload ne doit pas partir au fournisseur
    comme un nom de modèle — elle rendrait un 400 opaque."""
    assert worker._spec_du_job(_job(model="   ")).model is None


# ── 2. il atteint le fournisseur, sur les trois chemins ───────────────────────

class _FauxSdk:
    """Le SDK Anthropic, réduit à ce que `complete` en appelle."""

    def __init__(self, vu):
        self.vu = vu
        self.messages = self

    def Anthropic(self, api_key=None):  # noqa: N802 — le nom du SDK
        return self

    def create(self, **kwargs):
        self.vu.update(kwargs)
        return type("R", (), {"stop_reason": "end_turn", "content": [],
                              "usage": None, "model": None})()


def test_anthropic_appelle_LE_MODELE_DU_TRAVAIL(monkeypatch):
    vu: dict = {}
    monkeypatch.setattr(agent_llm, "_sdk", lambda: _FauxSdk(vu))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-plateforme")
    agent_llm.complete(system="s", messages=[], tools=[], modele="claude-opus-5")
    assert vu["model"] == "claude-opus-5"


def test_anthropic_sans_modele_garde_CELUI_DU_WORKER(monkeypatch):
    vu: dict = {}
    monkeypatch.setattr(agent_llm, "_sdk", lambda: _FauxSdk(vu))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-plateforme")
    monkeypatch.setenv("OTO_RUNNER_MODEL", "claude-haiku-4-5")
    agent_llm.complete(system="s", messages=[], tools=[])
    assert vu["model"] == "claude-haiku-4-5", "le comportement d'avant, intact"


def test_anthropic_estampille_CE_QU_ON_A_DEMANDE(monkeypatch):
    """Le fournisseur ne rend pas toujours le nom servi. Le repli doit être le
    modèle DEMANDÉ — celui du worker enverrait chercher au mauvais endroit."""
    monkeypatch.setattr(agent_llm, "_sdk", lambda: _FauxSdk({}))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-plateforme")
    monkeypatch.setenv("OTO_RUNNER_MODEL", "claude-haiku-4-5")
    turn = agent_llm.complete(system="s", messages=[], tools=[], modele="claude-opus-5")
    assert turn.model == "claude-opus-5"


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = ""

    def json(self):
        return self._payload


def test_openai_compatible_appelle_le_modele_du_travail(monkeypatch):
    vu: dict = {}

    def _post(url, json=None, headers=None, timeout=None, **kw):
        vu.update(json or {})
        return _Resp({"choices": [{"message": {"role": "assistant", "content": "ok"},
                                   "finish_reason": "stop"}],
                      "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    monkeypatch.setattr(agent_llm_openai.requests, "post", _post)
    turn = agent_llm_openai.complete(system="s", messages=[], tools=[], api_key="k",
                                     modele="mistral-large-2512")
    assert vu["model"] == "mistral-large-2512"
    assert turn.model == "mistral-large-2512", "l'estampille suit la demande"


def test_conversations_appelle_le_modele_du_travail(monkeypatch):
    vu: dict = {}

    def _post(url, json=None, headers=None, **kw):
        vu.update(json or {})
        return _Resp({"outputs": [], "usage": {}})

    monkeypatch.setattr(agent_conversations, "post_with_deadline", _post)
    monkeypatch.setattr(agent_conversations, "modele_resolu", lambda nom: None)
    monkeypatch.setenv("OTO_RUNNER_CONNECTOR_ID", "conn-banc")
    res = agent_conversations.run_once(instructions="s", inputs="i", tools=(),
                                       api_key="k", modele="mistral-large-2512")
    assert vu["model"] == "mistral-large-2512"
    # ⚠️ Pas d'assertion sur `res.model` ici : ce chemin estampille la version
    # CONCRÈTE résolue au catalogue, et la doublure ci-dessus ne la résout pas
    # (`test_modele_resolu.py` tient cet arbitrage). Ce qui est vérifié ici est
    # ce que ce lot change : le modèle DEMANDÉ part bien au fournisseur.


# ── 3. une famille étrangère ne s'exécute pas ─────────────────────────────────

class _Provider:
    def __init__(self, depot_):
        self._depot = depot_

    def depot(self):
        return self._depot


def test_un_travail_d_une_AUTRE_famille_est_refuse():
    """⚠️ LE banc du lot. Le serveur filtre déjà la file ; ce second verrou
    existe pour la mauvaise CONFIGURATION — un worker dont l'hôte a changé
    réserverait des travaux qu'il servirait sur le mauvais fournisseur, sans que
    rien ne le dise."""
    with pytest.raises(worker.FamilleEtrangere) as e:
        worker._exiger_ma_famille({"model_family": "mistral"}, _Provider("anthropic"))
    assert "mistral" in str(e.value) and "anthropic" in str(e.value)
    assert "OTO_RUNNER_PROVIDER" in str(e.value), "le refus dit où regarder"


def test_ma_propre_famille_passe():
    worker._exiger_ma_famille({"model_family": "anthropic"}, _Provider("anthropic"))


def test_un_travail_SANS_famille_passe_partout():
    """C'est tout l'existant : aucun agent déclaré avant ce lot ne porte de
    famille, et un worker sans dépôt connu n'en sert aucune."""
    worker._exiger_ma_famille({}, _Provider("anthropic"))
    worker._exiger_ma_famille({}, _Provider(""))


def test_un_worker_SANS_depot_refuse_une_famille():
    """Le pendant : réserver un travail Mistral sur un worker qui ne nomme aucun
    dépôt le ferait tourner sur la clé de la plateforme, chez un fournisseur que
    personne n'a choisi."""
    with pytest.raises(worker.FamilleEtrangere):
        worker._exiger_ma_famille({"model_family": "mistral"}, _Provider(""))


def test_un_provider_dont_le_depot_LEVE_ne_sert_aucune_famille():
    class _Cassé:
        def depot(self):
            raise RuntimeError("hôte illisible")

    with pytest.raises(worker.FamilleEtrangere):
        worker._exiger_ma_famille({"model_family": "anthropic"}, _Cassé())


# ── 4. la boucle transmet ce que le cadre porte ───────────────────────────────

def test_la_boucle_passe_le_modele_du_CADRE_au_fournisseur(monkeypatch):
    """Sans ce fil, tout ce qui précède reste décoratif : le cadre porterait un
    modèle que la boucle n'enverrait pas."""
    from oto_runner import agent_runtime

    vu: dict = {}

    from oto_runner.llm_types import Turn
    from tests.test_agent_runtime import FauxProvider, FauxTransport

    class _P(FauxProvider):
        def complete(self, **kwargs):
            vu.update(kwargs)
            return Turn(text="fini", tool_calls=(), stop_reason="end_turn",
                        raw_content=[], usage={}, model="servi")

    spec = AgentSpec(system="s", tools=frozenset(), max_steps=1,
                     model="claude-opus-5")
    agent_runtime.run(spec, FauxTransport(), _P([]), prompt="va")
    assert vu["modele"] == "claude-opus-5"
