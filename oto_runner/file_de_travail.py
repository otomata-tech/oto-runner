"""Ce qu'un travail attend de sa FILE — trois verbes, et deux façons de les servir.

Un travail dit trois choses à la file qui l'a réservé : « ce run est le mien »
(`bind_run`), « je suis encore là » (`extend`, le battement qui prolonge le
bail), « j'ai fini, voici mon résultat » (`complete`). Tout le reste — le run, le
fil, les outils, le journal — ne passe pas par la file.

Deux implémentations, nommées :

- **la file SERVEUR** : `backend.Backend`, dont les trois verbes sont des
  `POST /api/me/runner/jobs`. C'est la production : la flotte enfile, les workers
  réservent et concluent ;
- **aucune file** : `SansFile`, le mode direct (`python -m oto_runner.direct`).
  Aucun job n'existe côté serveur ; ce que le travail aurait dit à sa file est
  journalisé, et son résultat conservé pour le bilan de fin.

⚠️ Ces trois verbes sont le SEUL point de variation entre les deux modes. Le
corps d'exécution est le même (`worker._traiter`, `worker._un_travail`), le
travail construit est le même (`declaration.payload`), le journal est le même.
Le produit a déjà payé une divergence de deux chemins d'écriture : un banc qui
mesurerait un chemin différent de la production sous le même nom serait un
instrument menteur de plus. Si une quatrième chose doit varier, la couture est au
mauvais endroit — on le remonte, on ne recopie pas.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger("oto_runner")


@runtime_checkable
class FileDeTravail(Protocol):
    """Les trois verbes. `job_id` est l'identifiant du travail tel que la file
    l'a donné (un entier serveur, ou `direct-<horodatage>-<n>` sans file)."""

    def bind_run(self, job_id, run_id: str) -> None: ...

    def extend(self, job_id, lease_seconds: int = 600) -> None: ...

    def complete(self, job_id, ok: bool, error: Optional[str] = None,
                 run_id: Optional[str] = None,
                 result: Optional[dict] = None) -> dict: ...


class SansFile:
    """« Aucune file » : le travail n'a été réservé nulle part.

    Chaque verbe dit au journal ce qu'il AURAIT fait, et `complete` conserve le
    résultat déclaré — la matière du bilan, exactement ce que l'ordonnanceur de
    flotte lit dans `get_job` quand un worker a conclu."""

    def __init__(self) -> None:
        self.conclus: dict = {}
        self._verrou = threading.Lock()

    def bind_run(self, job_id, run_id: str) -> None:
        logger.info("aucune file : le run %s aurait été lié au travail %s", run_id, job_id)

    def extend(self, job_id, lease_seconds: int = 600) -> None:
        logger.debug("aucune file : aucun bail à prolonger pour %s (%ss)",
                     job_id, lease_seconds)

    def complete(self, job_id, ok: bool, error: Optional[str] = None,
                 run_id: Optional[str] = None,
                 result: Optional[dict] = None) -> dict:
        conclu = {"status": "done" if ok else "failed", "result": dict(result or {}),
                  "run_id": run_id, "error": error}
        with self._verrou:
            self.conclus[job_id] = conclu
        logger.info("aucune file : travail %s conclu — %s%s", job_id, conclu["status"],
                    f" : {error}" if error else "")
        return {"ok": True, "aucune_file": True, "job_id": job_id}
