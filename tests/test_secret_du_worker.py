"""Le worker ne possède qu'un secret de machine — et le fil se parle avec le jeton délégué.

Mesuré le 09/09/2026 : les trois agents tournaient sous le jeton personnel d'un
compte admin de quatorze organisations, et sondaient l'org ACTIVE de ce compte.
Ces bancs gardent les deux gestes qui l'empêchent de se reproduire : le refus
d'un jeton de compte au démarrage, et le jeton délégué sur le fil.
"""
from __future__ import annotations

import pytest

import oto_runner.backend as B
import oto_runner.worker as worker


def test_sans_secret_le_worker_ne_demarre_pas_et_dit_quoi_faire(monkeypatch):
    monkeypatch.delenv("OTO_WORKER_SECRET", raising=False)
    with pytest.raises(SystemExit) as e:
        worker._secret_du_worker()
    assert "OTO_WORKER_SECRET" in str(e.value) and "oto_admin_runner_worker" in str(e.value)


def test_un_jeton_de_COMPTE_est_refuse_nommement(monkeypatch):
    """Le cas qui a réellement tourné cinq heures en production."""
    monkeypatch.setenv("OTO_WORKER_SECRET", "oto_un_jeton_de_compte")
    with pytest.raises(SystemExit) as e:
        worker._secret_du_worker()
    assert "org active" in str(e.value), "le refus dit CE que ferait un jeton de compte"


def test_un_secret_de_worker_passe(monkeypatch):
    monkeypatch.setenv("OTO_WORKER_SECRET", "otow_abc")
    assert worker._secret_du_worker() == "otow_abc"


class _Reponse:
    status_code = 200
    content = b"{}"

    def json(self):
        return {}


def _entetes_vus(monkeypatch):
    vus = []

    def post(url, **kw):
        vus.append(kw["headers"])
        return _Reponse()

    monkeypatch.setattr(B, "post_with_deadline", post)
    return vus


def test_le_fil_est_parle_avec_le_jeton_DELEGUE_pas_le_secret(monkeypatch):
    """Le fil du run vit dans l'org du déclarant : le secret du worker n'y a
    aucun droit. Sans le jeton délégué, la lecture du fil à la reprise et
    chaque appose partiraient en refus — après la réservation, en plein vol."""
    vus = _entetes_vus(monkeypatch)
    b = B.Backend(base="https://x.invalide", token="otow_secret")
    b.thread_read("run-1", include_raw=True, token="oto_delegue")
    b.thread_append("run-1", "user", {"t": 1}, token="oto_delegue")
    assert [h["Authorization"] for h in vus] == ["Bearer oto_delegue"] * 2


def test_les_verbes_du_bail_partent_avec_le_secret(monkeypatch):
    vus = _entetes_vus(monkeypatch)
    b = B.Backend(base="https://x.invalide", token="otow_secret")
    b.claim(lease_seconds=60)
    assert vus[0]["Authorization"] == "Bearer otow_secret"
    assert "X-Oto-Org" not in vus[0], "un worker ne nomme AUCUNE organisation"
