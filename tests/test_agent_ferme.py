"""La voie `claude-farm` : un travail `anthropic` exécuté par Claude Code dans la ferme, sur
la clé de l'org — remise avec chaque run, jamais gardée. Flux synthétiques (dépôt public)."""
from __future__ import annotations

import json
import types

import pytest

from oto_runner import agent_ferme as F
from oto_runner import backend as B
from oto_runner import llm_select, worker
from oto_runner.agent_runtime import AgentSpec

INIT = {"type": "system", "subtype": "init", "apiKeySource": "ANTHROPIC_API_KEY",
        "model": "claude-x"}
FIN = {"type": "result", "subtype": "success", "is_error": False, "result": "fait",
       "usage": {"input_tokens": 10, "output_tokens": 5}}
FORFAIT = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}
MCP = types.SimpleNamespace(url="https://mcp", token="jeton-delegue", org=4242, project=7,
                            run_id="r1")


class Reponse:
    def __init__(self, statut, evenements=()):
        self.status_code, self._ev, self.text = statut, list(evenements), ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self, decode_unicode=True):
        return (json.dumps(e) for e in self._ev)


@pytest.fixture
def ferme(monkeypatch):
    monkeypatch.setenv("OTO_FERME_URL", "http://ferme:8190/")
    monkeypatch.setenv("OTO_FERME_TOKEN", "t")
    F._sandboxes_creees.clear()
    vu = {"put": [], "post": [], "reponses": []}
    monkeypatch.setattr(F.requests, "put", lambda url, **kw: vu["put"].append(url)
                        or types.SimpleNamespace(status_code=200, text=""))

    def post(url, json=None, **kw):
        vu["post"].append((url, json))
        return vu["reponses"].pop(0)
    monkeypatch.setattr(F.requests, "post", post)
    return vu


def _lancer(**kw):
    base = dict(instructions="i", inputs="go", tools=["data_rows"], api_key="sk-ant-api03-org",
                modele="claude-sonnet-5", mcp=MCP, attendre=lambda s: None)
    return F.run_once(**{**base, **kw})


def test_le_run_part_sur_la_cle_de_l_org_dans_le_sandbox_de_l_org(ferme):
    ferme["reponses"].append(Reponse(200, [INIT, FIN]))
    res = _lancer(workspace="wrkspc_01", effort="high", max_seconds=900, max_tokens=50_000)
    slug = F.sandbox_de_l_org(4242)
    assert slug.startswith("o") and "4242" not in slug
    assert ferme["put"] == [f"http://ferme:8190/api/sandboxes/{slug}"]
    url, corps = ferme["post"][0]
    assert url == f"http://ferme:8190/api/sandboxes/{slug}/runs"
    assert (corps["auth"], corps["api_key"], corps["workspace"]) == (
        "key", "sk-ant-api03-org", "wrkspc_01")
    assert (corps["effort"], corps["max_seconds"], corps["model"]) == (
        "high", 900, "claude-sonnet-5")
    assert corps["mcp"]["org"] == 4242 and corps["mcp"]["tools"] == ["data_rows"]
    assert res.stopped == "end_turn" and res.reply == "fait"


def test_le_sandbox_n_est_cree_qu_une_fois_par_processus(ferme):
    ferme["reponses"] += [Reponse(200, [INIT, FIN]), Reponse(200, [INIT, FIN])]
    _lancer()
    _lancer()
    assert len(ferme["put"]) == 1 and len(ferme["post"]) == 2


def test_sans_cle_d_org_rien_ne_part(ferme):
    with pytest.raises(RuntimeError, match="clé d'organisation"):
        _lancer(api_key=None)
    assert ferme["post"] == [] and ferme["put"] == []


def test_une_session_qui_paierait_a_la_place_de_la_cle_arrete_le_run(ferme):
    ferme["reponses"].append(Reponse(200, [dict(INIT, apiKeySource="none"), FIN]))
    with pytest.raises(RuntimeError, match="apiKeySource"):
        _lancer()


def test_une_cle_ne_rapporte_aucun_forfait(ferme):
    ferme["reponses"].append(Reponse(200, [INIT, FORFAIT, FIN]))
    assert _lancer().abonnement is None


def test_une_ferme_pleine_se_reessaie_bail_prolonge(ferme):
    ferme["reponses"] += [Reponse(429), Reponse(429), Reponse(200, [INIT, FIN])]
    prolonge, attentes = [], []
    res = _lancer(prolonger=lambda: prolonge.append(1), attendre=attentes.append)
    assert res.stopped == "end_turn"
    assert len(prolonge) == 2 and attentes == [20, 40]


def test_une_ferme_qui_reste_pleine_echoue_en_le_nommant(ferme):
    ferme["reponses"] += [Reponse(429)] * F._PLEINE_ESSAIS
    with pytest.raises(F.FermePleine):
        _lancer()


def test_les_limites_tiennent_en_vol_comme_sur_l_abonnement(ferme):
    msg = {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": ""}],
           "usage": {"input_tokens": 60, "output_tokens": 1, "cache_creation_input_tokens": 0}}}
    ferme["reponses"].append(Reponse(200, [INIT, msg, FIN]))
    assert _lancer(max_tokens=50).stopped == "max_tokens"


# ── le worker ────────────────────────────────────────────────────────────────

def test_le_worker_la_selectionne_et_lui_passe_effort_et_workspace(monkeypatch):
    monkeypatch.setenv("OTO_RUNNER_PROVIDER", "claude-farm")
    assert llm_select.get_provider() is F
    spec = AgentSpec(system="s", tools=frozenset(), effort="high")
    assert worker._reglages_du_one_shot(spec, F, "wrkspc_01") == {
        "effort": "high", "workspace": "wrkspc_01"}
    # Les autres one-shot ne reçoivent rien de plus qu'avant.
    from oto_runner import agent_abonnement, agent_conversations
    for autre in (agent_abonnement, agent_conversations):
        assert worker._reglages_du_one_shot(spec, autre, "wrkspc_01") == {}


def test_l_effort_n_est_pas_refuse_sur_la_ferme_mais_l_est_sur_conversations():
    from oto_runner import agent_conversations
    worker._exiger_effort_servi({"effort": "high"}, F)
    with pytest.raises(worker.EffortNonServi):
        worker._exiger_effort_servi({"effort": "high"}, agent_conversations)


def test_une_cle_d_org_se_dit_payee_par_l_org():
    from oto_runner import agent_abonnement
    assert worker._paye_par(F, "sk-ant-api03-org") == "cle_org"
    assert worker._paye_par(agent_abonnement, None) == "abonnement"


def test_la_famille_de_la_ferme_est_anthropic():
    worker._exiger_ma_famille({"model_family": "anthropic"}, F)
    with pytest.raises(worker.FamilleEtrangere):
        worker._exiger_ma_famille({"model_family": "claude_subscription"}, F)


# ── ne servir que certaines orgs ─────────────────────────────────────────────

def test_les_orgs_servies_se_lisent_au_boot(monkeypatch):
    monkeypatch.delenv("OTO_RUNNER_ORGS", raising=False)
    assert worker._orgs_servies() is None
    monkeypatch.setenv("OTO_RUNNER_ORGS", "4242, 4343")
    assert worker._orgs_servies() == [4242, 4343]
    for mauvaise in ("abc", ",", "4242;4343"):
        monkeypatch.setenv("OTO_RUNNER_ORGS", mauvaise)
        with pytest.raises(SystemExit):
            worker._orgs_servies()


def test_le_claim_n_envoie_org_ids_que_s_il_est_pose(monkeypatch):
    vus = []
    client = B.Backend.__new__(B.Backend)
    monkeypatch.setattr(client, "_post", lambda chemin, corps, **k: vus.append(corps) or {},
                        raising=False)
    client.claim(lease_seconds=600, depot="anthropic")
    client.claim(lease_seconds=600, depot="anthropic", org_ids=[4242])
    assert "org_ids" not in vus[0]
    assert vus[1]["org_ids"] == [4242]
