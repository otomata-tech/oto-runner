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
    job = {"id": 30, "kind": "start",
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
