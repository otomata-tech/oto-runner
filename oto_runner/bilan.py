"""Le BILAN d'une flotte — ce qu'il faut pour PILOTER, rendu par le driver.

Vécu : sur une campagne de plusieurs semaines, tout le pilotage (lignes
abouties, faux départs, refus d'écriture, coût par ligne) est sorti de deux
scripts maison déposés à la main sur la box — un savoir qui ne vivait que dans
une tête et dans un `/root`. Le driver le rend désormais lui-même, à intervalle
régulier ET à la fin quelle que soit la borne : c'est ce qui permet à quelqu'un
d'autre de reprendre la campagne.

Deux règles de lecture, toutes deux payées cher :

- **l'avancement se lit au TABLEAU, jamais aux jobs.** « Aboutie » = une ligne
  qui ne correspond PLUS au filtre de réservation de la flotte. Compter les
  jobs « done » comptait les tours perdus comme des succès ;
- **le coût se lit au résultat DÉCLARÉ des jobs** (`usage_tokens`, `claims`,
  `writes`, `claim_vide`, `faux_depart`), et les refus d'écriture au
  JOURNAL des appels de l'org : une écriture refusée (RBAC, quota, schéma) ne
  fait pas échouer le job — l'agent conclut « done » sans une ligne écrite.

Rien ici n'arrête une flotte : une lecture qui échoue devient un poste `null`
assumé et une ligne de journal, jamais un chiffre inventé ni un traceback qui
masquerait l'arrêt en cours (le bilan de fin tombe souvent APRÈS une panne).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("oto_runner.bilan")

PERIODE_S = 600            # défaut du champ de déclaration `bilan_periode_s`
_REFUS_OUTIL = "data_write"
_REFUS_FENETRE_MAX_MIN = 24 * 60   # une campagne dure des semaines : on plafonne
_REFUS_LIMITE = 200                # les N derniers appels lus au journal d'org


def _par_suffixe(compte: dict, suffixe: str) -> int:
    """La somme d'un compte d'appels par outil, testée par SUFFIXE.

    ⚠️ Les noms d'outils arrivent parfois PRÉFIXÉS par le connecteur MCP
    (`<connecteur>_data_write`) : une égalité stricte compte zéro écriture sur
    une campagne qui en fait des milliers."""
    return sum(int(v or 0) for k, v in compte.items() if str(k).endswith(suffixe))


def _claims_writes(resultat: dict) -> tuple[int, int]:
    """(lignes réservées, écritures) d'un job. Le worker les DÉCLARE quand il
    les connaît ; sinon elles se dérivent de `tool_counts`.

    ⚠️ Une RÉSERVATION n'est pas un APPEL de réservation : un `data_claim_next`
    qui ne rend aucune ligne n'a rien réservé (fin de file — il y a toujours
    plus d'agents que de lignes). Le worker, qui voit la sortie, le déclare ;
    à défaut on applique SA règle de repli, la même des deux côtés sous peine
    de voir le bilan et la borne de flotte se contredire : un job qui n'a fait
    qu'UN appel n'a pu faire que le claim, donc il n'a rien réservé."""
    claims, writes = resultat.get("claims"), resultat.get("writes")
    compte = resultat.get("tool_counts") or {}
    if claims is None:
        claims = (0 if sum(int(v or 0) for v in compte.values()) <= 1
                  else _par_suffixe(compte, "data_claim_next"))
    if writes is None:
        writes = _par_suffixe(compte, "data_write")
    return int(claims), int(writes)


def _faux_depart(resultat: dict, claims: int, writes: int) -> bool:
    """Le faux départ — la ligne RÉSERVÉE, rien d'écrit. Le worker le DÉCLARE
    (lui seul a vu les appels) et sa parole prime ; un résultat qui ne le porte
    pas (un job en ÉCHEC, par exemple) se juge sur l'asymétrie — sur des
    réservations réelles, donc, jamais sur des claims restés vides."""
    if "faux_depart" in resultat:
        return bool(resultat["faux_depart"])
    return bool(claims) and not writes


def _postes_jobs(jobs: dict) -> dict:
    """Ce que les jobs CONCLUS de la flotte disent : volumes, coût, écritures.

    Un job non conclu n'a pas de résultat : le compter serait compter du vide,
    on lève plutôt que de le taire."""
    postes = {"termines": 0, "echoues": 0, "faux_departs": 0,
              "jetons": 0, "claims": 0, "writes": 0}
    for jid, job in sorted(jobs.items()):
        statut = job.get("status")
        if statut not in ("done", "failed"):
            raise ValueError(f"bilan : le job {jid} n'est pas conclu (statut "
                             f"{statut!r}) — le bilan ne compte que des jobs conclus")
        resultat = job.get("result") or {}
        postes["jetons"] += int(resultat.get("usage_tokens") or 0)
        claims, writes = _claims_writes(resultat)
        postes["claims"] += claims
        postes["writes"] += writes
        if _faux_depart(resultat, claims, writes):
            postes["faux_departs"] += 1
        postes["termines" if statut == "done" else "echoues"] += 1
    return postes


def _restantes(spec, backend) -> Optional[int]:
    """Les lignes qui correspondent ENCORE au filtre de réservation."""
    try:
        return int(backend.count_rows(spec.namespace, filter=spec.filter,
                                      org=spec.org))
    except Exception as e:  # noqa: BLE001 — un compte illisible ≠ un compte faux
        logger.warning("bilan : lignes restantes illisibles : %s", e)
        return None


def _refus_ecriture(spec, backend, secondes: float) -> tuple[Optional[dict],
                                                             Optional[str]]:
    """« n appels, k refusés » sur `data_write`, lu au journal des appels d'org.

    Rend (poste, raison de l'omission) — l'un des deux vaut toujours None : un
    poste absent dit POURQUOI il l'est, il ne se confond jamais avec un zéro."""
    if getattr(spec, "org", None) is None:
        return None, "déclaration sans org : le journal des appels n'est pas lisible"
    minutes = max(1, min(int(secondes // 60), _REFUS_FENETRE_MAX_MIN))
    try:
        n, ko = backend.tool_health(spec.org, _REFUS_OUTIL, minutes=minutes,
                                    limit=_REFUS_LIMITE)
    except Exception as e:  # noqa: BLE001 — la sonde ne tue pas la flotte
        logger.warning("bilan : santé de %s illisible : %s", _REFUS_OUTIL, e)
        return None, f"journal des appels illisible : {e}"
    return ({"outil": _REFUS_OUTIL, "fenetre_minutes": minutes,
             "limite": _REFUS_LIMITE, "appels": n, "refuses": ko}, None)


def _jetons_lisibles(n: Optional[int]) -> str:
    """Un ordre de grandeur qui se lit d'un coup d'œil : « 1,8 M », « 24,1 k »."""
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} M".replace(".", ",")
    if n >= 1_000:
        return f"{n / 1_000:.1f} k".replace(".", ",")
    return str(n)


def _ligne(bilan: dict) -> str:
    """La ligne de journal : des effectifs bruts AVEC leur dénominateur — un
    pourcentage cacherait qu'il porte sur trois lignes."""
    lignes, jobs, jetons = bilan["lignes"], bilan["jobs"], bilan["jetons"]
    abouties = "?" if lignes["abouties"] is None else lignes["abouties"]
    postes = [f"abouties {abouties}/{lignes['depart']}",
              f"faux départs {jobs['faux_departs']}",
              f"{_jetons_lisibles(jetons['total'])} jetons"]
    postes.append(f"{_jetons_lisibles(jetons['par_aboutie'])}/aboutie"
                  if jetons["par_aboutie"] is not None
                  else "pas de jetons/aboutie (0 aboutie)")
    refus = bilan["refus_ecriture"]
    if refus:
        postes.append(f"{refus['outil']} {refus['appels']} appels, "
                      f"{refus['refuses']} refusé{'s' if refus['refuses'] > 1 else ''}")
    else:
        postes.append(f"{_REFUS_OUTIL} non mesuré "
                      f"({bilan['refus_ecriture_omis']})")
    return f"bilan flotte {bilan['flotte']} : " + " · ".join(postes)


def chemin_json(spec) -> Optional[str]:
    """Le bilan se pose À CÔTÉ de sa déclaration : `flotte.yaml` →
    `flotte.bilan.json`. Une flotte construite en mémoire (banc de test, appel
    embarqué) n'a pas de déclaration sur disque : il n'y a rien à écrire."""
    source = getattr(spec, "source", "")
    return os.path.splitext(source)[0] + ".bilan.json" if source else None


def _ecrire(chemin: str, bilan: dict) -> None:
    """Réécriture ATOMIQUE : le pilote lit ce fichier pendant que la flotte
    tourne — il ne doit jamais tomber sur un JSON à moitié écrit."""
    dossier = os.path.dirname(os.path.abspath(chemin))
    try:
        fd, tmp = tempfile.mkstemp(dir=dossier, suffix=".bilan.tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(bilan, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, chemin)
    except OSError as e:
        logger.error("bilan : écriture de %s impossible : %s", chemin, e)


def ecrire_bilan(spec, backend, jobs: dict, *, lignes_initiales: int,
                 secondes: float, arret: str = "") -> dict:
    """Calcule le bilan de la flotte, le journalise et le pose en JSON.

    `jobs` = les jobs CONCLUS de la flotte (id → job), `lignes_initiales` = le
    compte relevé au lancement, `secondes` = depuis ce lancement, `arret` = le
    motif quand c'est le bilan de FIN (vide pendant la flotte). Rend le bilan,
    pour que l'appelant n'ait rien à recalculer."""
    postes = _postes_jobs(jobs)
    restantes = _restantes(spec, backend)
    abouties = None if restantes is None else max(0, lignes_initiales - restantes)
    conclus = postes["termines"] + postes["echoues"]
    refus, refus_omis = _refus_ecriture(spec, backend, secondes)
    bilan = {
        "horodatage": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # Le nom de la flotte est le TAG apposé à ses jobs : c'est par lui
        # qu'on retrouve une campagne, pas par le tableau qu'elle draine.
        "flotte": spec.name,
        "namespace": spec.namespace,
        "procedure": spec.procedure,
        "final": bool(arret),
        "arret": arret or None,
        "secondes": round(float(secondes), 1),
        "lignes": {"depart": lignes_initiales, "restantes": restantes,
                   "abouties": abouties},
        "jobs": {"termines": postes["termines"], "echoues": postes["echoues"],
                 "faux_departs": postes["faux_departs"]},
        "jetons": {"total": postes["jetons"],
                   "par_job": round(postes["jetons"] / conclus) if conclus else None,
                   # Le vrai coût d'une campagne : ce que coûte une ligne qui
                   # ABOUTIT, jamais ce que coûte un job (un job peut n'avoir
                   # rien produit). Aucune aboutie ⟹ null, pas une division.
                   "par_aboutie": (round(postes["jetons"] / abouties)
                                   if abouties else None)},
        "ecritures": {"claims": postes["claims"], "writes": postes["writes"]},
        "refus_ecriture": refus,
        "refus_ecriture_omis": refus_omis,
    }
    logger.info("%s", _ligne(bilan))
    chemin = chemin_json(spec)
    if chemin:
        _ecrire(chemin, bilan)
    else:
        logger.debug("bilan non posé : la flotte n'a pas de déclaration sur disque")
    return bilan
