"""Le travail qui a RÉSERVÉ une ligne sans rien y écrire : « sans_ecriture », pas « done ».

⚠️ Le worker ne juge pas ce que l'agent a produit : il ne sait pas ce qu'écrire
veut dire (cf. `worker._traiter`). C'est la DÉCLARATION du passage qui le lui dit,
comme elle lui dit sa température :

    ecriture_attendue:
      reservation: data_claim_next    # l'outil qui tient une ligne
      ecriture: data_write            # l'outil sans lequel la ligne n'a rien reçu
      ligne: row                      # (défaut) la clé de la réservation qui porte la ligne

Un travail conclu (`done`) qui a tenu une ligne — réservation aboutie ET non vide —
sans aucun appel réussi de l'outil d'écriture se compte `sans_ecriture` au journal
et au bilan. La plateforme reçoit toujours `done` à `run_finish` : elle ne juge pas
ce cas (#143, fermée) ; l'instrument, c'est le runner. Sans la clé, rien n'est jugé.

Mesuré le 10/09/2026 (banc Audiens 660) : un agent a réservé une ligne, n'a appelé
aucun outil et a rendu un compte rendu inventé ; le bilan l'a compté abouti, et la
ligne a manqué au tour.
"""
from __future__ import annotations

import json
from typing import Callable, Optional

ISSUE = "sans_ecriture"
_CLES = {"reservation", "ecriture", "ligne"}


def lire(declaree) -> Optional[dict]:
    """La clé telle que déclarée, validée ; None si le passage ne la déclare pas.
    Mal formée, elle lève : un réglage qui n'arrive nulle part coûte plus cher
    qu'un réglage absent."""
    if declaree is None:
        return None
    if (not isinstance(declaree, dict) or not declaree.get("reservation")
            or not declaree.get("ecriture") or set(declaree) - _CLES):
        raise ValueError("ecriture_attendue : attendu {reservation: <outil>, "
                         f"ecriture: <outil>[, ligne: <clé>]}}, reçu {declaree!r}")
    return {"reservation": str(declaree["reservation"]),
            "ecriture": str(declaree["ecriture"]),
            "ligne": str(declaree.get("ligne") or "row")}


def _est(nom: str, outil: str) -> bool:
    # Le connecteur MCP peut PRÉFIXER les noms (`<connecteur>_data_write`) :
    # l'appartenance se teste par suffixe, jamais par égalité.
    return nom == outil or nom.endswith("_" + outil)


def verdict_vide(attendu: Optional[dict]) -> Optional[Callable[[str, str], bool]]:
    """Le `a_vide` que la boucle pose sur chaque pas (cf. `agent_runtime.run`) :
    une réservation qui a abouti sans rendre de ligne. None sans déclaration."""
    if not attendu:
        return None

    def a_vide(nom: str, sortie: str) -> bool:
        if not _est(nom, attendu["reservation"]):
            return False
        try:
            charge = json.loads(sortie)
        except (TypeError, ValueError):
            # Illisible : on ne la dit pas vide. Un faux « sans écriture » se voit
            # et se relit au journal ; un vrai, caché, ne se verrait jamais.
            return False
        return isinstance(charge, dict) and not charge.get(attendu["ligne"])

    return a_vide


def issue(outcome: str, steps, attendu: Optional[dict]) -> str:
    """L'issue écrite au journal et au bilan. Seul `done` peut devenir
    `sans_ecriture` ; sans déclaration, l'issue est celle de la boucle."""
    if not attendu or outcome != "done":
        return outcome
    tenue = any(_est(s.tool, attendu["reservation"]) and s.ok and not s.vide
                for s in steps)
    ecrite = any(_est(s.tool, attendu["ecriture"]) and s.ok for s in steps)
    return ISSUE if tenue and not ecrite else outcome
