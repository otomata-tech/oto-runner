"""Un travail sans écriture n'est pas un travail terminé.

Sept lignes sur cent conclues en prose, sans qu'aucun appel d'écriture ne parte
(28/08). La consigne l'interdit déjà en toutes lettres — « arrête-toi en écrivant » —
et n'a pas empêché ; c'est la quatrième fois ce jour-là qu'une règle écrite ne tient
pas. On ne peut pas empêcher un agent de se taire : on peut **refuser que son silence
compte comme un travail fini**, et lui rendre la main AU MOMENT DE LA FAUTE — seul
endroit où la phrase est encore actionnable.

⚠️ Ce que ces tests protègent en priorité : que le rappel ne fasse pas RÉSERVER une
ligne de plus. Sans le retrait de l'outil, l'agent recommence au lieu de finir, et
l'on compte trois réservations pour une ligne traitée, deux lignes restant sous bail
pour rien.
"""
import oto_runner.worker as W
from oto_runner.agent_runtime import AgentResult, AgentStep


class _Mcp:
    derniere_ligne = "01a0-la-ligne"

    def __init__(self, *a, **kw):
        pass

    def outil(self, name, arguments=None):
        if name == "run_start":
            return {"run_id": "r-1"}
        if name == "data_get_schema":
            return {"schema": {"fields": []}}
        return {}


class _Backend:
    def __init__(self):
        self.appels = []
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
        self.appels.append(("complete", result or {}))

    def patch_row(self, namespace, row_id, valeurs, org=None):
        self.appels.append(("patch", row_id, valeurs))
        return {}


def _job():
    return {"id": 1, "kind": "start", "run_id": None,
            "payload": {"procedure": "p", "namespace": "un-tableau", "org_id": 226,
                        "project_id": 360, "input": "fais",
                        "tools": ["data_claim_next", "data_write", "serper_search"]}}


def _pas(*noms):
    return [AgentStep(tool=n, ok=True, duration_ms=1) for n in noms]


def _monter(monkeypatch, suites):
    """`suites` : ce que rend chaque appel successif de la boucle d'agent."""
    vus = []

    def faux_run(spec, transport, provider, prompt=None, history=None, on_turn=None, **_):
        vus.append({"prompt": prompt, "tools": set(spec.tools)})
        return suites[min(len(vus) - 1, len(suites) - 1)]

    monkeypatch.setattr(W.agent_runtime, "run", faux_run)
    monkeypatch.setattr(W, "McpSession", _Mcp)
    monkeypatch.setattr(W, "_estampille", lambda *a, **kw: {})
    return vus


def _resultat(b):
    return next(a for a in b.appels if a[0] == "complete")[1]


def test_un_travail_avec_ecriture_n_est_pas_renvoye(monkeypatch):
    vus = _monter(monkeypatch, [AgentResult(
        reply="fait", stopped="end_turn",
        steps=_pas("data_claim_next", "serper_search", "data_write"))])
    b = _Backend()
    W._traiter(b, _job(), provider=None)
    assert len(vus) == 1, "aucun rappel : l'agent a écrit"
    assert _resultat(b)["renvois"] == 0


def test_un_travail_sans_ecriture_est_renvoye_puis_conclut(monkeypatch):
    """Le cas nominal du mécanisme : l'agent oublie, on lui rend la main, il écrit."""
    vus = _monter(monkeypatch, [
        AgentResult(reply="belle synthèse", stopped="end_turn",
                    steps=_pas("data_claim_next", "serper_search")),
        AgentResult(reply="écrit", stopped="end_turn", steps=_pas("data_write")),
    ])
    b = _Backend()
    W._traiter(b, _job(), provider=None)
    assert len(vus) == 2, "un rappel, puis l'agent écrit"
    r = _resultat(b)
    assert r["renvois"] == 1 and r["writes"] == 1
    assert r["abandon_enregistre"] is False


def test_le_rappel_nomme_la_ligne_et_interdit_de_reserver(monkeypatch):
    """⚠️ LE test qui compte : sans le retrait de l'outil, l'agent recommence au
    lieu de finir — trois réservations pour une ligne traitée."""
    vus = _monter(monkeypatch, [
        AgentResult(reply="rien", stopped="end_turn", steps=_pas("data_claim_next")),
        AgentResult(reply="écrit", stopped="end_turn", steps=_pas("data_write")),
    ])
    W._traiter(_Backend(), _job(), provider=None)
    rappel = vus[1]
    assert "01a0-la-ligne" in rappel["prompt"], "le rappel NOMME la ligne — sans " \
        "identifiant, il ne serait qu'un reproche"
    assert "data_claim_next" not in rappel["tools"], "l'outil de réservation est retiré"
    assert "data_write" in rappel["tools"]


def test_apres_deux_rappels_l_abandon_s_enregistre(monkeypatch):
    """Une perte silencieuse devient une perte lisible. ⚠️ Le harnais n'écrit RIEN
    sur l'entreprise — seulement un fait sur NOTRE traitement."""
    muet = AgentResult(reply="Je n'ai rien trouvé de probant sur cette maison.",
                       stopped="end_turn", steps=_pas("data_claim_next", "serper_search"))
    vus = _monter(monkeypatch, [muet])
    b = _Backend()
    W._traiter(b, _job(), provider=None)
    assert len(vus) == 3, "l'appel initial plus deux rappels"
    patch = next(a for a in b.appels if a[0] == "patch")
    assert patch[1] == "01a0-la-ligne"
    assert patch[2]["retraitement"] == "arbitrage", \
        "« épuisé » affirmerait une recherche conclue ; l'agent n'a rien conclu"
    motif = patch[2]["retraitement_motif"]
    assert "rien trouvé de probant" in motif, "le motif porte la raison DE L'AGENT"
    # ⚠️ La SIGNATURE du harnais, seul moyen de distinguer cet abandon d'un
    # `arbitrage` qu'un agent a JUGÉ : la valeur est la même, le motif non.
    assert motif.startswith("conclu sans écrire"), \
        "sans signature en tête, un abandon se compte comme un jugement d'agent"
    assert len(motif) < 500, "borné : un motif de trois pages se saute"
    # Aucun champ métier touché : ni qualification, ni notes, ni contact.
    assert set(patch[2]) == {"retraitement", "retraitement_motif"}
    assert _resultat(b)["abandon_enregistre"] is True


def test_les_jetons_des_rappels_se_cumulent(monkeypatch):
    """Le coût réel d'un travail est la somme de ses passages — sinon les bornes de
    flotte sous-estiment ce qu'on paie."""
    # Des objets DISTINCTS, comme en réel : le fournisseur rend un résultat neuf
    # à chaque passage. (Le harnais se protège aussi du cas contraire.)
    _monter(monkeypatch, [
        AgentResult(reply="rien", stopped="end_turn", steps=_pas("data_claim_next"),
                    usage={"input_tokens": 100, "output_tokens": 10}),
        AgentResult(reply="rien", stopped="end_turn", steps=_pas("serper_search"),
                    usage={"input_tokens": 100, "output_tokens": 10}),
        AgentResult(reply="rien", stopped="end_turn", steps=_pas("serper_search"),
                    usage={"input_tokens": 100, "output_tokens": 10}),
    ])
    b = _Backend()
    W._traiter(b, _job(), provider=None)
    r = _resultat(b)
    assert r["usage_tokens"] == 330, "trois passages à 110 jetons"
    assert r["claims"] == 1, "UNE ligne réservée, malgré trois passages"


def test_un_rappel_impossible_ne_tue_pas_le_travail(monkeypatch):
    """Un rappel qui échoue laisse le résultat initial et enregistre l'abandon :
    le mécanisme ne coûte jamais le travail qu'il tente de sauver."""
    appels = []

    def faux_run(spec, transport, provider, prompt=None, **_):
        appels.append(prompt)
        if len(appels) > 1:
            raise RuntimeError("le fournisseur ne répond pas")
        return AgentResult(reply="rien", stopped="end_turn",
                           steps=_pas("data_claim_next"))

    monkeypatch.setattr(W.agent_runtime, "run", faux_run)
    monkeypatch.setattr(W, "McpSession", _Mcp)
    monkeypatch.setattr(W, "_estampille", lambda *a, **kw: {})
    b = _Backend()
    W._traiter(b, _job(), provider=None)
    r = _resultat(b)
    assert r["renvois"] == 1
    assert r["abandon_enregistre"] is True, "l'abandon est enregistré malgré l'échec"
