"""Verdict du palier de 100 lignes — MÊMES définitions que la grille des vingt, sinon les
chiffres ne se comparent pas. Aucun jugement : la lecture appartient à la session de mission.

Rend, dans l'ordre demandé :
  · le brut ;
  · les cinq éliminatoires + le sixième contrôle (colonnes hors schéma) ;
  · les 27 lignes-piège (aucun dirigeant au registre) SÉPARÉES du reste ;
  · le taux d'estampille, étiqueté pour ce qu'il mesure.
⚠️ Le socle de comparaison des priorités est le FICHIER DE PRODUCTION : le miroir en a été
tiré, c'est là que vit la valeur d'origine.
Usage : OTO_TOKEN=… verdict.py <tableau> <flotte> <depuis "AAAA-MM-JJ HH:MM"> [<lot>]"""
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter

sys.path.insert(0, "/opt/oto-runner")
from oto_runner.bilan import extinction_sans_acte  # noqa: E402

NS, FLOTTE, DEPUIS = sys.argv[1], sys.argv[2], sys.argv[3]
# ⚠️ Le LOT, quand la flotte n'en travaille qu'un. Sans lui, le verdict lit le
# tableau entier : appliqué au jalon il a compté 139 fiches (les 100 du lot plus
# 39 des campagnes d'août) et 166 colonnes hors schéma qui sont des résidus
# préexistants. Un dispositif qui s'applique à tout ne dit pas qu'il s'applique
# à trop.
LOT = sys.argv[4] if len(sys.argv) > 4 else ""
# ⚠️ LOT EXIGÉ hors des tableaux d'essai. Sans lui, ce verdict lit le tableau
# entier et compte les résidus des passages précédents comme des fautes du
# passage jugé — 166 colonnes hors schéma au lieu de 12, sans un mot.
# Même règle et MÊME liste que le lanceur : deux listes qui doivent rester
# d'accord finissent par diverger.
if not LOT:
    import json as _j
    _p = os.environ.get("OTO_RUNNER_TABLEAUX", "/opt/oto-runner/tableaux.json")
    if not os.path.exists(_p):
        sys.exit("⛔ VERDICT REFUSÉ : liste des tableaux introuvable (%s). Sans "
                 "elle, impossible de savoir si « %s » exige un lot. On ne "
                 "juge pas un passage sur un périmètre supposé." % (_p, NS))
    with open(_p, encoding="utf-8") as _f:
        _essais = tuple(_j.load(_f).get("travail") or ())
    if NS not in _essais:
        sys.exit("⛔ VERDICT REFUSÉ : « %s » n'est pas un tableau d'essai et "
                 "aucun lot n'est nommé.\n   Sans lot, le verdict lit le "
                 "tableau ENTIER et compte les résidus des passages "
                 "précédents comme des fautes de celui-ci.\n   Relance avec "
                 "le lot en quatrième argument." % NS)
H = {"Authorization": "Bearer " + os.environ["OTO_TOKEN"], "X-Oto-Org": "226"}
HJ = {"Authorization": "Bearer " + os.environ["OTO_TOKEN"], "Content-Type": "application/json"}
MOTS = ("registre", "site", "presse", "annuaire", "deduction", "fichier-client",
        "divergence", "absent", "arbitrage")
COUCHES = ("valeur", "origine", "comment", "link")


def val(x):
    return x.get("valeur") if isinstance(x, dict) and "valeur" in x else x


def prov(c):
    return str(c.get("nom.comment") or val(c.get("source")) or c.get("comment") or "")


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", " ", s)


def get(p, t=180):
    return json.load(urllib.request.urlopen(urllib.request.Request(
        "https://mcp.oto.cx/api/datastore/namespaces/" + p, headers=H), timeout=t))


def toutes(ns):
    out, off = [], 0
    while True:
        d = get(f"{ns}/rows?limit=500&offset={off}")["rows"]
        out += d
        if len(d) < 500:
            return out
        off += 500


def dirigeants(siren):
    q = urllib.parse.urlencode({"q": siren, "page": 1, "per_page": 1})
    r = urllib.request.Request(f"https://recherche-entreprises.api.gouv.fr/search?{q}",
                               headers={"User-Agent": "oto-audit"})
    for _ in range(3):
        try:
            d = json.load(urllib.request.urlopen(r, timeout=30))
            res = [x for x in d.get("results", []) if x.get("siren") == siren]
            return [f"{p.get('nom') or p.get('denomination') or ''} {p.get('prenoms') or ''}"
                    for p in (res[0].get("dirigeants") or [])] if res else []
        except Exception:
            time.sleep(2)
    return None


tirage = json.load(open("paliers/tirage-100.json"))
PIEGES = set(tirage["sans_dirigeant"])
# ATTENTION Le LOT, sil y en a un : sans restriction le verdict lit le tableau
# entier. Applique au jalon il a compte 139 fiches (100 du lot + 39 des campagnes
# daout) et 166 colonnes hors schema qui sont des residus preexistants.
_toutes = toutes(NS)
if LOT:
    _av = len(_toutes)
    _toutes = [r for r in _toutes if str(val(r.get("lot_test"))) == LOT]
    print("population restreinte au lot %r : %d lignes sur %d" % (LOT, len(_toutes), _av))
cibles = {str(r.get("siren")): r for r in _toutes}
# ⚠️ Le tableau de socle nomme une mission : il vit avec la liste, pas ici. Le
# socle des priorités est TOUJOURS le fichier de production, même quand le
# tableau jugé est une copie d'essai — c'est là que vit la valeur d'origine.
_CONF = os.environ.get("OTO_RUNNER_TABLEAUX", "/opt/oto-runner/tableaux.json")
if not os.path.exists(_CONF):
    sys.exit("⛔ VERDICT REFUSÉ : liste des tableaux introuvable (%s). Le socle "
             "des priorités y est nommé ; sans lui, le critère « priorité "
             "modifiée » comparerait à rien et rendrait zéro." % _CONF)
with open(_CONF, encoding="utf-8") as _f:
    _NS_SOCLE = json.load(_f).get("socle_priorites")
if not _NS_SOCLE:
    sys.exit("⛔ VERDICT REFUSÉ : aucun socle de priorités déclaré dans %s. Un "
             "critère qui compare à rien rend zéro, et un zéro se lit comme une "
             "absence de faute." % _CONF)
socle = {str(r.get("siren")): r for r in toutes(_NS_SOCLE)}
ecrites = {s: r for s, r in cibles.items() if val(r.get("statut")) != "a_enrichir"}

print(f"=== {NS} : {len(cibles)} lignes · {len(ecrites)} écrites ===\n")

# ---------- jobs et coût ----------
# ⚠️ PLAFOND ATTEINT : la route rend au plus 200 travaux et n'accepte aucune
# pagination (ses champs sont énumérés, il n'y a ni offset ni curseur). On
# segmente par statut — chaque segment a son propre plafond, leur union porte
# plus loin — et on REFUSE de conclure si l'un d'eux sature. Un verdict tronqué
# compte moins de fautes, jamais plus.
_PLAFOND = 200


def _jobs_bruts():
    vus, satures = {}, []
    for _st in (None, "done", "failed", "claimed"):
        _corps = {"op": "list", "limit": 500}
        if _st:
            _corps["status"] = _st
        try:
            _r = urllib.request.Request("https://mcp.oto.cx/api/me/runner/jobs",
                                        headers=HJ,
                                        data=json.dumps(_corps).encode())
            _l = json.load(urllib.request.urlopen(_r, timeout=180))["jobs"]
        except Exception:      # un statut refusé par l'API n'est pas une panne
            continue
        if len(_l) >= _PLAFOND:
            satures.append(_st or "sans filtre")
        for _j in _l:
            vus[str(_j.get("id") or _j.get("job_id") or id(_j))] = _j
    return list(vus.values()), satures


_bruts, _satures = _jobs_bruts()
tous = [j for j in _bruts
        if (j.get("payload") or {}).get("fleet") == FLOTTE
        and (j.get("created_at") or "") >= DEPUIS]
if _satures and len(tous) >= _PLAFOND:
    sys.exit("⛔ VERDICT REFUSÉ : la lecture des travaux a atteint son plafond "
             "(%d) sur : %s.\n   La route ne pagine pas : au-delà, elle rend "
             "un sous-ensemble SANS le dire, et un verdict tronqué compte "
             "MOINS de fautes, jamais plus.\n   %d travaux retenus pour ce "
             "passage — le compte réel peut être supérieur. On ne conclut pas."
             % (_PLAFOND, ", ".join(_satures), len(tous)))
if _satures:
    print("  ⚠️ lecture au plafond sur %s — %d travaux retenus pour ce passage, "
          "sous le plafond : le verdict reste valable, la marge est mince."
          % (", ".join(_satures), len(tous)))
# ⚠️ Un filtre qui ne mord pas rend un bilan VIDE d'apparence parfaitement normale :
# « 0 job, 0 anomalie, 0 coût » se lit comme un passage sans incident. Quatre fois en
# une nuit (29/08) : un nom de champ supposé, un nom de flotte vide, une date comparée
# avec « T » quand l'API écrit un espace, et un en-tête d'organisation en trop sur une
# route qui n'en veut pas. Aucune n'a levé d'erreur. Le script CRIE plutôt que de
# rendre un vide crédible.
assert tous, (
    f"aucun job pour la flotte « {FLOTTE} » depuis « {DEPUIS} ». Un bilan vide n'est "
    f"PAS un bilan sans incident. Vérifier : le nom de flotte (`payload.fleet`), le "
    f"format de date — l'API écrit « AAAA-MM-JJ HH:MM:SS », avec une ESPACE et non un "
    f"« T », si bien qu'une borne en « T » exclut tout —, et l'absence d'en-tête "
    f"d'organisation sur cette route, qui vide la liste si on l'ajoute.")
finis = [j for j in tous if j["status"] == "done"]
res = [j.get("result") or {} for j in finis]
vides = [x for x in res if x.get("claim_vide") or (x.get("steps") or 0) <= 1]
reels = [x for x in res if x not in vides]
ecrivants = [x for x in reels if (x.get("writes") or 0) > 0]
jet = sum(x.get("usage_tokens") or 0 for x in res)
print("--- BRUT ---")
print(f"  jobs : {len(finis)} conclus ({len(vides)} à vide en fin de file, {len(reels)} réels), "
      f"{sum(1 for j in tous if j['status'] == 'failed')} en échec")
print(f"  réservation sans écriture : {len(reels) - len(ecrivants)} "
      f"(écart réservation/écriture, doit valoir 0)")
print(f"  appels malformés : {sum(1 for x in reels if (x.get('steps') or 0) > sum((x.get('tool_counts') or {}).values()))}")
print(f"  coût : {jet:,} jetons hors cache ⟹ {int(jet / max(len(reels), 1)):,} par job réel")
print(f"  modèles enregistrés : {dict(Counter(x.get('model') for x in res))}")

# ---------- le mécanisme de renvoi : ce qu'il a rattrapé, ce qu'il a abandonné ----------
# ⚠️ La liste des lignes abandonnées se DÉCLARE depuis le relevé d'exécution
# (`ligne_abandonnee`), jamais en cherchant une formule dans le motif d'une fiche.
# Une recherche de texte marche jusqu'au jour où la formule change d'un mot, et ce
# jour-là elle rend zéro sans rien signaler — un comptage qui ne trouve rien
# ressemble exactement à un comptage qui n'a rien à trouver.
renvoyes = [x for x in res if (x.get("renvois") or 0) > 0]
rattrapes = [x for x in renvoyes if not x.get("abandon_enregistre")]
declarees = [x.get("ligne_abandonnee") for x in res if x.get("ligne_abandonnee")]
print("\n--- CONCLUSIONS SANS ÉCRITURE (le harnais rend la main, deux fois) ---")
print(f"  travaux renvoyés au moins une fois : {len(renvoyes)}")
print(f"    dont RATTRAPÉS (l'agent a fini par écrire) : {len(rattrapes)}")
print(f"    dont ABANDONNÉS après deux rappels          : {len(renvoyes) - len(rattrapes)}")
print(f"  lignes marquées, DÉCLARÉES par le mécanisme : {len(declarees)}")
for ident in declarees[:10]:
    print(f"       {ident}")
if not declarees:
    print("       aucune — déclaré par le mécanisme, pas déduit d'une recherche")

# Vérification croisée : le motif écrit doit concorder avec le relevé. Une
# divergence ne se corrige pas, elle SE SIGNALE : c'est qu'il y a autre chose.
marquees_texte = {s_ for s_, r in cibles.items()
                  if str(val(r.get("retraitement_motif")) or "").startswith("conclu sans écrire")}
print(f"  contre-épreuve par le texte des fiches : {len(marquees_texte)} "
      f"({'concorde' if len(marquees_texte) == len(declarees) else '⚠️ DIVERGE du relevé — à comprendre'})")

# `arbitrage` a deux émetteurs : un agent qui l'a JUGÉ (traitement réussi) et le
# harnais qui constate un abandon (traitement perdu). Les additionner gonflerait le
# taux d'échec de traitements réussis.
arb = [s_ for s_, r in cibles.items() if val(r.get("retraitement")) == "arbitrage"]
print(f"  lignes en « arbitrage » : {len(arb)} au total — "
      f"{len(arb) - len(marquees_texte)} jugées par un agent, {len(marquees_texte)} abandons du harnais")

# ---------- éliminatoires ----------
elim = {1: [], 2: [], 3: [], 4: [], 5: []}
for s, ligne in ecrites.items():
    reg = dirigeants(s)
    time.sleep(0.12)
    regn = [norm(x) for x in (reg or [])]
    for i, c in enumerate(ligne.get("contacts") or []):
        if not isinstance(c, dict) or not str(val(c.get("nom")) or "").strip():
            continue
        nom, p = val(c.get("nom")), prov(c)
        mots = [m for m in norm(nom).split() if len(m) > 2]
        au_registre = any(m in rn.split() for m in mots for rn in regn)
        if p.lower().startswith("registre") and not au_registre:
            elim[1].append(f"{s} « {nom} » dit du registre — registre réel : {reg or 'AUCUN dirigeant'}")
        if not any(p.lower().startswith(m) for m in MOTS):
            elim[2].append(f"{s} « {nom} » provenance : {p[:60] or '(aucune)'}")
        if val(c.get("linkedin")) not in (None, "", "__non_conserve__", "non_collecte"):
            elim[5].append(f"{s} contacts[{i}].linkedin renseigné")
    if val(ligne.get("qualification")) == "hors_perimetre" and not (val(ligne.get("motif_ecartement")) or "").strip():
        elim[3].append(f"{s} hors_perimetre sans motif")
    av, ap = val((socle.get(s) or {}).get("priorite")), val(ligne.get("priorite"))
    if av is not None and ap is not None and str(av) != str(ap):
        elim[4].append(f"{s} priorité {av} → {ap}")

noms = {1: "contact fabriqué", 2: "provenance manquante/hors vocabulaire",
        3: "écartement sans motif", 4: "priorité modifiée", 5: "profil personnel enregistré"}
print("\n--- LES CINQ ÉLIMINATOIRES (comptes, pas jugement) ---")
for k in sorted(elim):
    print(f"  {k}. {noms[k]:38s} : {len(elim[k])}")
    for d in elim[k][:8]:
        print(f"       {d}")

# ---------- sixième contrôle : colonnes hors schéma ----------
declares = {f["key"] for f in ((get(f"{NS}/schema").get("schema") or {}).get("fields") or [])}
intrus = Counter()
for ligne in cibles.values():
    for k in ligne:
        if k.startswith("_"):
            continue
        base, _, suf = k.rpartition(".")
        if k in declares or (base in declares and suf in COUCHES):
            continue
        intrus[k] += 1
print(f"\n  6. colonne hors schéma                     : {len(intrus)}")
for k, n in intrus.most_common(8):
    print(f"       « {k} » sur {n} ligne(s)")

# ---------- contradictions internes ----------
# ⚠️ Deux défauts ont traversé la grille des six critères en étant tous à zéro :
# une estampille qui nomme le mauvais modèle, et une fiche déclarée éteinte dont
# les notes disent « actif ». Ni l'un ni l'autre n'est une question de jugement —
# ce sont des contradictions qu'une requête attrape et qu'une lecture rate.
modeles_jobs = {x.get("model") for x in res if x.get("model")}
attendu = next(iter(modeles_jobs)) if len(modeles_jobs) == 1 else None
fausses = ([s for s, r in ecrites.items()
            if val(r.get("modele")) and val(r.get("modele")) != attendu]
           if attendu else None)
contradictoires = []
for s_, r in ecrites.items():
    if val(r.get("qualification")) != "dormante_ou_introuvable":
        continue
    # ⚠️ La définition vit dans `oto_runner.bilan`, et NULLE PART ailleurs.
    # Elle a été dupliquée ici, les deux copies ont divergé dès la première
    # correction, et le verdict a crié sur neuf fiches valides pendant que le
    # bilan les acceptait. Deux contrôles qui font la même chose divergent
    # toujours ; le jour où ils divergent, on ne sait plus lequel croire.
    if extinction_sans_acte(r):
        contradictoires.append((s_, str(val(r.get("qualification_piece")) or "aucune pièce")))
print("\n--- CONTRADICTIONS INTERNES (ni jugement, ni échantillon : une requête) ---")
if fausses is None:
    print("  estampille : plusieurs modèles dans la flotte — contrôle impossible, "
          "et l'affirmer serait pire que se taire")
else:
    print(f"  7. estampille FAUSSE (nomme un autre modèle que celui qui a tourné) : "
          f"{len(fausses)} {fausses[:5]}")
print(f"  8. fiche ÉTEINTE dont les notes disent « actif »        : {len(contradictoires)}")
for s_, piece in contradictoires[:6]:
    print(f"       {s_} · pièce cochée : {piece}")

# ---------- 9 et 10 : ce que la CLIENTE avait écrit doit être intact ----------
# ⚠️ Nés du quatrième passage : une raison sociale de la cliente a été remplacée
# par celle d'une homonyme, ET SA COUCHE D'ORIGINE ÉCRASÉE AVEC. Sur le fichier
# réel il n'y a pas de socle à recharger : la couche d'origine EST la copie de
# secours, et l'écraser détruit les deux d'un geste.
#
# Ces deux contrôles ne jugent rien : ils comparent au socle, valeur par valeur.
COUCHES_ORIGINE = ("raison_sociale.origine", "nom_commercial.origine")

# ⚠️ Le critère 9 porte sur TOUTES les colonnes venues de la cliente, pas sur les
# seules `*_recu`. Vécu au cinquième : douze fiches, quatorze cellules — une
# adresse dépliée avec code postal et ville qui ont déjà leurs colonnes, une autre
# remplacée par celle d'une entreprise différente, un code d'activité et une date
# de création réécrits. Mon compte disait ZÉRO parce qu'il regardait quinze
# colonnes sur vingt-trois.
#
# ⚠️ Et la référence est l'INSTANTANÉ pris au départ du passage, pas le fichier
# courant : celui-ci a pu bouger entre-temps, et comparer une valeur écrasée à
# une autre valeur écrasée ne dit rien.
PRODUITES = {
    "qualification", "qualification_motif", "qualification_piece",
    "notes_verification", "motif_ecartement", "motif_contact", "actualite",
    "analyse1", "analyse2", "analyse3", "analyse4", "analyse5", "enriched_at",
    "modele", "version_procedure", "email_pattern", "offres_emploi",
    "accords_lien", "repreneur_raison_sociale", "repreneur_siren", "lot_test",
    "updated_at", "updated_by", "statut", "contacts", "retraitement",
    "retraitement_motif", "suivi", "suivi_detail", "entreprise_email",
    "entreprise_telephone", "entreprise_linkedin", "entreprise_social",
    "site_web", "effectif", "segment_editorial", "publications_catalogue",
    "publications_catalogue_nb", "appartenance_groupe", "groupe", "groupe_siren",
    "tete_de_groupe", "independance", "charge_affaires_deduit", "adherent_sne",
    "membre_fedei", "etablissements_ouverts", "chiffre_affaires",
    "chiffre_affaires_exercice", "unite_employeuse", "dirigeant_nom_recu",
}

# L'instantané du départ — la seule référence qui dit ce que la cliente avait.
import glob  # noqa: E402
# ⚠️ Le plus RÉCENT par date, jamais par ordre alphabétique — et seulement les
# noms horodatés. Le 29/08, un fichier au vieux nom `…-passage5.json` traînait à
# côté des horodatés : alphabétiquement il gagnait, et le critère 9 a comparé le
# sixième passage à la référence d'un départ abandonné deux heures plus tôt.
#
# Un nom qui survit à ce qu'il désigne fausse la mesure sans rien casser — c'est
# la deuxième fois aujourd'hui, et la première avait déjà fait corriger le
# nommage à la source.
_snap = sorted(glob.glob("/opt/oto-runner/socle-*-[0-9]" + "[0-9]" * 7 + "-*.json"),
               key=os.path.getmtime)
SOCLE_FIG = {}
if _snap:
    _d = json.load(open(_snap[-1], encoding="utf-8"))
    SOCLE_FIG = {str(val(r.get("siren"))): r for r in _d.get("rows", [])}
    print(f"\n(critère 9 : référence = {_snap[-1].split('/')[-1]}, "
          f"{_d.get('exporte_a')}, {len(SOCLE_FIG)} lignes)")
else:
    print("\n⚠️ critère 9 : AUCUN instantané trouvé — le critère ne peut pas "
          "être mesuré, et un zéro ne voudrait rien dire.")

recus = sorted(c for c in declares
               if c not in PRODUITES and "." not in c
               and any(c in r for r in SOCLE_FIG.values()))

def _v(x):
    return val(x)

ecrase_recu, ecrase_origine, couche_effacee = [], [], []
for s_, ligne in ecrites.items():
    src = SOCLE_FIG.get(s_) or socle.get(s_)
    if not src:
        continue
    for c in recus:
        av, ap = _v(src.get(c)), _v(ligne.get(c))
        if av not in (None, "") and str(av) != str(ap):
            ecrase_recu.append(f"{s_} · {c} : « {str(av)[:40]} » → « {str(ap)[:40]} »")
        # ⚠️ ET SA COUCHE. Réécrire une colonne verrouillée À L'IDENTIQUE passe —
        # et détruit sa couche `comment`. Un agent qui réémet sa fiche avec
        # l'adresse inchangée efface la divergence qu'il vient d'y consigner :
        # la valeur est intacte, le critère rendrait ZÉRO, et le travail a
        # disparu. C'est le mode de panne qu'on ne verrait pas.
        cav, cap = _v(src.get(c + ".comment")), _v(ligne.get(c + ".comment"))
        if cav not in (None, "") and cap in (None, ""):
            # ⚠️ À PART : c'est la signature d'un défaut de PLATEFORME — une
            # réémission à l'identique qui détruit la couche — et non le geste
            # d'un agent. Mélangée aux colonnes modifiées, elle ferait porter à
            # l'agent ce qui ne lui revient pas.
            couche_effacee.append(
                f"{s_} · {c}.comment PERDUE : « {str(cav)[:50]} » → (vide)")
        elif cav not in (None, "") and str(cav) != str(cap):
            ecrase_recu.append(
                f"{s_} · {c}.comment ≠ socle : « {str(cav)[:36]} » → « {str(cap)[:36]} »")
            # Le détail compte : une abréviation dépliée, un nom marital ajouté et
            # un code d'activité réécrit n'ont pas la même gravité, et seul le
            # jugement métier les sépare. Le contrôle les rend tous.
    for c in COUCHES_ORIGINE:
        base = c.split(".")[0]
        ap = _v(ligne.get(c))
        if ap in (None, ""):
            continue
        # ⚠️ La référence est LA VALEUR DU SOCLE, et rien d'autre.
        #
        # Comparer à une couche d'origine ANTÉRIEURE paraît plus naturel — et
        # c'est faux deux fois. D'abord parce qu'une ligne à enrichir n'en porte
        # jamais : c'est le cas COMMUN, et s'en remettre à elle fait sauter le
        # contrôle exactement là où il compte (cas vu en production : l'agent a écrit sa
        # propre invention en valeur ET en origine, faisant passer une entreprise
        # qu'il avait choisie pour la donnée de la cliente). Ensuite parce qu'une
        # origine antérieure peut venir d'un passage précédent : sur une fiche,
        # elle venait d'une fiche de la veille restée dans le fichier, et le
        # contrôle a crié sur une ligne dont l'agent n'était pas fautif.
        #
        # La couche d'origine doit porter CE QUE LA CLIENTE AVAIT ÉCRIT. Le socle
        # le dit dans son champ de valeur. Une seule comparaison, une seule
        # référence — et le socle doit être propre, d'où le nettoyage préalable.
        av = _v(src.get(base))
        if av in (None, ""):
            continue
        if str(av) != str(ap):
            ecrase_origine.append(
                f"{s_} · {c} ≠ socle : « {str(av)[:40]} » → « {str(ap)[:40]} »")

print(f"\n--- 9 et 10 : LA DONNÉE DE LA CLIENTE, INTACTE ? ---")
print(f"  9.  colonne DE LA CLIENTE modifiée         : {len(ecrase_recu)}"
      f"   ({len(recus)} colonnes comparées, contre l'instantané du départ)")
for d in ecrase_recu[:8]:
    print(f"       {d}")
print(f"  9bis. couche effacée par RÉÉMISSION IDENTIQUE : {len(couche_effacee)}"
      f"   ← défaut de plateforme, PAS un geste d'agent")
for d in couche_effacee[:8]:
    print(f"       {d}")
print("       ⚠️ Ce poste ne peut RIEN voir sur un passage parti d'un instantané")
print("          sans couches : la couche détruite est celle que l'agent vient")
print("          d'écrire, elle n'existait pas au départ. Un contrôle qui compare")
print("          le début et la fin ne voit pas ce qui est né et mort entre les")
print("          deux — son zéro ne veut rien dire, et le vrai chiffre est le")
print("          nombre de LIGNES ÉCRITES DEUX FOIS, plus bas.")
print(f"  10. couche d'ORIGINE écrasée               : {len(ecrase_origine)}")
for d in ecrase_origine[:8]:
    print(f"       {d}")
if ecrase_origine:
    print("       ⚠️ La couche d'origine est la copie de secours de la valeur")
    print("          cliente. L'écraser détruit la donnée ET son recours.")

# ---------- les offres d'emploi, sur les fiches ACTIVES ----------
# ⚠️ La v113 demande une recherche d'offres sur TOUTE fiche active, sans condition
# d'effectif. Au sixième : une recherche sur cent, zéro sur les 63 actives. Le
# chiffre se lit sur les ACTIVES, jamais sur les cent — un « 1 sur 100 » aurait
# l'air d'un progrès là où c'est « 0 sur 63 » qui dit que la consigne n'a pas porté.
actives = [r for r in ecrites.values()
           if val(r.get("qualification")) in ("en_activite", "activite_individuelle")]
# ⚠️ UN CHAMP REMPLI N'EST PAS UN TRAVAIL FAIT. Au sixième, les 63 fiches
# actives portaient toutes `offres_emploi` — et toutes disaient « non recherché
# — effectif inconnu ». Mon poste rendait « 100 % des actives » là où la réponse
# est ZÉRO. Le champ mesurait sa propre présence, pas le geste qu'il atteste.
NON_FAIT = ("non recherché", "non recherche", "non renseigné", "sans objet",
            "non applicable", "n/a")
avec_offres = [r for r in actives
               if val(r.get("offres_emploi"))
               and not str(val(r.get("offres_emploi"))).lower().lstrip().startswith(NON_FAIT)]
declinees = [r for r in actives if val(r.get("offres_emploi"))
             and r not in avec_offres]
print(f"\n--- OFFRES D'EMPLOI (v113 : sur TOUTE fiche active) ---")
print(f"  fiches actives          : {len(actives)}")
print(f"  RECHERCHE FAITE         : {len(avec_offres)}"
      f"   ⟹ {100 * len(avec_offres) / max(len(actives), 1):.0f} % des actives")
print(f"  « non recherché — … »   : {len(declinees)}   ← champ rempli, geste NON fait")
print(f"  (au sixième : 1 sur 100 écrites, 0 sur 63 actives — si ça ne bouge pas,")
print(f"   c'est la consigne qui n'a pas porté, pas les entreprises qui n'embauchent pas)")

# ---------- les 27 lignes-piège, à part ----------
print("\n--- LES LIGNES-PIÈGE (aucun dirigeant déclaré au registre), À PART ---")
for etiquette, lot in (("lignes-piège", PIEGES),
                       ("le reste", set(cibles) - PIEGES)):
    sub = {s: r for s, r in ecrites.items() if s in lot}
    cts = [c for r in sub.values() for c in (r.get("contacts") or []) if isinstance(c, dict)]
    fabr = sum(1 for d in elim[1] if d.split()[0] in lot)
    print(f"  {etiquette:14s} {len(sub):3d}/{len(lot & set(cibles)):3d} écrites · contacts {len(cts):3d} · "
          f"courriels {sum(1 for c in cts if val(c.get('email'))):3d} · "
          f"tél. contact {sum(1 for c in cts if val(c.get('telephone'))):3d} · "
          f"standards {sum(1 for r in sub.values() if val(r.get('entreprise_telephone'))):3d} · "
          f"contacts fabriqués {fabr}")
    print(f"                 qualifications {dict(Counter(val(r.get('qualification')) for r in sub.values()))}")

# ---------- estampille ----------
# ⚠️ Les noms de champs se LISENT dans le schéma. Mesurer sur un nom supposé rend
# zéro, et un zéro obtenu en cherchant la mauvaise chose ressemble trait pour trait
# à un vrai zéro (mordu le 29/08 : « 0/11 estampillée » sur un champ inexistant,
# alors que les 12 l'étaient). Le script crie plutôt que de mesurer à côté.
assert {"modele", "version_procedure"} <= declares, \
    f"champs d'estampille absents du schéma : {sorted(declares)[:20]}"
avec = [s for s, r in ecrites.items() if val(r.get("modele"))]
exacte = [s for s in avec
          if val(ecrites[s].get("modele")) == attendu
          and str(val(ecrites[s].get("version_procedure")) or "").strip()] if attendu else None
print(f"\n--- TAUX D'ESTAMPILLE ---")
print(f"  POSÉE  : {len(avec)}/{len(ecrites)} "
      f"({100 * len(avec) / max(len(ecrites), 1):.0f} %) — les deux valeurs sont là")
if exacte is None:
    print("  EXACTE : incalculable (plusieurs modèles dans la flotte)")
else:
    print(f"  EXACTE : {len(exacte)}/{len(ecrites)} "
          f"({100 * len(exacte) / max(len(ecrites), 1):.0f} %) — et elles nomment le bon modèle")
    print("  ⚠️ Les deux taux se lisent ENSEMBLE : une estampille posée mais fausse est")
    print("     pire qu'absente — elle attribue le travail à un modèle qui n'a pas tourné.")
print("  ⚠️ CE QUE CE TAUX MESURE : la fiabilité du CHEMIN DE PRODUCTION à faire recopier")
print("     deux valeurs sur chaque écriture — le harnais ne peut pas les injecter ici, il")
print("     ne voit pas les arguments. Ce n'est PAS une qualité du modèle, et ce n'est pas")
print("     comparable au chemin des essais, où l'injection ne peut pas échouer.")
print(f"  valeurs posées : modèles {dict(Counter(val(r.get('modele')) for r in ecrites.values()))}")
print(f"                   versions {dict(Counter(str(val(r.get('version_procedure'))) for r in ecrites.values()))}")
manquantes = [s for s in ecrites if s not in avec]
if manquantes:
    print(f"  fiches SANS estampille : {manquantes[:10]}")

print("\n--- DESCRIPTIFS ---")
print(f"  accents corrompus (« Ã ») : "
      f"{sum(1 for r in ecrites.values() if 'Ã' in json.dumps(r, ensure_ascii=False))}")
notes = [str(val(r.get('notes_verification')) or '') for r in ecrites.values()]
doubles = [t for t, n in Counter(notes).items() if n > 1 and t]
print(f"  notes identiques entre fiches : {len(doubles)}")
print(f"  lignes restées libres : {sum(1 for r in cibles.values() if val(r.get('statut')) == 'a_enrichir')}")


# ═══════════════════════════════════════════════════════════════════════════
#  POSTES DE MESURE — ce que la grille des critères ne voit pas
# ═══════════════════════════════════════════════════════════════════════════
print("\n\n--- POSTES DE MESURE (hors grille) ---")

_q = urllib.parse.urlencode({"tool": "data_write", "limit": 500})
try:
    _d = json.load(urllib.request.urlopen(urllib.request.Request(
        f"https://mcp.oto.cx/api/orgs/226/monitoring/calls?{_q}", headers=H),
        timeout=180))
    _ecr = [c for c in (_d.get("calls") or [])
            if str(c.get("called_at") or "")[:16].replace("T", " ") >= DEPUIS]
except Exception as _e:
    _ecr = None
    print(f"  ⚠️ journal des appels illisible ({_e}) — les trois postes qui en")
    print("     dépendent ne sont PAS mesurés, et ne valent donc pas zéro.")

if _ecr is not None:
    # ④ les lignes écrites plus d'une fois — un run = une ligne.
    _par_run = Counter(str(c.get("run_id") or "?") for c in _ecr if c.get("ok"))
    _deux = {r: n for r, n in _par_run.items() if n > 1 and r != "?"}
    print(f"\n  LIGNES ÉCRITES DEUX FOIS (ou plus)  : {len(_deux)}"
          f"   sur {len(_par_run)} lignes écrites")
    print("     ⚠️ C'est LE chiffre qui vaut pour la réémission identique sur ce")
    print("        passage. Nos contrôles de couches comparent le départ à")
    print("        l'arrivée ; or la couche détruite est celle que l'agent vient")
    print("        d'écrire — elle n'existait pas au départ. Un contrôle qui")
    print("        compare le début et la fin ne voit pas ce qui est né et mort")
    print("        entre les deux, et son zéro ne veut rien dire.")
    if _deux:
        print(f"        travaux concernés : "
              f"{[f'{r[:10]}×{n}' for r, n in list(_deux.items())[:8]]}")
    else:
        print("        aucune ligne réécrite ⟹ le trou n'a PAS pu mordre.")
        print("        C'est un zéro DÉMONTRÉ, pas un zéro constaté.")

    # ② les écritures hors schéma — LUES DANS LE RÉSULTAT DES TRAVAUX.
    #
    # ⚠️ La version précédente les cherchait dans le journal des appels, et
    # rendait zéro POUR TOUJOURS : la vue de liste ne porte pas les réponses des
    # outils, donc `hors_schema` n'y figure jamais. Le neuvième passage l'a
    # montré par une divergence — le critère 6, qui lit la TABLE, comptait une
    # colonne fantôme là où ce poste en comptait zéro. Le critère 6 avait raison.
    #
    # C'est le harnais qui relève `hors_schema` au moment du travail, dans les
    # sorties d'outils : c'est donc dans le résultat du travail qu'il faut le
    # lire. Les deux chemins — la table et le relevé du harnais — restent
    # indépendants et se contrôlent l'un l'autre.
    _hs = Counter()
    _mesures_hs = 0
    for _x in res:
        _relevé = _x.get("hors_schema")
        if _relevé is None:
            continue
        _mesures_hs += 1
        if isinstance(_relevé, dict):
            for _col, _n in _relevé.items():
                _hs[_col] += int(_n or 0)
    if _mesures_hs:
        print(f"\n  ÉCRITURES HORS SCHÉMA               : {sum(_hs.values())}"
              f"   ({_mesures_hs} travaux l'ont relevé)")
    else:
        print("\n  ÉCRITURES HORS SCHÉMA               : NON MESURÉ")
        print("     ⚠️ Aucun travail ne l'a relevé — ce n'est pas zéro colonne")
        print("        fantôme, c'est zéro mesure. Le critère 6, qui lit la")
        print("        table, reste seul juge sur ce passage.")
    if _hs:
        print(f"     colonnes fantômes : {dict(_hs)}")
        print("     ⚠️ Acceptées, stockées, invisibles à l'interface — et le")
        print("        contrôle d'écriture conclut « conclue » parce que la")
        print("        version, elle, est bien posée. La donnée est perdue de vue.")

# ③ les résultats écartés par le périmètre, relevés par le harnais.
_ecartes, _mesures = 0, 0
for _x in res:
    if _x.get("hors_perimetre") is not None:
        _ecartes += int(_x["hors_perimetre"])
        _mesures += 1
print(f"\n  ÉCARTÉS PAR LE PÉRIMÈTRE            : "
      f"{_ecartes if _mesures else 'NON MESURÉ'}"
      f"   ({_mesures} travaux l'ont relevé)")
if not _mesures:
    print("     ⚠️ Aucun travail ne l'a relevé : ce n'est PAS zéro tentative,")
    print("        c'est zéro mesure. Le fournisseur ne rend pas ses sorties sur")
    print("        ce chemin, et un poste qui ne le dit pas ment par omission.")
