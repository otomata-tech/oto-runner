"""L'ordre d'arrêt LU sur la plateforme — ce qui rend `op=stop` réel.

Un opérateur pose `stopping` depuis un écran ou une conversation. **Tant que
l'ordonnanceur ne le lit pas, c'est une écriture que personne ne lit** : l'état
annonce un arrêt qui n'arrive jamais, et le passage continue de réserver,
d'appeler, de DÉPENSER.

C'est pour cette raison que l'état s'appelle `stopping` et non `stopped` — la
plateforme ne ment pas sur ce qu'elle a fait. Ces tests couvrent l'autre moitié :
que quelqu'un le lise, et l'ACCUSE.

Trois exigences, dans l'ordre de ce qui coûterait le plus cher :

1. **l'arrêt demandé cesse l'enfilement** — sinon on continue de dépenser après
   avoir cru couper ;
2. **il est GRACIEUX** : les travaux en vol vont à leur terme. Couper au milieu
   laisserait des lignes sous bail et ferait repayer les jobs — le remède serait
   pire que le mal ;
3. **il est ACCUSÉ en sortant.** Sans accusé, l'état reste `stopping` pour
   toujours, ce qui est exactement le symptôme d'un ordonnanceur MORT : on le
   fabriquerait en étant vivant, et le diagnostic ne vaudrait plus rien.
"""
from __future__ import annotations

from oto_runner.backend import BackendError
from oto_runner.fleet import FleetSpec, run_fleet

from tests.test_fleet import FauxBackend, Horloge


def _spec(**kw) -> FleetSpec:
    # ⚠️ Une borne de SECOURS est TOUJOURS posée, même quand le test porte sur
    # l'ordre d'arrêt. Sans elle, une flotte dont l'ordre n'est pas lu tourne
    # INDÉFINIMENT : le test ne rougit pas, il PEND — et un test qui pend est pire
    # qu'un test absent, parce qu'en CI il ressemble à une infrastructure lente.
    # Mesuré : la mutation « l'ordre n'est plus lu » ne faisait rougir aucun des
    # sept tests de ce fichier, elle les BLOQUAIT.
    #
    # C'est le BUDGET et non le volume : le budget croît avec chaque job conclu,
    # donc il mord forcément ; le volume se déduit des lignes restantes, qu'une
    # doublure à compteurs constants ne fait jamais décroître (et à compteurs
    # décroissants, il coupe AVANT le premier enfilement — vécu en écrivant ce
    # fichier). Assez haut pour ne jamais devancer l'ordre, fini pour que
    # l'assertion sur `bilan.arret` distingue les deux raisons de s'arrêter.
    base = dict(procedure="p", namespace="ns", name="flotte-demo",
                tools=("data_claim_next",), filter={"statut": "a_traiter"},
                concurrency=3, ramp_seconds=0, fleet_id=42, budget_tokens=200_000)
    base.update(kw)
    return FleetSpec(**base)


def _run(spec, backend):
    h = Horloge()
    return run_fleet(spec, backend, sleep=h.sleep, clock=h.clock, poll_s=20)


class _AvecOrdreDArret(FauxBackend):
    """La plateforme répond « arrête-toi » dès le premier battement."""
    stop_demande = True


class _OrdreApresQuelquesDeparts(FauxBackend):
    """L'ordre arrive APRÈS que des travaux soient partis.

    ⚠️ Sans ce délai, il n'y a jamais rien EN VOL au moment de l'ordre — et le
    test du « gracieux » passerait au vert sur une flotte qui n'a rien à laisser
    finir. Mesuré : la première version de ce fichier avait ce défaut, et une
    mutation qui coupait au milieu ne la faisait pas rougir."""

    def __init__(self, *a, ordre_au=3, **kw):
        super().__init__(*a, **kw)
        self.ordre_au = ordre_au

    def battre_flotte(self, fleet_id):
        self.battements = getattr(self, "battements", 0) + 1
        return self.battements >= self.ordre_au


class _PlateformeMuette(FauxBackend):
    """Le battement échoue — une plateforme injoignable n'est PAS un ordre."""

    def battre_flotte(self, fleet_id):
        self.battements = getattr(self, "battements", 0) + 1
        raise BackendError("503 — bascule en cours")


# ── ① l'ordre cesse l'enfilement, ② gracieusement ────────────────────────────

def test_l_arret_demande_cesse_l_enfilement_et_conclut():
    b = _AvecOrdreDArret(counts=[100, 100])
    bilan = _run(_spec(), b)
    assert bilan.arret == "arrêt demandé", (
        f"arrêté pour la mauvaise raison : {bilan.arret!r} — si c'est une borne, "
        "c'est que l'ordre n'a pas été lu")
    assert b.enfiles <= 1, (
        f"{b.enfiles} jobs enfilés après l'ordre — on continue de dépenser après "
        "avoir cru couper")


def test_les_travaux_en_vol_vont_a_leur_terme():
    """⚠️ Gracieux, comme l'arrêt des agents. Couper au milieu laisserait des
    lignes sous bail et ferait repayer les jobs — le remède serait pire que le mal.

    L'ordre tombe APRÈS quelques départs, sinon il n'y a rien à laisser finir et
    le test passerait au vert sans rien prouver."""
    b = _OrdreApresQuelquesDeparts(counts=[100, 100], duree=4, ordre_au=3)
    bilan = _run(_spec(), b)
    assert b.enfiles >= 1, "des travaux étaient bien partis avant l'ordre"
    assert bilan.arret == "arrêt demandé"
    assert bilan.done + bilan.failed == b.enfiles, (
        f"{b.enfiles - bilan.done - bilan.failed} travail(aux) abandonné(s) en "
        "vol — leurs lignes resteraient sous bail et se repaieraient")


# ── ③ l'arrêt est ACCUSÉ ─────────────────────────────────────────────────────

def test_l_arret_est_accuse_en_sortant():
    """Sans accusé, `stopping` resterait éternel — le symptôme d'un ordonnanceur
    mort, fabriqué par un ordonnanceur vivant."""
    b = _AvecOrdreDArret(counts=[100, 100])
    _run(_spec(), b)
    assert getattr(b, "accuses", []) == [(42, None)]


def test_un_arret_par_BORNE_n_est_pas_accuse():
    """⚠️ On n'accuse que ce qui a été DEMANDÉ. Accuser un arrêt sur borne
    poserait `stopped` sans qu'aucun ordre n'existe — et effacerait la
    distinction entre « quelqu'un a demandé » et « la flotte a fini »."""
    b = FauxBackend(counts=[100, 100], usage_par_job=6000)
    bilan = _run(_spec(budget_tokens=10_000), b)
    assert bilan.arret.startswith("budget atteint")
    assert not getattr(b, "accuses", []), "aucun ordre n'avait été posé"


# ── ce que l'ordonnanceur ne doit PAS confondre ──────────────────────────────

def test_une_plateforme_injoignable_n_est_pas_un_ordre_d_arret():
    """⚠️ Confondre les deux éteindrait la flotte à CHAQUE bascule de
    déploiement — et le bilan dirait « arrêt demandé » pour une coupure réseau."""
    b = _PlateformeMuette(counts=[3, 3, 0, 0])
    bilan = _run(_spec(), b)
    assert b.battements >= 1, "le battement a bien été tenté"
    assert bilan.arret != "arrêt demandé"
    assert not getattr(b, "accuses", []), "rien à accuser : aucun ordre n'a été lu"


def test_la_flotte_est_PRISE_au_demarrage():
    """`armed` → `running` : c'est l'ordonnanceur qui pose le FAIT. Sans ça,
    l'écran afficherait « armée » pour un passage qui tourne."""
    b = FauxBackend(counts=[3, 3, 0, 0])
    _run(_spec(), b)
    assert getattr(b, "prises", []) == [42]


class _DeclarationRefusee(FauxBackend):
    """La plateforme refuse la déclaration : le passage tourne SANS flotte."""

    def declarer_flotte(self, **kw):
        raise BackendError("503 — la plateforme ne répond pas")


def test_sans_flotte_l_ordonnanceur_ne_bat_ni_n_accuse():
    """⚠️ Un passage qui n'a pas de flotte ne doit appeler aucun geste qui n'a
    pas de destinataire — sinon chaque tour de boucle produirait une erreur, et
    le journal noierait les vraies.

    Une spec sans `fleet_id` en DÉCLARE une : le seul cas réellement sans flotte
    est celui où la déclaration échoue. Le passage tourne quand même — le
    rattachement sert à LIRE, il ne conditionne pas le travail."""
    b = _DeclarationRefusee(counts=[3, 3, 0, 0])
    bilan = _run(_spec(fleet_id=None), b)
    assert bilan.arret, "le passage se conclut normalement"
    assert not getattr(b, "prises", []), "rien à prendre"
    assert not getattr(b, "battements", 0), "rien à qui demander"
    assert not getattr(b, "accuses", []), "rien à accuser"
