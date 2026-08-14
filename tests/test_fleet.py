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

    def count_rows(self, namespace, filter=None):
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
            self.jobs[jid] = {"status": self.statut,
                              "result": {"usage_tokens": self.usage},
                              "last_error": "boom" if self.statut == "failed" else None}
        return self.jobs[jid]


class Horloge:
    def __init__(self):
        self.t = 0.0

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _spec(**kw):
    base = dict(procedure="p", namespace="ns", tools=("data_claim_next",),
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
