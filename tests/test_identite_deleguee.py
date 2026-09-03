"""L'agent travaille sous l'identité du DEMANDEUR, pas sous celle du worker.

Le serveur remet à la réservation un jeton au nom de qui a programmé le travail,
borné à la durée du bail. ⚠️ **Un worker qui l'ignorerait écrirait tout au nom de
son propre compte — et rien ne le signalerait**, puisque les écritures
aboutiraient. C'est le défaut le plus silencieux de ce chemin : il ne produit
aucune erreur, seulement une attribution fausse.
"""
from __future__ import annotations

import pytest

from oto_runner import worker as W


class _Stop(RuntimeError):
    """Sentinelle : ce banc regarde la SESSION, pas le déroulé."""


class _FauxMcp:
    """Retient ce qu'on lui a passé — c'est le jeton qui nous intéresse."""

    dernier = None

    def __init__(self, **kw):
        _FauxMcp.dernier = kw
        self.kw = kw

    def outil(self, *a, **k):
        raise _Stop("ce banc s'arrête au premier appel d'outil")


def test_la_session_porte_le_jeton_du_demandeur(monkeypatch):
    """Le fait central : le jeton du travail gagne sur celui du worker."""
    monkeypatch.setattr(W, "McpSession", _FauxMcp)
    job = {"id": 7, "kind": "start", "payload": {"project_id": 3, "org_id": 42, "procedure": "veille"},
           "delegated_token": "oto_le-jeton-du-demandeur"}
    with pytest.raises(_Stop):      # on s'arrête au premier appel d'outil
        W._traiter(object(), job, object())
    assert _FauxMcp.dernier["token"] == "oto_le-jeton-du-demandeur"


def test_sans_jeton_delegue_le_worker_retombe_sur_le_sien(monkeypatch):
    """Travaux d'avant la délégation : pas de porteur connu, donc pas de jeton.
    On ne casse pas — on repasse par le comportement d'avant."""
    monkeypatch.setattr(W, "McpSession", _FauxMcp)
    job = {"id": 8, "kind": "start",
           "payload": {"project_id": 3, "org_id": 42, "procedure": "veille"}}
    with pytest.raises(_Stop):
        W._traiter(object(), job, object())
    assert _FauxMcp.dernier["token"] is None, (
        "un jeton vide doit valoir ABSENCE, pas chaîne vide — sinon la session "
        "présente un jeton invalide au lieu de retomber sur celui du worker")


def test_un_refus_d_identite_arrete_le_travail_sans_le_toucher(monkeypatch):
    """⚠️ Deux choses à la fois : on lève AVANT d'ouvrir quoi que ce soit, et on
    ne conclut pas le travail — le serveur l'a déjà marqué en échec avec sa
    raison. Le conclure ici rendrait une erreur de bail, qui accuserait la file
    alors que le problème est un DROIT."""
    def _interdit(**kw):
        pytest.fail("une session a été ouverte malgré un refus d'identité")

    monkeypatch.setattr(W, "McpSession", _interdit)
    job = {"id": 9, "kind": "start", "payload": {},
           "delegation_refusee": "le compte qui a programmé ce travail n'existe plus"}
    with pytest.raises(W.IdentiteInvalide) as e:
        W._traiter(object(), job, object())
    assert "n'existe plus" in str(e.value), "la RAISON doit remonter, pas juste l'échec"
