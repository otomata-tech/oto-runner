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

import requests  # noqa: F401 — la forme des kwargs

from .agent_runtime import serialize
from .deadline import post_with_deadline

_TIMEOUT = (10, 180)


class McpSession:
    """Une session MCP réutilisable — le transport d'outils de la boucle."""

    def __init__(self, url: Optional[str] = None, token: Optional[str] = None,
                 project: Optional[int] = None, run_id: Optional[str] = None,
                 org: Optional[int] = None):
        self.url = url or os.environ.get("OTO_MCP_URL", "https://mcp.oto.cx/mcp")
        self.token = (token or os.environ.get("OTO_TOKEN", "")).strip()
        # Les jetons de contexte d'appel (ADR 0038) : posés sur CHAQUE appel de
        # travail — le projet résout l'org et les identités, le run corrèle le
        # journal. C'est le worker qui les porte, pas le modèle.
        self.project = project
        self.org = org      # l'org de la MISSION — sert les tools qui déclarent
        # `_org` mais pas `_project` (oto_procedure : une doctrine d'org se
        # charge dans SON org, pas dans l'org maison du jeton)
        self.run_id = run_id
        self.session: Optional[str] = None
        self._n = 0
        self._props: Optional[dict] = None   # tool → propriétés d'entrée déclarées
        self._ouvrir()

    def _post(self, corps: dict, avec_entetes: bool = False):
        entetes = {"Authorization": f"Bearer {self.token}",
                   "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session:
            entetes["Mcp-Session-Id"] = self.session
        r = post_with_deadline(self.url, json=corps, headers=entetes,
                               timeout=_TIMEOUT, wall_s=300)
        charge = "".join(l[5:].strip() for l in r.text.splitlines()
                         if l.startswith("data:")) or r.text
        try:
            data = json.loads(charge) if charge.strip() else {}
        except Exception:  # noqa: BLE001
            data = {"_brut": charge[:400]}
        return (r.headers, data) if avec_entetes else data

    def _ouvrir(self):
        # Un 502 pendant l'initialize rendait une session MUETTE (session id
        # absent avalé) : tous les appels suivants mouraient en « Missing
        # session ID » cryptique (vécu, nuit du 15/08). Trois essais espacés,
        # puis un échec NET — le retry de job fait le reste.
        import time as _t
        for essai in range(3):
            self._n += 1
            entetes, _ = self._post(
                {"jsonrpc": "2.0", "id": self._n, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "oto-runner", "version": "0.1"}}},
                avec_entetes=True)
            self.session = entetes.get("mcp-session-id") or entetes.get("Mcp-Session-Id")
            if self.session:
                self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
                return
            _t.sleep(5 * (essai + 1))
        raise RuntimeError(
            "initialize MCP sans session id après 3 essais — backend indisponible ?")

    # ── le contrat ToolTransport de la boucle ────────────────────────────────
    def schemas(self, names: frozenset) -> list[dict]:
        """Les schémas de l'allowlist, au format modèle — lus du tools/list de la
        session (donc déjà filtrés par la visibilité du COMPTE du worker : deux
        crans, le compte puis l'allowlist du job)."""
        self._n += 1
        d = self._post({"jsonrpc": "2.0", "id": self._n,
                        "method": "tools/list", "params": {}})
        outils = (d.get("result") or {}).get("tools")
        if not outils:
            # Un tools/list qui échoue (502 en vol) laissait un cache VIDE :
            # le fail-safe ne posait plus AUCUN jeton, et le job mourait plus
            # loin sur une erreur MÉTIER trompeuse (« Aucune doctrine (scope
            # org) », vécu — jamais rejouée car non transitoire). Échec NET
            # ici : le retry de job repart d'une session saine.
            raise RuntimeError(
                f"tools/list vide ou en erreur ({str(d)[:120]}) — session dégradée")
        out = []
        self._props = {}
        for t in outils:
            props = ((t.get("inputSchema") or {}).get("properties") or {})
            self._props[t.get("name") or ""] = frozenset(props)
            if t.get("name") in names:
                out.append({"name": t["name"],
                            "description": (t.get("description") or "")[:1024],
                            "input_schema": t.get("inputSchema")
                            or {"type": "object", "properties": {}}})
        return out

    def _declares(self, name: str) -> frozenset:
        """Les propriétés d'entrée DÉCLARÉES par ce tool. C'est ce qui rend la
        pose des jetons de contexte SÉLECTIVE : ils sont advertisés par tool
        (ADR 0038), et les poser à l'aveugle fait refuser l'appel ENTIER à la
        validation — vécu au premier vol de flotte : `oto_procedure` ne déclare
        pas `_project`, 4 jobs en échec avant une seule ligne traitée. Un tool
        absent du cache ne reçoit AUCUN jeton (un appel sans contexte vaut
        mieux qu'un refus)."""
        if self._props is None:
            self.schemas(frozenset())
        return self._props.get(name, frozenset())

    def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        """UN appel d'outil → (texte pour le fil, is_error). Les jetons de contexte
        sont posés ici — le modèle n'a pas à les connaître."""
        args = dict(arguments or {})
        declares = self._declares(name)
        if self.project is not None and "_project" in declares:
            args.setdefault("_project", self.project)
        if self.org is not None and "_org" in declares and "_project" not in declares:
            # L'org SEULEMENT quand le projet ne peut pas la porter : deux
            # jetons redondants sur le même appel n'apportent rien.
            args.setdefault("_org", self.org)
        if self.run_id is not None and "_run_id" in declares:
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
