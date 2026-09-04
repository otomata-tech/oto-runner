"""Un travail COUPÉ à son plafond compte comme réussi — et ne l'est pas.

Un déroulé arrêté sur `max_steps` ou `max_tokens` rend la main sans erreur : le
job est `done`, le compte des lignes traitées est juste, et rien, dans le bilan,
ne le distingue d'un travail qui a conclu. Sa ligne porte pourtant ce qui avait
été fait au moment de l'arrêt — un succès apparent posé sur un travail tronqué.

C'est la même famille que `etat_muet` : le fait existait déjà (le worker DÉCLARE
son motif d'arrêt), personne ne le lisait. Un compte porté par le bilan se lit là
où on regarde le résultat.
"""
from __future__ import annotations

from tests.test_fleet import FauxBackend, _run, _spec


class BackendQuiCoupe(FauxBackend):
    """Des travaux qui rendent la main pour la raison qu'on choisit."""

    motif = "end_turn"

    def get_job(self, jid):
        j = super().get_job(jid)
        if j["status"] == "done":
            j["result"]["stopped"] = self.motif
        return j


def test_le_bilan_compte_les_motifs_d_arret_declares():
    bk = BackendQuiCoupe(counts=[2, 2, 0, 0], duree=1)
    bilan = _run(_spec(ramp_seconds=0), bk)
    assert bilan.arrets == {"end_turn": bilan.done}
    assert bilan.done >= 1


def test_un_travail_coupe_au_plafond_est_compte_done_et_signale(caplog):
    bk = BackendQuiCoupe(counts=[2, 2, 0, 0], duree=1)
    bk.motif = "max_steps"
    bilan = _run(_spec(ramp_seconds=0), bk)
    assert bilan.failed == 0 and bilan.done >= 1, "coupé ≠ échoué : il rend done"
    assert bilan.arrets.get("max_steps") == bilan.done
    assert "COUPÉS" in caplog.text and "max_steps" in caplog.text


def test_un_passage_qui_conclut_ne_declenche_aucune_alerte(caplog):
    """La conclusion ne doit pas crier sur un passage sain — sinon elle cesse
    d'être lue, exactement comme le compteur d'unités en échec d'une machine."""
    bk = BackendQuiCoupe(counts=[2, 2, 0, 0], duree=1)
    bilan = _run(_spec(ramp_seconds=0), bk)
    assert bilan.arrets and "COUPÉS" not in caplog.text


def test_un_worker_qui_ne_declare_pas_son_motif_ne_disparait_pas():
    """`inconnu` plutôt qu'une absence : un travail non compté se lirait comme
    un travail qui n'a pas eu lieu."""
    bk = FauxBackend(counts=[2, 2, 0, 0], duree=1)          # pas de `stopped` dans le résultat
    bilan = _run(_spec(ramp_seconds=0), bk)
    assert bilan.arrets == {"inconnu": bilan.done}
