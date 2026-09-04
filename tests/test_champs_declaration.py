"""Les champs qu'une déclaration peut porter — dérivés, jamais recopiés.

La liste était tenue à la main et avait pris **deux champs de retard**
(`critical_tools`, `max_tokens_per_row`). Conséquence vécue le 02/09 : le runner
avertissait « champs ignorés : max_tokens_per_row » alors qu'il LISAIT ce champ
et l'appliquait. Un opérateur a failli retirer de sa déclaration la ligne qui
bornait sa dépense, parce que l'outil lui disait qu'elle ne servait à rien.

⚠️ **Un avertissement faux est pire que pas d'avertissement** : il ne laisse pas
l'utilisateur dans l'ignorance, il le pousse au geste inverse du bon.
"""
from __future__ import annotations

from dataclasses import fields

from oto_runner.fleet import _CHAMPS, _NON_DECLARABLES, FleetSpec


def test_tout_champ_de_la_spec_est_reconnu_sauf_les_non_declarables():
    """Le garde-fou MÉCANIQUE : un champ ajouté demain à `FleetSpec` est reconnu
    sans que personne y pense. C'est ce qui ferme la classe — la liste manuelle,
    elle, prenait du retard en silence à chaque ajout."""
    attendus = {f.name for f in fields(FleetSpec)} - _NON_DECLARABLES
    assert _CHAMPS == attendus


def test_les_deux_champs_oublies_sont_reconnus():
    """Le cas qui a mordu, gardé nommément."""
    assert {"max_tokens_per_row", "critical_tools"} <= _CHAMPS


def test_ce_qui_ne_vient_pas_d_une_declaration_reste_exclu():
    """`name` vient du nom du fichier, `source` de son chemin, `fleet_id` de la
    base. Les accepter en déclaration laisserait un opérateur croire qu'il peut
    choisir l'identifiant que la base attribue."""
    assert not ({"name", "source", "fleet_id"} & _CHAMPS)


def test_l_avertissement_dit_ce_qui_EST_reconnu(tmp_path, caplog):
    """Sans le voisinage, une faute de frappe se lit comme une fonctionnalité
    absente — et on cherche dans le code au lieu du fichier."""
    import logging

    from oto_runner.fleet import load_spec

    d = tmp_path / "campagne.yaml"
    d.write_text("procedure: p\nnamespace: n\ninput: fais ceci\n"
                 "max_token_per_row: 40000\n")
    with caplog.at_level(logging.WARNING):
        load_spec(str(d))
    message = "\n".join(r.getMessage() for r in caplog.records)
    assert "max_token_per_row" in message          # la faute de frappe est nommée
    assert "max_tokens_per_row" in message         # ... à côté du nom JUSTE
    # ⚠️ Et plus un mot sur le modèle : l'ancien message accolait « le modèle
    # vient de l'env du WORKER » à un avertissement qui n'a rien à voir, ce qui
    # envoyait chercher une explication du mauvais côté.
    assert "OTO_RUNNER_MODEL" not in message


def test_une_declaration_valide_n_avertit_de_rien(tmp_path, caplog):
    """Le contrôle qui manquait : la déclaration COMPLÈTE, telle qu'un opérateur
    l'écrit, ne doit produire aucun avertissement."""
    import logging

    from oto_runner.fleet import load_spec

    d = tmp_path / "campagne.yaml"
    d.write_text("procedure: p\nnamespace: n\ninput: fais ceci\n"
                 "max_tokens_per_row: 40000\n"
                 "critical_tools: [data_write]\nconcurrency: 3\nvolume: 30\n")
    with caplog.at_level(logging.WARNING):
        spec = load_spec(str(d))
    assert not caplog.records
    assert spec.max_tokens_per_row == 40000
    assert spec.critical_tools == ("data_write",)
