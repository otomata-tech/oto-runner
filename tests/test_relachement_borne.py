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
