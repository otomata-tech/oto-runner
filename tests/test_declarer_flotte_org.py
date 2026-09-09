"""La campagne naît sous l'organisation DÉCLARÉE, pas sous celle du jeton.

⚠️ Mesuré le 09/09/2026 : 848 refus « guide introuvable » en 78 minutes, sans un
seul trou, sur six procédures qui existaient parfaitement dans l'organisation
écrite au fichier. La déclaration porte DEUX organisations — le contexte
d'exécution, posé sur chaque appel d'outil de l'agent, et celle sous laquelle la
campagne doit naître. Le runner ne propageait que la première.

⚠️ Ce n'est pas une organisation qu'on s'attribue : `_org` est un jeton de
CONTEXTE, vérifié côté serveur — un porteur qui n'en est pas membre reçoit un
refus nommé. La propriété reste posée par la règle d'autorisation, jamais lue
d'un champ du corps : c'est le verrou qui empêche de créer une ressource chez
autrui, et il n'est pas touché.
"""
from __future__ import annotations

from oto_runner.backend import Backend


class _Espion(Backend):
    def __init__(self):
        super().__init__(base="http://x", token="t")
        self.corps = None

    def _post(self, chemin, corps, avec_entetes=False):
        self.corps = corps
        return {"fleet": {"id": 1, "org_id": corps.get("_org")}}


def test_l_org_declaree_part_avec_la_creation():
    b = _Espion()
    b.declarer_flotte(label="p", procedure="pr", tools=["data_rows"], org=226)
    assert b.corps["_org"] == 226, (
        "sans elle, la campagne naît sous l'org active du jeton et ses travaux "
        "cherchent leur procédure au mauvais endroit")


def test_sans_org_declaree_rien_nest_pose():
    """Le bord qui garde le comportement d'avant : une déclaration qui ne nomme
    aucune organisation laisse le serveur décider, comme il l'a toujours fait.
    Poser un `_org` deviné serait pire que ne rien poser."""
    b = _Espion()
    b.declarer_flotte(label="p", procedure="pr", tools=["data_rows"])
    assert "_org" not in b.corps
