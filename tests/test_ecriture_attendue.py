"""Un travail qui a tenu une ligne sans y écrire se compte « sans_ecriture ».

Mesuré le 10/09/2026 (banc Audiens 660) : réservation, aucun outil, compte rendu
inventé, aucune écriture — et le bilan l'a compté abouti. Le runner ne sait pas ce
qu'écrire veut dire : c'est la déclaration du passage qui nomme les deux outils.
"""
from __future__ import annotations

import pytest

from oto_runner import ecriture_attendue as E
from oto_runner.agent_runtime import AgentStep

A = E.lire({"reservation": "data_claim_next", "ecriture": "data_write"})


def _pas(tool, ok=True, vide=False):
    return AgentStep(tool=tool, ok=ok, duration_ms=1, vide=vide)


def test_une_ligne_tenue_sans_ecriture_est_sans_ecriture():
    assert E.issue("done", [_pas("oto_procedure"), _pas("data_claim_next")], A) == "sans_ecriture"


def test_une_ecriture_reussie_laisse_done():
    assert E.issue("done", [_pas("data_claim_next"), _pas("data_write")], A) == "done"


def test_une_ecriture_refusee_ne_compte_pas():
    pas = [_pas("data_claim_next"), _pas("data_write", ok=False)]
    assert E.issue("done", pas, A) == "sans_ecriture"


def test_une_file_vide_n_est_pas_une_faute():
    assert E.issue("done", [_pas("data_claim_next", vide=True)], A) == "done"


def test_le_prefixe_du_connecteur_ne_trompe_pas():
    pas = [_pas("oto-11aout_data_claim_next"), _pas("oto-11aout_data_write")]
    assert E.issue("done", pas, A) == "done"


def test_sans_declaration_rien_n_est_juge():
    assert E.issue("done", [_pas("data_claim_next")], None) == "done"
    assert E.lire(None) is None and E.verdict_vide(None) is None


def test_seul_done_peut_devenir_sans_ecriture():
    assert E.issue("blocked", [_pas("data_claim_next")], A) == "blocked"


@pytest.mark.parametrize("sortie, vide", [
    ('{"datastore": "t", "row": null, "hint": "aucune ligne libre"}', True),
    ('{"datastore": "t", "row": {"_id": "01a0"}}', False),
    ("texte nu, tronqué", False),
])
def test_le_verdict_vide_lit_la_cle_declaree(sortie, vide):
    assert E.verdict_vide(A)("data_claim_next", sortie) is vide


def test_le_verdict_ne_regarde_que_la_reservation():
    assert E.verdict_vide(A)("data_rows", '{"row": null}') is False


@pytest.mark.parametrize("mal", [{}, {"reservation": "x"}, {"ecriture": "y"}, "data_write",
                                 {"reservation": "x", "ecriture": "y", "autre": 1}])
def test_une_declaration_mal_formee_leve(mal):
    with pytest.raises(ValueError):
        E.lire(mal)


# ── De la déclaration au travail ─────────────────────────────────────────────

def test_la_declaration_descend_dans_le_travail(tmp_path):
    from oto_runner.declaration import load_spec, payload
    y = tmp_path / "f.yaml"
    y.write_text("procedure: p\nnamespace: t\ntools: [oto_procedure]\ninput: x\n"
                 "ecriture_attendue: {reservation: data_claim_next, ecriture: data_write}\n")
    assert payload(load_spec(str(y)))["ecriture_attendue"] == {
        "reservation": "data_claim_next", "ecriture": "data_write", "ligne": "row"}


def test_sans_declaration_le_travail_ne_porte_pas_la_cle(tmp_path):
    from oto_runner.declaration import load_spec, payload
    y = tmp_path / "f.yaml"
    y.write_text("procedure: p\nnamespace: t\ntools: [oto_procedure]\ninput: x\n")
    assert "ecriture_attendue" not in payload(load_spec(str(y)))


def test_une_declaration_mal_formee_arrete_le_chargement(tmp_path):
    from oto_runner.declaration import load_spec
    y = tmp_path / "f.yaml"
    y.write_text("procedure: p\nnamespace: t\ntools: [oto_procedure]\ninput: x\n"
                 "ecriture_attendue: data_write\n")
    with pytest.raises(ValueError):
        load_spec(str(y))


# ── Au bilan ─────────────────────────────────────────────────────────────────

def test_le_bilan_compte_sans_ecriture_A_PART():
    from oto_runner.bilan import _postes_jobs
    postes = _postes_jobs({1: {"status": "done", "result": {"usage_tokens": 10, "issue": "sans_ecriture"}},
                           2: {"status": "done", "result": {"usage_tokens": 5}},
                           3: {"status": "failed", "result": {}}})
    assert (postes["termines"], postes["sans_ecriture"], postes["echoues"]) == (1, 1, 1)
    assert postes["jetons"] == 15, "un travail sans écriture a quand même coûté"


def test_la_ligne_de_journal_le_nomme():
    from oto_runner.bilan_ligne import ligne
    b = {"flotte": "banc", "lignes": {"sorties": 1, "depart": 2, "par_statut": {}, "abouties": 1, "abouties_omis": None},
         "jetons": {"total": 100, "par_aboutie": 100, "par_sortie": 100},
         "refus_ecriture": None, "refus_ecriture_omis": "banc sans org",
         "jobs": {"termines": 1, "echoues": 0, "sans_ecriture": 2}}
    assert "2 travaux sans écriture" in ligne(b, None)
