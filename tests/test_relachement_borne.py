"""Un relâchement qui échoue ARRÊTE le passage.

⚠️ Le 29/08, 54 relâchements consécutifs ont échoué sans qu'aucun bruit ne remonte :
le mécanisme journalisait un avertissement et continuait. Les lignes restaient sous
bail, le travail suivant ne les prenait pas, et le passage paraissait avancer
normalement — **il mentait sur son débit**.

Un échec de relâchement n'est pas moins grave qu'un échec d'écriture : dans les deux
cas, une ligne du lot n'est pas traitée et rien ne le dit.
"""
import inspect

from oto_runner import fleet, worker


def test_le_seuil_est_bas_et_c_est_voulu():
    """Deux, pas cinq : chaque échec laisse une ligne verrouillée derrière lui, et
    la file paraît avancer pendant qu'elle se vide de travers."""
    assert fleet._MAX_RELACHEMENTS_RATES_CONSECUTIFS == 2


def test_la_borne_est_lue_dans_la_boucle_de_flotte():
    src = inspect.getsource(fleet.run_fleet)
    assert "relachements_rates" in src, "le compteur existe"
    assert "_MAX_RELACHEMENTS_RATES_CONSECUTIFS" in src, "et il BORNE"


def test_seul_False_compte_comme_un_echec():
    """⚠️ `None` ou l'absence veut dire « pas de ligne à rendre » — un travail qui
    n'a rien réservé ne doit pas compter comme un relâchement raté, sinon la borne
    arrête un passage sain au deuxième claim à vide."""
    src = inspect.getsource(fleet.run_fleet)
    assert 'resultat.get("relachee") is False' in src


def test_le_travail_declare_son_relachement():
    src = inspect.getsource(worker._traiter)
    assert '"relachee"' in src


def test_rien_a_rendre_n_est_pas_un_echec():
    """⚠️ LE cas qui a coupé le sixième passage à cinq lignes sur cent.

    `_relacher` rendait `False` aussi bien pour un échec que pour « aucune ligne
    à rendre ». La borne, posée une heure plus tôt, a donc arrêté un passage sain
    au troisième travail sans ligne — fin de file, ligne inconnue, peu importe.

    Le test de la borne disait déjà « seul False compte » ; la fonction ne le
    respectait pas. Un contrôle qui ne distingue pas « rien à faire » de « ça a
    raté » finit toujours par arrêter ce qui marche."""
    assert worker._relacher(None, None, "un-tableau", 226, "r-1") is None
    assert worker._relacher(None, "une-ligne", None, 226, "r-1") is None
