"""Le worker sert PLUSIEURS organisations, et la liste vient de l'appartenance.

⚠️ Ce que ce fichier ferme, mesuré le 09/09/2026 : un worker qui ne nommait
aucune organisation n'était pas refusé — `current_org` se replie sur l'org
**maison** du porteur du jeton. Il sondait donc silencieusement UNE organisation
sur N, et les campagnes des autres n'étaient jamais servies. Une panne qui se lit
« tout va bien » dans les journaux : la file est vide, parce qu'on regarde la
mauvaise.

La forme retenue, contre les deux autres étudiées : le tour d'organisations dont
le compte est MEMBRE. On ajoute un client en l'invitant, on le retire en
révoquant — aucun privilège, aucun réglage, aucun redéploiement.
"""
from __future__ import annotations

import pytest

import oto_runner.worker as worker
from oto_runner.backend import BackendError


def _reset():
    worker._arret_demande = False


class _Plateforme:
    """Des travaux rangés PAR organisation — la seule façon de voir qui est servi."""

    def __init__(self, par_org: dict, orgs=None, orgs_leve=None):
        self.par_org = {k: list(v) for k, v in par_org.items()}
        self._orgs = orgs if orgs is not None else sorted(par_org)
        self._orgs_leve = orgs_leve
        self.base = "https://exemple.invalide"
        self.org = None
        self.sondees: list = []       # l'ordre EXACT des organisations sondées
        self.lectures = 0

    def mes_orgs(self):
        self.lectures += 1
        if self._orgs_leve:
            raise BackendError(self._orgs_leve)
        return list(self._orgs)

    #: ⚠️ Une boucle de worker n'a pas de fin : si la rotation se casse, elle
    #: sonde à l'infini l'organisation qui n'a rien et le banc PEND au lieu
    #: d'échouer. Mesuré en injectant un mutant « le tour rembobine » le
    #: 09/09/2026 — la CI se serait arrêtée sur un délai, sans rien nommer.
    PLAFOND_SONDAGES = 200

    def noter(self, org):
        """LE point unique où un sondage est compté — y compris depuis un `claim`
        surchargé par un banc. La garde posée dans `claim` seul laissait pendre
        le test qui remplace la méthode (vérifié : 120 s de délai, rien de
        nommé)."""
        self.sondees.append(org)
        if len(self.sondees) > self.PLAFOND_SONDAGES:
            raise AssertionError(
                f"{self.PLAFOND_SONDAGES} sondages sans vider les files : la "
                f"rotation ne tourne pas. Organisations sondées : "
                f"{self.sondees[:12]}… ; restant : "
                f"{ {k: len(v) for k, v in self.par_org.items()} }")

    def claim(self, lease_seconds=600, depot="", org=None):
        self.noter(org)
        file = self.par_org.get(org) or []
        if not file:
            if not any(self.par_org.values()):
                worker._demander_arret(15, None)   # tout servi : on fait finir
            return None
        return file.pop(0)

    def complete(self, job_id, ok=True, error=None, run_id=None, result=None):
        pass


class _Provider:
    ONE_SHOT = True
    __name__ = "agent_conversations"

    @staticmethod
    def resolve_key():
        return "x"

    @staticmethod
    def model():
        return "un-modele"


def _lancer(monkeypatch, backend, vus=None):
    monkeypatch.setenv("OTO_RUNNER_ARMED", "1")
    monkeypatch.setattr(worker, "Backend", lambda *a, **kw: backend)
    monkeypatch.setattr(worker, "get_provider", lambda: _Provider)
    monkeypatch.setattr(worker, "_POLL_S", 0)

    def _traiter(bk, job, prov, **kw):
        if vus is not None:
            # ⚠️ On note l'org PORTÉE PAR LE CLIENT au moment du traitement : c'est
            # elle qui décidera où partent la conclusion et la prolongation de bail.
            vus.append((job["id"], bk.org))

    monkeypatch.setattr(worker, "_traiter", _traiter)
    _reset()
    worker.main()


# ── Le tour ──────────────────────────────────────────────────────────────────

def test_les_trois_organisations_sont_sondees_a_TOUR_de_role(monkeypatch):
    b = _Plateforme({7: [], 8: [{"id": 1, "kind": "start"}], 9: []})
    _lancer(monkeypatch, b)
    assert b.sondees[:3] == [7, 8, 9], (
        "un tour STRICT : sans lui, l'organisation du milieu n'est jamais atteinte")


def test_une_organisation_chargee_ne_MONOPOLISE_pas_le_worker(monkeypatch):
    """Rembobiner sur celle qui vient de servir irait plus vite pour elle et
    affamerait les autres — c'est le mode d'échec de « celle du travail
    précédent », l'une des deux formes écartées."""
    b = _Plateforme({7: [{"id": i, "kind": "start"} for i in range(3)],
                     8: [{"id": 99, "kind": "start"}]})
    vus: list = []
    _lancer(monkeypatch, b, vus)
    assert 99 in [i for i, _ in vus], (
        "l'organisation 8 doit être servie sans attendre que 7 soit vidée")


def test_le_client_porte_l_organisation_du_travail_RESERVE(monkeypatch):
    """Le bug le plus cher si on l'oublie, et le plus silencieux : la réservation
    part sur la bonne organisation, puis la conclusion et la prolongation de bail
    repartent sur l'organisation MAISON du jeton — et ne retrouvent pas le
    travail. C'est le geste que `run_fleet` fait déjà pour un passage déclaré."""
    b = _Plateforme({7: [], 8: [{"id": 42, "kind": "start"}]})
    vus: list = []
    _lancer(monkeypatch, b, vus)
    assert (42, 8) in vus, "le client doit porter 8, pas la maison du jeton"


def test_une_organisation_qui_REFUSE_n_arrete_pas_les_autres(monkeypatch):
    """Elle a pu révoquer l'appartenance entre deux relectures de la liste."""
    b = _Plateforme({7: [], 8: [{"id": 5, "kind": "start"}]})
    vraie = b.claim

    def claim(lease_seconds=600, depot="", org=None):
        if org == 7:
            b.noter(org)
            raise BackendError("403 : plus membre", status=403)
        return vraie(lease_seconds=lease_seconds, depot=depot, org=org)

    b.claim = claim
    vus: list = []
    _lancer(monkeypatch, b, vus)
    assert [i for i, _ in vus] == [5]


# ── D'où vient la liste ──────────────────────────────────────────────────────

def test_AUCUNE_organisation_fait_sortir_le_worker(monkeypatch):
    """Lu, et vide : défaut de configuration permanent. Un worker qui entre dans
    sa boucle pour y sonder le vide est un worker dont personne ne verra qu'il
    ne sert à rien."""
    b = _Plateforme({}, orgs=[])
    with pytest.raises(SystemExit) as e:
        _lancer(monkeypatch, b)
    assert "AUCUNE organisation" in str(e.value)
    assert "Invite-le" in str(e.value), "le refus dit comment en sortir"


def test_une_plateforme_injoignable_au_boot_ne_fait_PAS_sortir(monkeypatch):
    """L'autre vide, et il ne se traite pas pareil : « je n'ai pas pu regarder »
    est transitoire. Un redémarrage pendant une bascule de couleur ne doit pas
    éteindre la flotte — c'est la propriété que `test_plateforme_indisponible`
    garde pour la réservation, et elle vaut aussi pour la liste."""
    b = _Plateforme({}, orgs_leve="502 Bad Gateway")

    # La boucle tournerait à vide : on la fait sortir au deuxième essai de lecture.
    def apres_deux(*a, **kw):
        b.lectures += 1
        if b.lectures >= 2:
            worker._demander_arret(15, None)
        raise BackendError("502 Bad Gateway")

    b.mes_orgs = apres_deux
    _lancer(monkeypatch, b)          # ne lève pas : c'est TOUT ce qu'on demande
    assert b.lectures >= 2, "l'agent doit RÉESSAYER, pas seulement survivre"


def test_une_invitation_prend_effet_SANS_redemarrage(monkeypatch):
    """Sinon « inviter le compte » ne remplace pas un redéploiement, et la forme
    retenue perd la seule chose qui la rendait meilleure que les autres."""
    b = _Plateforme({7: [], 8: [{"id": 77, "kind": "start"}]}, orgs=[7])

    def puis_deux():
        b.lectures += 1
        return [7] if b.lectures < 2 else [7, 8]

    b.mes_orgs = puis_deux
    monkeypatch.setattr(worker.Rotation, "TTL_S", 0)   # le délai, pas le mécanisme
    vus: list = []
    _lancer(monkeypatch, b, vus)
    assert (77, 8) in vus, "l'organisation ajoutée doit être servie sans relance"
