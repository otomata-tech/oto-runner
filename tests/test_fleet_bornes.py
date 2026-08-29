"""Un relâchement qui échoue ARRÊTE le passage.

⚠️ Le 29/08, 54 relâchements consécutifs ont échoué sans qu'aucun bruit ne
remonte : le mécanisme journalisait un avertissement et continuait. Les lignes
restaient sous bail, le travail suivant ne les prenait pas, et le passage
paraissait avancer normalement — **il mentait sur son débit**.

Un échec de relâchement n'est pas moins grave qu'un échec d'écriture.
"""
from oto_runner.fleet import _MAX_RELACHEMENTS_RATES_CONSECUTIFS


def test_le_seuil_est_bas_et_c_est_voulu():
    """Deux, pas cinq : chaque échec laisse une ligne verrouillée derrière lui,
    et la file paraît avancer pendant qu'elle se vide de travers."""
    assert _MAX_RELACHEMENTS_RATES_CONSECUTIFS == 2


def test_le_poste_existe_dans_le_resultat_d_un_travail():
    """`relachee` doit être un booléen explicite — `False` est un échec, un
    champ absent voudrait dire « pas de ligne à rendre » et ne compte pas."""
    import inspect
    from oto_runner import worker
    src = inspect.getsource(worker._traiter)
    assert '"relachee"' in src or "resultat[\"relachee\"]" in src
