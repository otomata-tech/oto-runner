"""Le client que les outils de mesure DOIVENT emprunter, au lieu d'en refaire un.

⚠️ Pourquoi ce module existe. Trois fois en deux jours (28-29/08), un instrument
de mesure a rapporté un défaut **qu'il fabriquait lui-même** :

- une garde qui annonçait « flotte ARRÊTÉE » sans avoir rien arrêté ;
- une commande de vérification qui se comptait elle-même et déclarait vivant un
  ordonnanceur qui ne l'était pas ;
- quatre scripts d'analyse qui lisaient `r.text`, donc laissaient la
  bibliothèque HTTP deviner l'encodage — le flux MCP annonce
  `text/event-stream` **sans charset**, elle retombe sur latin-1, et un UTF-8
  parfaitement valide devient du charabia. Conclusion tirée puis diffusée :
  « 203 outils sur 258 servent des descriptions illisibles ». **C'était faux.**
  Le serveur était sain, le runner aussi — il fait `r.content.decode("utf-8")`
  depuis un correctif de la veille. L'outil jetable refaisait le bug que le
  produit avait déjà corrigé, et l'imputait au produit.

**Mesurer avec le même client que le produit n'est pas une commodité : c'est ce
qui rend la mesure opposable.** Un outil de mesure qui refait son propre client
refait les bugs déjà corrigés dedans.

Usage :

    from scripts.sonde import Sonde
    s = Sonde(token=os.environ["OTO_TOKEN"])
    outils = s.outils()                     # tools/list, décodé correctement
    res = s.appel("data_write", {...})      # tools/call
"""
import json
import os
from typing import Any, Optional

import requests

BASE = os.environ.get("OTO_BASE", "https://mcp.oto.cx")


class Sonde:
    """Un client MCP minimal, mais qui décode comme le produit."""

    def __init__(self, token: Optional[str] = None, base: str = BASE,
                 org: Optional[int] = None):
        self.token = token or os.environ["OTO_TOKEN"]
        self.url = base.rstrip("/") + "/mcp"
        self.org = org
        self.session = requests.Session()
        self.sid: Optional[str] = None
        self._id = 0

    def _entetes(self) -> dict:
        h = {"Authorization": f"Bearer {self.token}",
             "Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        return h

    def rpc(self, methode: str, params: dict, timeout: int = 180) -> Any:
        self._id += 1
        r = self.session.post(self.url, headers=self._entetes(), timeout=timeout,
                              json={"jsonrpc": "2.0", "id": self._id,
                                    "method": methode, "params": params})
        self.sid = r.headers.get("Mcp-Session-Id") or self.sid
        # ⚠️ `r.content.decode("utf-8")` et JAMAIS `r.text` : cf. l'en-tête du
        # module. Le flux est de l'UTF-8 par spécification ; le deviner, c'est
        # inventer un défaut qui n'existe pas.
        texte = r.content.decode("utf-8")
        sortie = None
        for ligne in texte.splitlines():
            if ligne.startswith("data:"):
                sortie = json.loads(ligne[5:].strip())
        if sortie is None:
            raise RuntimeError(f"{methode} → réponse sans donnée ({r.status_code}) "
                               f"{texte[:200]}")
        if "error" in sortie:
            raise RuntimeError(f"{methode} → {sortie['error']}")
        return sortie.get("result")

    def notifier(self, methode: str, params: dict) -> None:
        """Une NOTIFICATION JSON-RPC : ni `id`, ni réponse attendue.

        ⚠️ L'envoyer comme une requête (avec `id`) fait répondre au serveur
        « Invalid request parameters » — le message est trompeur, il ne dit pas
        que c'est la FORME qui est fautive, pas les paramètres."""
        self.session.post(self.url, headers=self._entetes(), timeout=60,
                          json={"jsonrpc": "2.0", "method": methode,
                                "params": params})

    def ouvrir(self) -> "Sonde":
        if self.sid:
            return self
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "sonde", "version": "1"}})
        self.notifier("notifications/initialized", {})
        return self

    def outils(self) -> list[dict]:
        return (self.ouvrir().rpc("tools/list", {}) or {}).get("tools") or []

    def appel(self, nom: str, arguments: dict) -> Any:
        args = dict(arguments)
        if self.org is not None:
            args.setdefault("_org", self.org)
        return self.ouvrir().rpc("tools/call", {"name": nom, "arguments": args})


def texte(res: Any) -> str:
    """Le contenu textuel d'un résultat d'outil, sans supposer sa forme."""
    if not isinstance(res, dict):
        return str(res)
    morceaux = res.get("content") or []
    return " ".join(m.get("text", "") for m in morceaux
                    if isinstance(m, dict)) or json.dumps(res, ensure_ascii=False)
