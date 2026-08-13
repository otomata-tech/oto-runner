"""Le troisième contrat : la face MCP du backend — outils, gates et rédaction inclus.

Client streamable-http minimal (initialize → Mcp-Session-Id → tools/list,
tools/call), porté du harnais de campagne (`mcp_oto.py`), en requests. Ce qui
compte n'est pas ce qu'il fait mais ce qu'il N'A PAS à faire : credential, RBAC,
activation, rédaction de champs, journal d'audit — tout est appliqué CÔTÉ SERVEUR
au passage de l'appel, parce que ce client est un client comme un autre.
"""
from __future__ import annotations

import json
import os
from typing import Optional

import requests

from .agent_runtime import serialize

_TIMEOUT = (10, 180)


class McpSession:
    """Une session MCP réutilisable — le transport d'outils de la boucle."""

    def __init__(self, url: Optional[str] = None, token: Optional[str] = None,
                 project: Optional[int] = None, run_id: Optional[str] = None):
        self.url = url or os.environ.get("OTO_MCP_URL", "https://mcp.oto.cx/mcp")
        self.token = (token or os.environ.get("OTO_TOKEN", "")).strip()
        # Les jetons de contexte d'appel (ADR 0038) : posés sur CHAQUE appel de
        # travail — le projet résout l'org et les identités, le run corrèle le
        # journal. C'est le worker qui les porte, pas le modèle.
        self.project = project
        self.run_id = run_id
        self.session: Optional[str] = None
        self._n = 0
        self._ouvrir()

    def _post(self, corps: dict, avec_entetes: bool = False):
        entetes = {"Authorization": f"Bearer {self.token}",
                   "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session:
            entetes["Mcp-Session-Id"] = self.session
        r = requests.post(self.url, json=corps, headers=entetes, timeout=_TIMEOUT)
        charge = "".join(l[5:].strip() for l in r.text.splitlines()
                         if l.startswith("data:")) or r.text
        try:
            data = json.loads(charge) if charge.strip() else {}
        except Exception:  # noqa: BLE001
            data = {"_brut": charge[:400]}
        return (r.headers, data) if avec_entetes else data

    def _ouvrir(self):
        self._n += 1
        entetes, _ = self._post(
            {"jsonrpc": "2.0", "id": self._n, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "oto-runner", "version": "0.1"}}},
            avec_entetes=True)
        self.session = entetes.get("mcp-session-id") or entetes.get("Mcp-Session-Id")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    # ── le contrat ToolTransport de la boucle ────────────────────────────────
    def schemas(self, names: frozenset) -> list[dict]:
        """Les schémas de l'allowlist, au format modèle — lus du tools/list de la
        session (donc déjà filtrés par la visibilité du COMPTE du worker : deux
        crans, le compte puis l'allowlist du job)."""
        self._n += 1
        d = self._post({"jsonrpc": "2.0", "id": self._n,
                        "method": "tools/list", "params": {}})
        out = []
        for t in ((d.get("result") or {}).get("tools") or []):
            if t.get("name") in names:
                out.append({"name": t["name"],
                            "description": (t.get("description") or "")[:1024],
                            "input_schema": t.get("inputSchema")
                            or {"type": "object", "properties": {}}})
        return out

    def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        """UN appel d'outil → (texte pour le fil, is_error). Les jetons de contexte
        sont posés ici — le modèle n'a pas à les connaître."""
        args = dict(arguments or {})
        if self.project is not None:
            args.setdefault("_project", self.project)
        if self.run_id is not None:
            args.setdefault("_run_id", self.run_id)
        self._n += 1
        d = self._post({"jsonrpc": "2.0", "id": self._n, "method": "tools/call",
                        "params": {"name": name, "arguments": args}})
        res = (d or {}).get("result") or {}
        if res.get("isError"):
            blocs = res.get("content") or []
            texte = "\n".join(b.get("text", "") for b in blocs
                              if isinstance(b, dict)) or serialize(res)
            return texte, True
        if res.get("structuredContent") is not None:
            return serialize(res["structuredContent"]), False
        for bloc in res.get("content") or []:
            if isinstance(bloc, dict) and bloc.get("type") == "text":
                return bloc.get("text", ""), False
        err = d.get("error")
        if err:
            return serialize(err), True
        return serialize(d), False

    def outil(self, name: str, arguments: Optional[dict] = None) -> dict:
        """Appel direct hors boucle (run_start, run_finish…) — rend le payload."""
        texte, is_error = self.call(name, arguments or {})
        try:
            data = json.loads(texte)
        except Exception:  # noqa: BLE001
            data = {"_texte": texte}
        if is_error:
            raise RuntimeError(f"{name} : {texte[:300]}")
        return data
