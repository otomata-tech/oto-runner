"""L'ordonnanceur NOMME son preneur, et apprend du backend qui tient sa campagne.

oto-backend#1032 (21/09/2026) : `take`, `beat` et `ack_stop` exigent `taken_by`
— l'identifiant que l'ordonnanceur DÉCLARE, `OTO_FLEET_HOLDER`, posé par
`scripts/flotte.sh` à `<machine>/<unité systemd>`. Le backend en tire la règle :
seul celui qui TIENT une campagne la conduit.

Ce que ces tests figent :

1. **Chaque geste d'ordonnanceur porte le preneur**, et aucun ne part sans lui.
2. **La reprise par le même preneur** passe par la prise, que le backend accepte.
3. **`held_by_other` à la prise** : abandon définitif (exit 3), rien d'enfilé.
4. **`not_the_holder` au battement ou à l'accusé** : l'ordonnanceur cesse de
   conduire, et sort sans relance.
5. **`fleet` refuse de démarrer sans `OTO_FLEET_HOLDER`**, et le passe au client.
6. **L'unité pose le preneur** aligné sur le nom qu'elle porte réellement.
"""
from __future__ import annotations

import re
import sys

import pytest

from oto_runner import fleet as F
from oto_runner.backend import Backend
from oto_runner.fleet import SORTIE_ABANDON_DEFINITIF, AbandonDefinitif
from tests.test_armement_fatal import _refus, _spec
from tests.test_fleet import FauxBackend, _run
from tests.test_relance_ordonnanceur import _unite_de_l_ordonnanceur

_PRENEUR = "oto-platform/oto-fleet-vague3"


def _main(monkeypatch, argv, *, preneur=_PRENEUR, backend=None, run=None):
    if preneur is None:
        monkeypatch.delenv("OTO_FLEET_HOLDER", raising=False)
    else:
        monkeypatch.setenv("OTO_FLEET_HOLDER", preneur)
    monkeypatch.setattr(sys, "argv", ["fleet", *argv])
    monkeypatch.setattr(F, "load_spec", lambda p: _spec())
    clients = []
    monkeypatch.setattr(F, "Backend", lambda **kw: clients.append(kw) or backend)
    if run is not None:
        monkeypatch.setattr(F, "run_fleet", run)
    F.main()
    return clients


# ── ① chaque geste d'ordonnanceur porte le preneur ───────────────────────────

def test_chaque_geste_d_ordonnanceur_porte_son_preneur(monkeypatch):
    b = Backend(base="http://x", token="t", preneur=_PRENEUR)
    corps = []
    monkeypatch.setattr(b, "_post",
                        lambda chemin, c, **kw: corps.append(c) or {"fleet": {}})
    b.prendre_flotte(7)
    b.battre_flotte(7)
    b.accuser_arret(7, "fin")
    assert [c["op"] for c in corps] == ["take", "beat", "ack_stop"]
    assert [c.get("taken_by") for c in corps] == [_PRENEUR] * 3, (
        "un geste sans `taken_by` est refusé `400 missing_fields`")


def test_aucun_geste_ne_part_sans_preneur(monkeypatch):
    b = Backend(base="http://x", token="t")
    monkeypatch.setattr(b, "_post",
                        lambda *a, **kw: pytest.fail("geste parti sans preneur"))
    for geste in (b.prendre_flotte, b.battre_flotte, b.accuser_arret):
        with pytest.raises(RuntimeError, match="OTO_FLEET_HOLDER"):
            geste(7)


# ── ② la reprise par le même preneur ─────────────────────────────────────────

class _RepriseParLeMemePreneur(FauxBackend):
    """Relancé : `launch` refusé (déjà `running`), `take` ACCEPTÉ — le backend
    reconnaît le preneur qui la tient."""

    def armer_flotte(self, fleet_id):
        raise _refus(409, "not_launchable", "ce passage est `running`")

    def lire_flotte(self, fleet_id):
        pytest.fail("la reprise ne se prouve plus par l'état lu : le backend tranche")


def test_la_reprise_par_le_meme_preneur_repart():
    b = _RepriseParLeMemePreneur([1, 1, 0])
    bilan = _run(_spec(fleet_id=7), b)
    assert b.prises == [7]
    assert b.enfiles >= 1, "la campagne reprise enfile normalement"
    assert bilan.arret.startswith("volume atteint")


# ── ③ `held_by_other` à la prise : abandon définitif ────────────────────────

class _TenueParUnAutre(_RepriseParLeMemePreneur):
    def prendre_flotte(self, fleet_id):
        raise _refus(409, "held_by_other",
                     "cette campagne tourne et un AUTRE ordonnanceur la tient")


def test_held_by_other_ABANDONNE_sans_rien_enfiler():
    b = _TenueParUnAutre([1, 1, 0])
    with pytest.raises(AbandonDefinitif) as e:
        _run(_spec(fleet_id=7), b)
    assert b.enfiles == 0, "partir quand même doublerait ses exécutions"
    assert "un autre ordonnanceur tient cette campagne" in str(e.value)


def test_held_by_other_sort_en_3_sans_relance(monkeypatch):
    with pytest.raises(SystemExit) as e:
        _main(monkeypatch, ["x.yaml", "#7"], backend=_TenueParUnAutre([1, 1, 0]))
    assert e.value.code == SORTIE_ABANDON_DEFINITIF


# ── ④ `not_the_holder` en cours de passage : on cesse de conduire ────────────

class _PlusTenueAuBattement(FauxBackend):
    """Le troisième battement apprend que la campagne est tenue par un autre
    (ou réarmée) — APRÈS que des travaux sont partis."""

    def battre_flotte(self, fleet_id):
        self.battements = getattr(self, "battements", 0) + 1
        if self.battements >= 3:
            raise _refus(409, "not_the_holder", "tu ne tiens pas cette campagne")
        return False


def test_not_the_holder_au_battement_ARRETE_le_passage():
    b = _PlusTenueAuBattement(counts=[100, 100], duree=50)
    bilan = _run(_spec(fleet_id=7, volume=None, concurrency=3), b)
    assert bilan.arret.startswith(F._PLUS_TENUE)
    assert b.battements == 3, "plus un tour de boucle après le refus"
    assert b.enfiles == 2, "rien n'est enfilé après le refus"
    assert bilan.etat_muet == 0, "le serveur a RÉPONDU : ce n'est pas un état muet"
    assert not getattr(b, "accuses", []), "on n'accuse pas l'arrêt d'une campagne d'autrui"


class _PlusTenueALAccuse(FauxBackend):
    stop_demande = True

    def accuser_arret(self, fleet_id, raison=None):
        raise _refus(409, "not_the_holder", "tu ne tiens pas cette campagne")


def test_not_the_holder_a_l_accuse_n_est_pas_une_fin_normale():
    bilan = _run(_spec(fleet_id=7, volume=None), _PlusTenueALAccuse(counts=[100, 100]))
    assert bilan.arret.startswith(F._PLUS_TENUE), (
        "« arrêt demandé » sortirait en 0 : l'arrêt n'a pas été accusé par nous")
    assert bilan.etat_muet == 0


def test_une_campagne_plus_tenue_sort_sans_relance(monkeypatch):
    with pytest.raises(SystemExit) as e:
        _main(monkeypatch, ["x.yaml", "#7"],
              run=lambda s, b: F.FleetBilan(
                  arret=f"{F._PLUS_TENUE} — 409 : tu ne tiens pas cette campagne"))
    assert e.value.code == SORTIE_ABANDON_DEFINITIF, (
        "relancé, il buterait sur `held_by_other` ou reprendrait une campagne libérée")


# ── ⑤ pas de preneur, pas d'ordonnanceur ─────────────────────────────────────

@pytest.mark.parametrize("preneur", [None, "", "   "])
def test_fleet_refuse_de_demarrer_sans_OTO_FLEET_HOLDER(monkeypatch, preneur):
    monkeypatch.setattr(F, "run_fleet",
                        lambda *a, **k: pytest.fail("parti sans preneur"))
    with pytest.raises(SystemExit) as e:
        _main(monkeypatch, ["x.yaml", "#7"], preneur=preneur,
              backend=None)
    assert e.value.code == SORTIE_ABANDON_DEFINITIF, (
        "aucune relance ne fera apparaître la variable")


def test_fleet_passe_le_preneur_au_client(monkeypatch):
    clients = _main(monkeypatch, ["x.yaml", "#7"], backend=object(),
                    run=lambda s, b: F.FleetBilan(arret="file vide"))
    assert clients == [{"preneur": _PRENEUR}]


def test_declarer_n_exige_pas_de_preneur(monkeypatch, capsys):
    _main(monkeypatch, ["--declarer", "x.yaml"], preneur=None,
          backend=FauxBackend(counts=[1]),
          run=lambda *a, **k: pytest.fail("--declarer ne conduit pas la campagne"))
    assert capsys.readouterr().out.strip() == "42"


# ── ⑥ l'unité pose le preneur, aligné sur son nom ────────────────────────────

def test_l_unite_pose_un_preneur_aligne_sur_le_nom_qu_elle_porte():
    unite = _unite_de_l_ordonnanceur()
    nom = re.match(r'systemd-run --unit="(\$\w+)"', unite).group(1)
    m = re.search(r'--setenv=OTO_FLEET_HOLDER="\$\(hostname\)/(\$\w+)"', unite)
    assert m, "l'unité ne pose pas le preneur : l'ordonnanceur refuserait de démarrer"
    assert m.group(1) == nom, (
        "le preneur doit suivre le nom RÉELLEMENT posé : sinon deux unités "
        "partageraient un preneur, ou une relance en changerait")
