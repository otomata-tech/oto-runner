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


# ── Les deux contrôles déterministes du bilan de fin ─────────────────────────
# Ils existent parce que deux défauts ont traversé une grille de six critères tous
# à zéro (28/08). Aucun n'est une question de jugement : ce sont des contradictions
# internes qu'une requête attrape et qu'une relecture humaine rate.
from oto_runner.bilan import controler_fiches  # noqa: E402


class _BackendFiches(_Backend):
    def __init__(self, fiches, casse=False):
        super().__init__()
        self._fiches = fiches
        self._casse = casse

    def rows(self, namespace, filter=None, org=None, limit=200):
        if self._casse:
            raise RuntimeError("illisible")
        return self._fiches if (filter or {}).get("statut") == "enrichi" else []


def test_une_estampille_qui_nomme_le_mauvais_modele_est_relevee():
    """Une estampille absente se VOIT ; une estampille fausse MENT, et elle ment
    sur ce qui sert à trier."""
    b = _BackendFiches([{"siren": "1", "modele": "un-modele-2512"},
                        {"siren": "2", "modele": "un-modele-2407"}])
    r = controler_fiches(_Spec(), b, {1: _job(model="un-modele-2512")})
    assert r["estampille_fausse"] == ["2"]
    assert r["estampille_exacte"] == 1


def test_une_extinction_sans_acte_de_registre_est_relevee():
    """Le verrou forçait l'agent à CHOISIR une pièce, pas à en AVOIR une : il a
    coché « cessation au registre » sans citer le moindre acte."""
    b = _BackendFiches([
        {"siren": "1", "qualification": "dormante_ou_introuvable",
         "qualification_motif": "aucun dépôt, aucun salarié, aucune trace en ligne"},
        {"siren": "2", "qualification": "dormante_ou_introuvable",
         "qualification_motif": "radiation publiée au BODACC le 04/06/2013"},
        {"siren": "3", "qualification": "en_activite",
         "qualification_motif": "aucune trace récente"},
    ])
    r = controler_fiches(_Spec(), b, {1: _job()})
    # La 1 accumule des absences, qui ne prouvent rien. La 2 cite un acte daté.
    # La 3 ne se déclare pas éteinte — le contrôle ne la regarde pas.
    assert r["fiches_contradictoires"] == ["1"]


def test_une_fiche_qui_cite_un_acte_ET_un_repertoire_actif_est_BONNE():
    """⚠️ Le cas qui a fait retirer la première version du contrôle. Le répertoire
    RETARDE les radiations : une fiche qui cite l'acte ET signale honnêtement que
    le répertoire affiche encore « actif » est une bonne fiche, pas un manquement.
    La première version en criait cinq sur six."""
    b = _BackendFiches([
        {"siren": "1", "qualification": "dormante_ou_introuvable",
         "qualification_motif": "jugement de clôture du 08/11/2016 ; le répertoire "
                                "affiche encore un état administratif actif"},
        {"siren": "2", "qualification": "dormante_ou_introuvable",
         "qualification_motif": "liquidation pour insuffisance d'actif, jugement 2019"},
    ])
    assert controler_fiches(_Spec(), b, {1: _job()})["fiches_contradictoires"] == []


def test_plusieurs_modeles_dans_la_flotte_rendent_le_controle_impossible():
    """On ne peut pas dire laquelle ment, et l'affirmer serait pire que se taire."""
    b = _BackendFiches([{"siren": "1", "modele": "a"}])
    r = controler_fiches(_Spec(), b, {1: _job(model="a"), 2: _job(model="b")})
    assert r["estampille_fausse"] is None
    assert r["estampille_exacte"] is None


def test_des_fiches_illisibles_ne_font_pas_echouer_le_bilan():
    r = controler_fiches(_Spec(), _BackendFiches([], casse=True), {1: _job()})
    assert r["estampille_exacte"] is None and r["fiches_contradictoires"] is None
    assert "illisibles" in r["omis"]


def test_une_annee_seule_ne_vaut_pas_un_acte():
    """⚠️ LE cas qui a fait resserrer le critère. « Aucun dépôt depuis 2016 » date
    une ABSENCE, pas un acte — c'est l'accumulation de riens qu'on veut refuser.
    Avec l'année acceptée, le contrôle retenait ZÉRO fiche sur un palier réel, y
    compris le seul manquement."""
    b = _BackendFiches([
        {"siren": "1", "qualification": "dormante_ou_introuvable",
         "qualification_motif": "Aucun dépôt de comptes depuis 2016, aucun salarié "
                                "déclaré, aucune trace d'activité récente sur le web."},
        {"siren": "2", "qualification": "dormante_ou_introuvable",
         "qualification_motif": "Radiation au BODACC en 2015."},
    ])
    # La 1 n'a qu'une année d'absence ; la 2 nomme l'acte, même sans jour.
    assert controler_fiches(_Spec(), b, {1: _job()})["fiches_contradictoires"] == ["1"]
