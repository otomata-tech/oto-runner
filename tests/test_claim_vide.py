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
_RELEASE = "demo-connecteur_data_release"


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


def test_en_conversations_reserver_puis_relacher_na_rien_reserve(monkeypatch):
    """LE geste réel d'une file vide, vu en production le 28/08 : l'agent
    réserve, reçoit `row: null`, RELÂCHE, et conclut « la file est vide ». Deux
    appels, aucun travail — compter les appels ratait ces jobs (3 sur l'étape
    2). La règle porte sur la NATURE des appels, pas sur leur nombre."""
    for outils in ([_CLAIM], [_CLAIM, _RELEASE],
                   [_CLAIM, _RELEASE, _CLAIM, _RELEASE]):
        r = _conclure(monkeypatch, [], provider=ProviderOneShot(outils))
        # Le REPLI rend toujours le bon nombre de lignes — c'est lui qui fonde
        # les bornes de flotte, et il reste juste.
        assert r["claims"] == 0, f"aucun appel de travail dans {outils}"
        # ⚠️ Mais il ne l'AFFIRME plus : la sortie de la réservation n'est pas
        # remontée, donc le poste dit qu'il ne sait pas, et dit pourquoi. Un
        # booléen ici présenterait une déduction comme un fait (#4).
        assert r["claim_vide"] is None, f"le repli affirme encore sur {outils}"
        assert r["claims_mesures"] is False
        assert r["claim_vide_raison"], "un poste qui se tait doit dire pourquoi"


def test_en_conversations_un_seul_appel_de_travail_vaut_reservation(monkeypatch):
    """L'autre bord : dès qu'un outil MÉTIER est appelé, le job a eu une ligne
    à traiter — s'il n'écrit pas, c'est un faux départ, comme avant."""
    r = _conclure(monkeypatch, [], provider=ProviderOneShot([_CLAIM, "fr_get"]))
    assert (r["claims"], r["faux_depart"]) == (1, True)
    assert r["claim_vide"] is None and r["claims_mesures"] is False

    r = _conclure(monkeypatch, [], provider=ProviderOneShot(
        [_CLAIM, _RELEASE, "serper_search"]))
    assert (r["claims"], r["faux_depart"]) == (1, True), \
        "relâcher après avoir travaillé n'efface pas le travail"
    assert r["claim_vide"] is None

    r = _conclure(monkeypatch, [], provider=ProviderOneShot(
        [_CLAIM, "fr_get", _WRITE, _RELEASE]))
    assert (r["claims"], r["writes"], r["faux_depart"]) == (1, 1, False)


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
        # dérivé de `tool_counts` (job en échec, sans marqueur) : que des gestes
        # de TENUE, donc aucun travail et rien de réservé — la même règle de
        # repli que le worker, importée de lui
        2: {"status": "failed", "result": {"usage_tokens": 50,
                                           "tool_counts": {_CLAIM: 1,
                                                           _RELEASE: 1}}},
        # celui-là a bien réservé, cherché, et n'a rien écrit
        3: {"status": "done", "result": {"usage_tokens": 900,
                                         "tool_counts": {_CLAIM: 1, "fr_get": 4}}},
    }
    bilan = ecrire_bilan(_spec(), BackendBilan(restantes=1), jobs,
                         lignes_initiales=2, secondes=60)
    assert bilan["jobs"]["faux_departs"] == 1, "le seul qui ait réservé sans écrire"
    assert bilan["ecritures"] == {"claims": 1, "writes": 0}


class ProviderOneShotQuiRemonte(ProviderOneShot):
    """Le fournisseur qui RETOURNE ses exécutions d'outils.

    ⚠️ C'est le cas courant, et il change tout : la sortie de la réservation est
    lisible, donc le relevé MESURE au lieu de se replier. Sans ce test, un poste
    qui se tairait toujours passerait le premier test sans rien mesurer.
    """

    def __init__(self, outils, row):
        super().__init__(outils)
        self.row = row

    def run_once(self, *, instructions, inputs, tools):
        res = super().run_once(instructions=instructions, inputs=inputs, tools=tools)
        res.raw_outputs = [{"type": "tool.execution", "name": _CLAIM,
                            "info": {"row": self.row}}]
        return res


def test_en_conversations_une_sortie_lisible_redonne_un_verdict(monkeypatch):
    """Dès que la sortie de la réservation remonte, le poste REDEVIENT un
    booléen : se taire n'est légitime que faute de mesure (#4)."""
    r = _conclure(monkeypatch, [], provider=ProviderOneShotQuiRemonte(
        [_CLAIM, _RELEASE], row=None))
    assert r["claims_mesures"] is True, "une sortie lisible est une mesure"
    assert r["claim_vide"] is True, "row: null, donc rien n'a été réservé"
    assert r["claim_vide_raison"] is None, "une mesure n'a pas d'excuse à donner"

    r = _conclure(monkeypatch, [], provider=ProviderOneShotQuiRemonte(
        [_CLAIM, "fr_get"], row={"_id": "019ffb3a-0000-0000-0000-000000000001"}))
    assert r["claims_mesures"] is True
    assert r["claim_vide"] is False, "une ligne a bien été rendue"


def test_le_releve_dit_sur_quelle_fiche(monkeypatch):
    """⚠️ Les gardes nomment des colonnes détruites, réparées, corrigées — et
    le relevé ne disait pas SUR QUELLE FICHE. Mesuré le 01/09 : deux
    corrections d'agent relevées, impossibles à confronter à la source.

    Sans ligne réservée le poste vaut `None` — « pas de fiche », jamais une
    chaîne vide qui se lirait comme un identifiant manquant.
    """
    r = _conclure(monkeypatch, [], provider=ProviderOneShot([_CLAIM, _RELEASE]))
    assert "ligne" in r, "le relevé doit porter la ligne traitée"
    assert r["ligne"] is None


def test_la_reference_qualifie_la_MESURE_pas_la_presence_du_socle(monkeypatch):
    """⚠️ Le poste valait « socle » dès qu'un socle existait, même quand la
    comparaison n'avait pas eu lieu pour cette fiche — il rassurait sans rien
    attester (relevé 10695 du 01/09). Sans mesure, il se tait.
    """
    r = _conclure(monkeypatch, [], provider=ProviderOneShot([_CLAIM, _RELEASE]))
    assert r["valeurs_cliente_detruites"] is None, "aucune fiche, aucune mesure"
    assert r["reference_comparaison"] is None, \
        "une référence sans mesure fait passer un non-mesuré pour un contrôle"


def test_le_releve_dit_combien_de_fois_le_modele_a_TENTE(monkeypatch):
    """⚠️ Tous les autres postes parlent du DISPOSITIF — ce que la garde a vu au
    dernier tour, réparé, ou ce que l'agent a corrigé. Aucun ne disait combien
    de fois le modèle avait tenté d'altérer une valeur de la cliente.

    Le 01/09, comparer deux modèles n'a donné qu'un encadrement de 5 à 14 fois,
    faute de ce compteur : le poste « vu » est réassigné à chaque tour, celui
    des corrections cumule.

    Sans fiche, aucune comparaison : `None`, jamais une liste vide."""
    r = _conclure(monkeypatch, [], provider=ProviderOneShot([_CLAIM, _RELEASE]))
    assert "valeurs_cliente_tentees" in r, "le relevé doit porter les tentatives"
    assert r["valeurs_cliente_tentees"] is None, \
        "aucune comparaison possible n'est pas zéro tentative"
