"""Le BILAN d'une flotte — le pilotage se PROUVE, il ne se raconte pas.

Ce que le banc fige : « aboutie » se mesure au TABLEAU (départ − restantes) et
jamais au nombre de jobs conclus ; les faux départs se comptent même quand le
connecteur MCP préfixe les noms d'outils ; le coût par ligne aboutie vaut null
quand rien n'a abouti (jamais une division qui invente un chiffre) ; le JSON se
pose à côté de la déclaration et se réécrit ; et le bilan de FIN tombe quelle
que soit la borne — panne et interruption comprises, parce que c'est
précisément ces jours-là qu'on le lit.
"""
from __future__ import annotations

import json
import logging

import pytest

from oto_runner.bilan import ecrire_bilan
from oto_runner.fleet import run_fleet
from tests.test_fleet import FauxBackend, Horloge, _spec


class BackendBilan:
    """Le backend vu du bilan : un compte de lignes, un journal d'appels."""

    def __init__(self, restantes=0, journal=(0, 0)):
        self.restantes = restantes
        self.journal = journal
        self.sondes: list = []

    def count_rows(self, namespace, filter=None, org=None):
        return self.restantes

    def tool_health(self, org, tool, *, minutes=15, limit=20):
        self.sondes.append((tool, minutes, limit))
        return self.journal


def _job(statut="done", jetons=1000, **outils):
    """Un job conclu et son résultat DÉCLARÉ (les outils sont des kwargs :
    `_job(data_write=1)`, ou préfixés par le connecteur)."""
    return {"status": statut,
            "result": {"usage_tokens": jetons, "tool_counts": dict(outils)}}


def test_les_abouties_se_lisent_au_tableau():
    """Une ligne aboutie = une ligne qui ne correspond PLUS au filtre de
    réservation. Compter les jobs « done » comptait les tours perdus."""
    b = BackendBilan(restantes=18)
    bilan = ecrire_bilan(_spec(), b, {1: _job(), 2: _job()},
                         lignes_initiales=30, secondes=120)
    assert bilan["lignes"] == {"depart": 30, "restantes": 18, "abouties": 12}
    assert bilan["jobs"]["termines"] == 2, "2 jobs conclus, 12 lignes abouties"


def test_les_faux_departs_se_comptent_sur_des_noms_prefixes():
    """⚠️ Le connecteur MCP préfixe les noms d'outils : une égalité stricte
    compte zéro écriture sur une campagne qui en fait des milliers. Faux
    départ = la ligne a été réservée, rien n'a été écrit."""
    jobs = {1: _job(**{"mistral_data_claim_next": 1, "mistral_fr_get": 2}),
            2: _job(**{"data_claim_next": 1, "data_write": 1}),
            3: _job(**{"unconnecteur_data_claim_next": 2,
                       "unconnecteur_data_write": 0})}
    bilan = ecrire_bilan(_spec(), BackendBilan(restantes=9), jobs,
                         lignes_initiales=10, secondes=60)
    assert bilan["jobs"]["faux_departs"] == 2
    assert bilan["ecritures"] == {"claims": 4, "writes": 1}


def test_le_cout_par_aboutie_vaut_null_quand_rien_nabouti():
    """Zéro aboutie : le coût par ligne n'existe pas — null, jamais zéro ni
    une division. Le total, lui, reste dû (il a été payé)."""
    b = BackendBilan(restantes=30)
    bilan = ecrire_bilan(_spec(), b, {1: _job(jetons=4000), 2: _job(jetons=2000)},
                         lignes_initiales=30, secondes=60)
    assert bilan["lignes"]["abouties"] == 0
    assert bilan["jetons"] == {"total": 6000, "par_job": 3000, "par_aboutie": None}


def test_les_jobs_declares_portent_leurs_propres_comptes():
    """Quand le worker déclare `claims`/`writes`, ce sont EUX qui font foi —
    `tool_counts` reste la dérivation quand il ne les déclare pas."""
    jobs = {1: {"status": "done",
                "result": {"usage_tokens": 500, "claims": 3, "writes": 0}}}
    bilan = ecrire_bilan(_spec(), BackendBilan(restantes=1), jobs,
                         lignes_initiales=1, secondes=60)
    assert bilan["ecritures"] == {"claims": 3, "writes": 0}
    assert bilan["jobs"]["faux_departs"] == 1


def test_le_faux_depart_declare_par_le_worker_prime():
    """Le worker est le seul à avoir VU les appels : quand il déclare
    `faux_depart`, c'est lui qui tranche. Un job en échec ne le porte pas — il
    se juge alors sur l'asymétrie réservation / écriture."""
    jobs = {1: {"status": "done",
                "result": {"usage_tokens": 100, "claims": 1, "writes": 0,
                           "faux_depart": False}},
            2: {"status": "failed",
                "result": {"usage_tokens": 50,
                           "tool_counts": {"unconnecteur_data_claim_next": 1}}}}
    bilan = ecrire_bilan(_spec(), BackendBilan(restantes=1), jobs,
                         lignes_initiales=2, secondes=60)
    assert bilan["jobs"] == {"termines": 1, "echoues": 1, "faux_departs": 1}


def test_un_job_non_conclu_leve():
    """Compter un job en vol, ce serait compter du vide : on lève."""
    with pytest.raises(ValueError, match="n'est pas conclu"):
        ecrire_bilan(_spec(), BackendBilan(), {7: {"status": "claimed"}},
                     lignes_initiales=1, secondes=1)


def test_les_refus_decriture_se_lisent_au_journal_des_appels():
    """Une écriture refusée (RBAC, quota, schéma) ne fait pas échouer le job :
    l'agent conclut « done » sans une ligne écrite. La mesure vient du journal
    des appels de l'org, sur la fenêtre écoulée depuis le lancement."""
    b = BackendBilan(restantes=0, journal=(14, 2))
    bilan = ecrire_bilan(_spec(org=226), b, {}, lignes_initiales=3, secondes=3600)
    assert bilan["refus_ecriture"] == {"outil": "data_write", "fenetre_minutes": 60,
                                       "limite": 200, "appels": 14, "refuses": 2}
    assert bilan["refus_ecriture_omis"] is None
    assert b.sondes == [("data_write", 60, 200)]


def test_sans_org_le_poste_des_refus_est_omis_explicitement():
    """Un poste absent dit POURQUOI il l'est — il ne se confond jamais avec un
    zéro refus."""
    bilan = ecrire_bilan(_spec(), BackendBilan(restantes=0), {},
                         lignes_initiales=1, secondes=60)
    assert bilan["refus_ecriture"] is None
    assert "sans org" in bilan["refus_ecriture_omis"]


def test_un_backend_muet_ne_fabrique_pas_de_chiffre():
    """Le bilan de fin tombe souvent APRÈS une panne : un compte illisible
    devient null assumé, jamais un chiffre inventé, et rien ne remonte."""
    class Muet(BackendBilan):
        def count_rows(self, namespace, filter=None, org=None):
            raise RuntimeError("backend mort")

    bilan = ecrire_bilan(_spec(), Muet(), {1: _job()}, lignes_initiales=30,
                         secondes=60, arret="backend indisponible")
    assert bilan["lignes"]["restantes"] is None
    assert bilan["lignes"]["abouties"] is None
    assert bilan["jetons"]["par_aboutie"] is None


def test_la_ligne_de_journal_se_lit_dun_coup_doeil(caplog):
    """Des effectifs bruts AVEC leur dénominateur — un pourcentage cacherait
    qu'il porte sur trois lignes."""
    caplog.set_level(logging.INFO, logger="oto_runner.bilan")
    jobs = {i: _job(jetons=150_000, **{"data_claim_next": 1, "data_write": 1})
            for i in range(1, 13)}
    jobs[13] = _job(jetons=0, **{"data_claim_next": 1})   # un faux départ
    jobs[14] = _job(jetons=0, **{"data_claim_next": 1})   # un autre
    b = BackendBilan(restantes=18, journal=(14, 0))
    # Le bilan porte le NOM de la flotte (le tag de ses jobs), pas le nom du
    # tableau qu'elle draine — deux flottes peuvent drainer la même file.
    ecrire_bilan(_spec(name="prospects-demo", namespace="lignes-demo", org=226),
                 b, jobs, lignes_initiales=30, secondes=3600)
    assert ("bilan flotte prospects-demo : abouties 12/30 · faux départs 2 · "
            "1,8 M jetons · 150,0 k/aboutie · data_write 14 appels, 0 refusé"
            ) in caplog.text


def test_le_json_se_pose_a_cote_de_la_declaration_et_se_reecrit(tmp_path):
    """`flotte.yaml` → `flotte.bilan.json`, réécrit à chaque bilan : le pilote
    lit toujours l'état du moment, pas une archive."""
    decl = tmp_path / "flotte.yaml"
    decl.write_text("procedure: p\n")
    spec = _spec(source=str(decl))
    b = BackendBilan(restantes=18)

    ecrire_bilan(spec, b, {1: _job()}, lignes_initiales=30, secondes=60)
    pose = tmp_path / "flotte.bilan.json"
    intermediaire = json.loads(pose.read_text())
    assert set(intermediaire) == {"horodatage", "flotte", "namespace", "procedure",
                                  "final", "arret", "secondes", "lignes", "jobs",
                                  "jetons", "ecritures", "refus_ecriture",
                                  "refus_ecriture_omis"}, "la forme du bilan est un contrat"
    assert intermediaire["final"] is False and intermediaire["arret"] is None
    assert intermediaire["lignes"]["abouties"] == 12

    b.restantes = 0
    ecrire_bilan(spec, b, {1: _job(), 2: _job()}, lignes_initiales=30,
                 secondes=120, arret="file vide")
    final = json.loads(pose.read_text())
    assert final["final"] is True and final["arret"] == "file vide"
    assert final["lignes"]["abouties"] == 30
    assert list(tmp_path.glob("*.tmp")) == [], "réécriture atomique, sans résidu"


@pytest.mark.parametrize("scenario,attendu", [
    (dict(counts=[2, 2, 0, 0]), "file vide"),
    (dict(counts=[100, 100], statut="failed"), "échecs consécutifs"),
])
def test_le_bilan_de_fin_tombe_quelle_que_soit_la_borne(tmp_path, scenario, attendu):
    """Normale ou ANORMALE, la borne rend son bilan : un pilotage qui n'existe
    que sur une sortie propre n'existe pas les jours où il sert."""
    decl = tmp_path / "flotte.yaml"
    decl.write_text("procedure: p\n")
    h = Horloge()
    bilan = run_fleet(_spec(ramp_seconds=0, source=str(decl)),
                      FauxBackend(**scenario), sleep=h.sleep, clock=h.clock,
                      poll_s=20)
    assert attendu in bilan.arret
    pose = json.loads((tmp_path / "flotte.bilan.json").read_text())
    assert pose["final"] is True and attendu in pose["arret"]


def test_une_flotte_interrompue_rend_quand_meme_son_bilan(tmp_path):
    """Une panne qui n'est pas une BackendError traverse le driver : le bilan
    tombe AVANT que la pile remonte, et dit « interrompu »."""
    decl = tmp_path / "flotte.yaml"
    decl.write_text("procedure: p\n")

    class BackendQuiExplose(FauxBackend):
        def count_rows(self, namespace, filter=None, org=None):
            if self.counts:
                return self.counts.pop(0)
            raise RuntimeError("panne hors contrat")

    h = Horloge()
    with pytest.raises(RuntimeError, match="panne hors contrat"):
        run_fleet(_spec(ramp_seconds=0, source=str(decl)),
                  BackendQuiExplose(counts=[100]), sleep=h.sleep, clock=h.clock,
                  poll_s=20)
    pose = json.loads((tmp_path / "flotte.bilan.json").read_text())
    assert pose["arret"] == "interrompu" and pose["lignes"]["restantes"] is None


def test_le_bilan_intermediaire_tombe_a_la_cadence_declaree(monkeypatch):
    """Une campagne se pilote PENDANT qu'elle tourne : le bilan retombe toutes
    les `bilan_periode_s`, pas seulement à la fin."""
    from oto_runner import fleet as F

    appels: list = []
    monkeypatch.setattr(F, "ecrire_bilan",
                        lambda *a, **kw: appels.append(kw.get("arret", "")))
    h = Horloge()
    run_fleet(_spec(ramp_seconds=0, bilan_periode_s=20), FauxBackend(
        counts=[10, 10, 10, 10, 0, 0]), sleep=h.sleep, clock=h.clock, poll_s=20)
    assert [a for a in appels if not a], "aucun bilan intermédiaire"
    assert appels[-1] == "file vide", "le dernier bilan est celui de la fin"


def test_une_flotte_sans_declaration_sur_disque_ne_pose_rien(tmp_path):
    """Une flotte construite en mémoire n'a pas de déclaration : elle
    journalise son bilan et ne pose aucun fichier."""
    ecrire_bilan(_spec(), BackendBilan(restantes=0), {}, lignes_initiales=1,
                 secondes=60)
    assert list(tmp_path.iterdir()) == []
