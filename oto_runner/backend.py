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
import logging

logger = logging.getLogger(__name__)

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


_MOTIFS = (
    # (fragment cherché dans le message, nom du poste au bilan)
    # ⚠️ Le refus du cran a DEUX formes, et c'est la seconde qui compte pour les
    # fabrications : « aucune ligne de <table> ne porte <clé> = … ». Le 29/08 un
    # agent a écrit un dictionnaire entier dans le champ SIREN — clé factice, avec
    # son propre commentaire disant qu'il avait perdu la vraie. Sans le cran,
    # c'était une ligne fantôme de plus. Mon compteur ne cherchait que la première
    # forme : il a rangé le seul cas grave du passage dans « autre », et le bilan
    # a annoncé ZÉRO création refusée. Un compteur qui rate le cas qu'il existe
    # pour voir est pire qu'absent — il certifie qu'il ne s'est rien passé.
    ("business_key_required", "création refusée par le cran"),
    ("ne porte", "création refusée par le cran"),
    ("n'est pas renseigné", "création refusée par le cran"),
    ("introuvable", "ligne inconnue (identifiant inventé ou périmé)"),
    ("not found", "ligne inconnue (identifiant inventé ou périmé)"),
    ("réservée par", "ligne tenue par un autre travail"),
    ("reserved by", "ligne tenue par un autre travail"),
    ("required_when", "champ conditionnel manquant"),
    ("run_id", "jeton de travail manquant"),
)


def _motif(message: str) -> str:
    """Range un message de refus dans un poste lisible au bilan.

    ⚠️ Un refus non classé garde son texte tronqué plutôt que d'aller dans un
    « divers » : un poste fourre-tout masque précisément le motif neuf qu'on
    aurait voulu voir apparaître."""
    bas = message.lower()
    for fragment, poste in _MOTIFS:
        if fragment in bas:
            return poste
    return "autre : " + " ".join(message.split())[:60]


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

    def refus_par_motif(self, org: int, tool: str, *, minutes: int = 15,
                        limit: int = 200) -> dict[str, int]:
        """Les refus de `tool`, comptés PAR MOTIF, sur la fenêtre.

        ⚠️ Pourquoi ce poste existe. Tant qu'aucun cran n'empêchait la création,
        une tentative de fabriquer une entreprise LAISSAIT UNE LIGNE : on la
        voyait, on la comptait, on remontait à sa cause. Sous le cran
        `key_required`, la même tentative devient un refus — et **un refus ne se
        voit que si quelqu'un le compte**.

        Un zéro obtenu sous une garde ne dit pas que le geste a cessé : il dit
        que le geste ne réussit plus. Sans ce comptage, on lirait un progrès là
        où il n'y a qu'une protection qui tient — et une hausse ici serait un
        signal, pas un échec : elle dirait que la consigne n'a pas porté et que
        seul le cran retient."""
        from collections import Counter
        from datetime import datetime, timedelta, timezone
        d = self._get(f"/api/orgs/{org}/monitoring/calls",
                      {"tool": tool, "limit": limit})
        seuil = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        motifs: Counter = Counter()
        for c in d.get("calls") or []:
            if c.get("ok"):
                continue
            quand = str(c.get("called_at") or "")[:19].replace("T", " ")
            try:
                t = datetime.strptime(quand, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc)
            except ValueError:
                continue
            if t < seuil:
                continue
            motifs[_motif(str(c.get("error") or ""))] += 1
        return dict(motifs)

    # ── la file de jobs (runner.jobs, R2) ────────────────────────────────────
    def claim(self, lease_seconds: int = 600, depot: str = "") -> Optional[dict]:
        """Réserve un travail — et NOMME le dépôt de clé qu'on sait consommer.

        Le backend y répond par `model_key` quand l'org du travail a déposé cette
        clé-là. Le worker ne va jamais la chercher lui-même : il ne parle pas au
        coffre, il reçoit avec le travail ce qu'il faut pour l'exécuter, et rien
        de plus.
        """
        # ⚠️ Le champ `provider` n'est envoyé QUE parce que la route servie le
        # DÉCLARE. Le 04/09, l'avoir envoyé à une prod qui ne le déclarait pas a
        # mis les trois workers hors service 17 minutes, chaque réservation
        # partant en `400 : unknown_fields` en boucle : l'adaptateur REST compare
        # les clés reçues aux `Input.model_fields` de la capacité et REFUSE les
        # inconnues — là où pydantic seul les aurait ignorées. La mesure qui
        # m'avait rassuré portait sur pydantic, pas sur cette garde.
        #
        # Servi depuis oto-backend v1.195.0 : l'OpenAPI de la prod déclare
        # `provider` pour POST /api/me/runner/jobs. Avant d'ajouter un champ ici,
        # relire ce que la route déclare — `tests/test_corps_servi_par_la_route.py`
        # tient la liste et dit comment la vérifier.
        corps = {"op": "claim", "lease_seconds": lease_seconds}
        if depot:
            corps["provider"] = depot
        return self._post("/api/me/runner/jobs", corps).get("job")

    def enqueue(self, kind: str, payload: dict,
                run_id: Optional[str] = None,
                fleet_id: Optional[int] = None) -> int:
        """Enfile un job — c'est par LÀ qu'un ordonnanceur de flotte travaille :
        un client ordinaire de la même file que tout le monde (R5).

        ⚠️ `fleet_id` RATTACHE le job à sa flotte déclarée, et remplace le tag
        texte `payload["fleet"]` : un tag vit dans un JSON libre, une colonne
        porte une clé étrangère et se compte. C'est lui qui rend
        `runner.fleets op=state` capable d'agréger un passage.
        """
        corps = {"op": "enqueue", "kind": kind, "payload": payload,
                 "run_id": run_id}
        if fleet_id is not None:
            corps["fleet_id"] = fleet_id
        out = self._post("/api/me/runner/jobs", corps)
        return int(out["id"])

    def declarer_flotte(self, *, label: str, procedure: str, tools: list,
                        namespace: Optional[str] = None,
                        row_filter: Optional[dict] = None,
                        project_id: Optional[int] = None,
                        input: Optional[str] = None,
                        max_steps: Optional[int] = None,
                        provider: Optional[str] = None,
                        model: Optional[str] = None,
                        workers: Optional[int] = None,
                        max_rows: Optional[int] = None,
                        max_tokens: Optional[int] = None,
                        max_consecutive_failures: Optional[int] = None,
                        max_tokens_per_row: Optional[int] = None) -> dict:
        """Déclare la flotte EN BASE et rend la ligne créée.

        Une flotte vivait dans un fichier YAML sur la machine : rien n'en était
        visible du dashboard ni atteignable par un agent. La déclarer donne un
        domicile à sa cible, à son périmètre et à ses bornes — et un identifiant
        que chaque job portera.
        """
        corps = {"op": "create", "label": label, "procedure": procedure,
                 "tools": list(tools)}
        for cle, val in (("namespace", namespace), ("row_filter", row_filter),
                         ("project_id", project_id), ("input", input),
                         ("max_steps", max_steps), ("provider", provider),
                         ("model", model), ("workers", workers),
                         ("max_rows", max_rows), ("max_tokens", max_tokens),
                         ("max_consecutive_failures", max_consecutive_failures),
                         ("max_tokens_per_row", max_tokens_per_row)):
            if val is not None:
                corps[cle] = val
        return self._post("/api/me/runner/fleets", corps)["fleet"]

    def get_job(self, job_id: int) -> dict:
        return self._post("/api/me/runner/jobs",
                          {"op": "get", "job_id": job_id}).get("job") or {}

    def lire_flotte(self, fleet_id: int) -> dict:
        """La flotte DÉCLARÉE, telle que la plateforme la sert.

        C'est ce qui permet à un passage d'être piloté par sa configuration en
        base plutôt que par un fichier posé à côté de l'exécutable — et donc
        d'être le même objet pour l'ordonnanceur, pour le dashboard et pour un
        agent.
        """
        return self._post("/api/me/runner/fleets",
                          {"op": "get", "fleet_id": fleet_id}).get("fleet") or {}

    def armer_flotte(self, fleet_id: int) -> dict:
        """`draft` → `armed` : la campagne est DEMANDÉE. Sans ce geste, `prendre`
        refuse — il n'accepte qu'une flotte armée.

        ⚠️ Il manquait. Le runner déclarait (la campagne naît `draft`) puis
        tentait de prendre : refus systématique, rattrapé et poursuivi. Résultat
        mesuré le 03/09 : **14 campagnes en base, toutes `draft`, aucune jamais
        `running`** — alors que huit vagues avaient réellement tourné.
        """
        return self._post("/api/me/runner/fleets",
                          {"op": "launch", "fleet_id": fleet_id}).get("fleet") or {}

    def prendre_flotte(self, fleet_id: int) -> dict:
        """`armed` → `running` : je la PRENDS, et je le déclare.

        ⚠️ Un refus ici n'est pas une erreur à retenter : il veut dire qu'un autre
        ordonnanceur l'a prise, ou qu'elle n'était pas armée. Partir quand même
        doublerait le passage.
        """
        return self._post("/api/me/runner/fleets",
                          {"op": "take", "fleet_id": fleet_id}).get("fleet") or {}

    def battre_flotte(self, fleet_id: int) -> bool:
        """Bat, ET demande dans le même appel : « dois-je m'arrêter ? »

        ⚠️ C'est ce qui rend `op=stop` RÉEL. Un ordonnanceur qui bat sans jamais
        poser la question laisserait l'ordre d'arrêt sans lecteur — et l'écran
        annoncerait un arrêt qui n'arrive jamais.
        """
        out = self._post("/api/me/runner/fleets",
                         {"op": "beat", "fleet_id": fleet_id})
        return bool(out.get("stop_requested"))

    def accuser_arret(self, fleet_id: int, raison: str | None = None) -> None:
        """`stopping` → `stopped` : j'ai obéi.

        Le seul geste qui pose le FAIT. Sans lui, un arrêt demandé reste
        `stopping` pour toujours — ce qui est précisément le symptôme d'un
        ordonnanceur mort, et il ne faut pas le fabriquer en étant vivant.
        """
        corps = {"op": "ack_stop", "fleet_id": fleet_id}
        if raison:
            corps["reason"] = raison
        self._post("/api/me/runner/fleets", corps)

    def bind_run(self, job_id: int, run_id: str) -> None:
        self._post("/api/me/runner/jobs",
                   {"op": "bind_run", "job_id": job_id, "run_id": run_id})

    def extend(self, job_id: int, lease_seconds: int = 600) -> None:
        self._post("/api/me/runner/jobs",
                   {"op": "extend", "job_id": job_id, "lease_seconds": lease_seconds})

    def complete(self, job_id: int, ok: bool, error: Optional[str] = None,
                 run_id: Optional[str] = None,
                 result: Optional[dict] = None) -> dict:
        """`result` = le résumé déclaré du job (usage_tokens, stopped, steps…) :
        c'est ce que l'ordonnanceur de flotte lit pour sa garde budget.

        ⚠️ Rend la RÉPONSE ENTIÈRE, pas seulement le statut. La clôture relâche
        les lignes que le travail tient et le dit — un compte de lignes
        relâchées, présent seulement quand il y en a. On jetait cette réponse
        pour n'en garder qu'une chaîne, si bien que le seul témoin du bon
        fonctionnement de la libération était hors de portée : on a supposé
        pendant une journée entière ce que le serveur disait à chaque appel.
        """
        return self._post("/api/me/runner/jobs",
                          {"op": "complete", "job_id": job_id, "ok": ok,
                           "error": error, "run_id": run_id, "result": result})

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
        rep = self._patch(f"/api/datastore/namespaces/{namespace}/rows/{row_id}",
                          valeurs, org=org)
        # ⚠️ LE CANAL REST AUSSI. Le relevé de colonnes fantômes lit les sorties
        # d'outils rendues au modèle ; une écriture faite ici n'y apparaît
        # JAMAIS. Il a rendu zéro sur cent trois travaux là où la table portait
        # une colonne non déclarée — pas un rapporteur muet, un relevé qui
        # regardait un canal ne portant pas ces écritures.
        #
        # Une colonne fantôme née d'un geste du HARNAIS est plus grave que celle
        # d'un agent : personne ne relit ce que le harnais écrit.
        if isinstance(rep, dict):
            fantomes = rep.get("hors_schema")
            if fantomes:
                logger.warning(
                    "écriture HORS SCHÉMA par le harnais sur %s/%s : %s — "
                    "stockée, lisible, et invisible à tout contrôle qui "
                    "s'appuie sur le schéma", namespace, row_id, fantomes)
        return rep

    def appels_du_run(self, run_id: str, limit: int = 60) -> list:
        """Les appels d'outils d'un run, avec leurs ARGUMENTS.

        ⚠️ C'est le seul canal qui porte l'identifiant de la ligne travaillée sur
        le chemin Conversations : le harnais n'y reçoit pas les résultats
        d'outils, et toute lecture par les sorties rend None. Sans cette route,
        le rappel de contact et la garde du `NN` ne s'exécutent jamais.
        """
        out = self._get("/api/orgs/226/monitoring/calls",
                        {"tool": "data_write", "limit": limit})
        appels = (out or {}).get("calls") or []
        return [c for c in appels if str(c.get("run_id") or "") == str(run_id)]

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
