"""L'ordonnanceur se RELANCE après une panne passagère, et REPREND sa campagne.

Décision du 21/09/2026 : les ordonnanceurs doivent être autonomes. Jusque-là
l'unité que pose `scripts/flotte.sh` ne déclarait aucun `Restart=` — le `exit 1`
« pour que systemd relance » ne relançait rien.

Ce que ces tests figent :

1. **L'unité relance une panne** (`Restart=on-failure`) et jamais un abandon
   définitif (`RestartPreventExitStatus` = `SORTIE_ABANDON_DEFINITIF`).
2. **La relance attend** que les travaux en vol de la vie précédente aient fini
   (`RestartSec` ≥ le bail d'une ligne, 10 min).
3. **La limite de relances MORD** : un intervalle plus court que les relances
   qu'il est censé compter ne limiterait rien.
4. **Une relance REPREND** : la campagne est déclarée hors de l'unité, et
   l'unité reçoit son identifiant — sans quoi chaque relance en ouvrirait une
   neuve.
5. **Un arrêt demandé est une fin normale** : sinon une unité qui relance
   redémarrerait la campagne qu'on vient d'arrêter.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from oto_runner import fleet as F
from oto_runner.fleet import SORTIE_ABANDON_DEFINITIF
from tests.test_armement_fatal import _spec
from tests.test_fleet import FauxBackend

_SH = (Path(__file__).resolve().parents[1] / "scripts" / "flotte.sh").read_text()
_UNITES = {"s": 1, "min": 60, "h": 3600}


def _unite_de_l_ordonnanceur() -> str:
    """La commande `systemd-run` de l'unité de l'ordonnanceur, continuations jointes."""
    m = re.search(r'systemd-run --unit="\$FLOTTE"(?:[^\n]*\\\n)*[^\n]*', _SH)
    assert m, "flotte.sh ne pose plus l'unité de l'ordonnanceur"
    return m.group(0).replace("\\\n", " ")


def _propriete(nom: str) -> str:
    m = re.search(rf"--property={nom}=(\S+)", _unite_de_l_ordonnanceur())
    assert m, f"`{nom}` absent de l'unité de l'ordonnanceur"
    return m.group(1)


def _secondes(valeur: str) -> int:
    m = re.fullmatch(r"(\d+)(s|min|h)?", valeur)
    assert m, f"durée non reconnue par ce test : {valeur!r}"
    return int(m.group(1)) * _UNITES[m.group(2) or "s"]


def test_l_unite_relance_une_panne_et_jamais_un_abandon_definitif():
    assert _propriete("Restart") == "on-failure"
    assert _propriete("RestartPreventExitStatus") == str(SORTIE_ABANDON_DEFINITIF)


def test_la_relance_attend_la_fin_des_travaux_en_vol():
    assert _secondes(_propriete("RestartSec")) >= 10 * 60, (
        "l'ordonnanceur relancé ne suit plus les travaux de sa vie précédente : "
        "ré-enfiler avant la fin du bail doublerait la concurrence")


def test_la_limite_de_relances_mord():
    rafale = int(_propriete("StartLimitBurst"))
    intervalle = _secondes(_propriete("StartLimitIntervalSec"))
    assert 1 < rafale <= 10
    assert intervalle > (rafale - 1) * _secondes(_propriete("RestartSec")), (
        "un intervalle plus court que les relances qu'il compte ne limite rien")


def test_l_unite_reprend_la_campagne_declaree_hors_d_elle():
    assert "-m oto_runner.fleet --declarer" in _SH
    assert re.search(r'-m oto_runner\.fleet "\$yaml" "#\$_fid"',
                     _unite_de_l_ordonnanceur()), (
        "l'unité doit recevoir l'identifiant : une commande qui déclare ouvre "
        "une campagne neuve à chaque relance")


def _main(monkeypatch, argv, backend=None, run=None):
    monkeypatch.setattr(sys, "argv", ["fleet", *argv])
    monkeypatch.setenv("OTO_FLEET_HOLDER", "banc/oto-fleet-test")
    monkeypatch.setattr(F, "load_spec", lambda p: _spec())
    monkeypatch.setattr(F, "Backend", lambda **kw: backend)
    if run is not None:
        monkeypatch.setattr(F, "run_fleet", run)
    F.main()


def test_la_declaration_et_un_identifiant_REPRENNENT_la_campagne(monkeypatch):
    vues = []
    _main(monkeypatch, ["x.yaml", "#42"],
          run=lambda spec, b: vues.append(spec) or F.FleetBilan(arret="file vide"))
    assert vues[0].fleet_id == 42
    assert vues[0].name == "armement", "la déclaration LOCALE est gardée"


def test_declarer_rend_l_identifiant_sans_conduire_la_campagne(monkeypatch, capsys):
    b = FauxBackend(counts=[1])
    _main(monkeypatch, ["--declarer", "x.yaml"], backend=b,
          run=lambda *a, **k: pytest.fail("--declarer ne conduit pas la campagne"))
    assert capsys.readouterr().out.strip() == "42"
    assert len(b.declarations) == 1
    assert b.enfiles == 0


def test_un_arret_demande_est_une_fin_normale(monkeypatch):
    # Ne lève pas : exit 0, l'unité ne relance pas.
    _main(monkeypatch, ["x.yaml"],
          run=lambda spec, b: F.FleetBilan(arret="arrêt demandé"))
