"""La FLOTTE : un ordonnanceur MINCE au-dessus de la file de jobs.

Un client ordinaire de l'API jobs — aucun kind serveur, aucune boucle d'agent
ici : la boucle vit dans le worker, le claim de ligne vit dans la PROCÉDURE.
Le driver ne fait que trois choses : enfiler des jobs `start` avec une rampe,
maintenir la concurrence déclarée, et s'arrêter proprement sur l'une des
bornes — file vide, volume, budget de jetons, rendement effondré, ou trop
d'échecs consécutifs.

La déclaration est un YAML par flotte (cf. `docs/fleet-example.yaml`) — jamais
un secret dedans : le jeton et la clé de modèle viennent de l'environnement.
⚠️ Le worker est un pool HOMOGÈNE : son modèle vient de SON environnement
(`OTO_RUNNER_MODEL`), pas de la déclaration — un champ `model` dans le YAML
est logué puis ignoré, pour que la divergence soit VISIBLE, jamais silencieuse.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field, fields
from typing import Callable, Optional

import yaml

from .backend import Backend, BackendError
from .bilan import PERIODE_S as _BILAN_PERIODE_S
from .bilan import ecrire_bilan

logger = logging.getLogger("oto_runner.fleet")

_POLL_S = 20
_MAX_FAILED_CONSECUTIFS = 3
_MAX_ERREURS_BACKEND = 10   # ~3-4 min de panne DENSE (reset au 1er succès)
# Faux départs EN SÉRIE (27/08) : un job « done » qui a réservé une ligne sans
# rien écrire n'est pas un succès — et depuis que le run libère la ligne à sa
# conclusion, la ligne ratée est reservie dans la minute au job suivant, qui
# refait le même faux départ : une boucle qui vide le budget sans écrire, et
# qu'aucune borne ne voyait (les jobs sont « done »). N d'affilée ⟹ arrêt
# ANORMAL (relance auto) : « ça tourne à vide » n'est pas « ça tourne ».
# ⚠️ Seuls comptent les jobs qui ont RÉELLEMENT réservé une ligne. Un job à
# CLAIM VIDE (la file n'avait plus rien à rendre) est un non-événement : en fin
# de file il y a toujours plus d'agents que de lignes, et les compter a fait
# échouer une campagne ABOUTIE le 28/08 (18/20 lignes, les 2 dernières sous bail
# ⟹ 5 jobs à un seul appel ⟹ borne mordue, `exit 1`). Il ne remet pas non plus
# le compteur à zéro : la remise à zéro rendrait la borne contournable par
# alternance (un vrai faux départ, un claim à vide, indéfiniment).
_MAX_FAUX_DEPARTS_CONSECUTIFS = 5
# ⚠️ DEUX suffisent, et le seuil est bas exprès. Un relâchement qui échoue laisse
# la ligne sous bail : le travail suivant ne la prend pas, la file paraît avancer
# alors qu'elle se vide en laissant des lignes derrière, et le passage MENT SUR SON
# DÉBIT. Le 29/08, 54 échecs consécutifs sont passés inaperçus parce que le
# relâchement journalisait et continuait — un best-effort sans destination.
# Un échec de relâchement compte donc comme un échec d'écriture.
# ⚠️ Le message de lancement NOMME la file : un agent à qui on dit « la file de
# travail » sans la nommer DEVINE des noms de tableaux (vécu : entreprises,
# projet_220, data… tous inconnus, puis des SIREN hallucinés et une conclusion
# vide). Le harnais historique nommait le tableau dans sa conversation — le
# driver fait pareil, depuis la déclaration.
DEFAULT_INPUT = ("Ta file de travail est le tableau `{namespace}` : réserve chaque "
                 "ligne par data_claim_next avec namespace=\"{namespace}\" et "
                 "filter={filter}. Traite les lignes une par une selon la procédure, "
                 "autant que ton budget de tours le permet, puis conclus par un bilan "
                 "bref (lignes traitées, difficultés). N'invente JAMAIS une ligne ni "
                 "un identifiant : seule la file fait foi — si la réservation ne rend "
                 "rien, la file est vide, arrête-toi.")

@dataclass(frozen=True)
class FleetSpec:
    procedure: str
    namespace: str
    tools: tuple
    # Le nom de la flotte : le TAG apposé à chaque job (`fleet`), par lequel on
    # retrouve les jobs d'une campagne — plus par « id ≥ N ». `load_spec` le
    # tire du nom du fichier de déclaration ; une spec construite en code le
    # DÉCLARE. Aucun repli sur le namespace : deux flottes peuvent drainer la
    # même file, et un tag deviné est un tag faux — pire qu'un tag absent.
    name: str
    # L'identifiant de la flotte DÉCLARÉE EN BASE. Absent ⟹ le driver la déclare
    # au démarrage et journalise l'identifiant obtenu ; le remettre dans la
    # déclaration fait REPRENDRE le même passage au lieu d'en ouvrir un second.
    # ⚠️ Il remplace le tag texte `payload["fleet"]` comme rattachement de
    # référence : un tag vit dans un JSON libre, un identifiant porte une clé
    # étrangère, se compte, et se refuse s'il désigne la flotte d'une autre org.
    fleet_id: Optional[int] = None
    filter: dict = field(default_factory=dict)   # ce qui est encore à traiter
    project: Optional[int] = None
    org: Optional[int] = None       # l'org de la MISSION (le namespace y vit)
    concurrency: int = 3
    ramp_seconds: int = 60
    volume: Optional[int] = None                 # None = épuisement de la file
    budget_tokens: Optional[int] = None
    max_steps: int = 40
    input: str = DEFAULT_INPUT
    # Les outils sans lesquels un job « done » est un job FAUX : leur PANNE
    # arrête la flotte (arrêt ANORMAL ⟹ relance auto quand ils reviennent).
    #
    # ⚠️ CE N'EST PAS une liste de droits. Ce que l'agent a le DROIT d'appeler se
    # gouverne en base, par org (activation et restriction de connecteur) — et
    # l'allowlist d'un run est `tools`, juste au-dessus. Faire de ce champ-ci une
    # seconde source de vérité pour « qui peut appeler quoi » créerait un doublon
    # dont l'un des deux finirait par mentir. Ici on ne dit pas ce qui est
    # PERMIS : on dit ce dont la panne rend le résultat FAUX.
    critical_tools: tuple = ()
    # Le plafond de jetons D'UNE LIGNE, descendu dans CHAQUE travail enfilé —
    # donc appliqué par l'agent lui-même, quel que soit le chemin qui l'a mis en
    # file. Absent ⟹ aucune borne par ligne : 65 571 jetons sur une seule ligne,
    # mesurés le 01/09.
    #
    # ⚠️ Ce n'est PAS le « rendement » (jetons par écriture produite, jugé sur une
    # fenêtre glissante) que le README a décrit du 27/08 au 02/09 : ce
    # mécanisme-là a été conçu, documenté sous les noms `jetons_par_ecriture_max`
    # et `rendement_fenetre`, puis remplacé par cette borne simple — sans que la
    # doc suive. Aucun des deux noms n'a jamais existé dans le code. Qui écrivait
    # sa déclaration depuis le README repartait donc SANS borne, en croyant en
    # avoir une.
    max_tokens_per_row: Optional[int] = None
    bilan_periode_s: int = _BILAN_PERIODE_S   # cadence du bilan intermédiaire
    source: str = ""              # la déclaration : le bilan JSON se pose à côté

    def __post_init__(self):
        if not self.name:
            raise ValueError(
                "nom de flotte vide : c'est le tag `fleet` de chaque job, ce "
                "par quoi on retrouve une campagne. Il vient du nom du fichier "
                "de déclaration (`campagne.yaml` ⟹ `campagne`) ou se déclare "
                "explicitement — il ne se devine pas.")


# Ce qu'une déclaration ne peut PAS porter : `name` vient du nom du fichier,
# `source` de son chemin, `fleet_id` est attribué par la base.
_NON_DECLARABLES = frozenset({"name", "source", "fleet_id"})

# ⚠️ DÉRIVÉ du dataclass, jamais réécrit à la main. La liste manuelle avait pris
# deux champs de retard — `critical_tools` et `max_tokens_per_row` — et
# l'avertissement criait donc sur des réglages qui MARCHENT. Un opérateur a failli
# retirer la ligne qui bornait sa dépense parce que le runner lui disait qu'elle
# était ignorée (02/09). **Un avertissement faux est pire que pas d'avertissement :
# il pousse au geste inverse du bon.** Un champ ajouté demain à `FleetSpec` est
# reconnu ici sans que personne y pense.
_CHAMPS = frozenset(f.name for f in fields(FleetSpec)) - _NON_DECLARABLES


def load_spec(path: str) -> FleetSpec:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    inconnus = sorted(set(raw) - _CHAMPS)
    if inconnus:
        # ⚠️ Dire ce qui EST reconnu à côté de ce qui ne l'est pas : sans le
        # voisinage, une faute de frappe (`max_token_per_row`) se lit comme une
        # fonctionnalité absente, et on cherche dans le code plutôt que dans le
        # fichier.
        logger.warning("déclaration : champs inconnus, ignorés : %s — les champs "
                       "reconnus sont : %s", ", ".join(inconnus),
                       ", ".join(sorted(_CHAMPS)))
    volume = raw.get("volume")
    if not isinstance(volume, int):
        volume = None                            # « épuisement » ou absent
    return FleetSpec(
        procedure=raw["procedure"],
        namespace=raw["namespace"],
        tools=tuple(raw.get("tools") or ()),
        filter=dict(raw.get("filter") or {}),
        project=raw.get("project"),
        org=raw.get("org"),
        concurrency=int(raw.get("concurrency") or 3),
        ramp_seconds=int(raw.get("ramp_seconds") or 60),
        volume=volume,
        budget_tokens=raw.get("budget_tokens"),
        max_steps=int(raw.get("max_steps") or 40),
        max_tokens_per_row=raw.get("max_tokens_per_row"),
        input=raw.get("input") or DEFAULT_INPUT,
        critical_tools=tuple(raw.get("critical_tools") or ()),
        bilan_periode_s=int(raw.get("bilan_periode_s") or _BILAN_PERIODE_S),
        source=path,
        name=os.path.splitext(os.path.basename(path))[0])


def spec_depuis_flotte(f: dict) -> FleetSpec:
    """Une spec construite depuis la flotte DÉCLARÉE en base.

    C'est le pendant de `load_spec` : la même chose, lue là où le dashboard et
    les agents la lisent aussi. **Un passage piloté par sa configuration en base
    est le même objet pour tout le monde** — piloté par un fichier posé à côté de
    l'exécutable, il n'existe que pour qui a accès à la machine.

    ⚠️ Ce qui n'a PAS d'équivalent en base reste au défaut du runner :
    `ramp_seconds`, `critical_tools` et la cadence du bilan sont des réglages
    d'EXÉCUTION locale, pas de la configuration déclarée du passage. Les inventer
    en base pour « tout avoir au même endroit » mélangerait ce qu'un opérateur
    déclare et ce qu'une machine règle.

    **Le critère qui tient la frontière dans le temps : si ce réglage change,
    quelqu'un doit-il le savoir ?** La cadence d'un bilan, non. La montée en
    charge, non plus — *à condition que la borne de DÉPENSE soit déclarée*, sinon
    une machine mal réglée dépasserait sans que la configuration ait bougé. Elle
    l'est (`max_rows`, `max_tokens`, `max_tokens_per_row` vivent dans la flotte).

    ⚠️ Et `critical_tools` reste local parce qu'il désigne *ce dont la panne rend
    un résultat FAUX*, pas *ce que l'agent a le droit d'appeler* — cette
    seconde question a déjà son domicile en base (activation de connecteur par
    org), et deux domiciles pour une même règle finissent par diverger.
    """
    manquants = [c for c in ("id", "procedure") if not f.get(c)]
    if manquants:
        raise ValueError(
            f"flotte illisible — champs absents : {', '.join(manquants)}. "
            "Une flotte se déclare avant d'être pilotée.")
    return FleetSpec(
        procedure=f["procedure"],
        namespace=f.get("namespace") or "",
        tools=tuple(f.get("tools") or ()),
        filter=dict(f.get("row_filter") or {}),
        project=f.get("project_id"),
        org=f.get("org_id"),
        concurrency=int(f.get("workers") or 3),
        volume=f.get("max_rows"),
        budget_tokens=f.get("max_tokens"),
        max_steps=int(f.get("max_steps") or 40),
        max_tokens_per_row=f.get("max_tokens_per_row"),
        input=f.get("input") or DEFAULT_INPUT,
        # La flotte EXISTE déjà : on la reprend, on n'en déclare pas une seconde.
        fleet_id=int(f["id"]),
        source=f"flotte #{f['id']}",
        name=f.get("label") or f"flotte-{f['id']}")


def _payload(spec: FleetSpec) -> dict:
    import json as _json
    # Interpolation PRUDENTE (replace, jamais .format : un input custom peut
    # porter des accolades qui ne sont pas des placeholders).
    message = (spec.input
               .replace("{namespace}", spec.namespace)
               .replace("{filter}", _json.dumps(spec.filter, ensure_ascii=False)))
    return {"procedure": spec.procedure, "tools": list(spec.tools),
            "project_id": spec.project, "org_id": spec.org,
            "namespace": spec.namespace,
            "fleet": spec.name,
            "max_steps": spec.max_steps,
            # ⚠️ La borne DESCEND avec le travail. Elle vivait sur la flotte, où
            # seul un ordonnanceur savait la lire — donc personne dès qu'un
            # passage tourne sans lui. Portée par le travail, elle s'applique
            # quel que soit le chemin qui l'a enfilé.
            "max_tokens": spec.max_tokens_per_row,
            "input": message,
            "label": f"flotte {spec.namespace} — {spec.procedure}"}


# La borne « outil critique en échec » (27/08). Un job dont les outils
# échouent conclut quand même « done » — bornes, budget et heartbeat disaient
# « ça tourne » pendant que la flotte marquait 2 395 fiches « enrichies » sans
# une recherche web réussie (Serper à sec, 4 jours). La mesure vient du
# JOURNAL des appels (fenêtre glissante de 15 min) : à la relance après panne,
# aucun appel récent ⟹ pas de verdict, on laisse partir des jobs qui
# ré-alimentent la mesure — sans quoi les vieux échecs re-borneraient à vide.
_SANTE_FENETRE_MIN = 15
_SANTE_MIN_APPELS = 12
_SANTE_TAUX_KO = 0.9
_SANTE_PERIODE_S = 60


def _outil_critique_en_panne(spec, backend, clock) -> "str | None":
    """Le motif de borne si un outil critique échoue massivement, sinon None.
    Sondé au plus toutes les _SANTE_PERIODE_S ; toute erreur de sonde = pas de
    verdict (une sonde en panne n'arrête pas une campagne saine)."""
    if not spec.critical_tools or spec.org is None:
        return None
    now = clock()
    if now - _outil_critique_en_panne.dernier.get("t", -1e9) < _SANTE_PERIODE_S:
        return _outil_critique_en_panne.dernier.get("verdict")
    verdict = None
    for outil in spec.critical_tools:
        try:
            n, ko = backend.tool_health(spec.org, outil, minutes=_SANTE_FENETRE_MIN)
        except Exception as e:  # noqa: BLE001 — la sonde ne tue pas la flotte
            logger.warning("santé de %s illisible : %s", outil, e)
            continue
        if n >= _SANTE_MIN_APPELS and ko / n >= _SANTE_TAUX_KO:
            verdict = (f"outil critique `{outil}` en échec ({ko}/{n} appels "
                       f"sur {_SANTE_FENETRE_MIN} min) — chaque job « done » "
                       "serait faux")
            break
    _outil_critique_en_panne.dernier = {"t": now, "verdict": verdict}
    return verdict


_outil_critique_en_panne.dernier = {}


# ⚠️ Ce qui protège une campagne d'un prix payé sans sortie, ce sont DEUX bornes
# distinctes, et aucune n'est un « rendement » : les FAUX DÉPARTS EN SÉRIE
# (ci-dessus) attrapent « ça tourne à vide », et `max_tokens_per_row` borne la
# ligne elle-même. La borne de rendement — coût rapporté aux écritures sur une
# fenêtre glissante — a été conçue le 27/08 et n'a jamais été écrite ; le
# commentaire qui l'annonçait ici a survécu à sa propre annulation jusqu'au
# 02/09. **Un commentaire qui décrit un mécanisme absent le rend introuvable :
# on cherche le bug dans le code au lieu de constater qu'il n'y a pas de code.**


@dataclass
class FleetBilan:
    done: int = 0
    failed: int = 0
    usage_tokens: int = 0
    lignes_initiales: int = 0
    lignes_restantes: int = 0
    arret: str = ""


def run_fleet(spec: FleetSpec, backend: Backend, *,
              sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic,
              poll_s: int = _POLL_S) -> FleetBilan:
    """La boucle d'ordonnancement. `sleep`/`clock` injectables : les bornes se
    PROUVENT en test, elles ne s'affirment pas — c'est ce qui protège le compte
    pendant une campagne sans surveillance."""
    # La flotte se DÉCLARE en base avant d'enfiler quoi que ce soit : sans
    # identifiant, chaque job part orphelin et `runner.fleets op=state` répond
    # « aucun travail rattaché » pour un passage qui tourne. ⚠️ Une déclaration
    # qui échoue n'arrête PAS le passage — le rattachement sert à LIRE, il ne
    # conditionne pas le travail. Perdre l'observabilité est un moindre mal
    # devant une campagne qui refuse de partir.
    fleet_id = spec.fleet_id
    if fleet_id is None:
        try:
            f = backend.declarer_flotte(
                label=spec.name, procedure=spec.procedure, tools=list(spec.tools),
                namespace=spec.namespace, row_filter=spec.filter or None,
                project_id=spec.project, input=spec.input,
                max_steps=spec.max_steps, workers=spec.concurrency,
                max_rows=spec.volume, max_tokens=spec.budget_tokens,
                max_tokens_per_row=spec.max_tokens_per_row)
            fleet_id = f.get("id")
            logger.info("flotte déclarée en base : id=%s — remettre `fleet_id: %s` "
                        "dans la déclaration pour REPRENDRE ce passage",
                        fleet_id, fleet_id)
        except BackendError as e:
            logger.warning("flotte non déclarée (%s) — les jobs partiront sans "
                           "rattachement, `op=state` restera muet sur ce passage", e)
    # ⚠️ PRENDRE la flotte : `armed` → `running`. C'est l'ordonnanceur qui pose ce
    # FAIT, jamais l'opérateur — `armed` veut dire « on a demandé », `running`
    # veut dire « quelqu'un l'a prise et donne signe ». Un refus n'est pas une
    # erreur à retenter : un autre ordonnanceur l'a prise, ou elle n'était pas
    # armée. Partir quand même DOUBLERAIT le passage.
    if fleet_id is not None:
        try:
            backend.prendre_flotte(fleet_id)
            logger.info("flotte #%s prise — le passage est en cours", fleet_id)
        except BackendError as e:
            logger.warning("flotte #%s non prise (%s) — le passage tourne quand "
                           "même, mais son état ne dira pas « en cours »",
                           fleet_id, e)
    bilan = FleetBilan(lignes_initiales=backend.count_rows(spec.namespace,
                                                           filter=spec.filter,
                                                           org=spec.org))
    en_vol: set[int] = set()
    dernier_depart: Optional[float] = None
    failed_consecutifs = 0
    # L'ordre d'arrêt, LU sur la plateforme — jamais un état purement local :
    # c'est un opérateur qui le pose, depuis un écran ou une conversation.
    arret_demande = False
    # ⚠️ Initialise ICI : un compteur qui n'existe que sur un chemin et qu'on
    # lit sur tous leve au premier passage qui l'emprunte.
    pleine_charge_atteinte = False
    departs = 0
    # (jetons, écritures) des derniers jobs conclus — la matière du rendement.
    # Les erreurs BACKEND consécutives du driver lui-même (count/enqueue) : un
    # 502 isolé sur l'enfilement a TUÉ un vol entier (lot C, 16/08 — la rafale
    # #352 ; 30 min de flotte figée avant détection humaine). Le driver tolère
    # et retente ; seule une panne DENSE et continue l'arrête, PROPREMENT, avec
    # un bilan — jamais un traceback. Les workers en vol, eux, continuent.
    erreurs_backend = 0
    # La matière du BILAN : les jobs conclus (statut + résultat déclaré, jamais
    # leur payload). « Aboutie » se mesure au TABLEAU (départ − restantes), pas
    # au nombre de jobs « done » — le relevé de départ est déjà en `bilan`.
    conclus: dict[int, dict] = {}
    t0 = dernier_bilan = clock()
    logger.info("flotte %s : %d ligne(s) à traiter, concurrence %d, rampe %ds",
                spec.namespace, bilan.lignes_initiales, spec.concurrency,
                spec.ramp_seconds)

    try:
        while True:
            # 0. « Dois-je m'arrêter ? » — la question qui rend `op=stop` RÉEL.
            #
            # ⚠️ Sans cette lecture, l'arrêt demandé resterait une écriture que
            # personne ne lit : l'écran annoncerait `stopping` pour toujours, et
            # le passage continuerait de réserver, d'appeler, de DÉPENSER.
            # C'est exactement pour ça que l'état s'appelle `stopping` et non
            # `stopped` — la plateforme ne ment pas sur ce qu'elle a fait.
            #
            # ⚠️ Et l'arrêt est GRACIEUX, comme celui des agents : on cesse
            # d'enfiler, on laisse finir ce qui est en vol, PUIS on accuse.
            # Couper au milieu laisserait des lignes sous bail et ferait repayer
            # les jobs — le remède serait pire que le mal.
            if fleet_id is not None and not arret_demande:
                try:
                    if backend.battre_flotte(fleet_id):
                        arret_demande = True
                        bilan.arret = "arrêt demandé"
                        logger.info("arrêt DEMANDÉ pour la flotte #%s — plus aucun "
                                    "job enfilé, les %d en vol vont à leur terme",
                                    fleet_id, len(en_vol))
                except BackendError as e:
                    # Une plateforme injoignable n'est pas un ordre d'arrêt : la
                    # confondre éteindrait la flotte à chaque bascule.
                    logger.warning("battement flotte #%s : %s", fleet_id, e)

            # 1. Moissonner les jobs conclus — leur résultat DÉCLARÉ porte le coût.
            for jid in sorted(en_vol):
                try:
                    job = backend.get_job(jid)
                except BackendError as e:
                    logger.warning("get_job %s : %s", jid, e)
                    continue
                st = job.get("status")
                if st not in ("done", "failed"):
                    continue
                en_vol.discard(jid)
                resultat = job.get("result") or {}
                conclus[jid] = {"status": st, "result": resultat}
                jetons_du_job = int(resultat.get("usage_tokens") or 0)
                bilan.usage_tokens += jetons_du_job
                if st == "done":
                    bilan.done += 1
                    failed_consecutifs = 0
                    # Le claim à vide et le faux départ sont DÉCLARÉS par le
                    # worker — le seul à avoir vu les appels ET leurs sorties.
                    # Un résultat qui ne les porte pas vient d'un worker trop
                    # ancien : on lève, on ne redevine pas en silence.
                    # ⚠️ L'ordonnanceur ne juge plus ce que l'agent a produit.
                    # Il portait ici : le claim à vide, le faux départ, le
                    # relâchement de ligne, le rendement en écritures par jeton.
                    # Tout cela suppose de savoir ce qu'écrire veut dire — ce
                    # n'est pas le sujet d'un exécuteur d'agents.
                    #
                    # Restent les bornes qui ne regardent que l'exécution :
                    # volume, budget de jetons, file vide, échecs consécutifs.
                else:
                    bilan.failed += 1
                    failed_consecutifs += 1
                    logger.warning("job %s FAILED : %s", jid, job.get("last_error"))

            try:
                restantes = backend.count_rows(spec.namespace, filter=spec.filter,
                                               org=spec.org)
            except BackendError as e:
                erreurs_backend += 1
                if erreurs_backend >= _MAX_ERREURS_BACKEND:
                    bilan.arret = (f"backend indisponible ({erreurs_backend} erreurs "
                                   "consécutives du driver)")
                    logger.warning("flotte arrêtée : %s — %d done, %d failed",
                                   bilan.arret, bilan.done, bilan.failed)
                    return bilan
                logger.warning("count_rows toléré (%d/%d) : %s",
                               erreurs_backend, _MAX_ERREURS_BACKEND, e)
                sleep(poll_s)
                continue
            erreurs_backend = 0
            bilan.lignes_restantes = restantes
            traitees = max(0, bilan.lignes_initiales - restantes)

            # Le bilan INTERMÉDIAIRE : une campagne se pilote PENDANT qu'elle
            # tourne, pas après. Tout le calcul vit dans bilan.py.
            if clock() - dernier_bilan >= spec.bilan_periode_s:
                dernier_bilan = clock()
                ecrire_bilan(spec, backend, conclus, secondes=clock() - t0,
                             lignes_initiales=bilan.lignes_initiales)

            # 2. Les bornes — chacune arrête l'ENFILEMENT ; les jobs en vol finissent.
            # ⚠️ L'ordre d'arrêt EST une borne, et il passe par le même chemin
            # qu'elles : cesser d'enfiler, laisser finir ce qui est en vol,
            # conclure. Lui inventer une sortie à part ferait deux façons de
            # s'arrêter, dont une seule serait éprouvée.
            borne = "arrêt demandé" if arret_demande else None
            if borne is None and spec.budget_tokens is not None and bilan.usage_tokens >= spec.budget_tokens:
                borne = f"budget atteint ({bilan.usage_tokens} ≥ {spec.budget_tokens} jetons)"
            elif borne is None and spec.volume is not None and traitees >= spec.volume:
                borne = f"volume atteint ({traitees} ≥ {spec.volume} lignes)"
            elif borne is None and failed_consecutifs >= _MAX_FAILED_CONSECUTIFS:
                borne = (f"{failed_consecutifs} échecs consécutifs — enfiler encore, "
                         "c'est payer pour re-crasher")
            elif (panne := _outil_critique_en_panne(spec, backend, clock)):
                borne = panne
            # ⚠️ Trois bornes ont été retirées ici — relâchements ratés, faux
            # départs, rendement effondré. Les trois jugeaient ce que l'agent
            # avait PRODUIT : avoir réservé sans écrire, ne pas avoir rendu sa
            # ligne, écrire trop peu pour ce qu'il coûte. Un exécuteur d'agents
            # ne sait pas ce qu'écrire veut dire, et ne rien écrire est parfois
            # la bonne réponse.
            elif restantes == 0:
                borne = "file vide"

            if borne:
                if en_vol:
                    logger.info("borne (%s) : %d job(s) en vol finissent, plus un "
                                "n'est enfilé", borne, len(en_vol))
                    sleep(poll_s)
                    continue
                bilan.arret = borne
                logger.info("flotte arrêtée : %s — %d done, %d failed, %d jetons",
                            borne, bilan.done, bilan.failed, bilan.usage_tokens)
                # ⚠️ ACCUSER l'arrêt — le seul geste qui pose le FAIT `stopped`.
                # Sans lui, un arrêt demandé resterait `stopping` pour toujours,
                # ce qui est précisément le symptôme d'un ordonnanceur MORT : on
                # le fabriquerait en étant vivant, et le diagnostic ne vaudrait
                # plus rien.
                if fleet_id is not None and arret_demande:
                    try:
                        backend.accuser_arret(fleet_id)
                    except BackendError as e:
                        logger.warning("accusé d'arrêt flotte #%s : %s — l'état "
                                       "restera `stopping`, à corriger à la main",
                                       fleet_id, e)
                return bilan

            # 3. Enfiler, sous la rampe — un départ au plus par tour de boucle.
            #
            # ⚠️ La rampe est une MONTÉE EN CHARGE, pas un débit permanent.
            # Elle s'appliquait à chaque enfilement, pour toujours : un travail
            # au plus toutes les 60 s, quelle que soit la vitesse des agents.
            # Mesuré le 29/08 : travaux de 107 s, enfilements toutes les 64 s —
            # **c'est l'ordonnanceur qui donnait le tempo, pas les agents**, et
            # le débit plafonnait là où trois agents auraient pu faire trois fois
            # mieux. Sur un lot de 1 136 lignes, c'est des dizaines d'heures.
            #
            # Elle ne s'applique donc que TANT QU'ON MONTE — jusqu'à ce que la
            # concurrence visée soit atteinte une première fois. Ensuite, un
            # travail conclu libère immédiatement sa place : c'est la
            # concurrence qui borne, et elle seule.
            #
            # ⚠️ On ne SUPPRIME pas la rampe : démarrer trois conversations à la
            # même seconde a déjà gelé la plateforme. Elle protège le départ,
            # elle n'a jamais eu à brider la croisière.
            # ⚠️ La rampe se compte en DÉPARTS, pas en pleine charge atteinte.
            #
            # La première version se désactivait quand `len(en_vol)` atteignait la
            # concurrence — et c'est la rampe elle-même qui empêchait d'y arriver :
            # elle n'enfilait qu'un travail par minute, les travaux se concluaient
            # entre-temps, la file restait à deux en vol sur trois, et la
            # désactivation n'arrivait jamais. Un cercle vicieux, mesuré le 29/08 :
            # enfilements toujours à 62 s après le correctif censé les libérer.
            #
            # La rampe couvre donc les `concurrency` PREMIERS départs — le temps
            # de la montée, littéralement — et cesse ensuite.
            monte = departs < spec.concurrency
            pleine_charge_atteinte = not monte
            if (len(en_vol) < spec.concurrency
                    and (dernier_depart is None
                         or not monte
                         or clock() - dernier_depart >= spec.ramp_seconds)):
                try:
                    jid = backend.enqueue("start", _payload(spec),
                                          fleet_id=fleet_id)
                    departs += 1
                except BackendError as e:
                    erreurs_backend += 1
                    if erreurs_backend >= _MAX_ERREURS_BACKEND:
                        bilan.arret = (f"backend indisponible ({erreurs_backend} "
                                       "erreurs consécutives du driver)")
                        logger.warning("flotte arrêtée : %s — %d done, %d failed",
                                       bilan.arret, bilan.done, bilan.failed)
                        return bilan
                    logger.warning("enqueue toléré (%d/%d) : %s",
                                   erreurs_backend, _MAX_ERREURS_BACKEND, e)
                    sleep(poll_s)
                    continue
                erreurs_backend = 0
                en_vol.add(jid)
                dernier_depart = clock()
                logger.info("job %s enfilé (%d/%d en vol)", jid, len(en_vol),
                            spec.concurrency)

            sleep(poll_s)

    finally:
        # Le bilan de FIN tombe quelle que soit la borne — panne et interruption
        # comprises : un pilotage qui n'existe que sur une sortie propre n'existe
        # pas les jours où il sert.
        ecrire_bilan(spec, backend, conclus, secondes=clock() - t0,
                     lignes_initiales=bilan.lignes_initiales,
                     arret=bilan.arret or "interrompu")


# Les motifs d'arrêt NORMAUX — la campagne a fini son travail ou sa borne
# planifiée. Tout AUTRE motif (échecs consécutifs, backend indisponible) est une
# PANNE : le process sort en échec pour que systemd (Restart=on-failure +
# RestartSec long) relance la campagne tout seul quand la panne passe — vécu le
# 20/08 : un 402 Mistral (crédit épuisé) a arrêté proprement la flotte à 604
# fiches, et 26 h ont passé avant qu'un humain ne la relance. Le coût d'une
# relance sous panne persistante est quasi nul (les jobs 402 ne consomment pas).
_ARRETS_NORMAUX = ("file vide", "volume atteint", "budget atteint")


def main() -> None:
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if len(sys.argv) != 2:
        raise SystemExit(
            "usage : python -m oto_runner.fleet <flotte.yaml>\n"
            "        python -m oto_runner.fleet #<id>     (flotte DÉCLARÉE en base)")
    arg = sys.argv[1]
    backend = Backend()
    if arg.startswith("#"):
        # Piloté par la configuration DÉCLARÉE : la même que celle que le
        # dashboard affiche et qu'un agent peut lire. Le fichier YAML reste un
        # moyen de déclarer, pas la seule façon d'exister.
        spec = spec_depuis_flotte(backend.lire_flotte(int(arg[1:])))
        logger.info("flotte #%s chargée depuis la base : %s", arg[1:], spec.name)
    else:
        spec = load_spec(arg)
    bilan = run_fleet(spec, backend)
    logger.info("bilan : %s", bilan)
    if not any(bilan.arret.startswith(m) for m in _ARRETS_NORMAUX):
        sys.exit(1)   # panne → systemd relance (jamais sur une fin normale)


if __name__ == "__main__":
    main()
