"""Un poste de relevé doit dire ce qu'il vaut.

⚠️ Le 29/08, `claims: 1` a servi à affirmer qu'une ligne avait été attribuée à un
travail — donc à porter un fait de plateforme. C'était une RÈGLE DE REPLI : sur le
chemin où la boucle d'outils tourne chez le fournisseur, aucune sortie de
réservation ne remonte, et « un appel de travail après une réservation » vaut
« il tenait une ligne ». Le claim avait en réalité rendu `row: null`.

Le code le disait dans sa docstring. Le relevé ne le disait pas — et c'est le
relevé qu'on lit.
"""
import oto_runner.worker as W
from oto_runner.agent_runtime import AgentResult, AgentStep


class _Mcp:
    derniere_ligne = None

    def __init__(self, *a, **kw):
        pass

    def outil(self, name, arguments=None):
        return {"run_id": "r-1"} if name == "run_start" else {}


class _Backend:
    def __init__(self):
        self.res = {}
        self.base = "https://exemple.invalide"

    def bind_run(self, *a, **kw):
        pass

    def thread_append(self, *a, **kw):
        return 1

    def extend(self, *a, **kw):
        pass

    def thread_read(self, *a, **kw):
        return []

    def complete(self, job_id, ok=True, error=None, run_id=None, result=None):
        self.res = result or {}


class _UnShot:
    """Le chemin où la boucle d'outils tourne chez le fournisseur."""

    ONE_SHOT = True
    __name__ = "agent_conversations"

    @staticmethod
    def resolve_key():
        return "x"

    @staticmethod
    def model():
        return "m"

    @staticmethod
    def run_once(*, instructions, inputs, tools):
        return AgentResult(
            reply="fait", stopped="end_turn",
            steps=[AgentStep(tool="data_claim_next", ok=True, duration_ms=1),
                   AgentStep(tool="data_write", ok=True, duration_ms=1)])


def _lancer(monkeypatch, provider):
    monkeypatch.setattr(W.agent_runtime, "run", lambda *a, **k: AgentResult(
        reply="fait", stopped="end_turn",
        steps=[AgentStep(tool="data_claim_next", ok=True, duration_ms=1),
               AgentStep(tool="data_write", ok=True, duration_ms=1)]))
    monkeypatch.setattr(W, "McpSession", _Mcp)
    monkeypatch.setattr(W, "_estampille", lambda *a, **kw: {})
    b = _Backend()
    W._traiter(b, {"id": 1, "kind": "start", "run_id": None,
                   "payload": {"procedure": "p", "namespace": "t", "org_id": 226,
                               "input": "fais", "tools": ["data_claim_next",
                                                          "data_write"]}},
               provider=provider)
    return b.res


def test_sur_le_chemin_local_les_reservations_sont_MESUREES(monkeypatch):
    """Le worker voit la sortie du claim : le compte est fidèle."""
    assert _lancer(monkeypatch, None)["claims_mesures"] is True


def test_sur_le_chemin_du_fournisseur_elles_sont_ESTIMEES(monkeypatch):
    """⚠️ Aucune sortie ne remonte : `claims` est un repli, et le relevé doit le
    dire — sinon il sera lu comme une mesure par qui n'a pas lu la docstring."""
    assert _lancer(monkeypatch, _UnShot)["claims_mesures"] is False


# ── La réservation se MESURE quand le fournisseur rend ses sorties ──────────

def _res_avec_sorties(info):
    r = AgentResult(reply="fait", stopped="end_turn",
                    steps=[AgentStep(tool="data_claim_next", ok=True, duration_ms=1),
                           AgentStep(tool="data_write", ok=True, duration_ms=1)])
    r.raw_outputs = [{"type": "tool.execution", "name": "data_claim_next",
                      "info": info}]
    return r


def test_une_reservation_qui_rend_une_ligne_compte_pour_une():
    res = _res_avec_sorties({"row": {"_id": "01a04aef-26c0-7c16-9c58-abcdef012345"}})
    assert W._lignes_reservees(res, 1, one_shot=True) == 1


def test_row_null_est_une_FIN_NORMALE_et_ne_compte_aucune_ligne():
    """⚠️ Fin de file : la dernière ligne est sous le bail d'un pair. L'agent n'a
    rien à écrire, rien n'est perdu — et le 29/08, compter cette réservation a
    fait accuser la plateforme d'avoir perdu un lien qu'elle n'avait jamais eu."""
    assert W._lignes_reservees(_res_avec_sorties({"row": None}), 1,
                               one_shot=True) == 0


def test_sans_sorties_du_fournisseur_on_retombe_sur_le_repli():
    """Le repli reste, mais seulement à défaut de mesure."""
    r = AgentResult(reply="fait", stopped="end_turn",
                    steps=[AgentStep(tool="data_claim_next", ok=True, duration_ms=1),
                           AgentStep(tool="data_write", ok=True, duration_ms=1)])
    assert W._lignes_reservees(r, 1, one_shot=True) == 1
