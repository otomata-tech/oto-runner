"""Les deux contrats REST du worker : le FIL d'un run, et la FILE de jobs.

C'est tout ce que le worker connaît du backend en dehors de la face MCP — deux
familles d'endpoints op-aware, un seul jeton. Pas d'accès base, pas d'import
oto : si ces contrats tiennent, le worker est remplaçable (ADR 0064-D1).
"""
from __future__ import annotations

import os
from typing import Any, Optional

import requests  # noqa: F401

from .deadline import get_with_deadline, post_with_deadline

_TIMEOUT = (10, 60)


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

    def _post(self, chemin: str, corps: dict) -> dict:
        r = post_with_deadline(self.base + chemin, json=corps, timeout=_TIMEOUT,
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

    def _get(self, chemin: str, params: dict,
             org: Optional[int] = None) -> dict:
        entetes = {"Authorization": f"Bearer {self.token}"}
        if org is not None:
            # Le namespace d'une flotte vit dans l'org de la MISSION, pas dans
            # l'org maison du jeton — la consultation REST se scope par en-tête.
            entetes["X-Oto-Org"] = str(org)
        r = get_with_deadline(self.base + chemin, params=params, timeout=_TIMEOUT,
                              headers=entetes, wall_s=120)
        if r.status_code >= 400:
            raise BackendError(f"{chemin} → {r.status_code} : {r.text[:300]}",
                               status=r.status_code)
        return r.json() if r.content else {}

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
        import json as _json
        params: dict = {"limit": 1}
        if filter:
            # La grammaire riche du datastore : une valeur peut être un
            # opérateur ({"in": [...]}) ou une LISTE (raccourci pour in) —
            # c'est ce qui permet de borner une flotte à un LOT NOMMÉ de
            # lignes (une comparaison A/B se fait sur des populations exactes,
            # jamais sur des plages approximatives).
            clauses = []
            for k, v in filter.items():
                if isinstance(v, dict) and len(v) == 1:
                    op, val = next(iter(v.items()))
                    clauses.append({"field": k, "op": op, "value": val})
                elif isinstance(v, (list, tuple)):
                    clauses.append({"field": k, "op": "in", "value": list(v)})
                else:
                    clauses.append({"field": k, "op": "eq", "value": v})
            params["filters"] = _json.dumps(clauses)
        out = self._get(f"/api/datastore/namespaces/{namespace}/rows", params,
                        org=org)
        return int(out.get("total") or 0)

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
