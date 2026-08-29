"""Les deux contrats REST du worker : le FIL d'un run, et la FILE de jobs.

C'est tout ce que le worker connaît du backend en dehors de la face MCP — deux
familles d'endpoints op-aware, un seul jeton. Pas d'accès base, pas d'import
oto : si ces contrats tiennent, le worker est remplaçable (ADR 0064-D1).
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

import requests

from .deadline import DeadlineExceeded, get_with_deadline, post_with_deadline

_TIMEOUT = (10, 60)


def _params_filtre(filter: Optional[dict], limit: int) -> dict:
    """La grammaire riche du datastore : une valeur peut être un opérateur
    ({"in": [...]}) ou une LISTE (raccourci pour `in`) — c'est ce qui permet de
    borner une flotte à un LOT NOMMÉ de lignes (une comparaison entre deux
    modèles se fait sur des populations exactes, jamais sur des plages
    approximatives)."""
    params: dict = {"limit": limit}
    if not filter:
        return params
    clauses = []
    for k, v in filter.items():
        if isinstance(v, dict) and len(v) == 1:
            op, val = next(iter(v.items()))
            clauses.append({"field": k, "op": op, "value": val})
        elif isinstance(v, (list, tuple)):
            clauses.append({"field": k, "op": "in", "value": list(v)})
        else:
            clauses.append({"field": k, "op": "eq", "value": v})
    params["filters"] = json.dumps(clauses)
    return params


class BackendError(RuntimeError):
    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class Backend:
    def __init__(self, base: Optional[str] = None, token: Optional[str] = None):
        self.base = (base or os.environ.get("OTO_BASE", "https://mcp.oto.cx")).rstrip("/")
        self.token = (token or os.environ.get("OTO_TOKEN", "")).strip()
        if not self.token:
            raise BackendError("OTO_TOKEN absent de l'environnement du worker")

    def _reseau(self, chemin: str, fn, **kw):
        """TOUTE erreur de transport devient une BackendError (status=None).

        Un ReadTimeout de requests n'en était pas une : il traversait la
        tolérance du driver (qui n'attrape que BackendError) et l'a tué en plein
        vol le 27/08 à 08:26 — 5 jours de campagne nominale, puis un traceback
        pour un backend lent de 60 s. La conversion vit ICI, à la source : le
        driver, le worker (appose, extend) et la flotte en héritent d'un coup."""
        try:
            return fn(self.base + chemin, **kw)
        except (requests.RequestException, DeadlineExceeded) as e:
            raise BackendError(f"{chemin} → réseau : {type(e).__name__} "
                               f"{str(e)[:200]}", status=None)

    def _post(self, chemin: str, corps: dict) -> dict:
        r = self._reseau(chemin, post_with_deadline, json=corps, timeout=_TIMEOUT,
                         headers={"Authorization": f"Bearer {self.token}"},
                         wall_s=120)
        if r.status_code >= 400:
            try:
                detail = r.json().get("message") or r.json().get("error") or r.text
            except Exception:  # noqa: BLE001
                detail = r.text
            raise BackendError(f"{chemin} → {r.status_code} : {str(detail)[:300]}",
                               status=r.status_code)
        return r.json() if r.content else {}

    def _patch(self, chemin: str, corps: dict,
               org: Optional[int] = None) -> dict:
        entetes = {"Authorization": f"Bearer {self.token}"}
        if org is not None:
            entetes["X-Oto-Org"] = str(org)
        r = self._reseau(chemin, lambda u, **kw: requests.patch(u, **kw),
                         json=corps, timeout=_TIMEOUT, headers=entetes)
        if r.status_code >= 400:
            raise BackendError(f"{chemin} → {r.status_code} : {r.text[:300]}",
                               status=r.status_code)
        return r.json() if r.content else {}

    def _get(self, chemin: str, params: dict,
             org: Optional[int] = None) -> dict:
        entetes = {"Authorization": f"Bearer {self.token}"}
        if org is not None:
            # Le namespace d'une flotte vit dans l'org de la MISSION, pas dans
            # l'org maison du jeton — la consultation REST se scope par en-tête.
            entetes["X-Oto-Org"] = str(org)
        r = self._reseau(chemin, get_with_deadline, params=params, timeout=_TIMEOUT,
                         headers=entetes, wall_s=120)
        if r.status_code >= 400:
            raise BackendError(f"{chemin} → {r.status_code} : {r.text[:300]}",
                               status=r.status_code)
        return r.json() if r.content else {}

    # ── la santé d'un OUTIL, lue au journal des appels (la source de vérité) ──
    def tool_health(self, org: int, tool: str, *, minutes: int = 15,
                    limit: int = 20) -> tuple[int, int]:
        """(appels, échecs) de `tool` dans l'org sur les `minutes` dernières.

        Lu au journal backend des appels (monitoring d'org), jamais déduit du
        résultat déclaré des jobs : en Conversations les exécutions d'outils
        ne portent pas leur statut, et un agent dont les outils échouent
        conclut « done » — 2 395 fiches « enrichies » sans une recherche web
        réussie (Serper à sec du 23 au 27/08), aucune borne ne l'a vu."""
        from datetime import datetime, timedelta, timezone
        d = self._get(f"/api/orgs/{org}/monitoring/calls",
                      {"tool": tool, "limit": limit})
        seuil = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        n = ko = 0
        for c in d.get("calls") or []:
            quand = str(c.get("called_at") or "")[:19].replace("T", " ")
            try:
                t = datetime.strptime(quand, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if t < seuil:
                continue
            n += 1
            if not c.get("ok"):
                ko += 1
        return n, ko

    # ── la file de jobs (runner.jobs, R2) ────────────────────────────────────
    def claim(self, lease_seconds: int = 600) -> Optional[dict]:
        return self._post("/api/me/runner/jobs",
                          {"op": "claim", "lease_seconds": lease_seconds}).get("job")

    def enqueue(self, kind: str, payload: dict,
                run_id: Optional[str] = None) -> int:
        """Enfile un job — c'est par LÀ qu'un ordonnanceur de flotte travaille :
        un client ordinaire de la même file que tout le monde (R5)."""
        out = self._post("/api/me/runner/jobs",
                         {"op": "enqueue", "kind": kind, "payload": payload,
                          "run_id": run_id})
        return int(out["id"])

    def get_job(self, job_id: int) -> dict:
        return self._post("/api/me/runner/jobs",
                          {"op": "get", "job_id": job_id}).get("job") or {}

    def bind_run(self, job_id: int, run_id: str) -> None:
        self._post("/api/me/runner/jobs",
                   {"op": "bind_run", "job_id": job_id, "run_id": run_id})

    def extend(self, job_id: int, lease_seconds: int = 600) -> None:
        self._post("/api/me/runner/jobs",
                   {"op": "extend", "job_id": job_id, "lease_seconds": lease_seconds})

    def complete(self, job_id: int, ok: bool, error: Optional[str] = None,
                 run_id: Optional[str] = None,
                 result: Optional[dict] = None) -> str:
        """`result` = le résumé déclaré du job (usage_tokens, stopped, steps…) :
        c'est ce que l'ordonnanceur de flotte lit pour sa garde budget."""
        out = self._post("/api/me/runner/jobs",
                         {"op": "complete", "job_id": job_id, "ok": ok,
                          "error": error, "run_id": run_id, "result": result})
        return str(out.get("status") or "")

    # ── la file de LIGNES (datastore) — lecture seule, pour les bornes ───────
    def count_rows(self, namespace: str, filter: Optional[dict] = None,
                   org: Optional[int] = None) -> int:
        """Combien de lignes matchent encore le filtre de la flotte. Lecture
        d'observation (borne d'arrêt + ré-enfilement) — jamais un claim : le
        claim appartient à l'AGENT, dans la procédure."""
        out = self._get(f"/api/datastore/namespaces/{namespace}/rows",
                        _params_filtre(filter, limit=1), org=org)
        return int(out.get("total") or 0)

    def rows(self, namespace: str, filter: Optional[dict] = None,
             org: Optional[int] = None, limit: int = 200) -> list[dict[str, Any]]:
        """Les lignes qui matchent — lecture d'observation, jamais un claim.

        Sert au bilan de fin : une ligne SORTIE de la file (trois réservations
        sans écriture) ne dit ni pourquoi elle est sortie ni ce qui l'a traitée,
        parce que la bascule est opérée par la plateforme et que PERSONNE
        n'écrit à ce moment-là. C'est le seul événement de la campagne dont on
        a vraiment besoin et sur lequel on ne sait rien."""
        out = self._get(f"/api/datastore/namespaces/{namespace}/rows",
                        _params_filtre(filter, limit=limit), org=org)
        return out.get("rows") or []

    def row(self, namespace: str, row_id: str,
            org: Optional[int] = None) -> Optional[dict[str, Any]]:
        """Une ligne, par son identifiant. Sert à CONSTATER un effet.

        ⚠️ Le harnais ne peut pas croire ses propres compteurs d'appels : sur le
        chemin où la boucle d'outils tourne chez le fournisseur, un refus
        applicatif revient par un transport parfaitement sain et se compte comme
        un succès (vécu le 29/08 — un travail à « 2 écritures » sur une ligne
        restée vierge). Relire la ligne est le seul moyen de savoir ce qui s'est
        VRAIMENT passé. Rend None si la lecture échoue : on ne conclut jamais
        d'une panne de lecture qu'il ne s'est rien écrit."""
        try:
            return self._get(f"/api/datastore/namespaces/{namespace}/rows/{row_id}",
                             {}, org=org) or None
        except Exception:  # noqa: BLE001 — cf. docstring
            return None

    def patch_row(self, namespace: str, row_id: str, valeurs: dict,
                  org: Optional[int] = None) -> dict:
        """Écriture PARTIELLE d'une ligne, par son identifiant.

        ⚠️ Par IDENTIFIANT et jamais par clé métier : une écriture par clé sur
        une ligne absente la CRÉERAIT (vécu le 28/08 — une ligne fantôme dans un
        livrable client, repérée au compte qui passait de 504 à 505). Ici on
        annote une ligne dont on vient de lire l'identifiant : la création est
        impossible par construction."""
        return self._patch(f"/api/datastore/namespaces/{namespace}/rows/{row_id}",
                           valeurs, org=org)

    # ── le fil d'un run (runs.thread, R1) ────────────────────────────────────
    def thread_append(self, run_id: str, role: str, content: dict,
                      provider_raw: Optional[dict] = None) -> int:
        out = self._post("/api/me/runs/thread",
                         {"op": "append", "run_id": run_id, "role": role,
                          "content": content, "provider_raw": provider_raw})
        return int(out.get("seq") or 0)

    def thread_read(self, run_id: str, include_raw: bool = False,
                    limit: int = 500) -> list[dict[str, Any]]:
        out = self._post("/api/me/runs/thread",
                         {"op": "read", "run_id": run_id,
                          "include_raw": include_raw, "limit": limit})
        return out.get("messages") or []
