"""L'état d'un passage : la SÉQUENCE complète, et ce qu'on ne peut pas dire.

Mesuré le 03/09 : **14 campagnes déclarées, toutes `draft`, aucune jamais
`running`** — alors que huit vagues avaient réellement tourné dans la nuit. La
cause immédiate est un armement manquant : une flotte naît `draft`, et la prendre
exige `armed`.

⚠️ **Mais le défaut n'est pas l'armement manquant.** C'est qu'un échec d'appel
était rattrapé et poursuivi, laissant l'état mentir sans que rien ne le dise. Le
journal disait pourtant la vérité à chaque battement — *« le passage tourne quand
même, mais son état ne dira pas en cours »*. **Personne ne l'a lue pendant huit
vagues.**

D'où les deux familles de tests ici : la séquence est parcourue **dans l'ordre**,
et un passage qui n'a pas pu dire où il en était **le conclut**, au lieu d'ajouter
une ligne de journal de plus.
"""
from __future__ import annotations

import logging

import pytest

from oto_runner.backend import BackendError
from oto_runner.fleet import FleetSpec, run_fleet
from tests.test_fleet import FauxBackend, Horloge, _run


def _spec(**kw):
    base = dict(procedure="p", namespace="ns", name="passage",
                tools=("data_claim_next",), filter={"statut": "a_traiter"},
                concurrency=1, ramp_seconds=0, volume=1)
    base.update(kw)
    return FleetSpec(**base)


def _bk(cls=FauxBackend):
    # `counts` = ce que la file rend à chaque comptage : une ligne, puis vide.
    return cls([1, 0])


# ── la séquence : déclarer → ARMER → prendre ─────────────────────────────────

def test_la_flotte_est_ARMEE_avant_d_etre_prise():
    """Le cas mesuré. Sans armement, la prise est refusée à chaque fois — et le
    refus était rattrapé, donc invisible."""
    b = _bk()
    _run(_spec(), b)
    assert getattr(b, "armees", []) == [42], (
        "la flotte n'a pas été armée : `prendre` refusera, et l'état restera `draft`")


def test_l_ordre_compte_declarer_puis_armer_puis_prendre():
    """⚠️ Armer AVANT de prendre, pas après : `take` n'accepte qu'une flotte
    `armed`. L'ordre est le fond du correctif, pas un détail."""
    ordre = []
    b = _bk()
    for nom in ("declarer_flotte", "armer_flotte", "prendre_flotte"):
        vrai = getattr(b, nom)

        def trace(*a, _n=nom, _v=vrai, **k):
            ordre.append(_n)
            return _v(*a, **k)

        setattr(b, nom, trace)
    _run(_spec(), b)
    assert ordre == ["declarer_flotte", "armer_flotte", "prendre_flotte"]


# ── ce qu'on n'a pas pu dire se COMPTE, et se conclut ────────────────────────

class _ArmementRefuse(FauxBackend):
    def armer_flotte(self, fleet_id):
        raise BackendError("403 — pas le droit d'armer")


def test_un_armement_refuse_se_COMPTE_dans_le_bilan():
    """⚠️ Le cœur du lot. Le passage continue — c'est voulu, perdre
    l'observabilité vaut mieux qu'une campagne qui refuse de partir — mais il ne
    continue plus EN SILENCE."""
    bilan = _run(_spec(), _bk(_ArmementRefuse))
    assert bilan.etat_muet >= 1, (
        "l'échec a été rattrapé sans laisser de trace dans le bilan — "
        "c'est exactement le défaut qu'on corrige")


def test_un_passage_aveugle_le_DIT_a_la_fin(caplog):
    """⚠️ Et il le dit en ERREUR, pas en avertissement noyé. Chacun de ces ratés
    était déjà journalisé un par un ; une ligne de plus n'aurait rien changé.
    C'est la CONCLUSION qui manque — celle qu'on lit quand on regarde le
    résultat."""
    with caplog.at_level(logging.ERROR):
        _run(_spec(), _bk(_ArmementRefuse))
    dit = "\n".join(r.getMessage() for r in caplog.records
                    if r.levelno >= logging.ERROR)
    assert "AVEUGLE" in dit.upper(), "le passage n'a pas conclu sur son aveuglement"


def test_un_passage_SAIN_ne_crie_pas(caplog):
    """⚠️ Le contrôle symétrique, sans lequel le précédent ne prouve rien : une
    alerte qui se déclenche toujours ne se distingue pas d'un décor."""
    with caplog.at_level(logging.ERROR):
        bilan = _run(_spec(), _bk())
    assert bilan.etat_muet == 0
    dit = "\n".join(r.getMessage() for r in caplog.records
                    if r.levelno >= logging.ERROR)
    assert "AVEUGLE" not in dit.upper()
