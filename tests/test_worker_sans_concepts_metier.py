"""Le worker héberge une boucle agentique — il ne connaît aucun objet du produit.

⚠️ Tranché par Alexis le 05/09/2026 : « je veux juste un runner agentique ». La
même instruction, collée dans n'importe quel client, doit produire le même
travail. Un agent peut trier une boîte, faire une veille, relancer quelqu'un —
`procedure` est un concept qu'il n'a pas à connaître, et il pourrait tout aussi
bien ne jamais appeler oto.

Le worker chargeait pourtant l'objet nommé par le travail et l'injectait dans le
prompt système sous « ## Procédure », posait son nom en `doctrine` du run, et
refusait de démarrer s'il le trouvait vide. Trois façons de savoir ce que l'agent
allait faire.

⚠️ Ces bancs existent parce que la mutation ne rougissait NULLE PART : réinjecter
du métier dans le prompt laissait la suite verte. Une propriété que rien ne garde
n'est pas une propriété, c'est une intention.
"""
from __future__ import annotations

import pathlib

import oto_runner.worker as worker


def _job(**payload):
    base = {"input": "Fais ce qui est écrit.", "tools": ["oto_kb"]}
    base.update(payload)
    return {"id": 42, "kind": "start", "delegated_token": "oto_x", "payload": base}


def test_le_prompt_systeme_ne_porte_QUE_le_cadre_du_runtime():
    """Égalité stricte, pas un `in` : c'est ce qui attrape une réinjection."""
    spec = worker._spec_du_job(_job(procedure="enrichissement", label="x"))
    assert spec.system == worker._SYSTEM_FRAME


def test_le_prompt_ne_change_pas_selon_ce_que_le_travail_transporte():
    """Deux travaux au métier différent produisent le MÊME cadre : le worker ne
    sait pas ce qu'ils font, et le prompt le prouve."""
    a = worker._spec_du_job(_job(procedure="veille-hebdo"))
    b = worker._spec_du_job(_job(procedure="tri-inbox", namespace="clients"))
    assert a.system == b.system == worker._SYSTEM_FRAME


def test_le_worker_ne_nomme_AUCUN_objet_du_produit():
    """La classe : ni `procedure`, ni `doctrine` — ni dans le code, ni dans un
    appel d'outil. Les commentaires qui expliquent le retrait ne comptent pas,
    sinon le contrôle interdirait de dire ce qu'on a corrigé."""
    src = pathlib.Path(worker.__file__).read_text()
    corps = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    for mot in ("oto_procedure", "doctrine", '"procedure"', "'procedure'"):
        assert mot not in corps, f"le worker connaît encore `{mot}`"


def test_l_instruction_du_travail_est_le_SEUL_apport_de_l_appelant():
    """Ce qui reste, et c'est tout ce qui doit rester : le texte fourni. Le worker
    l'injecte tel quel — il ne le complète pas, ne le préfixe pas, ne le juge pas."""
    assert worker._instruction_du(_job(input="Trie ma boîte.")) == "Trie ma boîte."
