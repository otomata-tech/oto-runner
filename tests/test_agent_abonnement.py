"""La voie `claude-subscription` : le flux du CLI lu, le forfait rapporté, le relais borné."""
import pytest

from oto_runner import agent_abonnement as A
from oto_runner import conclusion, worker
from oto_runner.relais_mcp import Relais

INIT = {"type": "system", "subtype": "init", "apiKeySource": "none", "model": "claude-sonnet-5"}
FORFAIT = {"type": "rate_limit_event", "rate_limit_info": {
    "status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.13, "resetsAt": 1790216400},
                                            "seven_day": {"utilization": 0.31, "resetsAt": 1790776800}}}}
APPEL = {"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "t1", "name": "mcp__oto__data_rows", "input": {}}]}}
RETOUR = {"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "t1", "is_error": False}]}}
FIN = {"type": "result", "subtype": "success", "is_error": False, "result": "fait",
       "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100}}


def test_un_run_conclu_rend_reponse_pas_usage_et_forfait():
    res = A.lire_flux([INIT, FORFAIT, APPEL, RETOUR, FIN])
    assert (res.reply, res.stopped, res.model) == ("fait", "end_turn", "claude-sonnet-5")
    assert [(s.tool, s.ok) for s in res.steps] == [("data_rows", True)]
    assert res.abonnement == {"etat": "allowed",
                              "fenetres": FORFAIT["rate_limit_info"]["unifiedWindows"]}
    declare = conclusion.resultat_declare(res, "sub:sonnet")
    assert declare["abonnement"]["etat"] == "allowed"
    assert declare["usage_tokens"] == 15 and declare["tool_counts"] == {"data_rows": 1}


def test_un_run_paye_par_une_cle_est_refuse():
    with pytest.raises(RuntimeError, match="abonnement"):
        A.lire_flux([dict(INIT, apiKeySource="ANTHROPIC_API_KEY"), FIN])


def test_un_bac_deconnecte_conclut_en_echec_nomme_et_le_dit_au_backend():
    res = A.lire_flux([{"type": "ferme_resume", "connecte": False, "erreur": "Not logged in"}])
    assert res.abonnement == {"deconnecte": True}
    assert conclusion.echec_nomme(res) == "fin_anormale (bac_deconnecte)"


def test_une_erreur_du_cli_est_une_fin_anormale():
    res = A.lire_flux([INIT, dict(FIN, is_error=True, result="API Error: 529")])
    assert conclusion.echec_nomme(res) == "fin_anormale (success)"


def test_un_flux_sans_resultat_echoue_franchement():
    with pytest.raises(RuntimeError, match="aucun résultat"):
        A.lire_flux([INIT, FORFAIT])


def test_le_bail_se_prolonge_pendant_le_run_au_plus_une_fois_par_minute():
    temps = iter([0, 10, 70, 80, 200, 210])   # départ, puis un instant par événement
    appels = []
    A.lire_flux([INIT, FORFAIT, APPEL, RETOUR, FIN], prolonger=lambda: appels.append(1),
                horloge=lambda: next(temps))
    assert len(appels) == 2


def test_le_modele_du_catalogue_perd_son_prefixe_et_une_autre_famille_est_refusee():
    assert A._modele_du_cli("sub:opus") == "opus"
    with pytest.raises(ValueError):
        A._modele_du_cli("claude-sonnet-5")


def test_seule_la_voie_abonnement_recoit_le_contexte_du_bac():
    class Autre:
        pass
    assert worker._contexte_du_bac({"id": 1}, Autre, object(), object()) == {}
    ctx = worker._contexte_du_bac({"id": 1, "sandbox_id": "u42"}, A, "mcp", object())
    assert ctx["bac"] == "u42" and ctx["mcp"] == "mcp" and callable(ctx["prolonger"])


class SessionFactice:
    def __init__(self):
        self.appels = []

    def schemas(self, noms):
        return [{"name": n, "description": "d", "input_schema": {"type": "object"}}
                for n in sorted(noms)]

    def call(self, nom, args):
        self.appels.append((nom, args))
        return "ok", False


def test_le_relais_ne_sert_que_l_allowlist():
    session = SessionFactice()
    relais = Relais(session, frozenset({"data_rows"}))
    liste = relais.repondre({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert [t["name"] for t in liste["result"]["tools"]] == ["data_rows"]
    refus = relais.repondre({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": "data_write", "arguments": {}}})
    assert refus["result"]["isError"] and session.appels == []
    ok = relais.repondre({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "data_rows", "arguments": {"q": 1}}})
    assert ok["result"] == {"content": [{"type": "text", "text": "ok"}], "isError": False}
    assert session.appels == [("data_rows", {"q": 1})]


def test_le_relais_repond_a_l_initialize_et_ignore_les_notifications():
    relais = Relais(SessionFactice(), frozenset())
    init = relais.repondre({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert relais.repondre({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
