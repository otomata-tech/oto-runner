"""Les limites d'UN run déclarées sur l'agent : `max_seconds` et `max_tokens`, par moteur.

- boucle ordinaire : l'échéance murale se vérifie AVANT chaque tour ;
- Conversations : l'échéance borne passes et relances, rabotée à celle du chemin ;
- ferme (`claude-subscription`) : jetons et échéance tenus EN VOL sur le flux du CLI,
  qui répète l'usage d'un message sur chacun de ses blocs et n'y annonce qu'une sortie
  partielle ;
- et partout : sans limite déclarée, AUCUNE échéance nouvelle.

Les flux sont SYNTHÉTIQUES — la forme d'un flux réel, aucune de ses valeurs (dépôt public).
"""
from __future__ import annotations

import pytest
import requests

from oto_runner import agent_abonnement as A
from oto_runner import agent_conversations as C
from oto_runner import agent_runtime, conclusion, worker
from oto_runner.agent_runtime import AgentSpec
from oto_runner.deadline import DeadlineExceeded
from oto_runner.llm_types import ToolCall, Turn

from test_agent_runtime import FauxProvider, FauxTransport


class Horloge:
    """Une horloge qu'on avance à la main."""

    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


# ── boucle ordinaire ─────────────────────────────────────────────────────────

def _appel(i):
    return Turn(text="", tool_calls=(ToolCall(id=f"t{i}", name="data_rows", arguments={}),),
                stop_reason="tool_use", raw_content=[],
                usage={"input_tokens": 10, "output_tokens": 5})


def test_la_boucle_s_arrete_a_son_echeance_avant_le_tour_suivant(monkeypatch):
    h = Horloge()
    monkeypatch.setattr(agent_runtime.time, "monotonic", h)

    class Lent(FauxProvider):
        def complete(self, **kw):
            h.t += 40          # chaque tour coûte 40 s
            return super().complete(**kw)

    spec = AgentSpec(system="s", tools=frozenset({"data_rows"}), max_steps=10, max_seconds=100)
    evenements = []
    res = agent_runtime.run(spec, FauxTransport(), Lent([_appel(i) for i in range(10)]),
                            prompt="go", on_event=lambda e, c: evenements.append((e, c)))
    assert res.stopped == "max_seconds"
    # 0 → 40 → 80 → 120 : le 3e tour part à 80 s (< 100), le 4e ne part pas.
    assert len(res.steps) == 3
    borne = [c for e, c in evenements if e == "borne_atteinte"]
    assert borne and borne[0]["borne"] == "max_seconds" and borne[0]["tour"] == 3


def test_sans_echeance_declaree_la_boucle_n_en_a_aucune(monkeypatch):
    h = Horloge()
    monkeypatch.setattr(agent_runtime.time, "monotonic", h)

    class TresLent(FauxProvider):
        def complete(self, **kw):
            h.t += 10_000
            return super().complete(**kw)

    fin = Turn(text="fait", tool_calls=(), stop_reason="end_turn", raw_content=[],
               usage={"input_tokens": 1, "output_tokens": 1})
    spec = AgentSpec(system="s", tools=frozenset({"data_rows"}), max_steps=10)
    res = agent_runtime.run(spec, FauxTransport(), TresLent([_appel(0), _appel(1), fin]),
                            prompt="go")
    assert res.stopped == "end_turn" and len(res.steps) == 2


def test_le_travail_porte_son_echeance_dans_le_cadre():
    spec = worker._spec_du_job({"id": 1, "payload": {"tools": ["x"], "max_seconds": 600}})
    assert spec.max_seconds == 600
    assert worker._spec_du_job({"id": 1, "payload": {"tools": ["x"]}}).max_seconds is None


# ── Conversations ────────────────────────────────────────────────────────────

REPONSE = {"outputs": [{"type": "message.output", "content": "fait"}],
           "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "model": "m"}


@pytest.fixture
def conversation(monkeypatch):
    monkeypatch.setattr(C, "resolve_key", lambda: "k")
    monkeypatch.setattr(C, "connector_id", lambda: "c")
    monkeypatch.setattr(C, "modele_resolu", lambda nom: nom)
    vus = []

    def poster(url, corps, entetes, wall_s=C._WALL_S):
        vus.append(wall_s)
        return REPONSE
    monkeypatch.setattr(C, "_poster", poster)
    return vus


def test_conversations_rabote_l_echeance_a_celle_du_chemin_et_le_dit(conversation):
    evenements = []
    C.run_once(instructions="i", inputs="x", tools=["t"], max_seconds=3600,
               on_event=lambda e, c: evenements.append((e, c)))
    assert conversation == [C._WALL_S]
    rabot = [c for e, c in evenements if e == "borne_rabotee"]
    assert rabot == [{"borne": "max_seconds", "demande": 3600, "servie": C._WALL_S,
                      "raison": "échéance du chemin one-shot"}]


def test_conversations_passe_l_echeance_de_l_agent_a_la_requete(conversation):
    C.run_once(instructions="i", inputs="x", tools=["t"], max_seconds=120)
    assert conversation == [120]


def test_conversations_sans_echeance_garde_la_sienne(conversation):
    C.run_once(instructions="i", inputs="x", tools=["t"])
    assert conversation == [C._WALL_S]


def test_une_passe_coupee_par_l_echeance_de_l_agent_est_une_borne_pas_une_panne(monkeypatch):
    monkeypatch.setattr(C, "resolve_key", lambda: "k")
    monkeypatch.setattr(C, "connector_id", lambda: "c")
    monkeypatch.setattr(C, "modele_resolu", lambda nom: nom)

    def coupe(url, corps, entetes, wall_s=C._WALL_S):
        raise DeadlineExceeded("wall")
    monkeypatch.setattr(C, "_poster", coupe)
    res = C.run_once(instructions="i", inputs="x", tools=["t"], max_seconds=120)
    assert res.stopped == "max_seconds"
    # Rien n'est revenu : l'usage reste NON DÉCLARÉ, jamais un zéro.
    assert conclusion.resultat_declare(res, "m")["usage_tokens"] is None
    with pytest.raises(DeadlineExceeded):     # sans échéance d'agent : la panne d'avant
        C.run_once(instructions="i", inputs="x", tools=["t"])


# ── ferme : le flux du CLI ───────────────────────────────────────────────────

INIT = {"type": "system", "subtype": "init", "apiKeySource": "none", "model": "claude-x"}


def _blocs(mid, entree, sortie_partielle, ecrit, lu, types=("thinking", "tool_use")):
    """Un message du CLI tel qu'il le streame : UN événement par bloc, le MÊME usage
    répété sur chacun, et une sortie partielle."""
    usage = {"input_tokens": entree, "output_tokens": sortie_partielle,
             "cache_creation_input_tokens": ecrit, "cache_read_input_tokens": lu}
    return [{"type": "assistant", "message": {"id": mid, "usage": usage, "content": [
        {"type": t, "id": f"{mid}-{t}", "name": "mcp__oto__data_rows", "input": {}}
        if t == "tool_use" else {"type": t, "text": ""}]}} for t in types]


FLUX = (_blocs("m1", 2, 1, 8000, 0) + _blocs("m2", 2, 3, 200, 8000)
        + _blocs("m3", 2, 3, 10000, 8200) + _blocs("m4", 2, 1, 600, 18200, types=("text",)))
FIN = {"type": "result", "subtype": "success", "is_error": False, "result": "fait",
       "usage": {"input_tokens": 8, "output_tokens": 900},
       "modelUsage": {
           "claude-x[1m]": {"inputTokens": 8, "outputTokens": 900, "cacheReadInputTokens": 34400,
                            "cacheCreationInputTokens": 18800, "costUSD": 0.21,
                            "canonicalModel": "claude-x"},
           "claude-petit": {"inputTokens": 50, "outputTokens": 20, "cacheReadInputTokens": 0,
                            "cacheCreationInputTokens": 0, "costUSD": 0.0001}}}
# Ce que la borne compte, message par message (entrée + sortie + écriture, une fois) :
# 8003, 205, 10005, 603 → 18 816. Une somme naïve des blocs compterait 37 029.
BORNE_DU_FLUX = 8003 + 205 + 10005 + 603


def test_la_borne_de_jetons_compte_chaque_message_une_fois():
    res = A.lire_flux([INIT, *FLUX, FIN], max_tokens=BORNE_DU_FLUX + 1)
    assert res.stopped == "end_turn", "une somme naïve des blocs aurait coupé ce run"


def test_la_borne_de_jetons_arrete_le_run_en_vol():
    evenements = []
    res = A.lire_flux([INIT, *FLUX, FIN], max_tokens=10_000,
                      on_event=lambda e, c: evenements.append((e, c)))
    assert res.stopped == "max_tokens"
    # 8003 + 205 = 8208 < 10 000 ; + 10 005 au 3e message → arrêt, rien lu au-delà.
    assert res.usage["input_tokens"] == 6
    # La sortie en vol est un MINORANT : elle n'est pas publiée comme un compte.
    assert res.usage["output_tokens"] is None
    declare = conclusion.resultat_declare(res, "sub:x")
    assert declare["stopped"] == "max_tokens" and declare["usage_tokens"] is None
    borne = [c for e, c in evenements if e == "borne_atteinte"][0]
    assert borne["borne"] == "max_tokens" and borne["sortie"] == "minorant"


def test_l_echeance_arrete_le_flux_en_vol():
    h = Horloge()

    def flux():
        yield INIT
        for ev in FLUX:
            h.t += 30
            yield ev
        yield FIN
    res = A.lire_flux(flux(), horloge=h, echeance=100)
    assert res.stopped == "max_seconds"


def test_un_silence_au_dela_de_l_echeance_est_une_borne_pas_une_panne():
    h = Horloge()

    def muet():
        yield INIT
        h.t = 500
        raise requests.exceptions.ReadTimeout("silence")
    res = A.lire_flux(A._jusqu_a(muet(), echeance=300, horloge=h), horloge=h, echeance=300)
    assert res.stopped == "max_seconds"


def test_un_silence_avant_l_echeance_reste_une_panne():
    h = Horloge()

    def muet():
        yield INIT
        h.t = 100
        raise requests.exceptions.ReadTimeout("silence")
    with pytest.raises(requests.exceptions.ReadTimeout):
        A.lire_flux(A._jusqu_a(muet(), echeance=300, horloge=h), horloge=h, echeance=300)


def test_sans_limite_le_flux_va_au_bout():
    h = Horloge()

    def flux():
        yield INIT
        for ev in FLUX:
            h.t += 10_000
            yield ev
        yield FIN
    assert A.lire_flux(flux(), horloge=h).stopped == "end_turn"


def test_l_usage_final_vient_de_chaque_modele_sous_agents_compris():
    res = A.lire_flux([INIT, *FLUX, FIN])
    # Les postes sont la SOMME des modèles — le fil principal seul (`usage`) en oublierait.
    assert res.usage["input_tokens"] == 58 and res.usage["output_tokens"] == 920
    declare = conclusion.resultat_declare(res, "sub:x")
    assert declare["usage_tokens"] == 978
    assert declare["usage_par_modele"]["claude-x"] == {
        "entree": 8, "sortie": 900, "cache_lu": 34400, "cache_ecrit": 18800, "cout_usd": 0.21}
    assert set(declare["usage_par_modele"]) == {"claude-x", "claude-petit"}


def test_sans_detail_par_modele_l_usage_reste_celui_d_avant():
    fin = {k: v for k, v in FIN.items() if k != "modelUsage"}
    res = A.lire_flux([INIT, fin])
    assert res.usage == {"input_tokens": 8, "output_tokens": 900} and res.par_modele is None
    assert "usage_par_modele" not in conclusion.resultat_declare(res, "sub:x")


# ── le worker ────────────────────────────────────────────────────────────────

def test_le_worker_passe_les_bornes_que_le_moteur_sait_tenir():
    spec = AgentSpec(system="s", tools=frozenset(), max_tokens=5000, max_seconds=600)
    notes = []
    assert worker._bornes_du_one_shot(spec, A, lambda e, **c: notes.append(e)) == {
        "max_tokens": 5000, "max_seconds": 600}
    assert notes == []
    # Conversations ne sait pas arrêter en vol sur les jetons : il le DIT.
    assert worker._bornes_du_one_shot(spec, C, lambda e, **c: notes.append(e)) == {
        "max_seconds": 600}
    assert notes == ["borne_non_suivie"]
    assert worker._bornes_du_one_shot(AgentSpec(system="s", tools=frozenset()), A,
                                      lambda e, **c: None) == {}


def test_le_resultat_dit_qui_a_paye():
    assert worker._paye_par(A, None) == "abonnement"
    assert worker._paye_par(C, "sk-org") == "cle_org"
    assert worker._paye_par(C, None) == "cle_plateforme"


def test_sans_echeance_d_agent_une_passe_tardive_garde_ses_900_s(conversation):
    """Sans limite déclarée, rien ne se partage : une passe partie tard a toujours son
    échéance de chemin entière — le comportement d'avant, à l'octet."""
    temps = iter([0, 500, 500, 500])
    C.run_once(instructions="i", inputs="x", tools=["t"], horloge=lambda: next(temps))
    assert conversation == [C._WALL_S]



def test_l_unite_arretee_par_la_ferme_apres_l_echeance_est_une_borne():
    """Un outil muet dépasse l'échéance ; la ferme tue l'unité à SA durée et rend son
    résumé sans `result`. C'est la borne atteinte — pas une panne à rejouer."""
    h = Horloge()

    def flux():
        yield INIT
        yield from FLUX[:2]
        h.t = 700
        yield {"type": "ferme_resume", "code": 143, "connecte": True, "erreur": "unité arrêtée"}
    res = A.lire_flux(flux(), horloge=h, echeance=600)
    assert res.stopped == "max_seconds"


def test_un_flux_sans_resultat_avant_l_echeance_reste_une_panne():
    h = Horloge()

    def flux():
        yield INIT
        h.t = 100
        yield {"type": "ferme_resume", "code": 1, "connecte": True, "erreur": "plantage"}
    with pytest.raises(RuntimeError, match="aucun résultat"):
        A.lire_flux(flux(), horloge=h, echeance=600)


def test_un_run_arrete_a_sa_borne_est_quand_meme_refuse_s_il_payait_par_cle():
    with pytest.raises(RuntimeError, match="apiKeySource"):
        A.lire_flux([dict(INIT, apiKeySource="ANTHROPIC_API_KEY"), *FLUX, FIN], max_tokens=10)


def test_deux_cles_du_meme_modele_s_additionnent():
    fin = dict(FIN, modelUsage={
        "claude-x[1m]": {"inputTokens": 5, "outputTokens": 10, "costUSD": 0.1,
                         "canonicalModel": "claude-x"},
        "claude-x": {"inputTokens": 3, "outputTokens": 4, "costUSD": 0.05,
                     "canonicalModel": "claude-x"}})
    res = A.lire_flux([INIT, fin])
    assert res.par_modele == {"claude-x": {"entree": 8, "sortie": 14, "cache_lu": 0,
                                           "cache_ecrit": 0, "cout_usd": 0.15}}
