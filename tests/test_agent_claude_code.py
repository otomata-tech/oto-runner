"""Le moteur Claude Code — ce que le banc fige sans appeler le service.

Le SDK est remplacé par une doublure : ce qui est vérifié est ce que le WORKER
décide (outils exposés, permissions, environnement, plafonds), ce qu'il fait des
appels d'outils (tout passe par la session MCP du travail), et comment il lit le
résultat. L'essai réel valide contre Claude Code ce que le banc ne peut pas savoir.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from oto_runner import agent_claude_code as AC
from oto_runner import conclusion, llm_select
from oto_runner import worker as W
from oto_runner.agent_runtime import AgentSpec
from tests.test_worker_reprise import FauxBackend, FauxMcp, _job


# ── La doublure du SDK ───────────────────────────────────────────────────────

class ToolUseBlock(SimpleNamespace):
    pass


class TextBlock(SimpleNamespace):
    pass


class AssistantMessage(SimpleNamespace):
    pass


class ResultMessage(SimpleNamespace):
    pass


def _resultat(**kw):
    base = dict(subtype="success", is_error=False, num_turns=3, total_cost_usd=0.42,
                duration_ms=1000, permission_denials=[], stop_reason="end_turn",
                result="Fini : 2 lignes écrites.",
                usage={"input_tokens": 10, "output_tokens": 5,
                       "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20},
                model_usage=None)
    base.update(kw)
    return ResultMessage(**base)


class FauxSdk:
    """`script(options, outils)` est une coroutine-générateur : elle rend les messages
    et peut appeler les outils exposés, comme le ferait Claude Code."""

    def __init__(self, script):
        self.script = script
        self.options = None
        self.outils = {}

    def tool(self, nom, description, schema):
        def deco(fn):
            outil = SimpleNamespace(name=nom, description=description, schema=schema,
                                    handler=fn)
            self.outils[nom] = outil
            return outil
        return deco

    def create_sdk_mcp_server(self, nom, tools=None):
        return {"nom": nom, "outils": list(tools or ())}

    def ClaudeAgentOptions(self, **kw):  # noqa: N802 — le nom du SDK
        self.options = kw
        return SimpleNamespace(**kw)

    def query(self, prompt, options):
        return self.script(self, prompt, options)


class Mcp:
    def __init__(self, catalogue=("data_rows", "data_write", "slack_post_message")):
        self.catalogue_ = catalogue
        self.appels = []
        self.panne = None

    def schemas(self, noms):
        return [{"name": n, "description": f"desc {n}",
                 "input_schema": {"type": "object", "properties": {}}}
                for n in self.catalogue_ if n in noms]

    def call(self, nom, args):
        if self.panne:
            raise self.panne
        self.appels.append((nom, args))
        return f"sortie de {nom}", False


def _lancer(monkeypatch, script, *, tools=("data_rows", "data_write"), spec=None, mcp=None,
            workspace=None, heartbeat=None, cle="sk-org"):
    sdk = FauxSdk(script)
    monkeypatch.setattr(AC, "_sdk", lambda: sdk)
    evenements = []
    res = AC.run_once(instructions="cadre", inputs="Fais le travail.", tools=tools,
                      api_key=cle, modele="claude-sonnet-5",
                      on_event=lambda ev, c: evenements.append((ev, c)),
                      mcp=mcp or Mcp(), spec=spec or AgentSpec(system="s", tools=frozenset(tools)),
                      workspace=workspace, heartbeat=heartbeat)
    return res, sdk, evenements


async def _simple(sdk, prompt, options):
    yield AssistantMessage(content=[TextBlock(text="je commence")], model="claude-sonnet-5",
                           parent_tool_use_id=None, message_id="m1", usage={})
    yield _resultat()


# ── Ce que Claude Code reçoit ────────────────────────────────────────────────

def test_seule_l_allowlist_est_exposee_et_autorisee(monkeypatch):
    res, sdk, _ = _lancer(monkeypatch, _simple, mcp=Mcp())
    assert set(sdk.outils) == {"data_rows", "data_write"}, "slack_post_message hors allowlist"
    o = sdk.options
    assert o["mcp_servers"] == {"oto": {"nom": "oto", "outils": list(sdk.outils.values())}}
    assert o["strict_mcp_config"] is True
    assert o["tools"] == ["Agent"], "ni shell, ni fichiers, ni web"
    assert "mcp__oto__data_rows" in o["allowed_tools"] and "Agent" in o["allowed_tools"]
    assert not any("slack" in t for t in o["allowed_tools"])
    assert o["permission_mode"] == "dontAsk"
    assert o["setting_sources"] == []
    # Le CADRE DU TRAVAIL, seul : pas le preset `claude_code`, que le backend n'a
    # pas envoyé et que le worker n'a pas à composer.
    assert o["system_prompt"] == "cadre"


def test_la_cle_du_travail_part_et_les_secrets_du_worker_non(monkeypatch):
    monkeypatch.setenv("OTO_WORKER_SECRET", "otow_secret")
    _, sdk, _ = _lancer(monkeypatch, _simple, workspace="wrkspc_1")
    env = sdk.options["env"]
    assert env["ANTHROPIC_API_KEY"] == "sk-org"
    assert env["OTO_WORKER_SECRET"] == "" and env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "anthropic-workspace-id: wrkspc_1"
    assert env["CLAUDE_CONFIG_DIR"].startswith(sdk.options["cwd"])


def test_une_variable_que_personne_n_a_prevue_part_vide(monkeypatch):
    """La propriété qu'une liste NOIRE ne pouvait pas donner : le `.env` du worker
    peut gagner une variable demain, le CLI ne la lira pas pour autant."""
    monkeypatch.setenv("UN_SECRET_FUTUR", "à ne pas divulguer")
    monkeypatch.setenv("PATH", "/usr/bin")
    _, sdk, _ = _lancer(monkeypatch, _simple)
    env = sdk.options["env"]
    assert env["UN_SECRET_FUTUR"] == "", "tout ce qui n'est pas nommé part vide"
    assert "PATH" not in env, "la liste blanche laisse passer PATH tel quel"
    assert env["HOME"] == sdk.options["cwd"], "le foyer du CLI est celui du travail"


def test_le_journal_ne_dit_que_les_NOMS_de_l_environnement(monkeypatch):
    _, _, ev = _lancer(monkeypatch, _simple)
    (reglages,) = [c for e, c in ev if e == "claude_code"]
    assert "ANTHROPIC_API_KEY" in reglages["env"]
    assert not any("sk-org" in str(v) for v in reglages["env"])


def test_la_borne_de_depense_de_la_session_est_servie_quand_elle_est_posee(monkeypatch):
    monkeypatch.setenv("OTO_RUNNER_CLAUDE_CODE_MAX_USD", "2.5")
    _, sdk, _ = _lancer(monkeypatch, _simple)
    assert sdk.options["max_budget_usd"] == 2.5


def test_sans_borne_de_depense_rien_n_est_servi(monkeypatch):
    monkeypatch.delenv("OTO_RUNNER_CLAUDE_CODE_MAX_USD", raising=False)
    _, sdk, _ = _lancer(monkeypatch, _simple)
    assert "max_budget_usd" not in sdk.options


def test_une_borne_de_depense_illisible_est_refusee(monkeypatch):
    monkeypatch.setenv("OTO_RUNNER_CLAUDE_CODE_MAX_USD", "beaucoup")
    with pytest.raises(AC.LlmUnavailable, match="montant > 0"):
        _lancer(monkeypatch, _simple)


def test_les_deux_bornes_se_lisent_ensemble(monkeypatch):
    """Un plafond de tours généreux et un mur court se contredisent : le journal les
    porte côte à côte pour qu'on ne lise pas le premier comme une promesse."""
    monkeypatch.setenv("OTO_RUNNER_CLAUDE_CODE_WALL_S", "900")
    spec = AgentSpec(system="s", tools=frozenset(), max_steps=400)
    _, _, ev = _lancer(monkeypatch, _simple, spec=spec)
    (reglages,) = [c for e, c in ev if e == "claude_code"]
    assert reglages["max_turns"] == 400 and reglages["mur_s"] == 900


def test_le_repertoire_du_travail_est_efface(monkeypatch):
    import os
    _, sdk, _ = _lancer(monkeypatch, _simple)
    assert not os.path.exists(sdk.options["cwd"])


def test_le_plafond_de_tours_du_travail_est_servi_au_dela_de_64(monkeypatch):
    spec = AgentSpec(system="s", tools=frozenset({"data_rows"}), max_steps=200)
    _, sdk, _ = _lancer(monkeypatch, _simple, spec=spec)
    assert sdk.options["max_turns"] == 200


def test_un_plafond_au_dela_du_moteur_est_dit(monkeypatch):
    spec = AgentSpec(system="s", tools=frozenset({"data_rows"}), max_steps=1000)
    _, sdk, ev = _lancer(monkeypatch, _simple, spec=spec)
    assert sdk.options["max_turns"] == AC.PLAFOND_TOURS
    (note,) = [c for e, c in ev if e == "plafond_tours"]
    assert note == {"demande": 1000, "servi": AC.PLAFOND_TOURS, "plafond": AC.PLAFOND_TOURS}


def test_l_effort_du_travail_est_servi(monkeypatch):
    spec = AgentSpec(system="s", tools=frozenset(), effort="high")
    _, sdk, _ = _lancer(monkeypatch, _simple, spec=spec)
    assert sdk.options["effort"] == "high"


def test_une_temperature_declaree_est_dite_non_servie(monkeypatch):
    spec = AgentSpec(system="s", tools=frozenset(), temperature=0)
    _, sdk, ev = _lancer(monkeypatch, _simple, spec=spec)
    assert "temperature" not in sdk.options
    assert [c for e, c in ev if e == "temperature_non_servie"] == [{"temperature": 0}]


# ── Les appels d'outils ──────────────────────────────────────────────────────

async def _appelle(sdk, prompt, options):
    sortie = await sdk.outils["data_write"].handler({"id": 3, "row": {"statut": "ok"}})
    yield AssistantMessage(content=[ToolUseBlock(name="Agent", id="t1", input={})],
                           model="claude-sonnet-5", parent_tool_use_id=None,
                           message_id="m1", usage={})
    yield AssistantMessage(content=[TextBlock(text=sortie["content"][0]["text"])],
                           model="claude-sonnet-5", parent_tool_use_id="t1",
                           message_id="m2", usage={})
    yield _resultat()


def test_un_appel_passe_par_la_session_mcp_du_travail(monkeypatch):
    mcp = Mcp()
    res, _, ev = _lancer(monkeypatch, _appelle, mcp=mcp)
    assert mcp.appels == [("data_write", {"id": 3, "row": {"statut": "ok"}})]
    assert [s.tool for s in res.steps] == ["data_write"]
    assert conclusion.resultat_declare(res, "x")["tool_counts"] == {"data_write": 1}
    assert [c["parent_principal"] for e, c in ev if e == "delegation"] == [True]


def test_une_sortie_trop_longue_est_coupee_en_le_disant(monkeypatch):
    monkeypatch.setenv("OTO_RUNNER_MAX_TOOL_OUTPUT", "10")
    lu = {}

    async def script(sdk, prompt, options):
        lu["sortie"] = await sdk.outils["data_rows"].handler({})
        yield _resultat()

    _lancer(monkeypatch, script)
    assert "SORTIE TRONQUÉE" in lu["sortie"]["content"][0]["text"]


def test_un_transport_mort_fait_echouer_le_travail(monkeypatch):
    mcp = Mcp()
    mcp.panne = RuntimeError("session MCP rouverte mais refusée")
    suite = []

    async def script(sdk, prompt, options):
        await sdk.outils["data_rows"].handler({})
        yield AssistantMessage(content=[], model=None, parent_tool_use_id=None,
                               message_id="m1", usage={})
        suite.append("continué")
        yield _resultat()

    with pytest.raises(RuntimeError, match="transport MCP mort"):
        _lancer(monkeypatch, script, mcp=mcp)
    assert suite == [], "le déroulé s'arrête au premier message après la panne"


# ── La lecture du résultat ───────────────────────────────────────────────────

def test_un_succes_conclut_end_turn_avec_l_usage_de_la_session(monkeypatch):
    res, _, ev = _lancer(monkeypatch, _simple)
    assert res.stopped == "end_turn" and res.reply == "Fini : 2 lignes écrites."
    assert res.usage["input_tokens"] == 10 and res.usage["cache_read_input_tokens"] == 100
    assert res.usage["input_total_tokens"] == 130
    assert res.model == "claude-sonnet-5"
    (fin,) = [c for e, c in ev if e == "resultat_claude_code"]
    assert fin["cout_usd"] == 0.42


def test_l_usage_par_modele_couvre_les_sous_agents(monkeypatch):
    async def script(sdk, prompt, options):
        yield _resultat(model_usage={
            "claude-sonnet-5": {"inputTokens": 10, "outputTokens": 5,
                                "cacheReadInputTokens": 100, "cacheCreationInputTokens": 0},
            "claude-haiku-4-5": {"inputTokens": 7, "outputTokens": 3,
                                 "cacheReadInputTokens": 0, "cacheCreationInputTokens": 1}})

    res, _, ev = _lancer(monkeypatch, script)
    assert res.usage["input_tokens"] == 17 and res.usage["output_tokens"] == 8
    assert res.usage["input_total_tokens"] == 118
    (fin,) = [c for e, c in ev if e == "resultat_claude_code"]
    assert fin["modeles"] == ["claude-haiku-4-5", "claude-sonnet-5"]


def test_le_plafond_de_tours_atteint_conclut_max_steps(monkeypatch):
    async def script(sdk, prompt, options):
        yield _resultat(subtype="error_max_turns", is_error=True, result=None)

    res, _, _ = _lancer(monkeypatch, script)
    assert res.stopped == "max_steps" and conclusion.echec_nomme(res) is None


def test_une_erreur_d_execution_est_un_echec_nomme(monkeypatch):
    async def script(sdk, prompt, options):
        yield _resultat(subtype="error_during_execution", is_error=True, result=None)

    res, _, _ = _lancer(monkeypatch, script)
    assert res.stopped == "fin_anormale"
    assert conclusion.echec_nomme(res) == "fin_anormale (error_during_execution)"


def test_sans_message_de_resultat_le_travail_echoue(monkeypatch):
    async def script(sdk, prompt, options):
        yield AssistantMessage(content=[], model=None, parent_tool_use_id=None,
                               message_id="m1", usage={})

    with pytest.raises(RuntimeError, match="sans message de résultat"):
        _lancer(monkeypatch, script)


def test_le_budget_de_jetons_arrete_le_deroule_et_compte_un_message_une_fois(monkeypatch):
    async def script(sdk, prompt, options):
        for _ in range(3):   # le même message d'API, découpé en trois blocs
            yield AssistantMessage(content=[], model="claude-sonnet-5",
                                   parent_tool_use_id=None, message_id="m1",
                                   usage={"input_tokens": 400, "output_tokens": 100})
        yield AssistantMessage(content=[], model="claude-sonnet-5",
                               parent_tool_use_id=None, message_id="m2",
                               usage={"input_tokens": 400, "output_tokens": 200})
        yield _resultat()

    spec = AgentSpec(system="s", tools=frozenset(), max_tokens=1000)
    res, _, ev = _lancer(monkeypatch, script, spec=spec)
    assert res.stopped == "max_tokens"
    assert [c["jetons"] for e, c in ev if e == "budget_depasse"] == [1100]


def test_la_deadline_murale_coupe(monkeypatch):
    monkeypatch.setenv("OTO_RUNNER_CLAUDE_CODE_WALL_S", "1")

    async def script(sdk, prompt, options):
        await asyncio.sleep(5)
        yield _resultat()

    with pytest.raises(AC.DeadlineExceeded):
        _lancer(monkeypatch, script)


def test_le_bail_est_prolonge_pendant_le_deroule(monkeypatch):
    battements = []
    _lancer(monkeypatch, _appelle, heartbeat=lambda: battements.append(1))
    assert battements == [1], "un battement, puis au plus un par minute"


# ── Le branchement au worker ─────────────────────────────────────────────────

def test_le_moteur_se_choisit_par_l_environnement(monkeypatch):
    monkeypatch.setenv("OTO_RUNNER_PROVIDER", "claude-code")
    assert llm_select.get_provider() is AC
    assert AC.depot() == "anthropic"


def test_l_effort_du_travail_n_est_pas_refuse_sur_ce_moteur():
    W._exiger_effort_servi({"effort": "high"}, AC)


def test_le_worker_remet_la_session_le_cadre_le_workspace_et_le_bail(monkeypatch):
    class McpDuTravail(FauxMcp):
        def schemas(self, noms):
            return Mcp().schemas(noms)

    monkeypatch.setattr(W, "McpSession", McpDuTravail)
    vu = {}

    def faux_run_once(**kw):
        vu.update(kw)
        from oto_runner.agent_runtime import AgentResult
        return AgentResult(reply="fini", stopped="end_turn", model="claude-sonnet-5")

    monkeypatch.setattr(AC, "run_once", faux_run_once)
    job = _job("start")
    job["model_workspace"] = "wrkspc_9"
    job["payload"]["max_steps"] = 200
    backend = FauxBackend()
    W._traiter(backend, job, AC)
    assert isinstance(vu["mcp"], McpDuTravail) and vu["mcp"].run_id == "r-NEUF"
    assert vu["spec"].max_steps == 200 and vu["workspace"] == "wrkspc_9"
    assert callable(vu["heartbeat"])
    assert ("complete", True, "r-NEUF") in backend.appels


class SystemMessage(SimpleNamespace):
    pass


class ResultError(Exception):
    pass


def test_l_exception_du_sdk_apres_un_resultat_en_erreur_ne_masque_pas_le_resultat(monkeypatch):
    """Relevé sur le SDK 0.2.153 : un résultat `is_error` fait sortir le CLI en code 1, et
    le SDK lève `ResultError` après l'avoir rendu. Le résultat conclut."""
    async def script(sdk, prompt, options):
        yield _resultat(subtype="error_max_turns", is_error=True, result=None)
        raise ResultError("Claude Code returned an error result")

    res, _, _ = _lancer(monkeypatch, script)
    assert res.stopped == "max_steps"


def test_une_exception_du_sdk_sans_resultat_remonte(monkeypatch):
    async def script(sdk, prompt, options):
        raise ResultError("démarrage refusé")
        yield  # noqa — générateur

    with pytest.raises(ResultError):
        _lancer(monkeypatch, script)


def test_une_cle_refusee_arrete_le_travail_sans_attendre_les_rejeux(monkeypatch):
    rejeux = []

    async def script(sdk, prompt, options):
        for tentative in range(10):
            rejeux.append(tentative)
            yield SystemMessage(subtype="api_retry", data={"error_status": 401})
        yield _resultat()

    with pytest.raises(RuntimeError, match="clé de modèle refusée par Anthropic \\(401\\)"):
        _lancer(monkeypatch, script)
    assert rejeux == [0]


def test_un_rejeu_transitoire_ne_coupe_rien(monkeypatch):
    async def script(sdk, prompt, options):
        yield SystemMessage(subtype="api_retry", data={"error_status": 529})
        yield _resultat()

    res, _, _ = _lancer(monkeypatch, script)
    assert res.stopped == "end_turn"


def test_une_sortie_anticipee_ferme_le_flux(monkeypatch):
    ferme = []

    async def script(sdk, prompt, options):
        try:
            yield AssistantMessage(content=[], model="claude-sonnet-5", parent_tool_use_id=None,
                                   message_id="m1",
                                   usage={"input_tokens": 5000, "output_tokens": 1})
            yield _resultat()
        finally:
            ferme.append(True)

    spec = AgentSpec(system="s", tools=frozenset(), max_tokens=10)
    res, _, _ = _lancer(monkeypatch, script, spec=spec)
    assert res.stopped == "max_tokens" and ferme == [True]


def test_la_boucle_classique_dit_le_plafond_qu_elle_impose():
    from oto_runner import agent_runtime
    from oto_runner.llm_types import Turn
    from tests.test_effort_du_travail import FauxProvider, FauxTransport
    ev = []
    agent_runtime.run(AgentSpec(system="s", tools=frozenset(), max_steps=200), FauxTransport(),
                      FauxProvider([Turn(text="fini", raw_content=[])]),
                      prompt="go", on_event=lambda e, c: ev.append((e, c)))
    assert [c for e, c in ev if e == "plafond_tours"] == [
        {"demande": 200, "servi": 64, "plafond": 64}]


def test_le_premier_battement_part_meme_sur_une_machine_juste_demarree(monkeypatch):
    monkeypatch.setattr(AC.time, "monotonic", lambda: 5.0)
    battements = []
    _lancer(monkeypatch, _appelle, heartbeat=lambda: battements.append(1))
    assert battements == [1]


# ── Ce qu'un déroulé MORT a coûté ────────────────────────────────────────────
# Alexis, revue du 18/09 : « Budget dépassé : `resultat` vaut `None`, donc l'usage
# déclaré est vide et le travail conclut `max_tokens` sans aucun jeton déclaré. Les
# chemins deadline, clé refusée et transport mort ne posent pas `usage_partiel` non
# plus, contrairement à `agent_runtime`. Leur coût est perdu. »

def _messages(n, **usage):
    async def script(sdk, prompt, options):
        for i in range(n):
            yield AssistantMessage(content=[], model="claude-sonnet-5",
                                   parent_tool_use_id=None, message_id=f"m{i}",
                                   usage=dict(usage))
        await asyncio.sleep(5)     # ne conclut jamais de lui-même
        yield _resultat()
    return script


def test_le_budget_depasse_declare_CE_QU_IL_A_DEPENSE(monkeypatch):
    """Le cas nommé par la revue : sans bilan de session, l'usage venait vide."""
    async def script(sdk, prompt, options):
        for i in range(3):
            yield AssistantMessage(content=[], model="claude-sonnet-5",
                                   parent_tool_use_id=None, message_id=f"m{i}",
                                   usage={"input_tokens": 400, "output_tokens": 100,
                                          "cache_read_input_tokens": 7,
                                          "cache_creation_input_tokens": 3})
        yield _resultat()

    spec = AgentSpec(system="s", tools=frozenset(), max_tokens=1000)
    res, _, _ = _lancer(monkeypatch, script, spec=spec)
    assert res.stopped == "max_tokens"
    assert res.usage["input_tokens"] == 800 and res.usage["output_tokens"] == 200
    assert res.usage["cache_creation_input_tokens"] == 6
    assert res.couverture["tours"] == 2, "deux messages comptés, pas le troisième"
    assert conclusion.resultat_declare(res, "x")["usage_tokens"] == 1000


def test_la_deadline_emporte_ce_qui_a_ete_depense(monkeypatch):
    monkeypatch.setenv("OTO_RUNNER_CLAUDE_CODE_WALL_S", "1")
    with pytest.raises(AC.DeadlineExceeded) as pris:
        _lancer(monkeypatch, _messages(2, input_tokens=300, output_tokens=50))
    assert pris.value.usage_partiel["input_tokens"] == 600
    assert pris.value.couverture_partielle["tours"] == 2
    assert pris.value.modele_partiel == "claude-sonnet-5"
    assert conclusion.resultat_partiel(pris.value, "claude-sonnet-5")["usage_tokens"] == 700


def test_une_cle_refusee_emporte_ce_qui_a_ete_depense(monkeypatch):
    async def script(sdk, prompt, options):
        yield AssistantMessage(content=[], model="claude-sonnet-5",
                               parent_tool_use_id=None, message_id="m1",
                               usage={"input_tokens": 120, "output_tokens": 30})
        yield SystemMessage(subtype="api_retry", data={"error_status": 403})

    with pytest.raises(RuntimeError) as pris:
        _lancer(monkeypatch, script)
    assert pris.value.usage_partiel["input_tokens"] == 120
    assert pris.value.couverture_partielle["tours"] == 1


def test_un_transport_mort_emporte_ce_qui_a_ete_depense(monkeypatch):
    mcp = Mcp()
    mcp.panne = RuntimeError("session MCP rouverte mais refusée")

    async def script(sdk, prompt, options):
        yield AssistantMessage(content=[], model="claude-sonnet-5",
                               parent_tool_use_id=None, message_id="m1",
                               usage={"input_tokens": 90, "output_tokens": 10})
        await sdk.outils["data_rows"].handler({})
        yield AssistantMessage(content=[], model="claude-sonnet-5",
                               parent_tool_use_id=None, message_id="m2", usage={})

    with pytest.raises(RuntimeError, match="transport MCP mort") as pris:
        _lancer(monkeypatch, script, mcp=mcp)
    assert pris.value.usage_partiel["input_tokens"] is None, "un message muet : pas de somme"
    assert pris.value.couverture_partielle["sommes"]["input_tokens"] == 90
    assert pris.value.pas_partiels == 1


def test_un_deroule_mort_avant_son_premier_message_ne_declare_rien(monkeypatch):
    """⚠️ `None` n'est pas 0 : un déroulé mort avant d'avoir rien dépensé ne doit
    PAS rendre des zéros, qui se liraient comme une mesure."""
    async def script(sdk, prompt, options):
        raise ResultError("démarrage refusé")
        yield

    with pytest.raises(ResultError) as pris:
        _lancer(monkeypatch, script)
    assert pris.value.couverture_partielle["tours"] == 0
    assert conclusion.resultat_partiel(pris.value, "claude-sonnet-5") is None


# ── La borne devient muette : on s'arrête en le DISANT ───────────────────────

def test_un_usage_absent_sous_borne_arrete_en_le_nommant(monkeypatch):
    async def script(sdk, prompt, options):
        yield AssistantMessage(content=[], model="claude-sonnet-5",
                               parent_tool_use_id=None, message_id="m1",
                               usage={"input_tokens": 10, "output_tokens": 2})
        yield AssistantMessage(content=[], model="claude-sonnet-5",
                               parent_tool_use_id=None, message_id="m2", usage={})
        yield _resultat()

    spec = AgentSpec(system="s", tools=frozenset(), max_tokens=10_000)
    res, _, ev = _lancer(monkeypatch, script, spec=spec)
    assert res.stopped == "max_tokens_non_mesurable"
    (dit,) = [c for e, c in ev if e == "borne_non_suivie"]
    assert dit["manque"] == ["entrée", "sortie"] and dit["jetons_bornes"] == 12


def test_un_sous_agent_muet_n_aveugle_pas_la_borne(monkeypatch):
    """Le bilan de session couvre les sous-agents (`model_usage`) : couper le déroulé
    parce qu'un des leurs n'a rien déclaré arrêterait un déroulé sain."""
    async def script(sdk, prompt, options):
        yield AssistantMessage(content=[], model="claude-sonnet-5",
                               parent_tool_use_id=None, message_id="m1",
                               usage={"input_tokens": 10, "output_tokens": 2})
        yield AssistantMessage(content=[], model="claude-haiku-4-5",
                               parent_tool_use_id="t1", message_id="m2", usage={})
        yield _resultat()

    spec = AgentSpec(system="s", tools=frozenset(), max_tokens=10_000)
    res, _, ev = _lancer(monkeypatch, script, spec=spec)
    assert res.stopped == "end_turn"
    assert [c["manque"] for e, c in ev if e == "usage_absent_sous_agent"] == [["entrée", "sortie"]]


def test_sans_borne_demandee_un_message_muet_ne_coupe_rien(monkeypatch):
    async def script(sdk, prompt, options):
        yield AssistantMessage(content=[], model="claude-sonnet-5",
                               parent_tool_use_id=None, message_id="m1", usage={})
        yield _resultat()

    res, _, ev = _lancer(monkeypatch, script)
    assert res.stopped == "end_turn"
    assert not [c for e, c in ev if e == "borne_non_suivie"]


# ── SIGTERM : le CLI fils tué par un redémarrage ─────────────────────────────

class ProcessError(Exception):
    def __init__(self, message, exit_code=None):
        super().__init__(message)
        self.exit_code = exit_code


def test_le_cli_tue_par_un_signal_le_dit_et_emporte_son_cout(monkeypatch):
    """Sous le `KillMode` par défaut, un `systemctl restart` tue le `claude` fils : le
    SDK sort en code NÉGATIF. Le travail disait « sorti sans message de résultat »."""
    async def script(sdk, prompt, options):
        yield AssistantMessage(content=[], model="claude-sonnet-5",
                               parent_tool_use_id=None, message_id="m1",
                               usage={"input_tokens": 500, "output_tokens": 40})
        raise ProcessError("Command failed with exit code -15", exit_code=-15)

    with pytest.raises(RuntimeError, match="signal 15") as pris:
        _lancer(monkeypatch, script)
    assert "KillMode=mixed" in str(pris.value)
    assert pris.value.usage_partiel["input_tokens"] == 500
    assert pris.value.couverture_partielle["tours"] == 1


def test_une_sortie_en_erreur_ordinaire_n_est_pas_dite_signal(monkeypatch):
    async def script(sdk, prompt, options):
        raise ProcessError("Command failed with exit code 1", exit_code=1)
        yield

    with pytest.raises(ProcessError):
        _lancer(monkeypatch, script)


def test_le_drapeau_d_arret_du_worker_ne_coupe_pas_un_deroule_en_vol(monkeypatch):
    """Le handler de SIGTERM du worker ne fait que lever un drapeau : le travail en
    cours va jusqu'au bout, et c'est la BOUCLE qui s'arrête ensuite."""
    monkeypatch.setattr(W, "_arret_demande", False)
    W._demander_arret(15, None)
    try:
        res, _, _ = _lancer(monkeypatch, _simple)
        assert res.stopped == "end_turn", "le déroulé en vol n'est pas interrompu"
        assert W._arret_demande is True
    finally:
        W._arret_demande = False


def test_les_jetons_d_un_travail_MORT_arrivent_jusqu_au_serveur(monkeypatch, tmp_path):
    """Le bout de la chaîne : ce que `run_once` accroche à l'exception doit arriver
    dans le `complete` que le worker rend au serveur. Un attribut que personne ne lit
    n'est pas une correction — c'est là que le coût d'un déroulé mort se paie."""
    monkeypatch.setenv("OTO_RUNNER_JOURNAL_DIR", str(tmp_path))

    class McpDuTravail(FauxMcp):
        def schemas(self, noms):
            return Mcp().schemas(noms)

    monkeypatch.setattr(W, "McpSession", McpDuTravail)

    def faux_run_once(**kw):
        boum = AC.DeadlineExceeded("déroulé Claude Code > 900s wall-clock")
        boum.usage_partiel = {"input_tokens": 4000, "output_tokens": 900,
                              "cache_read_input_tokens": 12,
                              "cache_creation_input_tokens": 30}
        boum.couverture_partielle = {"tours": 5, "declares": {}, "sommes": {}}
        boum.modele_partiel = "claude-sonnet-5"
        boum.pas_partiels = 7
        raise boum

    monkeypatch.setattr(AC, "run_once", faux_run_once)
    backend = FauxBackend()
    W._un_travail(backend, _job("start"), AC, file=backend)

    (rendu,) = [a[1] for a in backend.appels if a[0] == "complete_result"]
    assert rendu["usage_tokens"] == 4900, "l'entrée non cachée + la sortie"
    assert rendu["usage_cache_read"] == 12
    assert rendu["steps"] == 7 and rendu["model"] == "claude-sonnet-5"
    assert rendu["stopped"] == "DeadlineExceeded"
    assert ("complete", False, "r-NEUF") in backend.appels


def test_ce_que_le_SDK_pose_lui_meme_n_est_pas_efface(monkeypatch):
    """`CLAUDE_CODE_ENTRYPOINT` est posé AVANT `options.env` dans la fusion du SDK :
    l'effacer y mettrait notre vide. `CLAUDECODE`, le SDK le retire exprès."""
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("CLAUDECODE", "1")
    _, sdk, _ = _lancer(monkeypatch, _simple)
    assert "CLAUDE_CODE_ENTRYPOINT" not in sdk.options["env"]
    assert "CLAUDECODE" not in sdk.options["env"]
