"""Ce que TOUS les bancs partagent.

Le worker écrit un journal par travail (`oto_runner/journal.py`), par défaut sous
`passages/` dans le répertoire courant — c'est-à-dire, quand la suite tourne, à
la racine du dépôt. Sans ce garde-fou, chaque test qui joue `worker.main()` y
laissait des `passages/hors-flotte/<id>.jsonl` : une pollution du dépôt qui
ressemblait à un vrai relevé de campagne. Le journal va donc dans le répertoire
temporaire du test, pour tous les tests, sans que chacun y pense.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _journaux_dans_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("OTO_RUNNER_PASSAGES_DIR", str(tmp_path / "passages"))
