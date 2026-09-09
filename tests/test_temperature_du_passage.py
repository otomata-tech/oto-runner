"""La température DÉCLARÉE par le passage, jusqu'au corps de la requête.

⚠️ Ce fichier existe parce que le lot a vécu une journée à moitié posé : la
campagne portait une colonne `temperature`, le backend la servait dans le
payload du travail (oto-backend v1.244.0) — et le runner ne la lisait pas. Un
réglage offert qui n'arrive nulle part est plus coûteux qu'un réglage absent :
on l'ajuste en croyant mesurer quelque chose. Les bancs de bout en bout sont
donc ici, du job au corps HTTP, et non de chaque côté d'une couture.

L'ordre que ces bancs figent : **le passage, puis l'hôte, puis le fournisseur**
— du plus proche du métier au plus lointain.
"""
from __future__ import annotations

import json

import pytest

from oto_runner import agent_llm as A
from oto_runner import agent_llm_openai as P
from oto_runner import worker


class _Resp:
    status_code = 200
    text = "{}"

    def json(self):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


@pytest.fixture
def corps_poste(monkeypatch):
    """Ce que le fournisseur reçoit VRAIMENT — pas ce que la spec dit vouloir."""
    vu = {}

    def _post(url, **kw):
        vu.update(kw.get("json") or {})
        return _Resp()

    monkeypatch.setattr(P.requests, "post", _post)
    monkeypatch.delenv("OTO_RUNNER_TEMPERATURE", raising=False)
    return vu


def _job(**payload):
    base = {"tools": ["data_rows"], "max_steps": 5}
    base.update(payload)
    return {"id": 1, "payload": base}


# ── Du travail à la spec ─────────────────────────────────────────────────────

def test_la_temperature_du_travail_arrive_dans_la_spec():
    assert worker._spec_du_job(_job(temperature=0.7)).temperature == 0.7


def test_ZERO_survit_au_trajet():
    """Le piège de ce lot, et le seul qui se serait vu en production : `0` est LA
    valeur qu'on déclare pour rendre deux passages comparables (mesure du
    06/09/2026 — sans elle, la même procédure sur le même banc de trois lignes va
    de 11 à 18 sur 18). Un test de véracité l'aurait jetée comme une absence, et
    la campagne aurait tourné au défaut du fournisseur en affichant `0`."""
    assert worker._spec_du_job(_job(temperature=0)).temperature == 0.0


def test_sans_declaration_la_spec_ne_porte_rien():
    assert worker._spec_du_job(_job()).temperature is None


# ── De la spec au corps de la requête ────────────────────────────────────────

def test_la_temperature_du_passage_atteint_le_fournisseur(corps_poste):
    P.complete(system="s", messages=[], tools=[], api_key="k", temperature=0.2)
    assert corps_poste["temperature"] == 0.2


def test_ZERO_atteint_le_fournisseur(corps_poste):
    """Le pendant du banc du haut, de l'autre côté de la couture : `0` doit être
    ENVOYÉ, pas retenu par un `if retenue:`."""
    P.complete(system="s", messages=[], tools=[], api_key="k", temperature=0)
    assert corps_poste["temperature"] == 0


def test_sans_rien_declare_le_corps_ne_porte_pas_le_champ(corps_poste):
    """Ne RIEN envoyer et envoyer le défaut du fournisseur ne sont pas la même
    chose : le second fige une valeur qu'on n'a pas choisie et qui bougera."""
    P.complete(system="s", messages=[], tools=[], api_key="k")
    assert "temperature" not in corps_poste


def test_le_passage_PRIME_sur_le_defaut_de_l_hote(corps_poste, monkeypatch):
    monkeypatch.setenv("OTO_RUNNER_TEMPERATURE", "0.9")
    P.complete(system="s", messages=[], tools=[], api_key="k", temperature=0.1)
    assert corps_poste["temperature"] == 0.1, (
        "l'hôte est un dernier recours, jamais un arbitre : deux campagnes "
        "servies par le même worker n'en veulent pas la même")


def test_l_hote_sert_QUAND_le_passage_ne_dit_rien(corps_poste, monkeypatch):
    monkeypatch.setenv("OTO_RUNNER_TEMPERATURE", "0.9")
    P.complete(system="s", messages=[], tools=[], api_key="k")
    assert corps_poste["temperature"] == 0.9


# ── Le provider qui n'en veut pas ────────────────────────────────────────────

def test_anthropic_REFUSE_franchement_au_lieu_d_ignorer():
    """Ce provider règle la profondeur par `output_config.effort`. Ignorer la
    température en silence laisserait une campagne l'ajuster pendant des heures
    sans effet ; la poser quand même rendrait un 400 opaque, que celui qui a
    déclaré la campagne irait chercher dans sa procédure."""
    with pytest.raises(ValueError) as e:
        A.complete(system="s", messages=[], tools=[], temperature=0.3)
    assert "effort" in str(e.value), "le refus DIT par quoi se règle la profondeur"
    assert "openai" in str(e.value), "et il dit par où passer si on y tient"


def test_anthropic_sans_temperature_ne_refuse_rien(monkeypatch):
    """La garde du haut ne doit pas se déclencher sur le cas ordinaire — sinon
    elle mettrait hors service tous les passages servis par ce provider."""
    monkeypatch.setattr(A, "_sdk", lambda: None)
    from oto_runner.llm_types import LlmUnavailable
    with pytest.raises(LlmUnavailable):
        A.complete(system="s", messages=[], tools=[])


# ── La couture du milieu : de la spec à l'appel du provider ──────────────────
# ⚠️ Elle n'était couverte par RIEN. La doublure de provider des autres bancs
# est un `complete(self, **kwargs)` : elle avale n'importe quel jeu d'arguments
# et serait restée verte si la boucle avait oublié de transmettre la valeur.
# C'est le cas d'école du banc qui passe sans rien prouver — les deux bancs
# ci-dessous regardent ce que la boucle PASSE, pas ce qu'elle rend.

def _boucle_avec(spec):
    """Rend les kwargs que la boucle a réellement servis au provider."""
    from oto_runner import agent_runtime
    from tests.test_agent_runtime import FauxProvider, FauxTransport, _turn

    vus: dict = {}

    class Mouchard(FauxProvider):
        """Le protocole entier de la doublure maison, et UN mouchard sur les
        arguments — c'est le seul point qu'on regarde ici."""
        def complete(self, **kw):
            vus.update(kw)
            return super().complete(**kw)

    agent_runtime.run(spec, FauxTransport(), Mouchard([_turn(text="fini")]),
                      prompt="go")
    return vus


def test_la_boucle_TRANSMET_la_temperature_de_la_spec():
    from oto_runner.agent_runtime import AgentSpec
    vus = _boucle_avec(AgentSpec(system="s", tools=frozenset(), max_steps=2,
                                 temperature=0.4))
    assert vus["temperature"] == 0.4


def test_la_boucle_transmet_ZERO_et_non_None():
    from oto_runner.agent_runtime import AgentSpec
    vus = _boucle_avec(AgentSpec(system="s", tools=frozenset(), max_steps=2,
                                 temperature=0))
    assert vus["temperature"] == 0, (
        "un `or None` quelque part sur le trajet rendrait la campagne muette "
        "en affichant `0` — c'est la valeur la plus utile du réglage")


def test_la_temperature_DECLAREE_descend_dans_chaque_travail(tmp_path):
    """Décision d'Alexis du 09/09/2026 : « je ne veux pas poser ce paramètre en
    env, il doit être paramétrable ». Le YAML la déclare, `payload` la porte,
    et `0` reste `0` — une valeur, pas une absence."""
    from oto_runner.declaration import load_spec, payload
    y = tmp_path / "f.yaml"
    y.write_text("procedure: p\nnamespace: t\ntools: [oto_procedure]\ninput: x\ntemperature: 0\n")
    spec = load_spec(str(y))
    assert spec.temperature == 0.0
    assert payload(spec)["temperature"] == 0.0
    y.write_text("procedure: p\nnamespace: t\ntools: [oto_procedure]\ninput: x\n")
    assert payload(load_spec(str(y)))["temperature"] is None, "absente = l'hôte décide"
