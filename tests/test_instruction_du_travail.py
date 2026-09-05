"""Le worker EXÉCUTE une instruction, il n'en compose aucune.

Trois textes de repli vivaient dans le runner — un dans l'ordonnanceur de
flotte, deux dans le worker (« Exécute la procédure. »). Ils inventaient le
travail à la place de qui l'avait déclaré, depuis le seul étage qui ne connaît
pas le métier : une instruction inventée ne se relit ni ne se corrige depuis le
produit, elle se découvre dans le résultat.

C'est la plateforme qui compose, à la déclaration (oto-backend, capacité
`_instruction`). Un travail qui arrive muet est donc l'anomalie du chemin qui
l'a enfilé — et il le DIT.
"""
import oto_runner.worker as worker
from tests.test_cle_de_modele_du_travail import _BackendMuet, _McpMuet  # noqa: F401

def test_un_travail_sans_instruction_est_refuse_au_lieu_d_etre_invente(monkeypatch):
    """Le worker exécute une instruction, il n'en compose pas. Un travail muet
    est une anomalie du chemin qui l'a enfilé : il le dit, et nomme l'endroit où
    regarder — au lieu de tourner sur un ordre fabriqué dont personne ne saura
    qu'il a remplacé le vrai."""
    import pytest
    monkeypatch.setattr(worker, "McpSession", _McpMuet)
    job = {"id": 30, "kind": "start", "delegated_token": "oto_delegue",
           "payload": {"project_id": 3, "org_id": 42, "procedure": "veille"}}

    class _P:
        __name__ = "agent_llm"
        ONE_SHOT = False

        @staticmethod
        def model():
            return "m"

    with pytest.raises(worker.SansInstruction) as e:
        worker._traiter(_BackendMuet(), job, _P)
    assert "sans instruction" in str(e.value) and "30" in str(e.value)


def test_aucun_texte_de_repli_ne_subsiste_dans_le_worker():
    """La classe, pas le cas : trois replis vivaient ici, à trois endroits
    différents. Le quatrième s'écrirait aussi naturellement que les autres."""
    import ast
    import pathlib
    arbre = ast.parse(pathlib.Path(worker.__file__).read_text())
    docs = {id(n) for p_ in ast.walk(arbre)
            if isinstance(p_, (ast.Module, ast.ClassDef, ast.FunctionDef))
            and (d := ast.get_docstring(p_, clean=False)) is not None
            for n in [p_.body[0].value]}
    replis = [n.value for n in ast.walk(arbre)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)
              and id(n) not in docs and "Exécute la procédure" in n.value]
    assert not replis, f"le worker compose encore : {replis}"


# ── ce que le worker ne juge plus, parce qu'il ne le connaît plus ────────────
#
# ⚠️ Deux bancs vivaient ici, retirés le 05/09/2026 avec le concept qu'ils
# gardaient : « une procédure nommée mais vide arrête l'agent » et « un travail
# sans procédure nommée reste possible ».
#
# Ils n'étaient pas faux — ils supposaient seulement que tout travail applique une
# PROCÉDURE. Or le worker héberge une boucle agentique : la même instruction,
# collée dans n'importe quel client, ferait le même travail sans qu'aucune
# procédure existe. Un agent peut trier une boîte, faire une veille, relancer
# quelqu'un. Le worker ne charge donc plus aucun objet et n'en juge aucun ; si
# l'instruction suppose d'en lire un, c'est l'AGENT qui le lit, avec ses outils.
#
# Ce que le worker refuse encore est ce qui l'empêche, LUI, de travailler : une
# instruction absente (il n'en compose pas) et un porteur absent (il n'a pas
# d'identité à prêter). Les bancs de ces deux-là sont ci-dessus.
