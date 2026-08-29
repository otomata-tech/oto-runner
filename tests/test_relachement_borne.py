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


def test_le_relachement_passe_par_l_alias_et_non_par_l_identifiant():
    """⚠️ Le harnais ne retrouvait la ligne qu'une fois sur cinq dans les sorties
    du fournisseur. Il n'a pas besoin de la connaître : le serveur sait ce que le
    travail tient, et l'alias le lui demande. Le harnais n'a qu'à tenir le jeton,
    et il le tient toujours."""
    vus = []

    class _Mcp:
        derniere_ligne = None

        def outil(self, name, arguments=None):
            vus.append((name, arguments or {}))
            return {}

    assert worker._relacher(_Mcp(), None, "un-tableau", 226, "r-42") is True
    nom, args = vus[-1]
    assert nom == "data_release"
    assert args["namespace"] == "@claimed" and args["id"] == "@claimed"
    assert args["worker"] == "r-42"


def test_la_rampe_ne_bride_que_la_MONTEE_en_charge():
    """⚠️ La rampe s'appliquait à chaque enfilement, pour toujours : un travail au
    plus toutes les 60 s, quelle que soit la vitesse des agents. Mesuré le 29/08 —
    travaux de 107 s, enfilements toutes les 64 s : l'ordonnanceur donnait le
    tempo, pas les agents.

    Elle protège le DÉPART — trois conversations à la même seconde ont déjà gelé
    la plateforme — et n'a jamais eu à brider la croisière."""
    src = inspect.getsource(fleet.run_fleet)
    assert "pleine_charge_atteinte" in src, "l'état de montée est suivi"
    assert "or not monte" in src, "et la rampe cesse de s'appliquer une fois montée"
    # ⚠️ Elle se compte en DÉPARTS, pas en pleine charge atteinte : la première
    # version attendait `len(en_vol) >= concurrency`, et c'est la rampe qui
    # empêchait d'y arriver. Elle ne s'est jamais désactivée.
    assert "departs < spec.concurrency" in src
