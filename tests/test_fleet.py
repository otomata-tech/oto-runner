"""L'ordonnanceur de flotte — les BORNES se prouvent, elles ne s'affirment pas.

C'est la protection du compte pendant une campagne sans surveillance : budget
atteint → plus un job enfilé ; volume atteint → idem ; échecs consécutifs →
arrêt propre au lieu de payer pour re-crasher ; file vide → bilan. Horloge et
sommeil injectés : aucun temps réel ici.
"""
from __future__ import annotations

import pytest

from oto_runner.fleet import FleetSpec, load_spec, run_fleet


class FauxBackend:
    """La file vue du driver : un scénario scripté de counts + jobs qui
    concluent après `duree` tours de boucle, avec leur résultat déclaré."""

    def __init__(self, counts, duree=1, usage_par_job=1000, statut="done"):
        self.counts = list(counts)         # count_rows successifs (dernier répété)
        self.duree = duree
        self.usage = usage_par_job
        self.statut = statut
        self.jobs: dict[int, dict] = {}
        self.enfiles = 0
        self._age: dict[int, int] = {}

    def count_rows(self, namespace, filter=None, org=None):
        self.org_vu = org
        return self.counts.pop(0) if len(self.counts) > 1 else self.counts[0]

    def enqueue(self, kind, payload, run_id=None):
        self.enfiles += 1
        jid = self.enfiles
        self.jobs[jid] = {"status": "claimed"}
        self._age[jid] = 0
        return jid

    def get_job(self, jid):
        self._age[jid] += 1
        if self._age[jid] >= self.duree:
            # Le résultat est celui d'un worker À JOUR : il DÉCLARE son faux
            # départ, le driver ne le redevine pas (cf. le test dédié).
            self.jobs[jid] = {"status": self.statut,
                              "result": {"usage_tokens": self.usage,
                                         "faux_depart": False},
                              "last_error": "boom" if self.statut == "failed" else None}
        return self.jobs[jid]


class BackendRendement(FauxBackend):
    """Des jobs conclus dont on choisit le coût et la SORTIE (nombre
    d'écritures). Les noms d'outils sont préfixés par le connecteur, comme en
    vol : le driver ne doit reconnaître une écriture qu'au suffixe."""

    writes = 1

    def get_job(self, jid):
        j = super().get_job(jid)
        if j["status"] == "done":
            j["result"]["tool_counts"] = {"demo-connecteur_data_claim_next": 1,
                                          "demo-connecteur_data_write": self.writes}
        return j


class Horloge:
    def __init__(self):
        self.t = 0.0

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _spec(**kw):
    base = dict(procedure="p", namespace="ns", name="flotte-demo",
                tools=("data_claim_next",),
                filter={"statut": "a_traiter"}, concurrency=3, ramp_seconds=60)
    base.update(kw)
    return FleetSpec(**base)


def _run(spec, backend):
    h = Horloge()
    return run_fleet(spec, backend, sleep=h.sleep, clock=h.clock, poll_s=20)


def test_le_budget_arrete_lenfilement():
    """LA borne des vacances : le budget atteint, plus UN job ne part — les
    en-vol finissent, le bilan dit pourquoi."""
    b = FauxBackend(counts=[100, 100], usage_par_job=6000)
    bilan = _run(_spec(budget_tokens=10_000, ramp_seconds=0), b)
    assert bilan.arret.startswith("budget atteint")
    assert bilan.usage_tokens >= 10_000
    apres_borne = b.enfiles
    assert apres_borne <= 3, f"{apres_borne} jobs enfilés après la borne budget"


def test_le_volume_arrete_lenfilement():
    # 10 lignes au départ, 4 restantes dès le 2e count : 6 traitées ≥ volume 5.
    b = FauxBackend(counts=[10, 4, 4])
    bilan = _run(_spec(volume=5, ramp_seconds=0), b)
    assert bilan.arret.startswith("volume atteint")


def test_la_file_vide_rend_le_bilan():
    b = FauxBackend(counts=[2, 2, 0, 0])
    bilan = _run(_spec(ramp_seconds=0), b)
    assert bilan.arret == "file vide"
    assert bilan.done >= 1
    assert bilan.usage_tokens == bilan.done * 1000, \
        "le coût vient du résultat DÉCLARÉ des jobs, sommé"


def test_les_echecs_consecutifs_arretent_la_flotte():
    """3 crashs d'affilée : enfiler encore, c'est payer pour re-crasher."""
    b = FauxBackend(counts=[100, 100], statut="failed")
    bilan = _run(_spec(ramp_seconds=0), b)
    assert "échecs consécutifs" in bilan.arret
    assert bilan.failed == 3


def test_la_rampe_espace_les_departs():
    """Deux départs ne sont JAMAIS à moins de ramp_seconds l'un de l'autre —
    N claims simultanés sur un service, c'est la rafale de 502 mesurée."""
    departs = []

    class Espion(FauxBackend):
        def __init__(self, horloge):
            super().__init__(counts=[100, 100], duree=10_000)
            self.h = horloge

        def enqueue(self, kind, payload, run_id=None):
            departs.append(self.h.t)
            return super().enqueue(kind, payload, run_id)

    h = Horloge()
    b = Espion(h)
    # Volume 0 jamais atteignable ici : on borne par budget pour sortir.
    spec = _spec(ramp_seconds=60, budget_tokens=1)

    # Le budget n'est jamais consommé (les jobs ne concluent pas) : on coupe
    # après quelques tours en levant depuis sleep.
    class Stop(Exception): ...

    tours = {"n": 0}

    def sleep(s):
        h.sleep(s)
        tours["n"] += 1
        if tours["n"] > 12:
            raise Stop

    with pytest.raises(Stop):
        run_fleet(spec, b, sleep=sleep, clock=h.clock, poll_s=20)
    assert len(departs) >= 3, "la concurrence doit finir par se remplir"
    ecarts = [b - a for a, b in zip(departs, departs[1:])]
    assert all(e >= 60 for e in ecarts), f"départs trop rapprochés : {ecarts}"


def test_la_declaration_harnais_se_charge_telle_quelle(tmp_path):
    """Le YAML d'un autre harnais (champs en plus : model, connector…) se
    charge — les champs inconnus sont logués puis ignorés, jamais avalés en
    silence ni fatals. `volume: épuisement` = pas de plafond."""
    f = tmp_path / "flotte.yaml"
    f.write_text(
        "procedure: enrichissement-fiche\n"
        "procedure_org: 42\n"
        "namespace: prospects-demo\n"
        "filter: {statut: a_traiter}\n"
        "project: 42\n"
        "model: un-modele\n"
        "connector: 'abc-123'\n"
        "tools: [data_claim_next, data_write]\n"
        "concurrency: 3\nramp_seconds: 60\n"
        "volume: épuisement\nbudget_tokens: 320000000\n")
    spec = load_spec(str(f))
    assert spec.procedure == "enrichissement-fiche"
    assert spec.volume is None, "« épuisement » = pas de plafond de lignes"
    assert spec.budget_tokens == 320000000
    assert spec.max_steps == 40, "défaut runner (absent de la déclaration harnais)"


def test_le_payload_porte_le_contrat_du_run():
    from oto_runner.fleet import _payload
    p = _payload(_spec(project=220, max_steps=48))
    assert p["procedure"] == "p" and p["project_id"] == 220
    assert p["max_steps"] == 48 and "data_claim_next" in p["tools"]
    assert p["fleet"] == "flotte-demo", "chaque job porte le nom de sa flotte"


def test_lorg_de_la_declaration_scope_le_comptage():
    """Le namespace d'une flotte vit dans l'org de la MISSION, pas dans l'org
    maison du jeton — sans `org:`, le comptage rend namespace_not_found (vécu
    au premier vol de la re-validation)."""
    b = FauxBackend(counts=[2, 2, 0, 0])
    _run(_spec(ramp_seconds=0, org=226), b)
    assert b.org_vu == 226


def test_le_filtre_de_flotte_accepte_les_lots_nommes():
    """Une comparaison A/B se borne à un LOT NOMMÉ de lignes (sirens exacts) —
    la grammaire riche ({"in": [...]}, ou une liste en raccourci) traverse le
    driver jusqu'au comptage."""
    import json as _json

    from oto_runner.backend import Backend

    captures = {}

    class _B(Backend):
        def __init__(self):
            self.base = "http://x"
            self.token = "t"

        def _get(self, chemin, params, org=None):
            captures.update(params=params)
            return {"total": 3}

    b = _B()
    b.count_rows("ns", filter={"statut": "a_enrichir",
                               "siren": {"in": ["1", "2", "3"]}})
    clauses = _json.loads(captures["params"]["filters"])
    assert {"field": "siren", "op": "in", "value": ["1", "2", "3"]} in clauses
    assert {"field": "statut", "op": "eq", "value": "a_enrichir"} in clauses


def test_un_502_isole_du_driver_ne_tue_pas_la_flotte():
    """Un 502 sur l'ENFILEMENT a tué un vol entier (lot C, 16/08 — rafale
    #352, 30 min de flotte figée). Le driver tolère et retente au tour
    suivant ; le vol continue."""
    from oto_runner.backend import BackendError

    class BackendUn502(FauxBackend):
        rates = 0

        def enqueue(self, kind, payload, run_id=None):
            if self.rates < 2:
                self.rates += 1
                raise BackendError("/api → 502 : bad gateway", status=502)
            return super().enqueue(kind, payload, run_id)

    b = BackendUn502(counts=[2, 2, 2, 2, 0, 0])
    bilan = _run(_spec(ramp_seconds=0), b)
    assert bilan.arret == "file vide", "le vol a survécu aux 502 isolés"
    assert b.rates == 2 and bilan.done >= 1


def test_une_panne_dense_arrete_le_driver_proprement():
    """Backend durablement mort : arrêt PROPRE avec bilan — jamais un
    traceback qui laisse une flotte figée sans un mot."""
    from oto_runner.backend import BackendError

    class BackendMort(FauxBackend):
        def count_rows(self, namespace, filter=None, org=None):
            if self.counts:      # le count initial passe (le vol démarre)
                return self.counts.pop(0)
            raise BackendError("/api → 502", status=502)

    b = BackendMort(counts=[100])
    bilan = _run(_spec(ramp_seconds=0), b)
    assert "backend indisponible" in bilan.arret


def test_un_arret_anormal_sort_en_echec_pour_que_systemd_relance(monkeypatch):
    """Un 402 provider (crédit épuisé) a arrêté la flotte à 604/7921 fiches et
    26 h ont passé avant relance humaine : un arrêt ANORMAL sort en exit 1 —
    systemd (Restart=on-failure, RestartSec long) relance quand la panne passe.
    Une fin NORMALE (file vide, volume, budget) sort en 0 : pas de relance."""
    import sys

    import pytest as _pt

    from oto_runner import fleet as F

    def _main_avec(arret):
        monkeypatch.setattr(sys, "argv", ["fleet", "x.yaml"])
        monkeypatch.setattr(F, "load_spec", lambda p: _spec())
        monkeypatch.setattr(F, "Backend", lambda: None)
        monkeypatch.setattr(F, "run_fleet",
                            lambda spec, b: F.FleetBilan(arret=arret))
        return F.main

    with _pt.raises(SystemExit) as e:
        _main_avec("4 échecs consécutifs — enfiler encore, c'est payer")()
    assert e.value.code == 1
    _main_avec("file vide")()          # ne lève pas : fin normale
    _main_avec("budget atteint (…)")()


def test_une_erreur_reseau_du_backend_est_une_backenderror(monkeypatch):
    """Un ReadTimeout de requests traversait la tolérance du driver (qui
    n'attrape que BackendError) et l'a tué en plein vol le 27/08 — cinq jours
    nominaux puis un traceback pour un backend lent de 60 s. Converti à la
    SOURCE : driver, worker et flotte en héritent d'un coup."""
    import requests as _rq

    import oto_runner.backend as B
    from oto_runner.backend import Backend, BackendError

    b = Backend(base="http://x", token="t")

    def _timeout(url, **kw):
        raise _rq.exceptions.ReadTimeout("Read timed out")

    monkeypatch.setattr(B, "get_with_deadline", _timeout)
    with pytest.raises(BackendError, match="réseau : ReadTimeout"):
        b.count_rows("ns", filter={"a": "b"})
    monkeypatch.setattr(B, "post_with_deadline", _timeout)
    with pytest.raises(BackendError):
        b.enqueue("start", {})


def test_un_outil_critique_en_echec_arrete_la_flotte(monkeypatch):
    """Serper à sec 4 jours : 2 395 fiches « enrichies » sans une recherche
    réussie, jobs « done », aucune borne. La santé se lit au JOURNAL des
    appels de l'org : ≥12 appels sur 15 min dont ≥90 % en échec ⟹ arrêt
    ANORMAL (relance auto quand l'outil revient)."""
    from oto_runner import fleet as F

    F._outil_critique_en_panne.dernier = {}

    class BackendSerperMort(FauxBackend):
        def tool_health(self, org, tool, *, minutes=15, limit=20):
            return (20, 19)

    b = BackendSerperMort(counts=[100, 100])
    bilan = _run(_spec(ramp_seconds=0, org=226, critical_tools=("serper_search",)), b)
    assert "outil critique `serper_search` en échec" in bilan.arret
    assert not any(bilan.arret.startswith(m) for m in F._ARRETS_NORMAUX), \
        "arrêt ANORMAL : systemd relance quand l'outil revient"


def test_sans_appels_recents_pas_de_verdict(monkeypatch):
    """À la relance après panne, aucun appel récent : on laisse partir des jobs
    qui ré-alimentent la mesure — les vieux échecs ne bornent pas à vide."""
    from oto_runner import fleet as F

    F._outil_critique_en_panne.dernier = {}

    class BackendSilencieux(FauxBackend):
        def tool_health(self, org, tool, *, minutes=15, limit=20):
            return (0, 0)

    b = BackendSilencieux(counts=[2, 2, 0, 0])
    bilan = _run(_spec(ramp_seconds=0, org=226, critical_tools=("serper_search",)), b)
    assert bilan.arret == "file vide"


def test_des_faux_departs_en_serie_arretent_la_flotte():
    """Rodage v96 : 4 jobs « done » sur 5 avaient réservé sans écrire, et la
    ligne ratée était reservie dans la minute au suivant — une boucle à vide
    invisible des bornes (les jobs sont done). N d'affilée ⟹ arrêt ANORMAL."""
    from oto_runner import fleet as F

    class BackendFauxDeparts(FauxBackend):
        def get_job(self, jid):
            j = super().get_job(jid)
            if j["status"] == "done":
                j["result"]["tool_counts"] = {"data_claim_next": 1, "fr_get": 2}
                j["result"]["faux_depart"] = True
            return j

    b = BackendFauxDeparts(counts=[100, 100])
    bilan = _run(_spec(ramp_seconds=0), b)
    assert "faux départs consécutifs" in bilan.arret
    assert not any(bilan.arret.startswith(m) for m in F._ARRETS_NORMAUX)


def test_un_resultat_sans_marqueur_de_faux_depart_leve():
    """Le marqueur est posé par le WORKER — le seul à avoir vu les appels. Un
    résultat qui ne le porte pas vient d'un worker trop ancien : on lève. Le
    recalculer ici ferait passer une flotte mal appariée pour une flotte
    saine, et le contrat resterait invisible jusqu'au prochain vol à vide."""

    class BackendAncien(FauxBackend):
        def get_job(self, jid):
            j = super().get_job(jid)
            (j.get("result") or {}).pop("faux_depart", None)
            return j

    b = BackendAncien(counts=[100, 100])
    with pytest.raises(RuntimeError, match="faux_depart"):
        _run(_spec(ramp_seconds=0), b)


def test_le_rendement_effondre_arrete_la_flotte():
    """La borne GÉNÉRALE du prix de la sortie : une campagne a tourné quatre
    jours en payant le prix plein pour des fiches vides — jobs « done », outils
    debout, aucune borne. Le rendement rapporte le coût aux ÉCRITURES."""
    from oto_runner import fleet as F

    b = BackendRendement(counts=[100, 100], usage_par_job=20_000)
    bilan = _run(_spec(ramp_seconds=0, jetons_par_ecriture_max=5_000,
                       rendement_fenetre=10), b)
    assert bilan.arret == ("rendement effondré (200000 jetons pour 10 "
                           "écriture(s) sur 10 jobs)")
    assert not any(bilan.arret.startswith(m) for m in F._ARRETS_NORMAUX), \
        "arrêt ANORMAL : systemd relance quand la campagne est corrigée"


def test_une_fenetre_incomplete_ne_juge_pas():
    """Quelques jobs ne font pas une tendance : sous la taille de fenêtre, aucun
    verdict — sinon le premier job cher d'un vol arrêterait la campagne."""
    b = BackendRendement(counts=[100, 100, 100, 100, 0, 0], usage_par_job=20_000)
    bilan = _run(_spec(ramp_seconds=0, jetons_par_ecriture_max=1,
                       rendement_fenetre=20), b)
    assert bilan.arret == "file vide", "fenêtre non pleine ⟹ pas de verdict"


def test_un_rendement_sain_laisse_la_flotte_tourner():
    """Fenêtre PLEINE et rendement dans le plafond : la flotte va jusqu'au
    bout de sa file — la borne ne doit pas mordre sur une campagne qui produit."""
    b = BackendRendement(counts=[100] * 12 + [0], usage_par_job=20_000)
    bilan = _run(_spec(ramp_seconds=0, jetons_par_ecriture_max=50_000,
                       rendement_fenetre=10), b)
    assert bilan.arret == "file vide"
    assert bilan.done >= 10, "la fenêtre s'est remplie sans borner"


def test_sans_plafond_declare_le_rendement_ne_borne_pas():
    """La borne est OPT-IN : sans `jetons_par_ecriture_max`, même un million de
    jetons par écriture ne l'arrête pas (la fenêtre est pleine ici)."""
    b = BackendRendement(counts=[100, 100, 100, 0, 0], usage_par_job=1_000_000)
    bilan = _run(_spec(ramp_seconds=0, rendement_fenetre=2), b)
    assert bilan.arret == "file vide"


def test_la_declaration_porte_le_rendement_et_nomme_la_flotte(tmp_path):
    """Les deux champs de rendement se lisent au YAML, et le nom du FICHIER
    (sans extension) devient le nom de la flotte : c'est le tag `fleet` de
    chaque job, par lequel on retrouve une campagne sans deviner un « id ≥ N »."""
    from oto_runner.fleet import _payload

    f = tmp_path / "campagne-demo.yaml"
    f.write_text("procedure: p\nnamespace: demo\n"
                 "jetons_par_ecriture_max: 40000\nrendement_fenetre: 25\n")
    spec = load_spec(str(f))
    assert spec.jetons_par_ecriture_max == 40_000 and spec.rendement_fenetre == 25
    assert spec.name == "campagne-demo"
    assert _payload(spec)["fleet"] == "campagne-demo"


def test_une_flotte_sans_nom_leve():
    """Le tag `fleet` ne se devine pas : ni repli sur le namespace (deux
    flottes peuvent drainer la même file), ni tag vide qui rendrait les jobs
    d'une campagne introuvables. Une spec sans nom ne se construit pas."""
    with pytest.raises(ValueError, match="nom de flotte vide"):
        _spec(name="")


def test_le_rendement_est_absent_par_defaut(tmp_path):
    f = tmp_path / "flotte.yaml"
    f.write_text("procedure: p\nnamespace: demo\n")
    spec = load_spec(str(f))
    assert spec.jetons_par_ecriture_max is None, "borne inactive sans déclaration"
    assert spec.rendement_fenetre == 10, "défaut runner : 10 jobs"
