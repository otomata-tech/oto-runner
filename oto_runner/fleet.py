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
_MAX_FAILED_CONSECUTIFS = 3   # au-delà, enfiler encore = payer pour re-crasher
DEFAULT_INPUT = ("Exécute la procédure sur la file de travail : traite les lignes "
                 "une par une, autant que ton budget de tours le permet, puis "
                 "conclus par un bilan bref (lignes traitées, difficultés).")

# Champs de déclaration reconnus ; le reste est logué puis ignoré (une flotte
# écrite pour un autre harnais reste lisible ici, sans mensonge silencieux).
_CHAMPS = {"procedure", "namespace", "filter", "project", "org", "tools", "concurrency",
           "ramp_seconds", "volume", "budget_tokens", "max_steps", "input"}


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
        input=raw.get("input") or DEFAULT_INPUT)


def _payload(spec: FleetSpec) -> dict:
    return {"procedure": spec.procedure, "tools": list(spec.tools),
            "project_id": spec.project, "org_id": spec.org,
            "max_steps": spec.max_steps,
            "input": spec.input,
            "label": f"flotte {spec.namespace} — {spec.procedure}"}


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
            else:
                bilan.failed += 1
                failed_consecutifs += 1
                logger.warning("job %s FAILED : %s", jid, job.get("last_error"))

        restantes = backend.count_rows(spec.namespace, filter=spec.filter,
                                       org=spec.org)
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
            jid = backend.enqueue("start", _payload(spec))
            en_vol.add(jid)
            dernier_depart = clock()
            logger.info("job %s enfilé (%d/%d en vol)", jid, len(en_vol),
                        spec.concurrency)

        sleep(poll_s)


def main() -> None:
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if len(sys.argv) != 2:
        raise SystemExit("usage : python -m oto_runner.fleet <flotte.yaml>")
    spec = load_spec(sys.argv[1])
    bilan = run_fleet(spec, Backend())
    logger.info("bilan : %s", bilan)


if __name__ == "__main__":
    main()
