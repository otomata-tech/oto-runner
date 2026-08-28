"""L'estampille : le modèle et la version de procédure posés sur chaque fiche écrite.

Ce que ces tests protègent, et qui a coûté cher : la pose était un script lancé à la
main en fin de campagne. Il a été passé sur les tables d'essai et oublié sur la
production — 504 fiches livrées dont aucune ne dit ce qui l'a produite, et donc
impossibles à trier par génération. Un geste manuel en fin de flotte sera oublié.

Le cas le plus important est le REFUS de poser : une colonne non déclarée dans un
tableau strict fait refuser l'écriture ENTIÈRE. Poser l'estampille à tout prix
perdrait la fiche pour un champ d'observabilité.
"""
from oto_runner import worker
from oto_runner.mcp import McpSession


class _SessionNue(McpSession):
    """Une session sans transport : on éprouve la pose, pas le réseau."""

    def __init__(self, estampille=None, schema=None):
        self.url = "https://exemple.invalide/mcp"
        self.token = "x"
        self.project = self.org = self.run_id = None
        self.session = None
        self._n = 0
        self._props = {}
        self.estampille = estampille or {}
        self._schema = schema
        self.appels = []

    def outil(self, name, arguments=None):
        self.appels.append((name, arguments))
        return self._schema if name == "data_get_schema" else {}


ESTAMPILLE = {"modele": "un-modele-2512", "version_procedure": "une-procedure v101"}


def test_pose_sur_une_fiche_et_sur_un_lot():
    """Les deux formes d'écriture sont couvertes : n'en traiter qu'une laisserait
    la moitié des campagnes sans estampille."""
    s = _SessionNue(ESTAMPILLE)
    args = {"namespace": "t", "row": {"siren": "1"}}
    s._appliquer_estampille("data_write", args)
    assert args["row"]["modele"] == "un-modele-2512"
    assert args["row"]["version_procedure"] == "une-procedure v101"

    lot = {"namespace": "t", "rows": [{"siren": "1"}, {"siren": "2"}]}
    s._appliquer_estampille("data_write", lot)
    assert all(f["modele"] == "un-modele-2512" for f in lot["rows"])


def test_le_nom_d_outil_peut_etre_prefixe_par_le_connecteur():
    """Le connecteur MCP préfixe les noms (`oto-11aout_data_write`) :
    l'appartenance se teste par SUFFIXE, jamais par égalité."""
    s = _SessionNue(ESTAMPILLE)
    args = {"row": {"siren": "1"}}
    s._appliquer_estampille("un-connecteur_data_write", args)
    assert args["row"]["modele"] == "un-modele-2512"


def test_ne_touche_pas_les_autres_outils():
    s = _SessionNue(ESTAMPILLE)
    args = {"namespace": "t", "row": {"siren": "1"}}
    s._appliquer_estampille("data_claim_next", args)
    assert "modele" not in args["row"]


def test_n_ecrase_jamais_ce_que_l_agent_a_ecrit():
    s = _SessionNue(ESTAMPILLE)
    args = {"row": {"siren": "1", "modele": "posé par l'agent"}}
    s._appliquer_estampille("data_write", args)
    assert args["row"]["modele"] == "posé par l'agent"


def test_sans_estampille_l_appel_passe_inchange():
    s = _SessionNue({})
    args = {"row": {"siren": "1"}}
    s._appliquer_estampille("data_write", args)
    assert args == {"row": {"siren": "1"}}


class _Provider:
    def __init__(self, nom="alias-latest", resolu=None):
        self._nom, self._resolu = nom, resolu

    def model(self):
        return self._nom

    def modele_resolu(self, nom):
        return self._resolu


_SCHEMA_COMPLET = {"schema": {"fields": [{"key": "siren"}, {"key": "modele"},
                                         {"key": "version_procedure"}]}}
_PAYLOAD = {"namespace": "un-tableau", "procedure": "une-procedure"}


def test_etablit_le_modele_resolu_quand_le_provider_sait_le_faire():
    s = _SessionNue(schema=_SCHEMA_COMPLET)
    e = worker._estampille(s, _PAYLOAD, _Provider(resolu="un-modele-2512"),
                           {"version": 101})
    assert e == {"modele": "un-modele-2512", "version_procedure": "une-procedure v101"}


def test_retombe_sur_l_alias_quand_la_resolution_echoue():
    """Une panne de catalogue ne doit pas priver la fiche de son estampille."""
    s = _SessionNue(schema=_SCHEMA_COMPLET)
    e = worker._estampille(s, _PAYLOAD, _Provider(nom="alias", resolu=None),
                           {"version": 101})
    assert e["modele"] == "alias"


def test_refuse_de_poser_si_un_champ_n_est_pas_declare():
    """LE cas qui protège la fiche : une colonne non déclarée dans un tableau
    strict fait refuser l'écriture entière — mieux vaut pas d'estampille."""
    s = _SessionNue(schema={"schema": {"fields": [{"key": "siren"}, {"key": "modele"}]}})
    assert worker._estampille(s, _PAYLOAD, _Provider(resolu="m"), {"version": 101}) == {}


def test_refuse_de_poser_une_demi_estampille():
    """Sans version, « écrit par tel modèle » n'a pas de sens : on ne pose rien."""
    s = _SessionNue(schema=_SCHEMA_COMPLET)
    assert worker._estampille(s, _PAYLOAD, _Provider(resolu="m"), {}) == {}


def test_un_schema_illisible_ne_fait_pas_echouer_le_job():
    class _Cassee(_SessionNue):
        def outil(self, name, arguments=None):
            raise RuntimeError("le tableau ne répond pas")

    assert worker._estampille(_Cassee(), _PAYLOAD, _Provider(resolu="m"),
                              {"version": 101}) == {}


def test_la_prose_porte_les_deux_valeurs_a_recopier():
    """Chemin où la boucle d'outils tourne chez le fournisseur : le worker ne voit
    pas les arguments, la prose est le seul recours — comme pour `_run_id`."""
    ordre = worker._ordre_one_shot("fais le travail", "run-42",
                                   {"namespace": "un-tableau"}, ESTAMPILLE)
    assert 'modele: "un-modele-2512"' in ordre
    assert 'version_procedure: "une-procedure v101"' in ordre
    assert "fais le travail" in ordre


def test_la_prose_reste_muette_sans_estampille():
    ordre = worker._ordre_one_shot("fais le travail", "run-42", {"namespace": "t"}, {})
    assert "modele" not in ordre


# ── Le PROJET imposé par le harnais ──────────────────────────────────────────
# Vécu le 28/08 : la procédure disait « passe `_project: 220` », et ce projet liait
# le slot `vivier` au FICHIER CLIENT. Des agents travaillant sur une copie ont donc
# écrit dans la table de production via `namespace: "slot:vivier"` — dont une ligne
# créée sans clé. Le dispositif de copies ne protégeait rien : le miroir n'était
# qu'un nom qu'on passait, l'autre restait joignable en permanence.
# Une procédure qui nomme son projet emporte sa cible partout où on la copie.

def test_le_projet_de_la_flotte_est_impose_dans_l_ordre():
    ordre = worker._ordre_one_shot("fais le travail", "run-42",
                                   {"namespace": "un-miroir", "project_id": 999})
    assert "`_project: 999`" in ordre
    # Et l'ordre prime explicitement sur ce que la procédure pourrait nommer :
    # sans cette phrase, un agent lisant les deux choisirait au hasard.
    assert "Ignore tout numéro de projet écrit dans la procédure" in ordre


def test_sans_projet_declare_l_ordre_n_en_invente_aucun():
    """Une flotte sans projet ne doit pas en voir surgir un : mieux vaut que
    l'agent n'en passe aucun que le mauvais."""
    ordre = worker._ordre_one_shot("fais le travail", "run-42",
                                   {"namespace": "un-miroir"})
    assert "_project" not in ordre


def test_le_projet_impose_est_celui_de_la_flotte_pas_un_autre():
    """Deux flottes, deux projets : c'est la déclaration qui décide, et elle
    change d'un essai à l'autre — contrairement à une procédure qu'on copie."""
    a = worker._ordre_one_shot("x", "r", {"namespace": "n", "project_id": 220})
    b = worker._ordre_one_shot("x", "r", {"namespace": "n", "project_id": 221})
    assert "`_project: 220`" in a and "`_project: 221`" in b
