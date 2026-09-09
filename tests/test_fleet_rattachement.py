"""La flotte se DÉCLARE en base et RATTACHE ses travaux (#791).

Une flotte vivait dans un fichier YAML sur la machine, et ses jobs portaient un
tag texte `payload["fleet"]`. **Un tag vit dans un JSON libre** : il ne se compte
pas, ne porte aucune clé étrangère, et rien ne refuse celui d'une autre org. Le
rattachement de référence est désormais `fleet_id` — une colonne, une contrainte,
et de quoi agréger un passage (`runner.fleets op=state`).

Ce que ces tests exigent, dans l'ordre de ce qui coûterait le plus cher :

1. **Une déclaration qui ÉCHOUE n'arrête pas le passage.** Le rattachement sert à
   LIRE ; il ne conditionne pas le travail. *Un dispositif d'observation qui casse
   ce qu'il observe est pire que pas d'observation.*
2. **Chaque job porte l'identifiant obtenu** — sinon `op=state` reste muet sur un
   passage qui tourne : actif et invisible, le pire des deux mondes.
3. **Un identifiant déjà déclaré est REPRIS, pas redéclaré** — relancer une
   flotte interrompue éclaterait sinon son état sur deux passages, et aucun des
   deux ne dirait la vérité sur ce qui a été fait.
"""
from __future__ import annotations

from oto_runner.backend import BackendError
from oto_runner.fleet import FleetSpec, run_fleet

from tests.test_fleet import FauxBackend, Horloge


def _spec(**kw) -> FleetSpec:
    base = dict(# ⚠️ L'instruction est OBLIGATOIRE depuis le 04/09 : il n'y a plus de
                # défaut dans le worker, qui ne sait pas ce qu'elle doit dire.
                input="Traite ce que la file te donne selon la procédure.",
                procedure="p", namespace="ns", name="flotte-demo",
                tools=("data_claim_next",), filter={"statut": "a_traiter"},
                concurrency=3, ramp_seconds=0)
    base.update(kw)
    return FleetSpec(**base)


def _run(spec, backend):
    h = Horloge()
    return run_fleet(spec, backend, sleep=h.sleep, clock=h.clock, poll_s=20)


class _DeclarationRefusee(FauxBackend):
    """La plateforme refuse la déclaration — le passage doit tourner quand même."""

    def declarer_flotte(self, **kw):
        self.declarations = getattr(self, "declarations", [])
        self.declarations.append(kw)
        raise BackendError("503 — la plateforme ne répond pas")


# ── ① le passage tourne même si la déclaration échoue ────────────────────────

def test_une_declaration_qui_echoue_ABANDONNE_le_passage():
    """⚠️ Renversement du 09/09/2026, et il faut dire ce qu'il défait.

    Ce banc affirmait l'inverse, avec un argument sérieux : refuser de partir
    ferait dépendre le TRAVAIL d'un dispositif qui ne sert qu'à le LIRE — une
    campagne bloquée parce que l'observabilité est en panne. C'était juste tant
    qu'un ordonnanceur LOCAL tenait le passage : on pouvait toujours le tuer.

    Le renversement a périmé cet argument. La déclaration n'est plus ce qui
    permet de lire le passage, c'est ce qui permet de L'ARRÊTER : sans elle,
    `op=stop` n'a aucune prise, et les travaux vont à leur terme quoi qu'il
    arrive. Ce n'est plus de l'observabilité, c'est du contrôle.

    Vécu le 09/09/2026 : cinq travaux partis sans rattachement, aboutis, et
    personne ne pouvait les arrêter. La règle de la maison le disait déjà —
    « pas de fallback qui masque un problème, lever une erreur »."""
    import pytest
    b = _DeclarationRefusee(counts=[3, 3, 0, 0])
    with pytest.raises(RuntimeError) as e:
        _run(_spec(), b)
    assert getattr(b, "declarations", []), "la déclaration a bien été TENTÉE"
    assert not getattr(b, "enfiles", 0), (
        "et RIEN n'a été enfilé : on abandonne avant, pas après — sinon on "
        "laisse derrière soi exactement ce qu'on refuse")
    assert "ABANDONNÉ" in str(e.value) and "op=stop" in str(e.value), (
        "le refus dit ce qui a été fait et pourquoi, sans quoi il fait relancer "
        "le même passage à l'identique")


# ── ② chaque travail porte l'identifiant obtenu ──────────────────────────────

def test_chaque_travail_porte_l_identifiant_de_sa_flotte():
    """Sans ça, `op=state` répond « aucun travail rattaché » pour un passage qui
    tourne."""
    b = FauxBackend(counts=[3, 3, 0, 0])
    _run(_spec(), b)
    assert b.enfiles >= 1
    assert set(b.rattachements) == {42}, (
        f"tous les travaux portent l'identifiant déclaré — vu : {b.rattachements}")


def test_la_declaration_porte_la_cible_le_perimetre_et_les_bornes():
    """Ce qui vivait dans le YAML entre en base : c'est ce qui donne un DOMICILE
    aux gardes, au lieu de les laisser dans un fichier sur une machine."""
    b = FauxBackend(counts=[3, 3, 0, 0])
    _run(_spec(volume=150, budget_tokens=1_000_000), b)
    d = b.declarations[0]
    assert d["label"] == "flotte-demo" and d["procedure"] == "p"
    assert d["namespace"] == "ns" and d["row_filter"] == {"statut": "a_traiter"}
    assert d["max_rows"] == 150 and d["max_tokens"] == 1_000_000
    assert d["tools"] == ["data_claim_next"]


# ── ③ un identifiant déclaré est REPRIS, pas redéclaré ───────────────────────

def test_un_identifiant_dans_la_spec_reprend_le_passage(monkeypatch):
    """Relancer une flotte interrompue doit REPRENDRE son passage. Redéclarer
    éclaterait son état sur deux lignes, dont aucune ne dirait la vérité."""
    b = FauxBackend(counts=[3, 3, 0, 0])
    _run(_spec(fleet_id=9), b)
    assert not getattr(b, "declarations", []), (
        "aucune déclaration : l'identifiant était donné, le passage se REPREND")
    assert set(b.rattachements) == {9}
