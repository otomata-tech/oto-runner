"""Un armement refusé ARRÊTE le passage — avant tout enfilement.

Le backend refuse désormais `op=launch` sans worker joignable (`400
no_runner_armed`) et `runner.jobs op=enqueue` sur une campagne ni `armed` ni
`running` (`409 fleet_not_serving`) — oto-backend#996. L'ordonnanceur, lui,
TOLÉRAIT ces refus : il journalisait et continuait, et laissait derrière lui des
exécutions enfilées sur une campagne `draft` qu'`op=stop` ne sait pas arrêter.

Ce que ces tests exigent :

1. **Un armement refusé est fatal**, et rien n'est enfilé.
2. **La reprise reste permise** : `not_launchable` puis `not_takeable` sur une
   campagne qui se LIT `running`.
3. **Une prise refusée hors reprise est fatale.**
4. **Un `409 fleet_not_serving` à l'enfilement abandonne sur-le-champ** — pas
   dix tours plus tard sous le faux motif « backend indisponible ».
"""
from __future__ import annotations

import pytest

from oto_runner.backend import BackendError
from oto_runner.fleet import _ARRETS_NORMAUX, FleetSpec
from tests.test_fleet import FauxBackend, _run


def _spec(**kw):
    base = dict(input="Traite ce que la file te donne selon la procédure.",
                procedure="p", namespace="ns", name="armement",
                tools=("data_claim_next",), filter={"statut": "a_traiter"},
                concurrency=1, ramp_seconds=0, volume=1)
    base.update(kw)
    return FleetSpec(**base)


def _refus(status, code, texte):
    return BackendError(f"/api/me/runner/fleets → {status} : {texte}",
                        status=status, code=code)


class _Lisible(FauxBackend):
    """La campagne telle que `op=get` la sert — l'état que la reprise exige."""
    statut_lu = "running"

    def lire_flotte(self, fleet_id):
        self.lectures = getattr(self, "lectures", 0) + 1
        return {"id": fleet_id, "status": self.statut_lu}


# ── ① un armement refusé est fatal ───────────────────────────────────────────

class _SansRunner(_Lisible):
    def armer_flotte(self, fleet_id):
        raise _refus(400, "no_runner_armed",
                     "aucun runner armé ne sert cette organisation")


def test_un_armement_refuse_ABANDONNE_avant_tout_enfilement():
    b = _SansRunner([1, 1, 0])
    with pytest.raises(RuntimeError) as e:
        _run(_spec(), b)
    assert b.enfiles == 0, (
        "RIEN ne part : un travail enfilé sur une campagne `draft` échappe à "
        "`op=stop`")
    assert not getattr(b, "prises", []), "on ne prend pas ce qui n'est pas armé"
    assert "ABANDONNÉ" in str(e.value)
    assert "aucun runner armé" in str(e.value), (
        "le texte du SERVEUR passe en clair — c'est lui qui dit quoi corriger")


# ── ② la reprise d'un passage en cours reste permise ─────────────────────────

class _DejaEnCours(_Lisible):
    def armer_flotte(self, fleet_id):
        raise _refus(409, "not_launchable", "ce passage est `running`")

    def prendre_flotte(self, fleet_id):
        raise _refus(409, "not_takeable", "ce passage est `running`")


def test_not_launchable_sur_une_campagne_running_se_REPREND():
    """Le contrôle symétrique : sans lui, « tout refus est fatal » passerait
    ces tests en rendant toute reprise impossible."""
    b = _DejaEnCours([1, 1, 0])
    bilan = _run(_spec(fleet_id=7), b)
    assert b.enfiles >= 1, "la reprise d'un passage en cours enfile normalement"
    assert set(b.rattachements) == {7}
    assert getattr(b, "lectures", 0) >= 1, (
        "la reprise est PROUVÉE par l'état lu, pas supposée sur le code du refus")
    assert bilan.etat_muet == 0
    assert bilan.arret.startswith("volume atteint")


# ── ③ une prise refusée hors reprise est fatale ──────────────────────────────

class _PriseRefuseeArretee(_DejaEnCours):
    statut_lu = "stopped"


def test_not_takeable_sur_une_campagne_qui_ne_tourne_pas_ABANDONNE():
    b = _PriseRefuseeArretee([1, 1, 0])
    with pytest.raises(RuntimeError) as e:
        _run(_spec(fleet_id=7), b)
    assert b.enfiles == 0
    assert "`stopped`" in str(e.value), "l'état lu est dit, pas deviné"


class _PriseInterdite(_Lisible):
    def prendre_flotte(self, fleet_id):
        raise _refus(403, "forbidden", "pas le droit de prendre ce passage")


def test_une_prise_refusee_pour_un_autre_motif_ABANDONNE():
    b = _PriseInterdite([1, 1, 0])
    with pytest.raises(RuntimeError) as e:
        _run(_spec(), b)
    assert b.enfiles == 0
    assert "pas le droit de prendre" in str(e.value)


# ── ④ `409 fleet_not_serving` à l'enfilement : abandon immédiat ──────────────

class _CampagneHorsService(FauxBackend):
    def enqueue(self, kind, payload, run_id=None, fleet_id=None):
        self.tentatives = getattr(self, "tentatives", 0) + 1
        raise BackendError(
            "/api/me/runner/jobs → 409 : cette campagne est `draft` : elle "
            "n'accepte une exécution que lorsqu'elle est armée (`armed`) ou en "
            "cours (`running`)", status=409, code="fleet_not_serving")


def test_fleet_not_serving_a_l_enfilement_ABANDONNE_sur_le_champ():
    b = _CampagneHorsService([5, 5])
    bilan = _run(_spec(volume=None), b)
    assert b.tentatives == 1, (
        f"{b.tentatives} tentatives : retenter ne réarmera pas la campagne")
    assert "backend indisponible" not in bilan.arret, (
        "le serveur répondait, et disait pourquoi — ce n'est pas une panne")
    assert "`draft`" in bilan.arret, "le motif d'arrêt dit l'état de la campagne"
    assert not any(bilan.arret.startswith(m) for m in _ARRETS_NORMAUX), (
        "un abandon n'est pas une fin normale : le process sort en échec")
