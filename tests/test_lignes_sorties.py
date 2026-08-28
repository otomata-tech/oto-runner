"""Les lignes SORTIES de la file — celles dont on a le plus besoin et sur lesquelles
on ne sait rien.

Une ligne réservée trois fois sans écriture est basculée en « échec » PAR LA
PLATEFORME. Personne n'écrit à ce moment-là : la ligne ne porte ni estampille, ni
motif, ni trace de ce qui a été tenté. Mesuré le 28/08 sur un palier : 2 lignes sur
23 — à l'échelle d'une vague, des centaines de fiches muettes, et une fiche muette
ne se rattrape pas après coup, contrairement à une fiche incomplète.

Ce que ces tests protègent avant tout : **l'annotation ne bloque JAMAIS rien.**
C'est une observation ; une file ne s'arrête pas pour une observation.
"""
from oto_runner.bilan import annoter_lignes_sorties


class _Spec:
    name = "flotte-x"
    namespace = "un-tableau"
    org = 226
    procedure = "une-procedure"
    filter = None


def _job(model="un-modele-2512"):
    return {"status": "done", "result": {"usage_tokens": 10, "model": model}}


class _Backend:
    """Un backend de test qui note ce qu'on lui écrit."""

    def __init__(self, sorties=None, enrichies=None, casse_lecture=False,
                 casse_ecriture=False):
        self._sorties = sorties if sorties is not None else []
        self._enrichies = enrichies if enrichies is not None else []
        self._casse_lecture = casse_lecture
        self._casse_ecriture = casse_ecriture
        self.ecrites = []

    def rows(self, namespace, filter=None, org=None, limit=200):
        if self._casse_lecture:
            raise RuntimeError("le tableau ne répond pas")
        statut = (filter or {}).get("statut")
        return self._sorties if statut == "echec" else self._enrichies

    def patch_row(self, namespace, row_id, valeurs, org=None):
        if self._casse_ecriture:
            raise RuntimeError("écriture refusée")
        self.ecrites.append((row_id, valeurs))
        return {}


def test_une_ligne_sortie_recoit_son_estampille_et_son_motif():
    b = _Backend(sorties=[{"_id": "r1", "siren": "1"}],
                 enrichies=[{"version_procedure": "une-procedure v101"}])
    n = annoter_lignes_sorties(_Spec(), b, {1: _job()})
    assert n == {"sorties": 1, "annotees": 1}
    (row_id, valeurs), = b.ecrites
    assert row_id == "r1"
    assert valeurs["modele"] == "un-modele-2512"
    # La version se lit sur une ligne RÉUSSIE du même lot : l'ordonnanceur ne la
    # connaît pas, mais ses propres fiches la portent. Relevée, jamais inventée.
    assert valeurs["version_procedure"] == "une-procedure v101"
    assert "réservée sans écriture" in valeurs["retraitement_motif"]


def test_une_ligne_deja_estampillee_n_est_pas_retouchee():
    """Une ligne sortie qui porte déjà un modèle a été traitée puis marquée
    autrement : on n'écrase pas ce qu'un agent a écrit."""
    b = _Backend(sorties=[{"_id": "r1", "modele": "un-autre-modele"}])
    assert annoter_lignes_sorties(_Spec(), b, {1: _job()}) == {"sorties": 1, "annotees": 0}
    assert b.ecrites == []


def test_aucune_ligne_sortie_ne_declenche_aucune_ecriture():
    b = _Backend(sorties=[])
    assert annoter_lignes_sorties(_Spec(), b, {1: _job()}) == {"sorties": 0, "annotees": 0}
    assert b.ecrites == []


def test_une_lecture_impossible_ne_leve_pas_et_le_dit():
    """`sorties: None` — un poste absent dit POURQUOI il l'est, il ne se confond
    jamais avec un zéro."""
    b = _Backend(casse_lecture=True)
    assert annoter_lignes_sorties(_Spec(), b, {1: _job()}) == {"sorties": None, "annotees": 0}


def test_une_ecriture_refusee_ne_bloque_ni_les_suivantes_ni_le_bilan():
    """LE cas qui compte : on ne perd pas le bilan d'une campagne parce qu'une
    annotation d'observabilité a échoué."""
    b = _Backend(sorties=[{"_id": "r1"}, {"_id": "r2"}], casse_ecriture=True)
    assert annoter_lignes_sorties(_Spec(), b, {1: _job()}) == {"sorties": 2, "annotees": 0}


def test_une_version_illisible_n_empeche_pas_de_poser_le_reste():
    """Une demi-estampille vaut mieux que rien ICI — contrairement à la pose sur
    une fiche réussie, la ligne sortie ne dirait sinon RIEN du tout."""
    class _SansEnrichies(_Backend):
        def rows(self, namespace, filter=None, org=None, limit=200):
            if (filter or {}).get("statut") == "enrichi":
                raise RuntimeError("illisible")
            return self._sorties

    b = _SansEnrichies(sorties=[{"_id": "r1"}])
    assert annoter_lignes_sorties(_Spec(), b, {1: _job()}) == {"sorties": 1, "annotees": 1}
    (_, valeurs), = b.ecrites
    assert valeurs["modele"] == "un-modele-2512"
    assert "version_procedure" not in valeurs


def test_sans_modele_connu_le_motif_est_pose_quand_meme():
    b = _Backend(sorties=[{"_id": "r1"}])
    n = annoter_lignes_sorties(_Spec(), b, {1: _job(model=None)})
    assert n == {"sorties": 1, "annotees": 1}
    (_, valeurs), = b.ecrites
    assert "modele" not in valeurs
    assert valeurs["retraitement_motif"]


def test_une_declaration_sans_tableau_ne_fait_rien():
    class _SansNs(_Spec):
        namespace = None

    b = _Backend(sorties=[{"_id": "r1"}])
    assert annoter_lignes_sorties(_SansNs(), b, {1: _job()}) == {"sorties": 0, "annotees": 0}
    assert b.ecrites == []
