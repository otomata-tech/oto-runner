"""Le motif d'un échec nommé porte la fin que le FOURNISSEUR a déclarée.

Les deux transports nomment cette fin différemment — Chat Completions pose
`finish_reason`, Anthropic pose `stop_reason` — et `echec_nomme` ne lisait que
le premier. Conséquence mesurée sur un agent événementiel de production : neuf
tentatives, les 18 et 22/09/2026, toutes conclues `fin_anormale (non déclarée)`
alors que la cause avait été captée par le transport et posée dans le même
dictionnaire, sous l'autre nom.

Ce que ces bancs figent :
- la voie Anthropic rend SA fin (`stop_reason`) ;
- la voie Chat Completions rend la sienne (`finish_reason`) — non régressée ;
- une fin réellement absente se dit « non déclarée », sans la deviner ;
- l'autre échec nommé (`appel_mal_encode`) n'est pas touché ;
- une boucle qui s'est arrêtée normalement ne nomme aucun échec.
"""
from __future__ import annotations

import types

import pytest

from oto_runner.conclusion import echec_nomme


def _res(stopped, defaut=None):
    """Ce que `echec_nomme` lit d'un `AgentResult` : deux champs, pas un de plus."""
    return types.SimpleNamespace(stopped=stopped, defaut=defaut)


def test_la_voie_anthropic_nomme_sa_fin():
    """`stop_reason` est le nom Anthropic — c'est CELUI de la voie qui casse."""
    res = _res("fin_anormale", {"forme": "fin_anormale", "stop_reason": "max_tokens"})
    assert echec_nomme(res) == "fin_anormale (max_tokens)"


@pytest.mark.parametrize("fin", ["max_tokens", "pause_turn",
                                 "model_context_window_exceeded", "stop_sequence"])
def test_toute_fin_anthropic_de_l_enumeration_remonte(fin):
    """L'énumération Anthropic entière, pas seulement la fin qu'on soupçonne.

    ⚠️ Le banc ne parie sur AUCUNE de ces fins : savoir laquelle a tué les trois
    travaux observés demande le journal du worker. Ce qui se fige ici, c'est que
    la réponse arrivera — quelle qu'elle soit."""
    assert echec_nomme(_res("fin_anormale", {"stop_reason": fin})) == f"fin_anormale ({fin})"


def test_la_voie_chat_completions_n_est_pas_regressee():
    """`finish_reason` marchait déjà : la correction ne doit pas le reprendre."""
    res = _res("fin_anormale", {"forme": "fin_anormale", "finish_reason": "length"})
    assert echec_nomme(res) == "fin_anormale (length)"


def test_une_fin_vraiment_absente_se_dit_sans_etre_devinee():
    """Le manque s'avoue. Un motif inventé ferait chercher une cause qui n'existe pas."""
    assert echec_nomme(_res("fin_anormale", {"forme": "fin_anormale"})) \
        == "fin_anormale (non déclarée)"
    assert echec_nomme(_res("fin_anormale", None)) == "fin_anormale (non déclarée)"


def test_une_fin_vide_ne_compte_pas_pour_une_fin():
    """`None` et `""` sont des non-réponses du transport, pas des fins déclarées."""
    assert echec_nomme(_res("fin_anormale", {"stop_reason": None})) \
        == "fin_anormale (non déclarée)"
    assert echec_nomme(_res("fin_anormale", {"finish_reason": ""})) \
        == "fin_anormale (non déclarée)"


def test_les_deux_noms_presents_la_voie_chat_completions_gagne():
    """Un ordre FIGÉ — pas pour départager les transports (aucun ne pose les deux),
    mais pour qu'un dictionnaire hybride rende toujours le même motif."""
    res = _res("fin_anormale", {"finish_reason": "length", "stop_reason": "max_tokens"})
    assert echec_nomme(res) == "fin_anormale (length)"


def test_l_appel_mal_encode_n_est_pas_touche():
    """L'autre échec nommé lit `outil` et ne partage rien avec la fin du tour."""
    assert echec_nomme(_res("appel_mal_encode", {"outil": "data_write"})) \
        == "appel_outil_mal_encode (data_write)"
    assert echec_nomme(_res("appel_mal_encode", {})) \
        == "appel_outil_mal_encode (outil inconnu)"


@pytest.mark.parametrize("arret", ["end_turn", "max_tokens", None])
def test_un_arret_normal_ne_nomme_aucun_echec(arret):
    """Un travail qui a conclu n'est pas un échec — même arrêté sur une borne."""
    assert echec_nomme(_res(arret, {"stop_reason": "max_tokens"})) is None
