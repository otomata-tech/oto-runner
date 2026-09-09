"""Le cadre système du worker : ce qu'il porte, et ce qu'il ne va PAS chercher.

⚠️ Le worker ne lit aucun objet d'Oto — c'est la règle d'ADR 0064, et elle
tient. Ce qui a changé le 09/09/2026 : le TRAVAIL peut porter un `system`, joint
par la plateforme à la réservation comme le sont déjà la clé de modèle et le
jeton délégué. Le worker le pose dans le cadre sans savoir ce que c'est.

Pourquoi : une consigne chargée par l'agent au premier tour est facturée plein
tarif au deuxième — 20 603 jetons sur les 41 204 d'un déroulé mesuré, cache à
zéro sur ce tour-là. Dans le cadre, elle entre dans le préfixe stable.
"""
from __future__ import annotations

from oto_runner import worker


def _job(**kw):
    base = {"id": 1, "payload": {"tools": ["data_rows"], "max_steps": 5}}
    base.update(kw)
    return base


def test_sans_texte_joint_le_cadre_est_celui_du_worker_SEUL():
    spec = worker._spec_du_job(_job())
    assert spec.system == worker._SYSTEM_FRAME, (
        "aucun travail ne doit hériter d'un cadre qu'on ne lui a pas donné")


def test_le_texte_JOINT_entre_dans_le_cadre():
    spec = worker._spec_du_job(_job(system="LA CONSIGNE MÉTIER"))
    assert spec.system.startswith(worker._SYSTEM_FRAME), "le cadre garde sa tête"
    assert "LA CONSIGNE MÉTIER" in spec.system


def test_le_cadre_DIT_de_ne_pas_recharger():
    """Sans cette phrase, l'instruction de la campagne — « Lis d'abord la
    procédure avec oto_procedure » — ferait charger le même texte une seconde
    fois, au tour le plus cher. Le gain serait annulé, et doublé d'un doublon."""
    spec = worker._spec_du_job(_job(system="LA CONSIGNE"))
    assert "ne la recharge" in spec.system


def test_un_system_VIDE_ne_pollue_pas_le_cadre():
    """Un champ présent mais vide est le cas d'une procédure absente côté
    plateforme : on retombe exactement sur le cadre nu, sans en-tête orpheline
    annonçant une procédure qui n'est pas là."""
    for vide in ("", "   ", None):
        assert worker._spec_du_job(_job(system=vide)).system == worker._SYSTEM_FRAME
