"""Un claim à VIDE n'est pas un faux départ — la fin de file n'est pas une panne.

Vécu le 28/08 : une flotte de 20 lignes ABOUTIE (18 traitées, les 2 dernières
sous bail chez des pairs encore en vol) s'est arrêtée en `exit 1` sur « 5 faux
départs consécutifs ». Les jobs suivants ne faisaient qu'UN appel — le claim,
qui ne rendait plus rien — et chacun était compté comme une réservation sans
écriture. En fin de file il y a TOUJOURS plus d'agents que de lignes : sans
cette distinction, toute flotte se termine désormais en panne, et une montée
par paliers s'arrête à tort.

Ce que ce banc fige, aux quatre étages : la sortie du claim (`row: null`), le
pas marqué par la boucle, le verdict du worker sur ses deux chemins, la borne
du driver, et le bilan — les deux derniers doivent dire la MÊME chose, sinon
le pilotage contredit la borne.
"""
from __future__ import annotations

from oto_runner import agent_runtime
from oto_runner import worker as W
from oto_runner.agent_runtime import AgentResult, AgentSpec, AgentStep
from oto_runner.bilan import ecrire_bilan
from tests.test_agent_runtime import FauxProvider, FauxTransport, _turn
from tests.test_bilan import BackendBilan
from tests.test_fleet import FauxBackend as FauxFile
from tests.test_fleet import _run, _spec
from tests.test_worker_reprise import FauxBackend, FauxMcp

_CLAIM = "demo-connecteur_data_claim_next"
_WRITE = "demo-connecteur_data_write"


# ── 1. la sortie du claim ────────────────────────────────────────────────────
def test_une_reservation_sans_ligne_se_lit_a_la_sortie():
    """`row: null` = la file n'avait rien à rendre. Le nom d'outil arrive
    parfois préfixé par le connecteur : l'appartenance se teste par SUFFIXE."""
    assert W._claim_sans_ligne(_CLAIM, '{"row": null}') is True
    assert W._claim_sans_ligne("data_claim_next", '{"row":null,"restantes":0}') is True
    assert W._claim_sans_ligne(_CLAIM, '{"row": {"id": 7}}') is False
    assert W._claim_sans_ligne("serper_search", '{"row": null}') is False, \
        "la question ne se pose que pour une réservation"
    assert W._claim_sans_ligne(_CLAIM, '{"row": null, "x": …[sortie tronquée') \
        is True, "une charge tronquée porte encore sa marque"
    assert W._claim_sans_ligne(_CLAIM, "ligne réservée") is False, \
        "on ne devine pas : sans marque, l'appel compte comme avant"


def test_la_boucle_marque_le_pas_dun_claim_a_vide():
    """La boucle ne connaît pas la sémantique des outils — elle pose au domaine
    la question « cet appel a-t-il rien rendu ? » et marque le pas."""
    spec = AgentSpec(system="s", tools=frozenset({_CLAIM}), max_steps=3)

    def _jouer(sortie):
        t = FauxTransport({_CLAIM: (sortie, False)})
        p = FauxProvider([_turn(calls=[(_CLAIM, {})]), _turn(text="conclu")])
        return agent_runtime.run(spec, t, p, prompt="go",
                                 a_vide=W._claim_sans_ligne).steps[0]

    assert _jouer('{"row": null}').vide is True
    assert _jouer('{"row": {"siren": "1"}}').vide is False
    t = FauxTransport({_CLAIM: ('{"row": null}', False)})
    p = FauxProvider([_turn(calls=[(_CLAIM, {})]), _turn(text="conclu")])
    assert agent_runtime.run(spec, t, p, prompt="go").steps[0].vide is False, \
        "sans question posée, aucun pas n'est vide — la boucle n'invente rien"


# ── 2. le verdict du worker, sur ses deux chemins ────────────────────────────
def _job():
    return {"id": 7, "kind": "start", "run_id": None,
            "payload": {"procedure": "demo", "namespace": "lignes-demo",
                        "tools": [_CLAIM], "input": "Vas-y.", "max_steps": 3}}


def _conclure(monkeypatch, etapes, provider=None):
    """Le résultat DÉCLARÉ d'un job dont la boucle a rendu `etapes`."""
    def faux_run(spec, transport, provider, prompt=None, history=None,
                 on_turn=None, **_):
        return AgentResult(reply="fini", stopped="end_turn", steps=etapes)

    monkeypatch.setattr(W.agent_runtime, "run", faux_run)
    monkeypatch.setattr(W, "McpSession", FauxMcp)
    b = FauxBackend()
    W._traiter(b, _job(), provider=provider)
    return next(a for a in b.appels if a[0] == "complete_result")[1]


def test_un_job_a_claim_vide_nest_pas_un_faux_depart(monkeypatch):
    """LE cas du 28/08 : un job qui n'a rien pu réserver n'a rien à écrire.
    Il le DÉCLARE (`claim_vide`) pour que le driver et le bilan le lisent."""
    r = _conclure(monkeypatch, [AgentStep(tool=_CLAIM, ok=True, duration_ms=1,
                                          vide=True)])
    assert (r["claims"], r["writes"]) == (0, 0)
    assert r["claim_vide"] is True and r["faux_depart"] is False
    assert r["tool_counts"] == {_CLAIM: 1}, \
        "l'APPEL reste compté — c'est la RÉSERVATION qui n'a pas eu lieu"


def test_un_job_qui_a_reserve_sans_ecrire_reste_un_faux_depart(monkeypatch):
    """Non-régression : la borne existe pour ça — une ligne réservée, du
    travail fait, et rien d'écrit."""
    r = _conclure(monkeypatch, [
        AgentStep(tool=_CLAIM, ok=True, duration_ms=1),
        AgentStep(tool="serper_search", ok=True, duration_ms=1),
        AgentStep(tool="fr_get", ok=True, duration_ms=1)])
    assert (r["claims"], r["writes"]) == (1, 0)
    assert r["claim_vide"] is False and r["faux_depart"] is True


def test_un_job_qui_a_ecrit_nest_ni_lun_ni_lautre(monkeypatch):
    r = _conclure(monkeypatch, [
        AgentStep(tool=_CLAIM, ok=True, duration_ms=1),
        AgentStep(tool=_WRITE, ok=True, duration_ms=1)])
    assert (r["claims"], r["writes"]) == (1, 1)
    assert r["claim_vide"] is False and r["faux_depart"] is False


class ProviderOneShot:
    """Le chemin Conversations : la boucle tourne CHEZ le fournisseur, le worker
    ne voit aucune sortie d'outil — seulement le relevé des appels."""

    ONE_SHOT = True

    def __init__(self, outils):
        self.outils = tuple(outils)

    def run_once(self, *, instructions, inputs, tools):
        return AgentResult(reply="fini", stopped="end_turn", steps=[
            AgentStep(tool=o, ok=True, duration_ms=1) for o in self.outils])


def test_en_conversations_un_seul_appel_na_rien_reserve(monkeypatch):
    """Règle de REPLI, explicite : sans sortie d'outil, un job qui n'a fait
    qu'UN appel n'a pu faire que le claim — il n'a donc rien réservé."""
    r = _conclure(monkeypatch, [], provider=ProviderOneShot([_CLAIM]))
    assert (r["claims"], r["claim_vide"], r["faux_depart"]) == (0, True, False)


def test_en_conversations_deux_appels_ne_dispensent_plus_du_verdict(monkeypatch):
    """Au-delà d'UN appel on ne sait pas — et on compte, comme avant : cinq
    appels sans une écriture restent un faux départ."""
    r = _conclure(monkeypatch, [], provider=ProviderOneShot(
        [_CLAIM, "serper_search", "fr_get", "fr_get", "serper_search"]))
    assert (r["claims"], r["claim_vide"], r["faux_depart"]) == (1, False, True)


# ── 3. la borne du driver ────────────────────────────────────────────────────
class FileDeClaimsAVide(FauxFile):
    """Des jobs qui concluent sans avoir rien pu réserver — la fin de file."""

    def get_job(self, jid):
        j = super().get_job(jid)
        if j["status"] == "done":
            j["result"]["claim_vide"] = True
            j["result"]["tool_counts"] = {_CLAIM: 1}
        return j


def test_des_claims_a_vide_en_serie_narretent_pas_la_flotte():
    """Le cas vécu : plus d'agents que de lignes en fin de file. Douze jobs à
    vide d'affilée, et la flotte s'arrête NORMALEMENT — sur la file vide."""
    from oto_runner import fleet as F

    b = FileDeClaimsAVide(counts=[10] * 14 + [0])
    bilan = _run(_spec(ramp_seconds=0), b)
    assert bilan.arret == "file vide", \
        "un claim à vide ne dit RIEN de la santé de la campagne"
    assert bilan.done > 5, "la borne des 5 aurait mordu avant"
    assert any(bilan.arret.startswith(m) for m in F._ARRETS_NORMAUX), \
        "arrêt NORMAL : exit 0, aucune relance systemd"


def test_un_claim_a_vide_ne_remet_pas_le_compteur_a_zero():
    """Ignorer, pas remettre à zéro : la remise à zéro rendrait la borne
    contournable par alternance — un vrai faux départ, un claim à vide, et la
    flotte tournerait à vide indéfiniment."""
    class FileAlternee(FauxFile):
        def get_job(self, jid):
            j = super().get_job(jid)
            if j["status"] == "done":
                vide = jid % 2 == 0
                j["result"]["claim_vide"] = vide
                j["result"]["faux_depart"] = not vide
                j["result"]["tool_counts"] = {_CLAIM: 1, "fr_get": 2}
            return j

    bilan = _run(_spec(ramp_seconds=0), FileAlternee(counts=[100, 100]))
    assert "faux départs consécutifs" in bilan.arret


# ── 4. le bilan, qui doit dire la même chose que la borne ────────────────────
def test_le_bilan_applique_la_meme_regle_que_la_borne():
    """Un bilan qui compterait des faux départs là où la borne n'en voit pas
    (ou l'inverse) rendrait le pilotage inutilisable : c'est le même job."""
    jobs = {
        # déclaré par un worker à jour : le claim n'a rien rendu
        1: {"status": "done", "result": {"usage_tokens": 100, "claims": 0,
                                         "writes": 0, "claim_vide": True,
                                         "faux_depart": False}},
        # dérivé de `tool_counts` (job en échec, sans marqueur) : UN appel, donc
        # rien de réservé — la même règle de repli que le worker
        2: {"status": "failed", "result": {"usage_tokens": 50,
                                           "tool_counts": {_CLAIM: 1}}},
        # celui-là a bien réservé, cherché, et n'a rien écrit
        3: {"status": "done", "result": {"usage_tokens": 900,
                                         "tool_counts": {_CLAIM: 1, "fr_get": 4}}},
    }
    bilan = ecrire_bilan(_spec(), BackendBilan(restantes=1), jobs,
                         lignes_initiales=2, secondes=60)
    assert bilan["jobs"]["faux_departs"] == 1, "le seul qui ait réservé sans écrire"
    assert bilan["ecritures"] == {"claims": 1, "writes": 0}
