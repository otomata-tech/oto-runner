"""Le dépôt local des faux départs : `OTO_RUNNER_FAUX_DEPARTS_DIR`.

Un faux départ conclut « done » sans rien produire, et le fil ne garde qu'une
synthèse : le texte final de l'agent est la seule pièce qui dise s'il a rédigé
sa fiche en prose sans appeler l'écriture, ou s'il a renoncé. On le dépose à
côté, en 0600, et SEULEMENT quand la variable est posée — le comportement par
défaut du worker reste celui d'avant.
"""
from __future__ import annotations

import json
import os
import stat

from oto_runner import worker as W
from oto_runner.agent_runtime import AgentResult, AgentStep
from tests.test_worker_reprise import FauxBackend, FauxMcp


def _job_avec_namespace():
    return {"id": 41, "kind": "start", "run_id": None,
            "payload": {"procedure": "veille-demo", "namespace": "vivier-demo",
                        "tools": ["data_rows"], "input": "Vas-y.",
                        "max_steps": 3}}


def _jouer(monkeypatch, job, reply, outils):
    etapes = [AgentStep(tool=t, ok=True, duration_ms=1) for t in outils]

    def faux_run(spec, transport, provider, prompt=None, history=None, on_turn=None, **_):
        return AgentResult(reply=reply, stopped="end_turn", steps=etapes)

    monkeypatch.setattr(W.agent_runtime, "run", faux_run)
    monkeypatch.setattr(W, "McpSession", FauxMcp)
    monkeypatch.setattr(W, "RENVOIS_MAX", 0)   # la capture porte sur le faux départ, pas sur le renvoi
    W._traiter(FauxBackend(), job, provider=None)


def test_un_faux_depart_depose_sa_sortie_en_0600(monkeypatch, tmp_path):
    """Le fichier porte de quoi diagnostiquer SANS ouvrir le fil : quels outils
    ont été appelés, et le texte final INTÉGRAL — c'est lui qui distingue la
    fiche rédigée en prose du renoncement."""
    depot = tmp_path / "faux-departs"        # inexistant : le worker le crée
    monkeypatch.setenv("OTO_RUNNER_FAUX_DEPARTS_DIR", str(depot))
    _jouer(monkeypatch, _job_avec_namespace(),
           reply="Voici la fiche rédigée : …",
           outils=["demo-connecteur_data_claim_next", "serper_search"])

    fichier = depot / "41.json"
    trace = json.loads(fichier.read_text(encoding="utf-8"))
    assert trace["job_id"] == 41
    assert trace["procedure"] == "veille-demo" and trace["namespace"] == "vivier-demo"
    assert trace["steps"] == ["demo-connecteur_data_claim_next", "serper_search"], \
        "les noms d'outils DANS L'ORDRE — l'écart entre les mots et les appels"
    assert trace["reply"] == "Voici la fiche rédigée : …", "le texte final entier"
    assert trace["horodatage"].endswith("+00:00"), "horodaté en UTC"
    assert stat.S_IMODE(os.stat(fichier).st_mode) == 0o600, \
        "donnée de la file de travail : lisible du seul compte du worker"


def test_sans_la_variable_rien_nest_ecrit(monkeypatch, tmp_path):
    """Le dépôt est un outil de diagnostic qu'on arme : variable absente ⟹
    comportement d'avant, à l'octet près."""
    monkeypatch.delenv("OTO_RUNNER_FAUX_DEPARTS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    _jouer(monkeypatch, _job_avec_namespace(), reply="renoncé",
           outils=["demo-connecteur_data_claim_next"])
    assert list(tmp_path.iterdir()) == []


def test_un_job_qui_a_ecrit_ne_depose_rien(monkeypatch, tmp_path):
    """Seul le faux départ se conserve : un job qui a écrit a produit sa trace
    là où elle compte — dans le tableau."""
    depot = tmp_path / "faux-departs"
    monkeypatch.setenv("OTO_RUNNER_FAUX_DEPARTS_DIR", str(depot))
    _jouer(monkeypatch, _job_avec_namespace(), reply="fiche écrite",
           outils=["demo-connecteur_data_claim_next", "demo-connecteur_data_write"])
    assert not depot.exists(), "rien n'est déposé, pas même le dossier"


def test_un_depot_impraticable_ne_tue_pas_le_job(monkeypatch, tmp_path):
    """La tolérance est ICI et nulle part ailleurs : le job est déjà payé, un
    diagnostic qui échoue se logge et se tait."""
    barrage = tmp_path / "barrage"
    barrage.write_text("je ne suis pas un dossier", encoding="utf-8")
    monkeypatch.setenv("OTO_RUNNER_FAUX_DEPARTS_DIR", str(barrage / "dedans"))
    b = FauxBackend()

    def faux_run(spec, transport, provider, prompt=None, history=None, on_turn=None, **_):
        return AgentResult(reply="prose", stopped="end_turn", steps=[
            AgentStep(tool="data_claim_next", ok=True, duration_ms=1)])

    monkeypatch.setattr(W.agent_runtime, "run", faux_run)
    monkeypatch.setattr(W, "McpSession", FauxMcp)
    W._traiter(b, _job_avec_namespace(), provider=None)      # ne lève PAS
    resultat = next(a for a in b.appels if a[0] == "complete_result")[1]
    assert resultat["faux_depart"] is True, "le job conclut comme avant"
