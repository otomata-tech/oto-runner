"""La reprise après mort : un start re-claimé REPREND son fil, il ne rouvre rien.

La mort du worker n'est jamais un événement : le bail expire, un pair
re-claime, recharge le fil, continue. Le discriminant est le RUN LIÉ (bind_run
a eu lieu avant la mort), pas le kind — sans ça, chaque kill -9 sur un start
entamé fabriquait un run orphelin et un doublon de travail.
"""
from __future__ import annotations

from oto_runner import worker as W


class FauxBackend:
    def __init__(self, fil=None):
        self.fil = fil or []
        self.appels = []

    def bind_run(self, job_id, run_id):
        self.appels.append(("bind_run", run_id))

    def thread_read(self, run_id, include_raw=False):
        self.appels.append(("thread_read", run_id))
        return self.fil

    def thread_append(self, run_id, role, content, provider_raw=None):
        self.appels.append(("append", role))
        return 1

    def extend(self, job_id, lease_seconds=600):
        pass

    def complete(self, job_id, ok, error=None, run_id=None, result=None):
        self.appels.append(("complete", ok, run_id))
        return "done"


class FauxMcp:
    def __init__(self, **kw):
        self.run_id = None
        self.outils = []

    def outil(self, name, args=None):
        self.outils.append(name)
        if name == "oto_procedure":
            return {"body_md": "la procédure"}
        if name == "run_start":
            return {"run_id": "r-NEUF"}
        return {}

    def schemas(self, names):
        return []

    def call(self, name, arguments):
        return "ok", False


def _job(kind, run_id=None):
    return {"id": 7, "kind": kind, "run_id": run_id,
            "payload": {"procedure": "demo", "tools": ["data_rows"],
                        "input": "Vas-y.", "max_steps": 3}}


def _run_stub(monkeypatch, resultat_prompt):
    """Capture le prompt/history passés à la boucle, sans tour de modèle."""
    vu = {}

    def faux_run(spec, transport, provider, prompt=None, history=None, on_turn=None):
        vu.update(prompt=prompt, history=list(history or []))
        from oto_runner.agent_runtime import AgentResult
        return AgentResult(reply="fini", stopped="end_turn")

    monkeypatch.setattr(W.agent_runtime, "run", faux_run)
    monkeypatch.setattr(W, "McpSession", FauxMcp)
    return vu


def test_un_start_vierge_ouvre_son_run(monkeypatch):
    vu = _run_stub(monkeypatch, None)
    b = FauxBackend()
    W._traiter(b, _job("start"), provider=None)
    assert ("bind_run", "r-NEUF") in b.appels, "le start vierge ouvre et lie son run"
    assert vu["prompt"] == "Vas-y." and vu["history"] == []


def test_un_start_reclaime_apres_mort_reprend_son_fil(monkeypatch):
    """LE cas kill -9 : le job start porte déjà run_id (lié avant la mort) —
    le fil se recharge, AUCUN run neuf, AUCUN prompt rejoué (le message initial
    est déjà dans le fil, apposé au premier vol)."""
    vu = _run_stub(monkeypatch, None)
    fil = [{"provider_raw": {"role": "user", "content": "Vas-y."}},
           {"provider_raw": {"role": "assistant", "content": "je commence"}}]
    b = FauxBackend(fil=fil)
    W._traiter(b, _job("start", run_id="r-SURVIVANT"), provider=None)
    assert ("thread_read", "r-SURVIVANT") in b.appels, "le fil est rechargé"
    assert not any(a[0] == "bind_run" for a in b.appels), "jamais un run neuf"
    assert vu["prompt"] is None, "reprise PURE — rien à rejouer"
    assert len(vu["history"]) == 2, "l'historique EST le fil du run survivant"
    assert ("complete", True, "r-SURVIVANT") in b.appels


def test_un_continue_garde_son_message(monkeypatch):
    vu = _run_stub(monkeypatch, None)
    b = FauxBackend(fil=[{"provider_raw": {"role": "user", "content": "avant"}}])
    W._traiter(b, _job("continue", run_id="r-1"), provider=None)
    assert vu["prompt"] == "Vas-y.", "le continue porte SON message user"
