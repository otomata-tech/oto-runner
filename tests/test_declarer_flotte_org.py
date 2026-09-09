"""La campagne naît sous l'organisation DÉCLARÉE, pas sous celle du jeton.

⚠️ Par l'EN-TÊTE `X-Oto-Org`, jamais par le corps : le modèle d'entrée de la
route refuse les champs qu'il ne déclare pas, et un `_org` glissé dans le corps
fait `400 unknown_fields` — donc AUCUNE campagne déclarée, et des travaux qui
partent sans rattachement, invisibles à `op=state` comme à `op=stop`. Vécu le
09/09/2026, sur cinq travaux.

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
        self.corps = self.org = None

    def _post(self, chemin, corps, org=None):
        self.corps, self.org = corps, org
        return {"fleet": {"id": 1, "org_id": org}}


def test_l_org_declaree_part_avec_la_creation():
    b = _Espion()
    b.declarer_flotte(label="p", procedure="pr", tools=["data_rows"], org=226)
    assert b.org == 226, (
        "sans elle, la campagne naît sous l'org active du jeton et ses travaux "
        "cherchent leur procédure au mauvais endroit")


def test_sans_org_declaree_rien_nest_pose():
    """Le bord qui garde le comportement d'avant : une déclaration qui ne nomme
    aucune organisation laisse le serveur décider, comme il l'a toujours fait.
    Poser un `_org` deviné serait pire que ne rien poser."""
    b = _Espion()
    b.declarer_flotte(label="p", procedure="pr", tools=["data_rows"])
    assert b.org is None


# ── L'organisation vit sur le CLIENT, pas sur chaque appel ───────────────────

def test_le_client_pose_l_entete_sur_TOUS_les_appels():
    """⚠️ La première version la posait appel par appel : `create` la portait,
    `launch`, le battement et l'enfilage non. La campagne naissait donc sous la
    bonne organisation et devenait introuvable au geste suivant — `404
    fleet_not_found` toutes les vingt secondes pendant 560 s, sans qu'un seul
    travail parte (09/09/2026).

    Un contexte qu'il faut penser à joindre à chaque appel finit par être oublié
    à l'un d'eux. Porté par le client, aucun appel ne peut plus l'omettre."""
    vus = []

    class _Tous(Backend):
        def _reseau(self, chemin, fn, **kw):
            vus.append(kw.get("headers", {}).get("X-Oto-Org"))
            class _R:
                status_code, content = 200, b"{}"
                def json(self): return {"fleet": {}, "job": {}, "ok": True}
            return _R()

    b = _Tous(base="http://x", token="t", org=226)
    b.declarer_flotte(label="p", procedure="pr", tools=["data_rows"])
    b._post("/api/me/runner/jobs", {"op": "enqueue"})
    b._post("/api/me/runner/fleets", {"op": "launch", "fleet_id": 1})

    assert vus == ["226", "226", "226"], (
        "l'en-tête part sur TOUS les appels, y compris ceux qui n'ont pas pensé "
        "à la passer eux-mêmes")


def test_sans_org_sur_le_client_aucun_entete():
    """Le bord d'avant : un client sans organisation n'en invente pas une."""
    vus = []

    class _Tous(Backend):
        def _reseau(self, chemin, fn, **kw):
            vus.append("X-Oto-Org" in kw.get("headers", {}))
            class _R:
                status_code, content = 200, b"{}"
                def json(self): return {"fleet": {}}
            return _R()

    _Tous(base="http://x", token="t")._post("/api/me/runner/fleets", {"op": "list"})
    assert vus == [False]
