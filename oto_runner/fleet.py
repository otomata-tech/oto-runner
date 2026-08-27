"""La FLOTTE : un ordonnanceur MINCE au-dessus de la file de jobs.

Un client ordinaire de l'API jobs — aucun kind serveur, aucune boucle d'agent
ici : la boucle vit dans le worker, le claim de ligne vit dans la PROCÉDURE.
Le driver ne fait que trois choses : enfiler des jobs `start` avec une rampe,
maintenir la concurrence déclarée, et s'arrêter proprement sur l'une des
bornes — file vide, volume, budget de jetons, ou trop d'échecs consécutifs.

La déclaration est un YAML par flotte (cf. `docs/fleet-example.yaml`) — jamais
un secret dedans : le jeton et la clé de modèle viennent de l'environnement.
⚠️ Le worker est un pool HOMOGÈNE : son modèle vient de SON environnement
(`OTO_RUNNER_MODEL`), pas de la déclaration — un champ `model` dans le YAML
est logué puis ignoré, pour que la divergence soit VISIBLE, jamais silencieuse.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import yaml

from .backend import Backend, BackendError

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
_MAX_FAUX_DEPARTS_CONSECUTIFS = 5
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

# Champs de déclaration reconnus ; le reste est logué puis ignoré (une flotte
# écrite pour un autre harnais reste lisible ici, sans mensonge silencieux).
_CHAMPS = {"procedure", "namespace", "filter", "project", "org", "tools", "concurrency",
           "ramp_seconds", "volume", "budget_tokens", "max_steps", "input",
           "critical_tools"}


@dataclass(frozen=True)
class FleetSpec:
    procedure: str
    namespace: str
    tools: tuple
    filter: dict = field(default_factory=dict)   # ce qui est encore à traiter
    project: Optional[int] = None
    org: Optional[int] = None       # l'org de la MISSION (le namespace y vit)
    concurrency: int = 3
    ramp_seconds: int = 60
    volume: Optional[int] = None                 # None = épuisement de la file
    budget_tokens: Optional[int] = None
    max_steps: int = 40
    input: str = DEFAULT_INPUT
    # Les outils sans lesquels un job « done » est un job FAUX : leur panne
    # arrête la flotte (arrêt ANORMAL ⟹ relance auto quand ils reviennent).
    critical_tools: tuple = ()


def load_spec(path: str) -> FleetSpec:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    inconnus = sorted(set(raw) - _CHAMPS)
    if inconnus:
        logger.warning("déclaration : champs ignorés par le runner : %s "
                       "(le modèle vient de l'env du WORKER, OTO_RUNNER_MODEL)",
                       ", ".join(inconnus))
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
        input=raw.get("input") or DEFAULT_INPUT,
        critical_tools=tuple(raw.get("critical_tools") or ()))


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
            "max_steps": spec.max_steps,
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
    bilan = FleetBilan(lignes_initiales=backend.count_rows(spec.namespace,
                                                           filter=spec.filter,
                                                           org=spec.org))
    en_vol: set[int] = set()
    dernier_depart: Optional[float] = None
    failed_consecutifs = 0
    faux_departs_consecutifs = 0
    # Les erreurs BACKEND consécutives du driver lui-même (count/enqueue) : un
    # 502 isolé sur l'enfilement a TUÉ un vol entier (lot C, 16/08 — la rafale
    # #352 ; 30 min de flotte figée avant détection humaine). Le driver tolère
    # et retente ; seule une panne DENSE et continue l'arrête, PROPREMENT, avec
    # un bilan — jamais un traceback. Les workers en vol, eux, continuent.
    erreurs_backend = 0
    logger.info("flotte %s : %d ligne(s) à traiter, concurrence %d, rampe %ds",
                spec.namespace, bilan.lignes_initiales, spec.concurrency,
                spec.ramp_seconds)

    while True:
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
            bilan.usage_tokens += int((job.get("result") or {}).get("usage_tokens") or 0)
            if st == "done":
                bilan.done += 1
                failed_consecutifs = 0
                tc = (job.get("result") or {}).get("tool_counts") or {}
                claims = sum(v for k, v in tc.items() if k.endswith("data_claim_next"))
                writes = sum(v for k, v in tc.items() if k.endswith("data_write"))
                if claims and not writes:
                    faux_departs_consecutifs += 1
                    logger.warning("job %s : faux départ (%d claim, 0 write) — %d d'affilée",
                                   jid, claims, faux_departs_consecutifs)
                else:
                    faux_departs_consecutifs = 0
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

        # 2. Les bornes — chacune arrête l'ENFILEMENT ; les jobs en vol finissent.
        borne = None
        if spec.budget_tokens is not None and bilan.usage_tokens >= spec.budget_tokens:
            borne = f"budget atteint ({bilan.usage_tokens} ≥ {spec.budget_tokens} jetons)"
        elif spec.volume is not None and traitees >= spec.volume:
            borne = f"volume atteint ({traitees} ≥ {spec.volume} lignes)"
        elif failed_consecutifs >= _MAX_FAILED_CONSECUTIFS:
            borne = (f"{failed_consecutifs} échecs consécutifs — enfiler encore, "
                     "c'est payer pour re-crasher")
        elif (panne := _outil_critique_en_panne(spec, backend, clock)):
            borne = panne
        elif faux_departs_consecutifs >= _MAX_FAUX_DEPARTS_CONSECUTIFS:
            borne = (f"{faux_departs_consecutifs} faux départs consécutifs (réservation "
                     "sans écriture) — la flotte tourne à vide")
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
            return bilan

        # 3. Enfiler, sous la rampe — un départ au plus par tour de boucle.
        if (len(en_vol) < spec.concurrency
                and (dernier_depart is None
                     or clock() - dernier_depart >= spec.ramp_seconds)):
            try:
                jid = backend.enqueue("start", _payload(spec))
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
        raise SystemExit("usage : python -m oto_runner.fleet <flotte.yaml>")
    spec = load_spec(sys.argv[1])
    bilan = run_fleet(spec, Backend())
    logger.info("bilan : %s", bilan)
    if not any(bilan.arret.startswith(m) for m in _ARRETS_NORMAUX):
        sys.exit(1)   # panne → systemd relance (jamais sur une fin normale)


if __name__ == "__main__":
    main()
