"""Le bilan dit l'ISSUE des lignes, pas leur sortie de la file — et lit les refus ENTIERS.

`banc-v151-medium`, 06/09/2026 : « abouties 3/3 », alors que deux lignes sur
trois avaient fini en `echec` — l'état d'ABANDON du cycle de vie, posé par la
plateforme après trois réservations sans écriture. Et les sept refus d'écriture
étaient coupés à soixante caractères, juste avant la colonne et la raison.

Ce que ces bancs figent : la colonne de statut se LIT au schéma (`role="status"`),
jamais à son nom ; la ventilation compte juste sur un jeu fictif ; « abouties »
ne compte que les états terminaux hors abandon, et vaut null AVEC sa raison
quand on ne peut pas le dire ; les motifs de refus portent le texte complet et
mènent au travail, donc au journal JSONL que le worker a écrit pour lui.
"""
from __future__ import annotations

import logging

import pytest

from oto_runner import journal
from oto_runner.bilan import ecrire_bilan
from tests.test_bilan import BackendBilan, _job
from tests.test_fleet import FauxBackend, _run, _spec

SCHEMA = {"fields": [
    {"key": "siren", "role": "key"},
    {"key": "statut", "role": "status",
     "lifecycle": {"states": ["a_enrichir", "enrichi", "echec", "ecarte"],
                   "terminal": ["enrichi", "echec", "ecarte"],
                   "max_claims": 3, "abandon_state": "echec"}},
]}


class BackendStatuts(BackendBilan):
    """Le tableau vu du bilan : un schéma, et l'agrégat que le serveur rend."""

    def __init__(self, groupes, schema=SCHEMA, **kw):
        super().__init__(**kw)
        self.groupes, self._schema, self.agregats = groupes, schema, []

    def schema(self, namespace, org=None):
        return self._schema

    def aggregate(self, namespace, group_by, filter=None, org=None):
        self.agregats.append((group_by, filter))
        return self.groupes


def _spec_lot(**kw):
    base = dict(filter={"statut": "a_enrichir", "lot_test": "banc"}, org=2)
    base.update(kw)
    return _spec(**base)


def _trois_jobs():
    return {1: _job(), 2: _job(), 3: _job()}


def test_la_ventilation_par_statut_compte_juste_sur_un_jeu_fictif():
    """Le cas réel, rejoué : 3 lignes sorties, 1 enrichie, 2 abandonnées."""
    b = BackendStatuts([{"statut": "enrichi", "count": 1},
                        {"statut": "echec", "count": 2}], restantes=0)
    bilan = ecrire_bilan(_spec_lot(), b, _trois_jobs(), lignes_initiales=3, secondes=60)
    lignes = bilan["lignes"]
    assert lignes["sorties"] == 3, "trois lignes ne correspondent plus au filtre"
    assert lignes["par_statut"] == {"enrichi": 1, "echec": 2}
    assert lignes["abouties"] == 1 and lignes["abouties_omis"] is None
    assert lignes["statut"] == {"colonne": "statut", "perimetre": {"lot_test": "banc"},
                                "abandon": "echec",
                                "terminaux": ["enrichi", "echec", "ecarte"]}
    assert b.agregats == [("statut", {"lot_test": "banc"})], (
        "le périmètre est le filtre SANS sa clause de statut")
    assert bilan["jetons"] == {"total": 3000, "par_job": 1000,
                               "par_sortie": 1000, "par_aboutie": 3000}


def test_la_colonne_de_statut_se_lit_au_schema_pas_a_son_nom():
    schema = {"fields": [{"key": "etat", "role": "status",
                          "lifecycle": {"states": ["a_faire", "fait"],
                                        "transitions": {"a_faire": ["fait"]}}}]}
    b = BackendStatuts([{"etat": "fait", "count": 2}], schema=schema, restantes=0)
    bilan = ecrire_bilan(_spec(filter={"etat": "a_faire", "lot": "x"}, org=2), b,
                         {1: _job(), 2: _job()}, lignes_initiales=2, secondes=60)
    assert b.agregats == [("etat", {"lot": "x"})]
    assert bilan["lignes"]["par_statut"] == {"fait": 2}
    assert bilan["lignes"]["statut"]["terminaux"] == ["fait"], (
        "sans `terminal` explicite : les états sans transition sortante")
    assert bilan["lignes"]["abouties"] == 2


def test_une_valeur_en_couches_est_deballee():
    b = BackendStatuts([{"statut": {"valeur": "enrichi", "comment": "posé"}, "count": 1}],
                       restantes=0)
    bilan = ecrire_bilan(_spec_lot(), b, {1: _job()}, lignes_initiales=1, secondes=60)
    assert bilan["lignes"]["par_statut"] == {"enrichi": 1}


def test_une_ligne_abandonnee_n_est_PAS_aboutie():
    """Zéro aboutie est un ZÉRO mesuré, pas une omission : les trois lignes sont
    sorties, toutes dans l'état d'abandon."""
    b = BackendStatuts([{"statut": "echec", "count": 3}], restantes=0)
    bilan = ecrire_bilan(_spec_lot(), b, _trois_jobs(), lignes_initiales=3, secondes=60)
    assert bilan["lignes"]["sorties"] == 3
    assert bilan["lignes"]["abouties"] == 0 and bilan["lignes"]["abouties_omis"] is None
    assert bilan["jetons"]["par_aboutie"] is None and bilan["jetons"]["par_sortie"] == 1000


@pytest.mark.parametrize("schema,raison", [
    ({"fields": [{"key": "statut"}]}, "role=status"),
    (None, "role=status"),
])
def test_sans_colonne_role_status_le_poste_est_omis_AVEC_sa_raison(schema, raison):
    b = BackendStatuts([], schema=schema, restantes=0)
    bilan = ecrire_bilan(_spec_lot(), b, _trois_jobs(), lignes_initiales=3, secondes=60)
    assert bilan["lignes"]["par_statut"] is None
    assert bilan["lignes"]["abouties"] is None and raison in bilan["lignes"]["abouties_omis"]
    assert raison in bilan["lignes"]["statut"]["omis"]
    assert b.agregats == [], "sans colonne, on n'agrège rien"


def test_un_filtre_qui_ne_borne_que_le_statut_ventile_tout_le_tableau_et_le_DIT():
    b = BackendStatuts([{"statut": "enrichi", "count": 2470}, {"statut": "echec", "count": 30}],
                       restantes=0)
    bilan = ecrire_bilan(_spec(filter={"statut": "a_enrichir"}, org=2), b,
                         _trois_jobs(), lignes_initiales=3, secondes=60)
    assert bilan["lignes"]["par_statut"] == {"enrichi": 2470, "echec": 30}
    assert bilan["lignes"]["abouties"] is None
    assert "tout le tableau" in bilan["lignes"]["abouties_omis"]


def test_plus_de_terminales_que_de_sorties_dit_que_le_perimetre_deborde():
    """Un lot rejoué : ses lignes étaient déjà terminales avant ce passage."""
    b = BackendStatuts([{"statut": "enrichi", "count": 5}], restantes=3)
    bilan = ecrire_bilan(_spec_lot(), b, {}, lignes_initiales=3, secondes=60)
    assert bilan["lignes"]["sorties"] == 0
    assert bilan["lignes"]["abouties"] is None
    assert "antérieures à ce passage" in bilan["lignes"]["abouties_omis"]


def test_la_ligne_de_journal_nomme_ce_qu_elle_compte(caplog):
    b = BackendStatuts([{"statut": "enrichi", "count": 1},
                        {"statut": "echec", "count": 2}], restantes=0)
    with caplog.at_level(logging.INFO):
        ecrire_bilan(_spec_lot(), b, _trois_jobs(), lignes_initiales=3, secondes=60)
    ligne = next(r.getMessage() for r in caplog.records if "bilan flotte" in r.getMessage())
    assert "sorties 3/3" in ligne
    assert "statut final : echec 2 · enrichi 1" in ligne
    assert "abouties 1" in ligne and "abouties 3" not in ligne


# ── Les refus, ENTIERS, et le chemin vers le travail ─────────────────────────

LONG = ("Error calling tool 'data_write': écriture refusée par le schéma : "
        "`qualification_piece` = `cessation_registre` n'est pas une option déclarée "
        "(options : radiation, liquidation, dissolution, depot_comptes) — "
        + "et la raison continue " * 20).strip()


class BackendRefus(BackendStatuts):
    def tool_health(self, org, tool, *, minutes=15, limit=20):
        return (8, 7)

    def refus_detail(self, org, tool, *, minutes=15, limit=200):
        return [{"quand": "2026-09-06 01:23:10", "run_id": "r-12648", "erreur": LONG},
                {"quand": "2026-09-06 01:24:02", "run_id": "r-ailleurs",
                 "erreur": "row `0000` introuvable"}]


def test_les_motifs_sont_les_textes_serveur_ENTIERS_groupes_par_texte_identique():
    """⚠️ Plus aucun libellé de notre cru : « création refusée par le cran ×1 »
    sur onze refus qui étaient tous des mises à jour, « ligne inconnue » pour un
    NAMESPACE introuvable — le classificateur inventait, et il a été cru."""
    b = BackendRefus([{"statut": "echec", "count": 1}], restantes=0)
    jobs = {12648: {**_job(), "run_id": "r-12648",
                    "journal": "passages/flotte-demo/12648.jsonl"}}
    bilan = ecrire_bilan(_spec_lot(), b, jobs, lignes_initiales=1, secondes=240)
    refus = bilan["refus_ecriture"]
    assert refus["appels"] == 8 and refus["refuses"] == 7
    assert len(LONG) > 60
    assert refus["motifs"] == {LONG: 1, "row `0000` introuvable": 1}
    for motif in refus["motifs"]:
        assert not motif.startswith("autre"), "aucun libellé qui ne soit le texte serveur"
    premier, second = refus["detail"]
    assert premier["erreur"] == LONG, "le texte complet, pas un préfixe"
    assert premier["job"] == 12648 and premier["run_id"] == "r-12648"
    assert premier["journal"] == "passages/flotte-demo/12648.jsonl", (
        "le refus mène au journal JSONL que l'ordonnanceur a RELU pour ce travail")
    assert "motif" not in premier, "le détail ne porte aucune interprétation"
    assert second["job"] is None and second["journal"] is None, (
        "un run d'une autre flotte, ou non conclu : dit, pas inventé")


def test_un_travail_conclu_dont_le_journal_n_a_pas_ete_relu_ne_pointe_nulle_part():
    b = BackendRefus([{"statut": "echec", "count": 1}], restantes=0)
    jobs = {12648: {**_job(), "run_id": "r-12648", "journal": None}}
    bilan = ecrire_bilan(_spec_lot(), b, jobs, lignes_initiales=1, secondes=240)
    premier = bilan["refus_ecriture"]["detail"][0]
    assert premier["job"] == 12648 and premier["journal"] is None


def test_le_journal_de_flotte_reste_compact_mais_pointe_vers_le_detail(caplog, tmp_path):
    decl = tmp_path / "flotte.yaml"
    decl.write_text("procedure: p\n")
    b = BackendRefus([{"statut": "echec", "count": 1}], restantes=0)
    jobs = {12648: {**_job(), "run_id": "r-12648",
                    "journal": "passages/flotte-demo/12648.jsonl"}}
    with caplog.at_level(logging.INFO):
        ecrire_bilan(_spec_lot(source=str(decl)), b, jobs, lignes_initiales=1,
                     secondes=240, arret="file vide")
    dits = [r.getMessage() for r in caplog.records]
    ligne = next(d for d in dits if d.startswith("bilan flotte"))
    assert LONG not in ligne and "…" in ligne, "la ligne abrège"
    assert f"détail complet : {tmp_path / 'flotte.bilan.json'}" in ligne
    refus = next(d for d in dits if d.startswith("refus data_write"))
    assert LONG in refus, "au bilan de FIN, chaque refus est dit ENTIER sur sa ligne"
    assert "job 12648" in refus and "flotte-demo/12648.jsonl" in refus


def test_pendant_la_flotte_les_refus_ne_sont_pas_deroules_ligne_a_ligne(caplog):
    b = BackendRefus([{"statut": "echec", "count": 1}], restantes=0)
    with caplog.at_level(logging.INFO):
        ecrire_bilan(_spec_lot(), b, {}, lignes_initiales=1, secondes=240)
    assert not [r for r in caplog.records if r.getMessage().startswith("refus data_write")]


# ── L'ordonnanceur : le run de chaque travail, et le chemin de son journal ────

class _BackendAvecRun(FauxBackend):
    def get_job(self, jid):
        return {**super().get_job(jid), "run_id": f"r-{jid}"}


def _flotte_avec_bilan_capture(monkeypatch, backend):
    from oto_runner import fleet as F
    vus: dict = {}
    monkeypatch.setattr(F, "ecrire_bilan",
                        lambda spec, bk, conclus, **kw: vus.update(conclus=dict(conclus)))
    return _run(_spec(ramp_seconds=0), backend), vus


def test_l_ordonnanceur_n_annonce_un_journal_qu_APRES_l_avoir_relu(monkeypatch, caplog):
    """LE fait du 06/09 : « journal complet : passages/…/12670.jsonl » écrit pour
    un fichier qui n'existait nulle part. Le journal du travail 1 existe ici ;
    celui du 2 n'existe pas : le premier est annoncé APRÈS relecture, avec son
    compte d'événements ; le second est dit ABSENT, en erreur, et compté."""
    journal.Journal(journal.chemin("flotte-demo", 1)).ecrire("fin", stopped="end_turn")
    with caplog.at_level(logging.INFO):
        bilan, vus = _flotte_avec_bilan_capture(monkeypatch,
                                                _BackendAvecRun(counts=[10, 10, 10, 0, 0]))
    assert len(vus["conclus"]) >= 2, "plusieurs travaux ont conclu : un avec journal, les autres sans"
    assert all(j["run_id"] == f"r-{jid}" for jid, j in vus["conclus"].items())
    dits = [r.getMessage() for r in caplog.records]
    relu = next(d for d in dits if d.startswith("job 1 conclu"))
    assert "journal complet : " in relu and "1.jsonl (1 événement, dernier : fin)" in relu
    absent = next(d for d in dits if d.startswith("job 2 conclu"))
    assert "SANS JOURNAL RELU" in absent and "2.jsonl" in absent
    assert "journal complet" not in absent, "jamais annoncé sans relecture"
    assert vus["conclus"][1]["journal"] == journal.chemin("flotte-demo", 1)
    assert vus["conclus"][2]["journal"] is None
    assert bilan.journaux_absents == len(vus["conclus"]) - 1
    erreurs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("SANS JOURNAL RELU depuis cet ordonnanceur" in e for e in erreurs), (
        "la conclusion se lit là où on lit le résultat")


def test_quand_tous_les_journaux_sont_relus_l_ordonnanceur_ne_crie_pas(monkeypatch, caplog):
    for jid in range(1, 8):
        journal.Journal(journal.chemin("flotte-demo", jid)).ecrire("fin")
    with caplog.at_level(logging.INFO):
        bilan, _ = _flotte_avec_bilan_capture(monkeypatch,
                                              _BackendAvecRun(counts=[10, 10, 10, 0, 0]))
    assert bilan.journaux_absents == 0
    assert not [r for r in caplog.records if "SANS JOURNAL" in r.getMessage()]


# ── Le bilan de FIN sur des lignes en couches (`layers=nested`) ───────────────

class _BackendFinal(BackendStatuts):
    """Ce que le bilan de fin lit en plus : les lignes sorties et les fiches, dans
    la forme `nested` — une cellule est `{"valeur": …, "comment": …}`."""

    def rows(self, namespace, filter=None, org=None, limit=200):
        statut = (filter or {}).get("statut")
        if statut == "echec":
            return [{"_id": "r-2", "siren": {"valeur": "222", "comment": "c"},
                     "modele": {"valeur": "m-1"}}]
        return [{"_id": "r-1", "siren": {"valeur": "111"}, "modele": {"valeur": "m-1"},
                 "qualification": {"valeur": "en_activite", "comment": "x"}}]

    def patch_row(self, namespace, row_id, valeurs, org=None):
        return {}


def test_le_bilan_de_fin_sur_des_cellules_en_couches_ne_plante_pas_et_distingue_ses_postes():
    """⚠️ Le plantage du 06/09 (`int / dict`) : le poste « lignes sorties »
    annotées (un dict) écrasait le compte des sorties (un entier) au bilan de
    FIN — celui qu'aucun bilan intermédiaire n'exerce. Aucun `.bilan.json` n'a
    été écrit pour cette flotte."""
    b = _BackendFinal([{"statut": {"valeur": "enrichi"}, "count": 1},
                       {"statut": {"valeur": "echec"}, "count": 2}], restantes=0)
    jobs = {1: {**_job(), "result": {"usage_tokens": 3000, "model": "m-1"}}}
    bilan = ecrire_bilan(_spec_lot(), b, jobs, lignes_initiales=3, secondes=60,
                         arret="file vide")
    assert bilan["final"] is True
    assert bilan["lignes"]["sorties"] == 3, "un entier : les lignes hors filtre"
    assert bilan["lignes_sorties"] == {"sorties": 1, "annotees": 0}, (
        "un dict, à part : les lignes ABANDONNÉES relues, et celles qu'on a annotées")
    assert bilan["lignes"]["par_statut"] == {"enrichi": 1, "echec": 2}
    assert bilan["lignes"]["abouties"] == 1
    assert bilan["jetons"]["par_sortie"] == 1000 and bilan["jetons"]["par_aboutie"] == 3000
    assert bilan["controles"]["fiches"] == 1
