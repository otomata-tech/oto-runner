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
    r = _resultat(b)
    assert r["abandon_enregistre"] is True
    # ⚠️ Le relevé DÉCLARE la ligne marquée. Sans cet identifiant, le bilan devrait
    # la retrouver en cherchant une formule dans un motif — une source qui casse
    # sans bruit le jour où la formule bouge.
    assert r["ligne_abandonnee"] == "01a0-la-ligne"


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


# ── Le compteur d'appels ment : constater l'effet ───────────────────────────
# Reproduit `f89d393d` (29/08) : deux `data_write` comptés RÉUSSIS, une ligne
# restée vierge, et le rappel qui n'a pas tiré sur le cas qu'il vise.

class _BackendLigne(_Backend):
    """Un backend dont la ligne relue porte, ou non, l'estampille."""

    def __init__(self, estampillee: bool):
        super().__init__()
        self.estampillee = estampillee
        self.relectures = 0

    def row(self, namespace, row_id, org=None):
        self.relectures += 1
        return {"siren": "924260243",
                "modele": "mistral-large-2512" if self.estampillee else None}


def _monter_estampille(monkeypatch, suites):
    vus = _monter(monkeypatch, suites)
    monkeypatch.setattr(W, "_estampille", lambda *a, **kw: {"modele": "mistral-large-2512"})
    return vus


def test_une_ecriture_refusee_ne_compte_pas_comme_une_ecriture(monkeypatch):
    """⚠️ LE cas de f89d393d : l'agent appelle data_write, le transport réussit,
    la plateforme REFUSE, la ligne reste vierge. Le compteur voit une écriture ;
    la ligne dit le contraire. C'est la ligne qui a raison."""
    vus = _monter_estampille(monkeypatch, [
        AgentResult(reply="j'ai écrit", stopped="end_turn",
                    steps=_pas("data_claim_next", "data_write", "data_write")),
        AgentResult(reply="cette fois pour de bon", stopped="end_turn",
                    steps=_pas("data_write")),
    ])
    b = _BackendLigne(estampillee=False)
    W._traiter(b, _job(), provider=None)
    assert b.relectures >= 1, "la ligne est RELUE, pas déduite du compteur"
    assert len(vus) >= 2, "le rappel tire malgré deux écritures comptées"
    assert "01a0-la-ligne" in vus[1]["prompt"], "et il nomme la ligne"


def test_une_ecriture_reelle_ne_declenche_aucun_rappel(monkeypatch):
    """Le pendant : la ligne porte l'estampille, donc l'écriture a abouti. Un
    rappel ici ferait retravailler un agent qui a bien fini — un tour payé pour
    rien, et le mécanisme deviendrait plus coûteux que le défaut qu'il corrige."""
    vus = _monter_estampille(monkeypatch, [
        AgentResult(reply="fait", stopped="end_turn",
                    steps=_pas("data_claim_next", "data_write"))])
    b = _BackendLigne(estampillee=True)
    W._traiter(b, _job(), provider=None)
    assert len(vus) == 1, "aucun rappel : la ligne PORTE l'estampille"


def test_une_relecture_en_panne_ne_declenche_pas_de_rappel(monkeypatch):
    """On ne conclut jamais « rien écrit » d'une incertitude. Une lecture qui
    échoue laisse le compteur décider — se tromper vers le rappel coûterait un
    tour entier à chaque incident réseau."""
    class _Muet(_BackendLigne):
        def row(self, namespace, row_id, org=None):
            self.relectures += 1
            return None

    vus = _monter_estampille(monkeypatch, [
        AgentResult(reply="fait", stopped="end_turn",
                    steps=_pas("data_claim_next", "data_write"))])
    b = _Muet(estampillee=False)
    W._traiter(b, _job(), provider=None)
    assert b.relectures == 1
    assert len(vus) == 1, "lecture en panne ⟹ on s'en remet au compteur"


# ── Le HARNAIS relâche, plus l'agent ────────────────────────────────────────
# ⚠️ 29 agents sur 30 relâchaient AVANT d'écrire, inversant l'ordre de leur
# consigne. Le rappel leur rendait alors une ligne qu'un autre travail avait
# reprise : 20 collisions, autant de tours repayés. La consigne le disait déjà —
# on retire donc l'outil au lieu d'ajouter une phrase.

class _McpRelache(_Mcp):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.relaches = []

    def outil(self, name, arguments=None):
        if name == "data_release":
            self.relaches.append((arguments or {}).get("id"))
            return {}
        return super().outil(name, arguments)


def test_le_harnais_relache_la_ligne_a_la_fin(monkeypatch):
    vus = _monter(monkeypatch, [AgentResult(
        reply="fait", stopped="end_turn",
        steps=_pas("data_claim_next", "data_write"))])
    monkeypatch.setattr(W, "McpSession", _McpRelache)
    b = _Backend()
    W._traiter(b, _job(), provider=None)
    assert len(vus) == 1
    assert _resultat(b)["relachee"] is True


def test_le_relachement_vient_APRES_les_rappels(monkeypatch):
    """⚠️ Relâcher avant le rappel rouvrirait la fenêtre qu'on ferme : un autre
    travail prendrait la ligne pendant qu'on la rend à l'agent. Ce serait la
    faute corrigée, commise par le harnais lui-même."""
    ordre = []

    class _Mcp2(_McpRelache):
        def outil(self, name, arguments=None):
            if name == "data_release":
                ordre.append("release")
            return super().outil(name, arguments)

    def faux_run(spec, transport, provider, prompt=None, **_):
        ordre.append("tour")
        return AgentResult(reply="rien", stopped="end_turn",
                           steps=_pas("data_claim_next")
                           if len(ordre) < 3 else _pas("data_write"))

    monkeypatch.setattr(W.agent_runtime, "run", faux_run)
    monkeypatch.setattr(W, "McpSession", _Mcp2)
    monkeypatch.setattr(W, "_estampille", lambda *a, **kw: {})
    W._traiter(_Backend(), _job(), provider=None)
    assert ordre.count("release") == 1, "une seule fois"
    assert ordre[-1] == "release", f"le relâchement est le DERNIER geste : {ordre}"


# ── La FORME de l'appel, imposée par le harnais ─────────────────────────────
# ⚠️ Le 29/08, une consigne montrait `namespace: "@claimed"`. Les agents ont
# copié la forme qu'on leur montrait — 2 écritures refusées sur 5. Une forme se
# copie là où une règle se relit, y compris quand elle est fausse. Le harnais
# donne donc l'appel entier plutôt que des morceaux à assembler.

def test_l_ordre_impose_la_forme_complete_de_l_ecriture():
    ordre = W._ordre_one_shot("fais", "run-1",
                              {"namespace": "un-tableau", "project_id": 7}, None)
    assert 'namespace: "un-tableau"' in ordre, "le tableau est nommé"
    assert 'id: "@claimed"' in ordre, "et @claimed est montré À SA PLACE"
    assert "NULLE PART ailleurs" in ordre, "avec l'interdit qui a été enfreint"


def test_l_ordre_ne_suggere_jamais_claimed_comme_tableau():
    """⚠️ LE piège : l'agent remplace le champ qu'on lui demandait de recopier."""
    ordre = W._ordre_one_shot("fais", "run-1", {"namespace": "un-tableau"}, None)
    assert 'namespace: "@claimed"' not in ordre
