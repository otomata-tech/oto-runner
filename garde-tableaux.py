"""GARDE DES TABLEAUX — coupe la flotte à la première ligne créée ou écrite là où
elle ne devrait pas l'être.

⚠️ Elle surveille TROIS tableaux, et la troisième porte a failli être oubliée deux
fois. Le dispositif des copies reposait sur une promesse : les agents travaillent sur
un miroir, jamais sur les fichiers de la cliente. Mesuré le 28/08, elle ne tenait
pas — le projet que la procédure faisait activer liait DEUX tableaux de production :
le fichier de travail et le LIVRABLE déjà remis. Les deux ont été empruntés (quatre
lignes estampillées dans l'un, une ligne créée dans l'autre), et une ligne vide a
été créée dans la copie d'essai.

**Fermer un chemin ne ferme pas la classe.** La porte qu'on connaît est celle dont on
a eu la preuve, pas la seule qui existe.

DEUX RÉGIMES, parce que les tableaux ne se protègent pas de la même chose :
  · fichiers de la CLIENTE → toute écriture coupe : l'agent n'a rien à y faire ;
  · tableau de TRAVAIL → les écritures sont normales ; seules une CRÉATION ou une
    ligne SANS SIREN sont fautives.

DEUX CONTRÔLES, partout :
  · le compte de lignes ne bouge pas (une création se voit même sans estampille) ;
  · aucune ligne sans SIREN — le SIREN est la clé : une ligne qui n'en porte pas a
    été créée par un appel malformé. C'est le contrôle qui aurait attrapé les trois
    incidents du jour en quelques minutes.

Usage : OTO_TOKEN=… garde-tableaux.py <ref_1> <ref_2> <ref_3> [<flotte>]"""
import json
import os
import subprocess
import sys
import urllib.request

# ⚠️ LES NOMS NE SONT PAS ICI. Ce fichier part dans un dépôt public : il porte
# la règle, jamais l'identification. La liste vit dans le dépôt privé de la
# mission et se dépose à côté. Absente, la garde REFUSE en le disant — elle ne
# conclut jamais d'un vide, sinon elle protégerait un périmètre qu'elle croit
# lire et qui n'existe pas.
_CONF = os.environ.get("OTO_RUNNER_TABLEAUX", "/opt/oto-runner/tableaux.json")
if not os.path.exists(_CONF):
    sys.exit("⛔ GARDE INOPÉRANTE : liste des tableaux introuvable (%s). "
             "Elle vit dans le dépôt privé de la mission et se dépose ici. "
             "La garde refuse plutôt que de surveiller un périmètre vide." % _CONF)
with open(_CONF, encoding="utf-8") as _f:
    _t = json.load(_f)
CLIENTE = tuple(_t.get("cliente") or ())
TRAVAIL = tuple(_t.get("travail") or ())
if not CLIENTE:
    sys.exit("⛔ GARDE INOPÉRANTE : aucun tableau de production déclaré dans %s."
             % _CONF)
REFS = dict(zip(CLIENTE + TRAVAIL, (int(a) for a in sys.argv[1:4])))
# ⚠️ Le défaut nomme une flotte de mission : il vit avec la liste, pas ici.
FLOTTE = (sys.argv[4] if len(sys.argv) > 4
          else _t.get("flotte_defaut") or "")
DEPUIS = os.environ.get("GARDE_DEPUIS", "2026-08-28 21:00")
# ⚠️ LE LOT QU'UN PASSAGE A LE DROIT D'ÉCRIRE, s'il y en a un. Sans lui, la garde
# coupe à la première fiche d'un jalon — vécu le 31/08 à 11:14:55, cinq fiches
# après le départ : elle a fait exactement ce pour quoi elle est écrite, sur le
# seul passage où c'était faux. Le jalon écrit dans le fichier de la cliente,
# c'est son objet. Hors de ce lot, elle coupe comme avant.
LOT_AUTORISE = os.environ.get("GARDE_LOT", "").strip()
ALERTE = "/opt/oto-runner/GARDE_VIOLATION"
H = {"Authorization": "Bearer " + os.environ["OTO_TOKEN"], "X-Oto-Org": "226"}


def toutes(ns):
    out, off = [], 0
    while True:
        d = json.load(urllib.request.urlopen(urllib.request.Request(
            f"https://mcp.oto.cx/api/datastore/namespaces/{ns}/rows?limit=500&offset={off}",
            headers=H), timeout=180))["rows"]
        out += d
        if len(d) < 500:
            return out
        off += 500


# ⚠️ Un instantané des IDENTIFIANTS, pas seulement du compte. Une garde de comptage
# est AVEUGLE AU REMPLACEMENT : supprimer une ligne puis la recréer laisse le total
# intact. Vécu le 28/08 — cinq lignes supprimées-recréées pendant une campagne sans
# que la garde ne bronche, parce qu'elle ne regardait que le nombre. C'est
# l'identité des lignes qu'il faut suivre, pas leur quantité.
EMPREINTE = "/opt/oto-runner/.garde-empreinte.json"
try:
    with open(EMPREINTE, encoding="utf-8") as f:
        connu = json.load(f)
except Exception:  # noqa: BLE001 — premier passage : on pose l'instantané, on ne juge pas
    connu = {}

violations, etat, instantane = [], [], {}
for ns, ref in REFS.items():
    try:
        rows = toutes(ns)
    except Exception as e:  # noqa: BLE001 — une garde qui coupe sur SON incident
        # transforme sa panne en verdict : on journalise et on laisse tourner.
        print(f"garde : {ns} illisible ({e}) — on ne coupe pas")
        continue
    # ⚠️ Seulement les lignes sans SIREN CRÉÉES APRÈS la borne : trois lignes
    # fautives existent déjà (une par tableau, écrites aujourd'hui avant que la
    # garde n'existe). Les compter ferait couper la flotte à sa première seconde,
    # sur un incident passé — et une garde qui crie à tort cesse d'être lue.
    # Les références de compte, elles, sont armées sur l'état ACTUEL : elles
    # devront être rabaissées après le nettoyage, sinon la garde tolérera une
    # création de plus.
    sans_cle = [r for r in rows
                if not str(r.get("siren") or "").strip()
                and str(r.get("_created_at") or "") >= DEPUIS]
    creees = len(rows) - ref
    recentes = [r for r in rows if str(r.get("_updated_at") or "") >= DEPUIS]
    ids = sorted(str(r.get("_id")) for r in rows)
    instantane[ns] = ids
    etat.append(f"{ns}: {len(rows)} lignes (réf {ref})")
    anciens = connu.get(ns)
    if anciens:
        disparus = set(anciens) - set(ids)
        if disparus:
            violations.append(f"{ns} — {len(disparus)} ligne(s) DISPARUE(S) depuis le "
                              f"dernier passage : {sorted(disparus)[:4]}")
    if creees > 0:
        violations.append(f"{ns} — {creees} ligne(s) CRÉÉE(S) : {len(rows)} vs {ref} attendu")
    if sans_cle:
        violations.append(f"{ns} — {len(sans_cle)} ligne(s) SANS SIREN : "
                          f"{[str(r.get('_id'))[:13] for r in sans_cle][:4]}")
    if ns in CLIENTE and recentes:
        # Les lignes du lot déclaré sont le TRAVAIL du passage, pas une atteinte.
        # On les met à part plutôt que de les taire : leur compte est utile au
        # bilan, et le jour où le lot est mal déclaré on veut le voir.
        def _lot_de(r):
            x = r.get("lot_test")
            return str(x.get("valeur") if isinstance(x, dict) and "valeur" in x
                       else x or "")
        au_lot = [r for r in recentes if LOT_AUTORISE and _lot_de(r) == LOT_AUTORISE]
        hors_lot = [r for r in recentes if r not in au_lot]
        if au_lot:
            print(f"   {ns} — {len(au_lot)} écriture(s) dans le lot « "
                  f"{LOT_AUTORISE} » : c'est le travail du passage, pas une "
                  f"atteinte.")
        if hors_lot:
            violations.append(f"{ns} — {len(hors_lot)} ÉCRITURE(S) HORS DU LOT "
                              f"autorisé, dans un fichier de la cliente depuis "
                              f"{DEPUIS} : "
                              f"{[str(r.get('siren')) for r in hors_lot][:6]}")

# L'instantané se pose AVANT le verdict : sinon une coupure figerait la garde sur un
# état périmé, et elle crierait sur la même disparition à chaque passage.
try:
    with open(EMPREINTE, "w", encoding="utf-8") as f:
        json.dump(instantane, f)
except Exception as e:  # noqa: BLE001 — cf. le principe : la garde ne coupe pas sur
    # son propre incident.
    print(f"garde : instantané non posé ({e})")

if not violations:
    print("garde : RAS · " + " · ".join(etat))
    raise SystemExit(0)

print("⚠️ GARDE — VIOLATION")
for v in violations:
    print("   " + v)
with open(ALERTE, "w", encoding="utf-8") as f:
    f.write("\n".join(violations) + "\n")
# ⚠️ COUPER POUR DE VRAI, ET NE L'ANNONCER QU'APRÈS L'AVOIR CONSTATÉ.
#
# Le 29/08, ces deux lignes disaient « flotte ARRÊTÉE » alors que rien ne s'était
# arrêté : l'unité visée n'existait pas (`Unit ... not loaded`), `check=False`
# avalait l'échec, et le message suivait quand même. Douze minutes de violation
# répétée, trois agents qui tournaient, et une trace qui affirmait le contraire.
#
# L'ordonnanceur n'est PAS toujours une unité systemd : selon le lancement, c'est
# un simple processus. Une garde ne peut pas supposer la forme de ce qu'elle
# arrête — elle essaie tout, puis elle REGARDE.
arrets = []
subprocess.run(["systemctl", "stop", FLOTTE], check=False,
               capture_output=True)
arrets.append(("unité " + FLOTTE, subprocess.run(
    ["systemctl", "is-active", FLOTTE], capture_output=True,
    text=True).stdout.strip() not in ("active", "activating")))

# L'ordonnanceur lancé à la main, qui continuerait à remplir la file.
subprocess.run(["pkill", "-f", "oto_runner.fleet"], check=False)
arrets.append(("ordonnanceur", subprocess.run(
    ["pgrep", "-f", "oto_runner.fleet"], capture_output=True).returncode != 0))

# Les agents : ils s'arrêtent en douceur, donc l'arrêt peut prendre le temps du
# travail en cours. On le lance, et on dit franchement qu'il est en cours.
for n in (1, 2, 3):
    subprocess.run(["systemctl", "stop", "--no-block", f"oto-runner@{n}"],
                   check=False, capture_output=True)
vivants = [n for n in (1, 2, 3) if subprocess.run(
    ["systemctl", "is-active", f"oto-runner@{n}"], capture_output=True,
    text=True).stdout.strip() in ("active", "activating", "deactivating")]

for quoi, ok in arrets:
    print(f"   {quoi} : {'arrêté' if ok else '⚠️ TOUJOURS VIVANT'}")
if vivants:
    print(f"   agents {vivants} : arrêt en douceur DEMANDÉ — ils finissent leur "
          f"travail en cours puis sortent. Ils ne réservent plus rien.")
else:
    print("   agents : tous arrêtés")

if any(not ok for _, ok in arrets):
    print("   ⚠️ LA COUPURE A ÉCHOUÉ EN PARTIE — la flotte peut continuer. "
          "Intervention manuelle requise.")
    with open(ALERTE, "a", encoding="utf-8") as f:
        f.write("COUPURE INCOMPLÈTE : voir le journal de la garde\n")
raise SystemExit(1)
