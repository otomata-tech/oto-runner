"""Les postes du bilan lus AU SERVEUR : l'issue réelle des lignes, les refus d'écriture.

Deux lectures que le bilan de flotte ne faisait pas, et qui lui ont fait dire
« abouties 3/3 » sur un passage où deux lignes sur trois avaient fini en `echec`
— l'état d'ABANDON du cycle de vie, posé par la plateforme après trois
réservations sans écriture (06/09/2026, `banc-v151-medium`).

- **L'issue d'une ligne se lit à sa colonne de statut**, pas à sa sortie de la
  file. « Sortie » = ne correspond plus au filtre de réservation ; une ligne
  abandonnée est sortie autant qu'une ligne enrichie. La colonne ne se devine
  pas : c'est le field `role="status"` du schéma du tableau, celui qui porte le
  cycle de vie (`lifecycle.terminal`, `lifecycle.abandon_state`) — exactement ce
  que le serveur lit pour abandonner une ligne.

- **Un refus d'écriture se lit ENTIER, et ne s'interprète pas.** Le motif du
  schéma nomme la colonne et la raison ; c'était la partie coupée. Et le
  classificateur qui rangeait les textes sous des libellés de son cru inventait
  (cf. `backend.py`). Le détail garde le texte complet et le `run_id` de l'appel,
  qui mène au travail — et au journal JSONL que le worker a écrit pour lui, quand
  l'ordonnanceur l'a RELU (jamais un chemin supposé).

Rien ici n'arrête une flotte : une lecture impossible rend un poste `omis` qui
dit POURQUOI — jamais un zéro, jamais un chiffre inventé.
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Optional


logger = logging.getLogger("oto_runner.bilan")

REFUS_OUTIL = "data_write"
REFUS_FENETRE_MAX_MIN = 24 * 60   # une campagne dure des semaines : on plafonne
REFUS_LIMITE = 200                # les N derniers appels lus au journal d'org


def valeur(x):
    """La valeur d'une case, nue ou en couches (`{"valeur": …, "comment": …}`)."""
    return x.get("valeur") if isinstance(x, dict) and "valeur" in x else x


# ── L'issue des lignes ───────────────────────────────────────────────────────

def _champ_statut(schema: Optional[dict]) -> Optional[dict]:
    for f in (schema or {}).get("fields") or []:
        if isinstance(f, dict) and f.get("role") == "status" and f.get("key"):
            return f
    return None


def _terminaux(lifecycle: dict) -> list:
    """Les états terminaux : `terminal` explicite, sinon les états sans transition
    sortante. ⚠️ Recopie de `terminal_states` du backend (`datastore/schema.py`) :
    le runner est un client pur, il n'importe rien du serveur — un écart se verrait
    ici, dans un bilan qui compte mal, et se corrige des deux côtés."""
    explicite = lifecycle.get("terminal")
    if isinstance(explicite, list):
        return [str(s) for s in explicite]
    etats = [str(s) for s in lifecycle.get("states") or []]
    sortants = {str(k) for k, v in (lifecycle.get("transitions") or {}).items() if v}
    return [s for s in etats if s not in sortants]


def lignes_par_statut(spec, backend) -> dict:
    """La ventilation des lignes du PÉRIMÈTRE par valeur finale de la colonne de
    statut. Rend `{colonne, perimetre, par_statut, abandon, terminaux}`, ou
    `{omis: raison}` (avec `colonne` quand elle est connue).

    Le périmètre est le filtre de la flotte SANS sa clause de statut : ce qui
    désigne les lignes du passage quel que soit l'état où elles ont fini. Un
    filtre qui ne porte que le statut laisse un périmètre vide — la ventilation
    porte alors sur tout le tableau, et `abouties_de` le dit."""
    ns = getattr(spec, "namespace", None)
    if not ns:
        return {"omis": "déclaration sans tableau"}
    org = getattr(spec, "org", None)
    try:
        schema = backend.schema(ns, org=org)
    except Exception as e:  # noqa: BLE001 — un poste illisible ne tue pas le bilan
        logger.warning("bilan : schéma de %s illisible : %s", ns, e)
        return {"omis": f"schéma illisible : {e}"}
    champ = _champ_statut(schema)
    if champ is None:
        return {"omis": "le schéma ne déclare aucune colonne role=status"}
    colonne = str(champ["key"])
    lifecycle = champ.get("lifecycle") if isinstance(champ.get("lifecycle"), dict) else {}
    perimetre = {k: v for k, v in (getattr(spec, "filter", None) or {}).items()
                 if k != colonne}
    try:
        groupes = backend.aggregate(ns, group_by=colonne, filter=perimetre, org=org)
    except Exception as e:  # noqa: BLE001
        logger.warning("bilan : agrégat par %s illisible : %s", colonne, e)
        return {"omis": f"agrégat illisible : {e}", "colonne": colonne}
    par_statut: dict = {}
    for g in groupes or []:
        v = valeur(g.get(colonne))
        cle = "(vide)" if v is None else str(v)
        par_statut[cle] = par_statut.get(cle, 0) + int(g.get("count") or 0)
    abandon = lifecycle.get("abandon_state")
    return {"colonne": colonne, "perimetre": perimetre, "par_statut": par_statut,
            "abandon": str(abandon) if abandon is not None else None,
            "terminaux": _terminaux(lifecycle)}


def abouties_de(statut: dict, sorties: Optional[int]) -> tuple[Optional[int],
                                                                Optional[str]]:
    """Les lignes ABOUTIES : sorties de la file dans un état terminal qui n'est
    pas l'abandon. Rend (nombre, None) ou (None, raison) — trois états, jamais un
    entier seul qui pourrait vouloir dire « personne n'a regardé »."""
    if "omis" in statut:
        return None, statut["omis"]
    if not statut["perimetre"]:
        return None, ("le filtre ne borne que le statut : la ventilation porte sur "
                      "tout le tableau, les lignes de ce passage ne s'isolent pas")
    if not statut["terminaux"]:
        return None, "le schéma ne déclare pas d'états terminaux"
    n = sum(v for s, v in statut["par_statut"].items()
            if s in statut["terminaux"] and s != statut["abandon"])
    if sorties is not None and n > sorties:
        return None, (f"{n} lignes terminales pour {sorties} sortie(s) : le périmètre "
                      "porte des lignes antérieures à ce passage")
    return n, None


# ── Les refus d'écriture, ENTIERS ────────────────────────────────────────────

def _texte(erreur: str) -> str:
    """Le texte serveur, espaces normalisés — c'est la seule transformation, et
    elle ne change pas un mot : deux refus identiques se groupent, rien d'autre."""
    return " ".join(str(erreur or "").split())


def refus_ecriture(spec, backend, secondes: float, jobs: dict) -> tuple[Optional[dict],
                                                                    Optional[str]]:
    """« n appels, k refusés » sur `data_write`, lu au journal des appels d'org —
    avec chaque refus en DÉTAIL : quand, quel run, quel travail de cette flotte,
    quel journal JSONL (celui que l'ordonnanceur a relu), et le texte complet du
    refus. `motifs` groupe les textes IDENTIQUES ; il n'interprète pas.

    Rend (poste, raison de l'omission) — l'un des deux vaut toujours None."""
    org = getattr(spec, "org", None)
    if org is None:
        return None, "déclaration sans org : le journal des appels n'est pas lisible"
    minutes = max(1, min(int(secondes // 60), REFUS_FENETRE_MAX_MIN))
    try:
        n, ko = backend.tool_health(org, REFUS_OUTIL, minutes=minutes,
                                    limit=REFUS_LIMITE)
    except Exception as e:  # noqa: BLE001 — la sonde ne tue pas la flotte
        logger.warning("bilan : santé de %s illisible : %s", REFUS_OUTIL, e)
        return None, f"journal des appels illisible : {e}"
    # ⚠️ Le DÉTAIL, et non le seul compte. Sous un cran qui empêche la création,
    # fabriquer une entreprise ne laisse plus de ligne : ça devient un refus, et
    # un refus ne se voit que si on le compte — et ne se comprend que si on le
    # lit ENTIER.
    try:
        liste = backend.refus_detail(org, REFUS_OUTIL, minutes=minutes,
                                     limit=REFUS_LIMITE)
    except Exception as e:  # noqa: BLE001
        logger.warning("bilan : détail des refus illisible : %s", e)
        liste = None
    motifs = detail = None
    if liste is not None:
        par_run = {j.get("run_id"): jid for jid, j in jobs.items() if j.get("run_id")}
        motifs, detail = Counter(), []
        for r in liste:
            erreur = _texte(r.get("erreur"))
            job = par_run.get(r.get("run_id"))
            motifs[erreur] += 1
            detail.append({"quand": r.get("quand"), "run_id": r.get("run_id"),
                           "job": job,
                           # Le chemin RELU par l'ordonnanceur, ou null : jamais
                           # un chemin supposé pour un fichier qu'on n'a pas vu.
                           "journal": (jobs[job].get("journal") if job is not None
                                       else None),
                           "erreur": erreur})
        motifs = dict(motifs)
    return ({"outil": REFUS_OUTIL, "fenetre_minutes": minutes, "limite": REFUS_LIMITE,
             "appels": n, "refuses": ko, "motifs": motifs, "detail": detail}, None)
