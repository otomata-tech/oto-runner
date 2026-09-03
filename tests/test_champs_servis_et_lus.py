"""Tout champ qu'une campagne DÉCLARE est lu, ou son absence est JUSTIFIÉE.

⚠️ Ce contrôle existe parce que trois champs avaient divergé sans que rien ne le
dise : `provider`, `model` et `max_consecutive_failures` étaient servis par le
serveur, validés à la déclaration — et **ignorés** par le runner qui charge la
campagne.

Les deux premiers sont des étiquettes : les ignorer fausse une attribution. **Le
troisième était une garde** : quelqu'un déclare « arrête après N échecs
d'affilée », et le runner appliquait sa constante. *Une borne déclarée qui n'est
pas appliquée ne se découvre que le jour où on comptait dessus* — elle ne fausse
pas un relevé, elle laisse tourner une campagne qu'on croyait bornée.

**Une correction ciblée aurait raté le troisième**, qui est le seul à protéger
quelque chose. D'où ce contrôle : il vise l'axe où le défaut VIT — *un champ
déclaré que personne ne lit* — pas les trois cas sous lesquels il s'est présenté.
"""
from __future__ import annotations

import re

from oto_runner import fleet


# Ce que la campagne porte côté serveur, hors état et métadonnées : la liste des
# colonnes servies par `runner.fleets` (oto-backend, `db/runner_fleets._COLS`).
# ⚠️ Recopiée, donc datée — mais un écart se voit ici, à la lecture, au lieu de
# vivre en production. Un contrôle cross-dépôt vaut mieux qu'aucun contrôle.
_SERVIS = {
    "label", "procedure", "project_id", "tools", "input", "max_steps",
    "namespace", "row_filter", "provider", "model", "workers", "max_rows",
    "max_tokens", "max_consecutive_failures", "max_tokens_per_row",
}

# Ce qu'on ne lit PAS, et POURQUOI. ⚠️ Une exclusion sans raison est une dette
# déguisée en décision : la raison est obligatoire, elle se relit dans six mois.
_NON_LUS = {
    "provider": (
        "Le worker est un pool HOMOGÈNE : son fournisseur vient de SON "
        "environnement, décidé au démarrage du processus. Un fil commencé chez "
        "un fournisseur ne se continue pas chez un autre — les formats de "
        "messages diffèrent. Le rendre variable par campagne demanderait que le "
        "fil porte son fournisseur."),
    "model": (
        "Même raison : le modèle est épinglé par worker, et le cache de prompt "
        "en dépend. Le faire varier par campagne invaliderait le cache d'un "
        "worker partagé entre plusieurs campagnes."),
}


def _lus_par_le_runner() -> set[str]:
    """Les clés que `spec_depuis_flotte` lit RÉELLEMENT dans la campagne servie."""
    s = open(fleet.__file__).read()
    i = s.index("def spec_depuis_flotte")
    j = s.index("\ndef ", i + 10)
    corps = s[i:j]
    return {a or b for a, b in
            re.findall(r'f\.get\("([a-z_]+)"\)|f\["([a-z_]+)"\]', corps)}


def test_tout_champ_servi_est_lu_ou_justifie():
    """⚠️ LE contrôle. Un champ ajouté demain côté serveur et oublié côté runner
    fait rougir ici — au lieu d'attendre qu'un utilisateur s'aperçoive que sa
    déclaration n'a aucun effet."""
    manquants = _SERVIS - _lus_par_le_runner() - set(_NON_LUS)
    assert not manquants, (
        f"champs SERVIS que le runner ignore, sans justification : {sorted(manquants)}. "
        "Soit les lire, soit les inscrire dans `_NON_LUS` AVEC leur raison — "
        "une déclaration sans effet est pire qu'un champ absent, parce qu'on s'y fie.")


def test_la_borne_d_echecs_est_LUE_desormais():
    """Le cas qui a mordu, gardé nommément : c'était le seul des trois à protéger
    quelque chose."""
    assert "max_consecutive_failures" in _lus_par_le_runner()


def test_chaque_exclusion_porte_sa_raison():
    """Une exclusion sans raison se relit comme un oubli, et se « corrige » par
    quelqu'un qui rouvre le débat sans les éléments."""
    for champ, raison in _NON_LUS.items():
        assert len(raison) > 80, f"`{champ}` est exclu sans raison substantielle"


def test_aucune_exclusion_ne_survit_a_sa_lecture():
    """⚠️ Le contrôle symétrique : un champ qu'on FINIT par lire doit sortir de la
    liste des exclus. Sans ça, la liste devient un cimetière qu'on ne relit plus,
    et elle couvrirait un futur champ mort du même nom."""
    zombies = set(_NON_LUS) & _lus_par_le_runner()
    assert not zombies, (
        f"ces champs sont LUS mais encore déclarés non lus : {sorted(zombies)}")


def test_la_borne_declaree_gagne_sur_le_defaut():
    """Et elle agit : ce n'est pas qu'une lecture, c'est la valeur appliquée."""
    spec = fleet.spec_depuis_flotte(
        {"id": 1, "procedure": "p", "namespace": "n", "tools": ["data_write"],
         "input": "fais ceci", "max_consecutive_failures": 7})
    assert spec.max_consecutive_failures == 7
    corps = open(fleet.__file__).read()
    assert "plafond_echecs = spec.max_consecutive_failures or _MAX_FAILED_CONSECUTIFS" in corps
    assert "failed_consecutifs >= plafond_echecs" in corps
