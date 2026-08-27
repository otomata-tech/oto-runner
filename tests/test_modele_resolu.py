"""La version CONCRÈTE derrière l'alias de modèle — `/v1/models`.

Le worker appelle par un ALIAS (`mistral-large-latest`) : le fournisseur le
fait pointer ailleurs quand il le décide, sans préavis. Sans la version, une
anomalie de campagne ne se date pas — on ignore quels jobs ont tourné avant la
bascule et lesquels après.

⚠️ Ce que ce banc fige avant tout : la forme SYMÉTRIQUE du catalogue réel
(relevé le 27/08). La première règle livrée déduisait « ce nom flotte » du fait
qu'il est cité en alias — la symétrie faisait alors flotter la version concrète
elle-même, aucun candidat ne restait, et la production a rendu None sur les
56 modèles du catalogue. Le discriminant est le SUFFIXE `-latest`, pas la
citation.
"""
from __future__ import annotations

import pytest

import oto_runner.agent_conversations as C
from tests.test_agent_conversations import (_MODELS, _REPONSE, _FauxRequests,
                                            _R, _env)


@pytest.fixture(autouse=True)
def _catalogue(monkeypatch):
    """Aucun test ne sort sur le réseau ; le cache est un état de MODULE."""
    C._resolutions.clear()
    monkeypatch.setattr(C, "requests", _FauxRequests(_R(corps=_MODELS)))
    yield
    C._resolutions.clear()


def test_un_alias_est_resolu_malgre_un_catalogue_symetrique(monkeypatch):
    """LE cas de la panne : l'alias cite la version autant que l'inverse."""
    _env(monkeypatch)
    faux = _FauxRequests(_R(corps=_MODELS))
    monkeypatch.setattr(C, "requests", faux)
    assert C.modele_resolu("mistral-large-latest") == "mistral-large-2512"
    url, kw = faux.appels[0]
    assert url == "https://api.mistral.ai/v1/models"
    assert kw["timeout"] == (10, 30)
    assert kw["headers"]["Authorization"] == "Bearer k", "la clé des conversations"


def test_la_resolution_reutilise_la_base_sans_doubler_le_v1(monkeypatch):
    """Même dédoublement que les conversations : la base de l'env porte /v1."""
    _env(monkeypatch)
    monkeypatch.setenv("OTO_RUNNER_OPENAI_BASE", "https://api.mistral.ai/v1")
    faux = _FauxRequests(_R(corps=_MODELS))
    monkeypatch.setattr(C, "requests", faux)
    C.modele_resolu("mistral-large-latest")
    assert faux.appels[0][0] == "https://api.mistral.ai/v1/models"


def test_une_version_concrete_citee_par_son_alias_se_rend_elle_meme(monkeypatch):
    """La symétrie cite la version : elle ne doit pas se résoudre en son propre
    alias pour autant. Seul candidat = un `-latest`, donc écarté."""
    _env(monkeypatch)
    assert C.modele_resolu("mistral-large-2512") == "mistral-large-2512"


def test_un_nom_concret_inconnu_du_catalogue_se_rend_tel_quel(monkeypatch):
    """Il ne flotte pas : ce qu'on a appelé EST ce qui a tourné."""
    _env(monkeypatch)
    assert C.modele_resolu("mistral-medium-2508") == "mistral-medium-2508"


def test_plusieurs_millesimes_le_plus_recent_gagne(monkeypatch):
    """Un alias a connu plusieurs versions et le catalogue les cite toutes :
    on retient le millésime le plus grand plutôt que l'ordre de la liste."""
    _env(monkeypatch)
    cat = {"data": [
        {"id": "mistral-large-2512", "aliases": ["mistral-large-latest"]},
        {"id": "mistral-large-2411", "aliases": ["mistral-large-latest"]},
        {"id": "mistral-large-2407", "aliases": ["mistral-large-latest"]},
        {"id": "mistral-large-latest", "aliases": ["mistral-large-2512"]}]}
    monkeypatch.setattr(C, "requests", _FauxRequests(_R(corps=cat)))
    assert C.modele_resolu("mistral-large-latest") == "mistral-large-2512"


def test_un_alias_introuvable_rend_none_plutot_que_lui_meme(monkeypatch):
    """Rendre l'alias serait indiscernable d'une version : on ne devine pas."""
    _env(monkeypatch)
    monkeypatch.setattr(C, "requests", _FauxRequests(_R(corps={"data": []})))
    assert C.modele_resolu("mistral-large-latest") is None


def test_une_panne_reseau_rend_none_sans_lever(monkeypatch):
    """SEULE tolérance : un relevé ne fait pas échouer un job déjà payé."""
    _env(monkeypatch)
    monkeypatch.setattr(C, "requests",
                        _FauxRequests(OSError("connexion refusée")))
    assert C.modele_resolu("mistral-large-latest") is None


def test_un_job_survit_a_une_resolution_impossible(monkeypatch):
    """Le run se joue et se conclut ; seul le champ `model` manque."""
    _env(monkeypatch)
    monkeypatch.setattr(C, "requests", _FauxRequests(OSError("dns")))
    monkeypatch.setattr(C, "post_with_deadline", lambda url, **kw: _R(corps=_REPONSE))
    res = C.run_once(instructions="p", inputs="i", tools=())
    assert res.reply and res.model is None


def test_la_resolution_est_en_cache_dans_la_fenetre_puis_rafraichie(monkeypatch):
    """Une bascule doit se voir dans l'heure de vol — pas au redémarrage du
    worker, et pas au prix d'un GET par job non plus."""
    _env(monkeypatch)
    faux = _FauxRequests(_R(corps=_MODELS))
    monkeypatch.setattr(C, "requests", faux)
    horloge = {"t": 1_000.0}
    monkeypatch.setattr(C.time, "monotonic", lambda: horloge["t"])

    assert C.modele_resolu("mistral-large-latest") == "mistral-large-2512"
    horloge["t"] += C._TTL_RESOLUTION_S - 1
    assert C.modele_resolu("mistral-large-latest") == "mistral-large-2512"
    assert len(faux.appels) == 1, "dans la fenêtre, le cache répond"

    horloge["t"] += 2
    faux.reponse = _R(corps={"data": [
        {"id": "mistral-large-2601", "aliases": ["mistral-large-latest"]},
        {"id": "mistral-large-latest", "aliases": ["mistral-large-2601"]}]})
    assert C.modele_resolu("mistral-large-latest") == "mistral-large-2601"
    assert len(faux.appels) == 2, "passée la fenêtre, on redemande"


def test_un_echec_nest_pas_mis_en_cache(monkeypatch):
    """Sinon une coupure de 3 s aveuglerait le worker 10 minutes."""
    _env(monkeypatch)
    monkeypatch.setattr(C, "requests", _FauxRequests(OSError("blip")))
    assert C.modele_resolu("mistral-large-latest") is None
    monkeypatch.setattr(C, "requests", _FauxRequests(_R(corps=_MODELS)))
    assert C.modele_resolu("mistral-large-latest") == "mistral-large-2512"
