"""Le FIL d'un run, tel qu'on le TRANSPORTE au modèle après une reprise.

Le fil persisté au backend n'est jamais touché ; ce module ne sait que
reconstruire la vue cohérente qu'une API de complétion accepte. Sorti du worker
le 06/09/2026 (une fonction pure, sans concept du worker), pour que celui-ci
tienne dans ses cinq cents lignes.
"""
from __future__ import annotations


def assainir_pour_transport(historique: list) -> list:
    """Le fil TRANSPORTÉ doit être cohérent pour l'API de complétion — le fil
    persisté, lui, n'est jamais touché. Les morts en plein tour et les 502
    « rendus après écriture » laissent trois incohérences, toutes vécues la
    même nuit et toutes PERSISTANTES (chaque re-claim re-frappe le même 400
    jusqu'à l'échec définitif) : un tour assistant final sans (tous) ses
    résultats (« Expected last role User or Tool », « Not the same number of
    function calls and responses »), un résultat d'outil ORPHELIN ou DOUBLÉ
    (« Unexpected tool call id in tool results »), et un segment incomplet en
    MILIEU de fil — le tour qu'une reprise antérieure avait écarté de son
    transport reste dans le fil persisté, et la suite s'appose après lui.
    On reconstruit donc LA VUE QUE LE MODÈLE REPRIS A RÉELLEMENT EUE : chaque
    résultat répond à un appel du tour assistant ouvert (premier gagne, le
    reste est écarté), un segment incomplet saute ENTIER, et le fil ne se
    termine jamais par un tour assistant."""
    out: list = []
    attendus: set = set()
    seg_debut = None
    for t in historique:
        t = t or {}
        role = t.get("role")
        if role == "tool":
            tid = t.get("tool_call_id")
            if tid in attendus:
                attendus.discard(tid)
                out.append(t)
            continue
        if attendus and seg_debut is not None:
            del out[seg_debut:]
        attendus, seg_debut = set(), None
        if role == "assistant":
            appels = t.get("tool_calls") or []
            if appels:
                attendus = {c.get("id") for c in appels}
                seg_debut = len(out)
        out.append(t)
    if attendus and seg_debut is not None:
        del out[seg_debut:]
    while out and (out[-1] or {}).get("role") == "assistant":
        out.pop()
    return out
