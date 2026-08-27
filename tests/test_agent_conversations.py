"""Le chemin one-shot Conversations — les outils tournent chez Mistral.

Ce que le banc fige : le PARSE des outputs (le bilan de surveillance en dérive —
usage, pas, tool_counts), les rejeux transitoires explicites (le SDK ne rejoue
pas, et on ne l'utilise pas : son timeout est troué — basesdk.py:227, 20 h de
gel sur la boucle locale), la conformité `store=False`, la RÉSOLUTION de
l'alias de modèle en version concrète, et le chemin worker complet (fil à deux
tours, bilan déclaré). L'essai réel des 20 fiches valide
contre le service ce que le banc ne peut pas savoir.
"""
from __future__ import annotations

import json

import pytest

import oto_runner.agent_conversations as C
from oto_runner import worker as W
from tests.test_worker_reprise import FauxBackend, _job


_REPONSE = {
    "outputs": [
        {"type": "tool.execution", "name": "data_claim_next"},
        {"type": "tool.execution", "name": "serper_search"},
        {"type": "tool.execution", "name": "serper_search"},
        {"type": "tool.execution", "name": "data_write"},
        {"type": "tool.execution", "name": "data_release"},
        {"type": "message.output", "content": [
            {"type": "text", "text": "Fiche enrichie, "},
            {"type": "text", "text": "ligne libérée."}]},
    ],
    "usage": {"prompt_tokens": 21000, "completion_tokens": 900, "total_tokens": 21900},
}


class _R:
    def __init__(self, status=200, corps=None, texte=""):
        self.status_code = status
        self._corps = corps
        self.text = texte

    def json(self):
        return self._corps


# Le catalogue tel que le fournisseur le rend RÉELLEMENT (relevé 27/08 sur
# /v1/models) : SYMÉTRIQUE — la version cite l'alias ET l'alias cite la
# version. C'est ce qui a fait échouer la première règle en production.
_MODELS = {"data": [
    {"id": "mistral-large-2512", "aliases": ["mistral-large-latest"]},
    {"id": "mistral-large-latest", "aliases": ["mistral-large-2512"]},
    {"id": "mistral-small-2506", "aliases": ["mistral-small-latest"]},
    {"id": "mistral-small-latest", "aliases": ["mistral-small-2506"]},
]}


class _FauxRequests:
    """Le `requests` du module, simulé. La résolution de version est le SEUL
    appel qui y passe — les conversations vont par `post_with_deadline`."""

    def __init__(self, reponse):
        self.reponse = reponse
        self.appels = []

    def get(self, url, **kw):
        self.appels.append((url, kw))
        if isinstance(self.reponse, Exception):
            raise self.reponse
        return self.reponse


@pytest.fixture(autouse=True)
def _catalogue(monkeypatch):
    """AUCUN test ne sort : `run_once` résout l'alias au début de chaque job.
    Le cache est un état de MODULE — il se vide entre deux tests, sinon le
    premier décide pour les suivants."""
    C._resolutions.clear()
    monkeypatch.setattr(C, "requests", _FauxRequests(_R(corps=_MODELS)))
    yield
    C._resolutions.clear()


def _env(monkeypatch):
    monkeypatch.setenv("OTO_RUNNER_OPENAI_API_KEY", "k")
    monkeypatch.setenv("OTO_RUNNER_CONNECTOR_ID", "conn-1")
    monkeypatch.delenv("OTO_RUNNER_MODEL", raising=False)   # DEFAULT_MODEL, l'alias


def test_le_bilan_de_surveillance_derive_des_outputs(monkeypatch):
    """La boucle tourne chez Mistral, mais le JOB garde son bilan : usage,
    pas, tool_counts — la page /automations reste vraie."""
    _env(monkeypatch)
    vu = {}

    def post(url, **kw):
        vu.update(url=url, corps=kw["json"])
        return _R(corps=_REPONSE)

    monkeypatch.setattr(C, "post_with_deadline", post)
    res = C.run_once(instructions="la procédure", inputs="vas-y",
                     tools=("data_claim_next", "serper_search"))
    assert res.reply == "Fiche enrichie, ligne libérée."
    assert res.usage == {"input_tokens": 21000, "output_tokens": 900}
    assert [s.tool for s in res.steps] == [
        "data_claim_next", "serper_search", "serper_search",
        "data_write", "data_release"]
    assert vu["url"].endswith("/v1/conversations")
    assert vu["corps"]["model"] == C.DEFAULT_MODEL, "l'ALIAS est ce qu'on appelle"
    assert res.model == "mistral-large-2512", "la VERSION est ce qu'on enregistre"


def test_store_false_est_toujours_envoye(monkeypatch):
    """`store=False` est une contrainte de CONFORMITÉ du contrat client —
    rien ne reste chez Mistral. Pas un défaut implicite : ENVOYÉ, toujours."""
    _env(monkeypatch)
    vu = {}
    monkeypatch.setattr(C, "post_with_deadline",
                        lambda url, **kw: vu.update(kw["json"]) or _R(corps=_REPONSE))
    C.run_once(instructions="p", inputs="i", tools=())
    assert vu["store"] is False
    assert vu["tools"][0]["connector_id"] == "conn-1"


def test_un_transitoire_est_rejoue_puis_passe(monkeypatch):
    """Le SDK ne rejoue pas ; nous si — explicitement, sur les seuls
    transitoires HTTP, jamais sur une DeadlineExceeded (re-payer un run
    entier n'est pas une politique de rejeu)."""
    _env(monkeypatch)
    monkeypatch.setattr(C.time, "sleep", lambda s: None)
    essais = {"n": 0}

    def post(url, **kw):
        essais["n"] += 1
        return _R(status=502, texte="bad gateway") if essais["n"] < 3 \
            else _R(corps=_REPONSE)

    monkeypatch.setattr(C, "post_with_deadline", post)
    res = C.run_once(instructions="p", inputs="i", tools=())
    assert essais["n"] == 3 and res.reply


def test_un_400_echoue_net_sans_rejeu(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(C, "post_with_deadline",
                        lambda url, **kw: _R(status=400, texte="invalid connector"))
    with pytest.raises(RuntimeError, match="conversations → 400"):
        C.run_once(instructions="p", inputs="i", tools=())


def test_sans_connector_id_erreur_franche(monkeypatch):
    monkeypatch.setenv("OTO_RUNNER_OPENAI_API_KEY", "k")
    monkeypatch.delenv("OTO_RUNNER_CONNECTOR_ID", raising=False)
    with pytest.raises(C.LlmUnavailable, match="OTO_RUNNER_CONNECTOR_ID"):
        C.run_once(instructions="p", inputs="i", tools=())


def test_le_worker_one_shot_appose_ordre_et_synthese(monkeypatch):
    """Le chemin worker complet : fil à DEUX tours (l'ordre, la synthèse avec le
    relevé des exécutions), bilan déclaré (tool_counts au grain job) — la
    surveillance des vacances ne perd pas son poste."""
    _env(monkeypatch)

    class FauxConv:
        ONE_SHOT = True

        @staticmethod
        def run_once(*, instructions, inputs, tools):
            from oto_runner.agent_runtime import AgentResult, AgentStep
            assert "la procédure" in instructions
            return AgentResult(reply="fait", stopped="end_turn",
                               model="mistral-large-2512",
                               usage={"input_tokens": 20000, "output_tokens": 500},
                               steps=[AgentStep(tool="data_write", ok=True,
                                                duration_ms=0)])

    class McpSansBoucle:
        def __init__(self, **kw):
            self.run_id = None

        def outil(self, name, args=None):
            if name == "oto_procedure":
                return {"body_md": "la procédure"}
            if name == "run_start":
                return {"run_id": "r-C"}
            return {}

    monkeypatch.setattr(W, "McpSession", McpSansBoucle)
    b = FauxBackend()
    W._traiter(b, _job("start"), provider=FauxConv)
    roles = [a[1] for a in b.appels if a[0] == "append"]
    assert roles == ["user", "assistant"], "le fil garde l'ordre et la synthèse"
    result = next(a for a in b.appels if a[0] == "complete_result")[1]
    assert result["tool_counts"] == {"data_write": 1}
    assert result["usage_tokens"] == 20500
    assert result["model"] == "mistral-large-2512", \
        "le job porte la version, pas l'alias : une bascule se date"


def test_une_base_openai_avec_v1_ne_double_pas_le_chemin(monkeypatch):
    """L'env de la box porte la base openai-compat AVEC /v1 : la réutiliser
    donnait /v1/v1/conversations → 404 « no Route matched » (vécu, job 274)."""
    _env(monkeypatch)
    monkeypatch.setenv("OTO_RUNNER_OPENAI_BASE", "https://api.mistral.ai/v1")
    vu = {}
    monkeypatch.setattr(C, "post_with_deadline",
                        lambda url, **kw: vu.update(url=url) or _R(corps=_REPONSE))
    C.run_once(instructions="p", inputs="i", tools=())
    assert vu["url"] == "https://api.mistral.ai/v1/conversations"


def test_les_noms_prefixes_par_le_connecteur_sont_normalises(monkeypatch):
    """Le connecteur Mistral préfixe les outils de son nom (oto-11aout_data_write) :
    sans normalisation, le bilan disait « zéro data_write » sur des fiches
    écrites (vécu). L'allowlist du job normalise — exacte, jamais une devinette."""
    _env(monkeypatch)
    rep = {"outputs": [
        {"type": "tool.execution", "name": "oto-11aout_data_claim_next"},
        {"type": "tool.execution", "name": "oto-11aout_data_write"},
        {"type": "message.output", "content": "ok"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    monkeypatch.setattr(C, "post_with_deadline", lambda url, **kw: _R(corps=rep))
    res = C.run_once(instructions="p", inputs="i",
                     tools=("data_claim_next", "data_write"))
    assert [s.tool for s in res.steps] == ["data_claim_next", "data_write"]


def test_le_worker_one_shot_impose_lidentite_de_run_et_le_nom_du_tableau(monkeypatch):
    """57 % des data_write refusés en campagne (27/08) : en Conversations
    personne ne pose `_run_id`, le titulaire d'une ligne réservée était refusé
    sur sa propre ligne. Le worker impose l'identité dans l'ordre — et le nom
    EXACT du tableau (700+ refus sur des slots/variantes inventés)."""
    _env(monkeypatch)
    vu = {}

    class FauxConv:
        ONE_SHOT = True

        @staticmethod
        def run_once(*, instructions, inputs, tools):
            vu["inputs"] = inputs
            from oto_runner.agent_runtime import AgentResult
            return AgentResult(reply="ok", stopped="end_turn")

    class Mcp:
        def __init__(self, **kw):
            self.run_id = None

        def outil(self, name, args=None):
            return {"body_md": "p"} if name == "oto_procedure" else {"run_id": "r-ID"}

    monkeypatch.setattr(W, "McpSession", Mcp)
    job = _job("start")
    job["payload"]["namespace"] = "edition-vivier"
    W._traiter(FauxBackend(), job, provider=FauxConv)
    assert '_run_id: "r-ID"' in vu["inputs"] and 'worker: "r-ID"' in vu["inputs"]
    assert 'namespace: "edition-vivier"' in vu["inputs"]
    assert vu["inputs"].endswith("Vas-y."), "l'ordre de la flotte suit, intact"


_CALL_BIDON = {
    "type": "function.call", "object": "entry", "id": "e-9",
    "tool_call_id": "call-abc",
    "name": "--- **Bilan** — je poursuis, prochaine étape : vérifier.",
    "arguments": "{}",
}


def _reponse_avec_appel_renvoye():
    return {"outputs": [{"type": "tool.execution", "name": "demo_lookup",
                         "arguments": "{}", "id": "e-8"},
                        dict(_CALL_BIDON)],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}


def _suite(monkeypatch, reponses):
    """Un POST = une réponse de la liste ; les corps envoyés sont conservés."""
    corps = []

    def post(url, **kw):
        corps.append(kw["json"])
        return _R(corps=reponses[len(corps) - 1])

    monkeypatch.setattr(C, "post_with_deadline", post)
    return corps


def test_sans_la_variable_un_appel_renvoye_narrete_quun_seul_post(monkeypatch):
    """La relance est un cran qu'on arme : variable absente ⟹ le comportement
    d'avant à l'octet près — un POST, l'appel rendu compté non exécuté."""
    _env(monkeypatch)
    monkeypatch.delenv("OTO_RUNNER_RELANCES_MAX", raising=False)
    corps = _suite(monkeypatch, [_reponse_avec_appel_renvoye()])
    res = C.run_once(instructions="p", inputs="i", tools=("demo_lookup",))
    assert len(corps) == 1, "aucune relance"
    assert corps[0]["inputs"] == "i"
    assert [(s.tool, s.ok) for s in res.steps][-1][1] is False
    assert res.steps[-1].error == "function.call non exécuté"


def test_une_relance_rejoue_le_fil_avec_un_function_result(monkeypatch):
    """Le fil rejoué porte l'ordre initial, les outputs REÇUS tels quels, puis
    la réponse à l'appel rendu — c'est ce qui remet l'agent au travail au lieu
    de perdre la ligne pour le prix d'un run entier."""
    _env(monkeypatch)
    monkeypatch.setenv("OTO_RUNNER_RELANCES_MAX", "2")
    fin = {"outputs": [{"type": "tool.execution", "name": "demo_write",
                        "arguments": "{}"},
                       {"type": "message.output", "content": "fiche écrite"}],
           "usage": {"prompt_tokens": 500, "completion_tokens": 50}}
    corps = _suite(monkeypatch, [_reponse_avec_appel_renvoye(), fin])
    res = C.run_once(instructions="la procédure", inputs="vas-y",
                     tools=("demo_lookup", "demo_write"))

    assert len(corps) == 2, "un second POST, pas un /restart (store=False)"
    assert corps[1]["store"] is False
    assert corps[1]["instructions"] == corps[0]["instructions"]
    assert corps[1]["tools"] == corps[0]["tools"]

    entrees = corps[1]["inputs"]
    assert entrees[0] == {"object": "entry", "type": "message.input",
                          "role": "user", "content": "vas-y"}
    assert entrees[1]["type"] == "tool.execution"
    assert entrees[2] == _CALL_BIDON, "les outputs repartent VERBATIM"
    assert entrees[-1] == {"object": "entry", "type": "function.result",
                           "tool_call_id": "call-abc",
                           "result": C._CONSIGNE_APPEL_RENVOYE}

    assert res.usage == {"input_tokens": 1500, "output_tokens": 150}, \
        "le prix payé est la somme des passes"
    assert [(s.tool, s.ok) for s in res.steps] == [
        ("demo_lookup", True), (_CALL_BIDON["name"], False),
        ("demo_write", True)], "l'appel rendu reste visible au bilan"
    assert res.reply == "fiche écrite"
    assert res.raw_outputs == fin["outputs"], "les entrées brutes de la DERNIÈRE passe"


def test_le_maximum_borne_les_relances(monkeypatch):
    """Un fil qui rend un appel à chaque passe s'arrête au plafond : la relance
    est bornée, jamais une boucle qui re-paie le run indéfiniment."""
    _env(monkeypatch)
    monkeypatch.setenv("OTO_RUNNER_RELANCES_MAX", "2")
    corps = _suite(monkeypatch, [_reponse_avec_appel_renvoye() for _ in range(3)])
    res = C.run_once(instructions="p", inputs="i", tools=("demo_lookup",))
    assert len(corps) == 3, "1 passe + 2 relances, puis stop"
    assert res.usage == {"input_tokens": 3000, "output_tokens": 300}
    assert len(res.steps) == 6
    assert corps[2]["inputs"][-1]["type"] == "function.result"


def test_une_relance_repond_a_chaque_appel_de_la_traine(monkeypatch):
    """Le fournisseur peut en rendre plusieurs d'affilée : chacun reçoit sa
    réponse, sur SON tool_call_id — un appel sans réponse bloque le fil."""
    _env(monkeypatch)
    monkeypatch.setenv("OTO_RUNNER_RELANCES_MAX", "1")
    second = dict(_CALL_BIDON, tool_call_id="call-def", id="e-10")
    debut = {"outputs": [dict(_CALL_BIDON), second],
             "usage": {"prompt_tokens": 10, "completion_tokens": 1}}
    fin = {"outputs": [{"type": "message.output", "content": "ok"}],
           "usage": {"prompt_tokens": 10, "completion_tokens": 1}}
    corps = _suite(monkeypatch, [debut, fin])
    C.run_once(instructions="p", inputs="i", tools=())
    resultats = [e for e in corps[1]["inputs"] if e["type"] == "function.result"]
    assert [e["tool_call_id"] for e in resultats] == ["call-abc", "call-def"]


def test_un_appel_sans_tool_call_id_leve(monkeypatch):
    """Répondre à un appel exige son identifiant : une réponse qui n'en porte
    pas est mal formée — elle lève, elle ne se devine pas."""
    _env(monkeypatch)
    monkeypatch.setenv("OTO_RUNNER_RELANCES_MAX", "1")
    muet = {"outputs": [{"type": "function.call", "name": "x", "arguments": "{}"}],
            "usage": {}}
    _suite(monkeypatch, [muet])
    with pytest.raises(RuntimeError, match="sans tool_call_id"):
        C.run_once(instructions="p", inputs="i", tools=())


def test_une_valeur_de_relance_non_entiere_leve(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("OTO_RUNNER_RELANCES_MAX", "beaucoup")
    with pytest.raises(C.LlmUnavailable, match="OTO_RUNNER_RELANCES_MAX"):
        C.run_once(instructions="p", inputs="i", tools=())


def test_un_transitoire_est_rejoue_a_chaque_passe(monkeypatch):
    """Les rejeux HTTP valent pour la relance comme pour la première passe."""
    _env(monkeypatch)
    monkeypatch.setenv("OTO_RUNNER_RELANCES_MAX", "1")
    monkeypatch.setattr(C.time, "sleep", lambda s: None)
    fin = {"outputs": [{"type": "message.output", "content": "ok"}],
           "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    suite = [_R(corps=_reponse_avec_appel_renvoye()),
             _R(status=503, texte="unavailable"),
             _R(corps=fin)]
    monkeypatch.setattr(C, "post_with_deadline",
                        lambda url, **kw: suite.pop(0))
    res = C.run_once(instructions="p", inputs="i", tools=())
    assert suite == [] and res.reply == "ok"


def test_le_faux_depart_conserve_les_entrees_brutes(monkeypatch, tmp_path):
    """Ce que l'agent a RÉELLEMENT fait avant de s'arrêter ne survit qu'ici :
    le fil ne garde qu'une synthèse, et rien n'est stocké chez le fournisseur."""
    from oto_runner.agent_runtime import AgentResult, AgentStep
    depot = tmp_path / "faux-departs"
    monkeypatch.setenv("OTO_RUNNER_FAUX_DEPARTS_DIR", str(depot))
    res = AgentResult(reply="prose", stopped="end_turn",
                      steps=[AgentStep(tool="data_claim_next", ok=True,
                                       duration_ms=0)],
                      raw_outputs=[dict(_CALL_BIDON)])
    W._conserver_faux_depart({"id": 7}, {"procedure": "demo"}, res)
    trace = json.loads((depot / "7.json").read_text(encoding="utf-8"))
    assert trace["raw_outputs"] == [_CALL_BIDON]
