"""Le corps envoyé au serveur ne porte QUE des champs que la route déclare.

⚠️ Incident du 04/09/2026, 15 minutes de production à l'arrêt. Un champ
`provider` ajouté au corps du claim a fait répondre à la route REST
`400 : unknown_fields` — à CHAQUE réservation, sur les trois workers, en boucle.
Rien ne l'avait vu :

- la mesure faite avant le déploiement portait sur le modèle pydantic de la
  capacité (`JobsInput`, `extra` ignoré par défaut) — **la mauvaise couche**.
  C'est le pont REST qui tranche, et lui refuse ce qu'il ne déclare pas ;
- les 203 bancs du runner passaient au vert : aucun ne regardait le CORPS
  réellement envoyé. Les doublures de test acceptent `**kw` et notent l'appel,
  pas la requête.

D'où ce banc, qui tient la classe : ajouter un champ à un corps servi est un
changement de CONTRAT, et il se déploie côté serveur AVANT le client.
"""
from __future__ import annotations

import pytest

from oto_runner.backend import Backend


@pytest.fixture
def _client(monkeypatch):
    monkeypatch.setenv("OTO_TOKEN", "jeton-de-test")
    return Backend(base="https://exemple.invalide")


def _corps_de(client, appel, monkeypatch):
    vu = {}

    def _post(chemin, corps, *a, **k):
        vu["chemin"], vu["corps"] = chemin, corps
        return {}

    monkeypatch.setattr(client, "_post", _post)
    appel()
    return vu


def test_le_claim_n_envoie_que_ce_que_la_route_sert(_client, monkeypatch):
    """`provider` ne repartira dans ce corps QUE le jour où la production le
    sert (oto-backend#874 tagué). Le paramètre `depot` reste accepté par la
    méthode : c'est l'ENVOI qui attend, pas l'appelant."""
    vu = _corps_de(_client, lambda: _client.claim(lease_seconds=600, depot="anthropic"),
                   monkeypatch)
    assert vu["chemin"] == "/api/me/runner/jobs"
    assert set(vu["corps"]) == {"op", "lease_seconds"}, (
        "un champ de plus dans ce corps = `400 : unknown_fields` sur toute la "
        "flotte, tant que la route ne le déclare pas")
