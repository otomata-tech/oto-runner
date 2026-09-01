"""Les gardes qui protègent le fichier de la cliente — celles qui ont failli
détruire ce qu'elles existent pour protéger.

⚠️ Elles n'avaient AUCUN test. Ce sont pourtant les plus récentes et les plus
tranchantes : l'une d'elles a signalé une présidente réelle comme fabriquée, et
seule une erreur du serveur a empêché sa suppression. L'autre a rendu « aucune
destruction » là où personne n'avait regardé.

Ce fichier grave les trois règles apprises à ce prix :
  · une réponse absente n'est pas une réponse négative ;
  · un zéro légitime n'est pas une case vide ;
  · une valeur qu'on croit sur parole n'est pas une valeur vérifiée.
"""
import pytest

from oto_runner import worker as W  # noqa: N817


@pytest.fixture(autouse=True)
def _socle_neuf(monkeypatch, tmp_path):
    """Chaque test pose son propre socle — le cache est global."""
    monkeypatch.setattr(W, "_SOCLE_CACHE", {}, raising=False)
    monkeypatch.delenv("OTO_RUNNER_SOCLE", raising=False)


def _pose_socle(monkeypatch, tmp_path, lignes):
    import json
    p = tmp_path / "socle.json"
    p.write_text(json.dumps({"namespace": "essai", "rows": lignes}),
                 encoding="utf-8")
    monkeypatch.setenv("OTO_RUNNER_SOCLE", str(p))
    return p


# ── LA GARDE DES VALEURS ────────────────────────────────────────────────────

def test_sans_socle_la_garde_dit_NON_MESURE_et_pas_zero(monkeypatch):
    """⚠️ LE piège de toute la série : `[]` affirme « rien détruit », `None` dit
    « je n'ai pas pu regarder ». Sans socle, la seconde est la seule vraie."""
    assert W._valeurs_cliente_detruites({"siren": "1", "effectif": "x"}) is None


def test_un_siren_absent_du_socle_nest_pas_une_absence_de_destruction(
        monkeypatch, tmp_path):
    _pose_socle(monkeypatch, tmp_path, [{"siren": "111", "effectif": "50_99"}])
    assert W._valeurs_cliente_detruites({"siren": "999", "effectif": ""}) is None


def test_une_valeur_videe_est_vue(monkeypatch, tmp_path):
    _pose_socle(monkeypatch, tmp_path, [{"siren": "111", "effectif": "50_99"}])
    perdues = W._valeurs_cliente_detruites({"siren": "111", "effectif": ""})
    assert [c for c, _, _ in perdues] == ["effectif"]


def test_une_case_vide_au_depart_peut_etre_remplie(monkeypatch, tmp_path):
    """Remplir n'est pas détruire — sinon la garde interdirait le travail."""
    _pose_socle(monkeypatch, tmp_path, [{"siren": "111", "effectif": ""}])
    assert W._valeurs_cliente_detruites({"siren": "111", "effectif": "50_99"}) == []


def test_un_zero_legitime_nest_pas_une_case_vide(monkeypatch, tmp_path):
    """⚠️ Un effectif exact de 0 est une VALEUR. Le lire comme « vide » a fait
    annoncer une restauration ratée qui n'avait pas eu lieu."""
    _pose_socle(monkeypatch, tmp_path,
                [{"siren": "111", "effectif_exact": 0}])
    perdues = W._valeurs_cliente_detruites({"siren": "111", "effectif_exact": ""})
    assert [c for c, _, _ in perdues] == ["effectif_exact"], \
        "un zéro effacé est une destruction, pas un remplissage"


def test_un_arbitrage_est_CONFRONTE_au_registre_pas_cru_sur_parole(
        monkeypatch, tmp_path):
    """La valeur que le registre rend passe ; toute autre est une destruction —
    même annoncée comme un arbitrage. L'exemption d'avant fermait la forme vue,
    pas la classe : un agent écrivant `1_2` par-dessus `50_99` passait."""
    _pose_socle(monkeypatch, tmp_path, [{"siren": "111", "effectif": "50_99"}])
    fiche = {"siren": "111", "effectif": "20_49"}
    assert W._valeurs_cliente_detruites(fiche, registre="20_49") == [], \
        "la valeur du registre est vérifiable, donc recevable"
    assert [c for c, _, _ in W._valeurs_cliente_detruites(fiche, registre="100_199")], \
        "une valeur que le registre ne porte pas reste une destruction"


# ── LA GARDE DES CONTACTS ───────────────────────────────────────────────────

def test_le_SECOND_dirigeant_du_registre_nest_pas_un_faux_contact():
    """⚠️ LE cas du 01/09 : une présidente réelle signalée comme fabriquée
    parce que la garde ne comparait qu'au PREMIER nom rendu. Seule une erreur
    du serveur a empêché sa suppression."""
    fiche = {"contacts": [{"nom": "Vera Michalski",
                           "nom.comment": "registre — présidente"}]}
    retires, _ = W._contacts_a_retirer(fiche, ["Jean Dupont", "Vera Michalski"])
    assert retires == [], "le second dirigeant est aussi un dirigeant"


def test_un_nom_absent_du_registre_est_toujours_vu():
    """L'autre bord : une garde qui ne signale plus rien n'est pas réparée."""
    fiche = {"contacts": [{"nom": "Personne Inventée",
                           "nom.comment": "registre — dirigeante unique"}]}
    retires, _ = W._contacts_a_retirer(fiche, ["Jean Dupont"])
    assert retires == ["Personne Inventée"]


def test_ce_qui_vient_de_la_cliente_nest_JAMAIS_retire():
    fiche = {"contacts": [{"nom": "Contact Historique",
                           "nom.comment": "fichier-client — colonne contact"}]}
    retires, garde = W._contacts_a_retirer(fiche, [])
    assert retires == [] and len(garde) == 1


def test_une_provenance_absente_nautorise_pas_a_supprimer():
    """Une provenance manquante est une faute en soi — elle ne donne pas le
    droit d'effacer une donnée qu'on n'a pas su tracer."""
    fiche = {"contacts": [{"nom": "Sans Provenance"}]}
    retires, garde = W._contacts_a_retirer(fiche, [])
    assert retires == [] and len(garde) == 1


def test_registre_muet_le_harnais_ne_retire_rien(monkeypatch):
    """⚠️ Une absence de réponse n'est pas une absence de dirigeant. Le
    harnais rend `None`, et l'appelant ne doit rien supprimer sur ce doute."""
    class McpMuet:
        def outil(self, nom, args):
            raise RuntimeError("registre injoignable")

    assert W._tous_les_dirigeants(McpMuet(), "111") is None


def test_sans_siren_on_ne_demande_pas_au_registre():
    class McpQuiCompte:
        appels = 0

        def outil(self, nom, args):
            McpQuiCompte.appels += 1
            return {}

    assert W._tous_les_dirigeants(McpQuiCompte(), "") is None
    assert McpQuiCompte.appels == 0




# ── LES INTERLOCUTEURS ──────────────────────────────────────────────────────

def test_un_contact_present_avant_et_absent_apres_est_vu():
    """⚠️ La garde surveillait cinq colonnes et pas celle qui porte les gens.
    Cinq interlocuteurs perdus le 01/09, dont trois confirmés au registre."""
    avant = {"contacts": [{"nom": "Jacqueline Richard"},
                          {"nom": "Jean-Noël Joly"}]}
    apres = {"contacts": [{"nom": "JEAN-NOEL JOLY"}]}
    perdus = W._contacts_perdus(apres, avant)
    assert [c["nom"] for c in perdus] == ["Jacqueline Richard"]


def test_un_contact_ajoute_par_lagent_ne_compte_pas_comme_perte():
    """L'autre bord : enrichir n'est pas détruire."""
    avant = {"contacts": [{"nom": "Jean Dupont"}]}
    apres = {"contacts": [{"nom": "Jean Dupont"}, {"nom": "Marie Martin"}]}
    assert W._contacts_perdus(apres, avant) == []


def test_sans_reference_la_perte_nest_pas_declaree_nulle():
    """⚠️ `None`, pas `[]` : sans état d'avant, on ne sait pas."""
    assert W._contacts_perdus({"contacts": []}, None) is None


def test_une_fiche_sans_contact_avant_ne_peut_rien_perdre():
    assert W._contacts_perdus({"contacts": [{"nom": "X"}]}, {"contacts": []}) == []


def test_le_nom_se_compare_sans_casse_ni_accents():
    """Les registres écrivent en capitales sans accents ; la fiche non. Une
    comparaison littérale déclarerait perdue une personne présente."""
    avant = {"contacts": [{"nom": "Christèle Meulin"}]}
    apres = {"contacts": [{"nom": "CHRISTELE MEULIN"}]}
    assert W._contacts_perdus(apres, avant) == []


def test_une_reparation_reecrit_le_motif_avec_la_valeur(monkeypatch):
    """⚠️ Le motif de l'agent survivait à la réparation : la fiche portait la
    valeur d'origine et l'annonce qui disait l'avoir remplacée (mesuré le
    01/09). Une fiche qui se contredit est pire qu'une fiche fausse."""
    vus = {}

    class FauxMcp:
        def outil(self, nom, args):
            vus.update(args.get("row") or {})
            return {}

    W._reparer_ligne(FauxMcp(), None, "ns", "42", {"site_web": "https://a.fr"},
                     "run", 226)
    assert vus["site_web"] == "https://a.fr"
    assert "site_web.comment" in vus, "le motif doit suivre la valeur"
    assert "rétablie" in vus["site_web.comment"]


def test_la_liste_des_contacts_na_pas_de_motif_de_colonne(monkeypatch):
    """Chaque contact porte sa propre provenance ; un motif de colonne n'aurait
    aucun sens et créerait un champ que le schéma ne connaît pas."""
    vus = {}

    class FauxMcp:
        def outil(self, nom, args):
            vus.update(args.get("row") or {})
            return {}

    W._reparer_ligne(FauxMcp(), None, "ns", "42", {"contacts": [{"nom": "X"}]},
                     "run", 226)
    assert "contacts.comment" not in vus


def test_le_registre_rend_des_personnes_pas_des_fragments():
    """⚠️ L'extraction par expression régulière rendait « MELISON » et
    « OLIVIER » comme deux dirigeants distincts."""
    class McpStruct:
        def outil(self, nom, args):
            return {"result": [
                {"nom": "MICHALSKI (HOFFMANN)", "prenoms": "VERA MARIA",
                 "type_dirigeant": "personne physique"},
                {"denomination": "FIPAR AUDIT", "type_dirigeant": "personne morale"}]}

    gens = W._tous_les_dirigeants(McpStruct(), "1")
    assert gens == ["MICHALSKI (HOFFMANN) VERA MARIA", "FIPAR AUDIT"]


def test_un_prenom_commun_ne_vaut_pas_une_correspondance():
    """⚠️ « Nathalie Bernard » passait pour « CHAUSSEGROS BERNARD JEAN
    CESARIN » : « Bernard » est le PRÉNOM de quelqu'un d'autre."""
    fiche = {"contacts": [{"nom": "Nathalie Bernard",
                           "nom.comment": "registre — dirigeante"}]}
    faux, _ = W._contacts_a_retirer(fiche, ["CHAUSSEGROS BERNARD JEAN CESARIN"])
    assert faux == ["Nathalie Bernard"]


def test_une_dirigeante_reelle_reste_reconnue_malgre_lordre_et_les_seconds_prenoms():
    """L'autre bord, et c'est celui qui a failli coûter cher : la fiche écrit
    « Vera Michalski », le registre « MICHALSKI (HOFFMANN) VERA MARIA »."""
    fiche = {"contacts": [{"nom": "Vera Michalski",
                           "nom.comment": "registre — présidente"}]}
    faux, _ = W._contacts_a_retirer(
        fiche, ["COSSON MATHIEU FRANCOIS", "MICHALSKI (HOFFMANN) VERA MARIA"])
    assert faux == []


def test_un_registre_de_forme_inattendue_ne_fait_rien_retirer():
    class McpBizarre:
        def outil(self, nom, args):
            return "une chaîne, pas la structure attendue"

    assert W._tous_les_dirigeants(McpBizarre(), "1") is None


def test_une_ecriture_qui_ne_prend_pas_nest_pas_une_reparation():
    """⚠️ La réparation déclarait un succès dès que l'appel ne levait pas. Une
    route peut répondre sans rien écrire — dix retraits ont échoué ainsi le
    01/09, tous silencieux — et c'est ce poste qui fait décider la veille."""
    class McpQuiAccepte:
        def outil(self, nom, args):
            return {}

    class BackendQuiNaPasChange:
        def row(self, ns, lid, org=None):
            return {"site_web": "https://ancienne.fr"}

        def patch_row(self, ns, lid, valeurs, org=None):
            return {}

    chemin = W._reparer_ligne(McpQuiAccepte(), BackendQuiNaPasChange(), "ns", "1",
                              {"site_web": "https://voulue.fr"}, "run", 226)
    assert chemin is None, "une valeur qui n'est pas revenue n'est pas réparée"


def test_une_ecriture_confirmee_est_bien_declaree():
    """L'autre bord : une garde qui ne confirme jamais ne sert à rien."""
    class McpQuiAccepte:
        def outil(self, nom, args):
            return {}

    class BackendQuiPorte:
        def row(self, ns, lid, org=None):
            return {"site_web": "https://voulue.fr"}

    chemin = W._reparer_ligne(McpQuiAccepte(), BackendQuiPorte(), "ns", "1",
                              {"site_web": "https://voulue.fr"}, "run", 226)
    assert chemin == "run"
