"""Le troisième contrat : la face MCP du backend — outils, gates et rédaction inclus.

Client streamable-http minimal (initialize → Mcp-Session-Id → tools/list,
tools/call), porté du harnais de campagne (`mcp_oto.py`), en requests. Ce qui
compte n'est pas ce qu'il fait mais ce qu'il N'A PAS à faire : credential, RBAC,
activation, rédaction de champs, journal d'audit — tout est appliqué CÔTÉ SERVEUR
au passage de l'appel, parce que ce client est un client comme un autre.

⚠️ Une session MCP ne survit pas au REDÉPLOIEMENT du service : le serveur ne la
connaît plus (`-32600` « Session not found ») et tous les appels suivants
échouent d'un coup. L'agent, lui, lit ça comme une réponse — il l'annonce
poliment et conclut : job « done » sans écriture, donc jamais rejoué, et la
ligne reste « à traiter » sans que personne ne le sache (2 fiches perdues en
silence le 28/08). La session se ROUVRE donc ici, une seule fois par appel, et
l'appel est rejoué ; si la réouverture échoue, on LÈVE — le job échoue et le
backend le rejoue, ce qui est la seule issue honnête.

Tout ce qui revient du serveur se décode en UTF-8 EXPLICITEMENT (28/08/2026) : le
flux SSE arrive en `text/event-stream` SANS charset, et requests applique alors le
défaut HTTP des `text/*` — ISO-8859-1 — donc « é » ressortait en « Ã© ». Le modèle
RECOPIE ses résultats d'outils : la corruption finissait dans les fiches produites.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

import requests  # noqa: F401 — la forme des kwargs

from .agent_runtime import serialize
from .deadline import post_with_deadline

logger = logging.getLogger("oto_runner")

_TIMEOUT = (10, 180)
# La signature d'une session que le serveur ne connaît plus : son message, et le
# code JSON-RPC qu'il rend quand l'en-tête `Mcp-Session-Id` est refusé. Un
# `-32600` (« Invalid Request ») n'a de toute façon jamais été exécuté : le
# rejouer après réouverture ne peut pas doubler une écriture.
_SESSION_PERDUE = re.compile(r"session not found|missing session id", re.I)
_CODE_REQUETE_INVALIDE = -32600


def _session_perdue(d: dict) -> bool:
    """La réponse dit-elle que notre session n'existe plus côté serveur ?"""
    err = (d or {}).get("error")
    if isinstance(err, dict) and (err.get("code") == _CODE_REQUETE_INVALIDE
                                  or _SESSION_PERDUE.search(str(err.get("message")
                                                                or ""))):
        return True
    return bool(_SESSION_PERDUE.search(str((d or {}).get("_brut") or "")))


def _utf8(r) -> str:
    """Le corps d'une réponse du serveur, décodé en UTF-8 — quoi qu'annoncent les
    en-têtes. `r.text` ne convient pas : il suit le charset déclaré, et le flux
    SSE n'en déclare AUCUN. `errors="strict"` : un corps qui n'est pas de l'UTF-8
    est un échec NET, jamais des remplacements muets au milieu d'une fiche."""
    try:
        return r.content.decode("utf-8")
    except UnicodeDecodeError as e:
        raise RuntimeError(
            f"réponse MCP non décodable en UTF-8 (octet {e.start}, {e.reason}) — "
            f"content-type « {r.headers.get('Content-Type', '?')} »") from e


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
        # L'ESTAMPILLE (modèle + version de procédure) : ce que le harnais pose
        # sur chaque fiche écrite, et qu'on ne demande JAMAIS à l'agent — il ne
        # sait pas de façon fiable quel modèle le fait tourner. Vide tant que le
        # worker ne l'a pas renseignée, et vide aussi quand le tableau ne
        # déclare pas les deux champs : injecter une colonne non déclarée dans
        # un tableau strict ferait refuser TOUTE l'écriture, donc perdre la
        # fiche pour un champ d'observabilité. Une fiche sans estampille vaut
        # mieux qu'une fiche perdue.
        self.estampille: dict = {}
        # La DERNIÈRE ligne réservée par l'agent, mémorisée au vol. Le harnais ne
        # la connaît pas autrement — c'est l'agent qui réserve — et il en a besoin
        # pour lui rendre la main quand il conclut sans avoir écrit : « ta ligne est
        # celle-ci, écris-la ». Sans identifiant, le renvoi ne serait qu'un reproche.
        self.derniere_ligne: Optional[str] = None
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
        brut = _utf8(r)
        charge = "".join(l[5:].strip() for l in brut.splitlines()
                         if l.startswith("data:")) or brut
        try:
            data = json.loads(charge) if charge.strip() else {}
        except Exception:  # noqa: BLE001
            data = {"_brut": charge[:400]}
        return (r.headers, data) if avec_entetes else data

    def _ouvrir(self):
        # L'ancien id part AVANT le premier POST : un serveur qui refuse une
        # session inconnue refuserait aussi l'initialize qui la porte.
        self.session = None
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
        self._appliquer_estampille(name, args)
        self._n += 1
        corps = {"jsonrpc": "2.0", "id": self._n, "method": "tools/call",
                 "params": {"name": name, "arguments": args}}
        d = self._post(corps)
        if _session_perdue(d):
            # UNE réouverture par appel, jamais une boucle : `_ouvrir` lève
            # après 3 initialize muets, et le job échoue — c'est voulu. Laisser
            # l'erreur revenir au modèle ferait conclure « done » sans écriture.
            logger.warning("session MCP perdue sur %s : réouverture", name)
            self._ouvrir()
            logger.info("session MCP rouverte après « Session not found » — "
                        "%s rejoué", name)
            self._n += 1
            d = self._post(dict(corps, id=self._n))
            if _session_perdue(d):
                raise RuntimeError(
                    f"session MCP rouverte mais {name} reste refusé "
                    f"({str((d or {}).get('error') or d)[:200]}) — le job échoue "
                    "pour être rejoué")
        res = (d or {}).get("result") or {}
        if res.get("isError"):
            blocs = res.get("content") or []
            texte = "\n".join(b.get("text", "") for b in blocs
                              if isinstance(b, dict)) or serialize(res)
            return texte, True
        if res.get("structuredContent") is not None:
            self._noter_ligne(name, res["structuredContent"])
            return serialize(res["structuredContent"]), False
        for bloc in res.get("content") or []:
            if isinstance(bloc, dict) and bloc.get("type") == "text":
                self._noter_ligne(name, bloc.get("text", ""))
                return bloc.get("text", ""), False
        err = d.get("error")
        if err:
            return serialize(err), True
        return serialize(d), False

    def _noter_ligne(self, name: str, sortie) -> None:
        """Retient l'identifiant qu'une RÉSERVATION vient de rendre.

        ⚠️ Par SUFFIXE : le connecteur MCP préfixe les noms d'outils. Et
        silencieusement : ne pas retenir un identifiant n'empêche rien de
        fonctionner, ça prive seulement le renvoi de sa précision."""
        if not name.endswith("data_claim_next"):
            return
        try:
            d = sortie if isinstance(sortie, dict) else json.loads(str(sortie))
            ligne = (d or {}).get("row")
            if isinstance(ligne, dict) and ligne.get("_id"):
                self.derniere_ligne = str(ligne["_id"])
        except Exception:  # noqa: BLE001 — cf. docstring
            pass

    def _appliquer_estampille(self, name: str, args: dict) -> None:
        """Pose l'estampille sur les fiches d'un `data_write`, sans écraser
        l'agent s'il a renseigné le champ lui-même.

        ⚠️ Le connecteur MCP peut PRÉFIXER les noms d'outils : l'appartenance se
        teste par SUFFIXE, comme partout ailleurs dans ce dépôt.
        Les deux formes d'écriture sont couvertes — une fiche (`row`) et un lot
        (`rows`) : n'en traiter qu'une laisserait la moitié des campagnes sans
        estampille, ce qui est précisément le défaut qu'on corrige."""
        if not self.estampille or not name.endswith("data_write"):
            return
        fiches = []
        if isinstance(args.get("row"), dict):
            fiches.append(args["row"])
        for f in args.get("rows") or ():
            if isinstance(f, dict):
                fiches.append(f)
        for fiche in fiches:
            for cle, valeur in self.estampille.items():
                if valeur:
                    fiche.setdefault(cle, valeur)

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
