#!/bin/bash
# Lancer et arrêter une flotte — flotte et garde dans le MÊME geste.
#
# ⚠️ Ce script existe parce que séparer les deux a coûté une campagne le
# 2026-08-29. Deux erreurs, une seule racine :
#
#   · la flotte lancée à la main, hors service, est morte avec la connexion qui
#     l'avait lancée — sans borne, sans bilan, à trois lignes de la fin ;
#   · la garde, qui arrête une flotte PAR LE NOM DE SON UNITÉ, n'a trouvé aucune
#     unité à arrêter. Elle a détecté sa violation, échoué à couper, et annoncé
#     « flotte ARRÊTÉE » toutes les deux minutes pendant que tout continuait.
#
# D'où la règle que ce script rend mécanique : **on ne lance pas une flotte sans
# sa garde, et on n'arrête pas une garde sans sa flotte.**
#
# ⚠️ Et surtout : METTRE UNE GARDE EN PAUSE N'EXISTE PAS. Ses unités sont
# TRANSITOIRES — `systemctl stop` ne les suspend pas, il les SUPPRIME
# définitivement. Il n'y a donc pas de geste « je désarme le temps d'un essai » :
# il y a « j'arrête la campagne », qui emporte sa garde, et rien d'autre.
#
# Usage :
#   flotte.sh lancer <nom> <fichier.yaml> <ref-a> <ref-b> <ref-c>
#   flotte.sh arreter <nom>
#   flotte.sh etat <nom>

set -u
RACINE=/opt/oto-runner
PY="$RACINE/.venv/bin/python"

geste=${1:-}
nom=${2:-}
[ -n "$geste" ] && [ -n "$nom" ] || { sed -n '/^# Usage/,/^$/p' "$0"; exit 2; }

FLOTTE="oto-fleet-$nom"
GARDE="garde-vivier-$nom"
PROFILS="garde-profils-$nom"
# ⚠️ Le tableau surveillé est LU DANS LA DÉCLARATION de flotte, pas supposé.
# Il valait `copie-eval-palier100` par défaut quelle que soit la flotte : pour
# un passage sur un autre tableau, la garde des profils aurait surveillé le
# miroir — armée, active, et regardant ce que personne ne touche. Inerte, et
# rassurante. Un garde-fou qui dépend d'une variable à ne pas oublier est une
# friction, et la friction se contourne.
NS_MIROIR="${NS_MIROIR:-}"

case "$geste" in
lancer)
  yaml=${3:?fichier de flotte}; a=${4:?référence a}; b=${5:?référence b}; c=${6:?référence c}
  [ -f "$yaml" ] || { echo "fichier de flotte introuvable : $yaml"; exit 1; }
  # ⚠️ Le lot est lu ICI, avant tout le reste : la garde du vivier le reçoit
  # bien avant l'export, et l'ordre du script suivait l'ordre d'écriture des
  # correctifs plutôt que l'ordre des besoins (`_lot: unbound variable`).
  _lot=$(sed -n 's/^[[:space:]]*lot_test:[[:space:]]*//p' "$yaml" | head -1)
  _arg_lot=""
  if [ -n "$_lot" ]; then
    _arg_lot="lot_test=$_lot"
    echo "instantané : restreint au lot $_lot"
  fi
  # Le tableau que la garde des profils doit surveiller : celui que la flotte
  # travaille, lu dans sa déclaration. Une valeur passée à la main garde la
  # priorité, mais on ne dépend plus d'elle.
  if [ -z "$NS_MIROIR" ]; then
    NS_MIROIR=$(sed -n 's/^namespace:[[:space:]]*//p' "$yaml" | head -1)
  fi
  [ -n "$NS_MIROIR" ] || { echo "ABANDON : pas de 'namespace' dans $yaml — la garde des profils n'aurait rien à surveiller."; exit 1; }
  echo "garde des profils : surveille « $NS_MIROIR » (lu dans la déclaration)"
  # ⚠️ Un lancement AVORTÉ laisse son minuteur derrière lui, et le suivant se
  # voit refuser « unit already exists » — donc refuser de lancer, faute de
  # pouvoir armer sa garde. Le refus est le bon comportement ; le résidu ne l'est
  # pas. On nettoie donc AVANT, au lieu de laisser un état intermédiaire bloquer
  # le geste suivant.
  systemctl stop "$GARDE.timer" "$GARDE" "$PROFILS.timer" "$PROFILS" 2>/dev/null
  systemctl reset-failed "$FLOTTE" "$GARDE" "$PROFILS" 2>/dev/null

  # ⚠️ LE CODE DÉPLOYÉ D'ABORD : on ne lance pas une campagne sur un dépôt en
  # retard, parce qu'on la corrigerait EN VOL.
  #
  # Le 29/08, j'ai déployé deux fois pendant un passage — dont une modification
  # du texte donné aux agents. Les travaux d'avant et d'après n'avaient pas reçu
  # la même instruction : le passage a été abandonné à douze fiches. J'avais fait
  # reporter deux mises en production le même jour pour cette raison exacte, et
  # la règle a cédé chez celui qui l'imposait.
  #
  # Une règle qu'un humain doit tenir finit par céder. Celle-ci ne dépend donc
  # plus de personne.
  git -C "$RACINE" fetch origin main --quiet 2>/dev/null || true
  retard=$(git -C "$RACINE" rev-list --count HEAD..origin/main 2>/dev/null || echo 0)
  if [ "${retard:-0}" != "0" ]; then
    echo "ABANDON : $retard commit(s) non déployé(s) sur cette box."
    echo "  Une campagne lancée sur du code en retard se corrige EN VOL, et un"
    echo "  passage corrigé en vol ne se juge plus. Déploie, puis relance."
    git -C "$RACINE" log --oneline HEAD..origin/main | head -5
    exit 1
  fi
  echo "code déployé : à jour avec origin/main ✅"

  # ⚠️ LOT EXIGÉ HORS DES TABLEAUX D'ESSAI
  #
  # Le cran comparatif ci-dessous ne voit rien quand les deux valeurs sont
  # fausses ensemble : deux périmètres larges et d'accord le satisfont. Cette
  # borne-ci ne compare rien — elle exige une forme, et se trompe du côté qui
  # gêne : un tableau d'essai oublié dans la liste bloque le départ ; un fichier
  # de cliente oublié, lui, reste exigeant. La liste est LUE dans la garde, pas
  # recopiée.
  _essai=$("$PY" - "$NS_MIROIR" <<'PYESSAI'
import ast, os, sys
p = "/opt/oto-runner/garde-vivier.py"
if not os.path.exists(p):
    print("absent")          # ⚠️ dit par le lecteur, pas déduit d'un vide
    raise SystemExit(0)
src = open(p, encoding="utf-8").read()
noms = ()
for n in ast.parse(src).body:
    if isinstance(n, ast.Assign) and any(
            getattr(t, "id", "") == "TRAVAIL" for t in n.targets):
        noms = tuple(ast.literal_eval(n.value))
print("oui" if sys.argv[1] in noms else "non")
PYESSAI
)
  if [ "$_essai" = "absent" ] || [ -z "$_essai" ]; then
    echo "⛔ REFUS DE LANCER — LISTE DES TABLEAUX D'ESSAI INTROUVABLE"
    echo "   « garde-vivier.py » est absente ou illisible : le cran ne peut pas"
    echo "   savoir si « $NS_MIROIR » est un tableau d'essai. Il refuse plutôt"
    echo "   que de deviner. ⚠️ Cette liste n'est pas versionnée — elle ne vit"
    echo "   que sur la box. Rien n'a été armé."
    exit 1
  fi
  if [ "$_essai" != "oui" ] && [ -z "$_lot" ]; then
    echo "⛔ REFUS DE LANCER — « $NS_MIROIR » n'est pas un tableau d'essai"
    echo "   et la déclaration ne nomme AUCUN lot."
    echo "   Un fichier de cliente ne se travaille jamais en entier : une vague"
    echo "   sans nom ne se compare à rien et ne se défait pas. Rien n'a été armé."
    exit 1
  fi
  if [ "$_essai" = "oui" ] && [ -z "$_lot" ]; then
    echo "tableau d'essai « $NS_MIROIR » sans lot — le tableau EST le périmètre ✅"
  fi

  # ⚠️ LE PÉRIMÈTRE SERVI EST-IL CELUI QU'ON DEMANDE ?
  #
  # Le tableau déclare ce qui est réservable (`lifecycle.claimable`). Un
  # périmètre plus large que le filtre de la flotte ouvre des lignes qu'on
  # n'a pas demandées — un `claimable` réduit à `{statut: a_enrichir}` rendrait
  # les 8 871 lignes du fichier réservables d'un coup, et le lancement partirait
  # content. C'est la garde qui a manqué le 31/08 à 14:07, quand un travail en
  # vol a écrit sur le lot après l'arrêt du passage.
  #
  # On compare donc ce que le tableau SERT à ce que la déclaration DEMANDE, et
  # on refuse en nommant l'écart. Mécanique : personne n'a à se rappeler.
  if [ -n "$_lot" ]; then
    _servi=$("$PY" - "$NS_MIROIR" <<'PYCRAN'
import json, os, sys, urllib.request
ns = sys.argv[1]
H = {"Authorization": "Bearer " + os.environ["OTO_TOKEN"], "X-Oto-Org": "226"}
d = json.load(urllib.request.urlopen(urllib.request.Request(
    "https://mcp.oto.cx/api/datastore/namespaces/%s/schema" % ns,
    headers=H), timeout=90))
c = d.get("schema") if isinstance(d.get("schema"), dict) else d
st = next((f for f in (c.get("fields") or []) if f.get("key") == "statut"), {})
print(((st.get("lifecycle") or {}).get("claimable") or {}).get("lot_test") or "")
PYCRAN
)
    if [ "$_servi" != "$_lot" ]; then
      echo "⛔ REFUS DE LANCER — périmètre de réservation servi : « ${_servi:-AUCUN} »"
      echo "   la déclaration demande le lot « $_lot »."
      echo "   Un périmètre plus large ouvre des lignes qu'on n'a pas demandées ;"
      echo "   un périmètre absent les ouvre TOUTES. Rien n'a été armé."
      exit 1
    fi
    echo "périmètre servi : « $_servi » — conforme à la déclaration ✅"
  fi

  # ⚠️ LES EXÉCUTANTS, avant tout le reste. Le 29/08, le septième départ est
  # parti avec flotte, garde, ordonnanceur et code TOUS VERTS et les trois
  # agents éteints : la flotte a enfilé dans le vide pendant quarante secondes.
  # La vérification regardait chaque pièce du dispositif SAUF celles qui
  # travaillent. Elle refuse — elle ne se contente pas de le signaler, parce
  # qu'un avertissement au milieu de vingt lignes de sortie se manque.
  # Les agents s'arrêtent d'eux-mêmes quand la file est vide : entre deux
  # passages ils sont éteints, c'est normal. Le lancement les démarre — il est
  # responsable de son dispositif — puis il CONSTATE. Il ne refuse que si le
  # démarrage échoue : là, il y a quelque chose à comprendre avant de partir.
  systemctl start oto-runner@1 oto-runner@2 oto-runner@3 2>/dev/null
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    eteints=""
    for i in 1 2 3; do
      [ "$(systemctl is-active "oto-runner@$i")" = "active" ] || eteints="$eteints $i"
    done
    [ -z "$eteints" ] && break
    sleep 2
  done
  if [ -n "$eteints" ]; then
    echo "⛔ REFUS DE LANCER — agents encore éteints après démarrage :$eteints"
    echo "   'journalctl -u oto-runner@${eteints## } -n 30' pour comprendre."
    echo "   Rien n'a été armé, il n'y a rien à défaire."
    exit 1
  fi
  echo "agents : les trois actifs ✅ (démarrés par le lancement)"

  # ⚠️ LA SONDE ENSUITE : c'est le MODÈLE qui dit ce qu'on lui sert.
  #
  # La liste d'outils d'une déclaration de flotte est une DÉCLARATION. Le 29/08,
  # j'ai affirmé qu'elle n'était pas appliquée — sur la foi de 54 appels d'un
  # outil retiré — et j'avais tort : les appels venaient de mon propre harnais.
  # La sonde tranche en une question posée au modèle : nomme les outils que tu
  # vois. **Deux listes qui concordent, c'est une fenêtre mesurée ; une seule,
  # c'est une fenêtre déclarée.**
  #
  # Elle coûte un appel au fournisseur (~9 000 jetons, une trentaine de
  # secondes). C'est le prix d'une fenêtre qu'on peut opposer.
  echo "sonde : ce que le modèle voit…"
  if ! "$PY" "$RACINE/sonde-outils-vus.py" "$yaml"; then
    echo "ABANDON : la sonde ne retrouve pas la liste déclarée — la fenêtre du"
    echo "protocole ne mesure pas ce que les agents voient. On ne lance pas."
    exit 1
  fi

  # La GARDE ENSUITE : une flotte qui tourne une seconde sans surveillance est
  # une seconde pendant laquelle une écriture peut partir sans que rien ne la voie.
  # ⚠️ La fenêtre de la garde démarre AU LANCEMENT, jamais à une date fixe.
  # Le 29/08, son défaut « depuis hier 21:00 » englobait les réparations faites à
  # la main la veille au soir : elle a vu trois écritures légitimes dans le
  # fichier de la cliente et coupé un passage à 2 lignes sur 100. Une garde dont
  # la référence est plus ancienne que le geste qu'elle surveille crie sur le
  # passé — c'est la troisième fois cette semaine, sous trois formes.
  DEPUIS=$(date -u "+%Y-%m-%d %H:%M:%S")
  echo "fenêtre de la garde : depuis $DEPUIS (le lancement, pas une date fixe)"
  systemd-run --unit="$GARDE" --on-calendar="*:0/2" \
    --setenv=GARDE_DEPUIS="$DEPUIS" --setenv=GARDE_LOT="$_lot" \
    --property=EnvironmentFile="$RACINE/.env" --working-directory="$RACINE" \
    "$PY" "$RACINE/garde-vivier.py" "$a" "$b" "$c" "$FLOTTE" >/dev/null || {
      echo "ABANDON : la garde n'a pas pu être créée — on ne lance pas sans elle."
      exit 1; }
  echo "garde $GARDE armée (toutes les 2 min, elle arrête $FLOTTE)"

  # La garde des PROFILS PERSONNELS, armée dans le même geste que les autres.
  # ⚠️ Ce n'est pas un cran : le harnais ne voit pas les appels d'outils sur ce
  # chemin, donc il ne peut pas empêcher une consultation — seulement la
  # constater et arrêter les frais. À deux consultations sur cent, elle coupe
  # toutes les cinquante lignes : tenable pour un jalon, pas pour un lot.
  systemd-run --unit="$PROFILS" --on-calendar="*:0/2" \
    --property=EnvironmentFile="$RACINE/.env" --working-directory="$RACINE" \
    "$PY" "$RACINE/garde-profils.py" "$NS_MIROIR" "$FLOTTE" >/dev/null || {
      echo "ABANDON : garde des profils non armée — on ne lance pas sans elle."
      systemctl stop "$GARDE.timer" 2>/dev/null; exit 1; }
  echo "garde $PROFILS armée (profils personnels)"

  # ⚠️ L'INSTANTANÉ DE RÉFÉRENCE, exporté ICI et par personne d'autre.
  # Le 29/08 il était un geste séparé, à faire avant le lancement — et il a été
  # oublié : pris une minute APRÈS le départ, valide par chance seulement.
  # L'enjeu dépasse la propreté : c'est la référence de TOUS les contrôles
  # d'écart. Pris trop tard, il contient déjà le travail des agents, et ce qu'il
  # contient des deux côtés devient invisible à la comparaison.
  # Placé après les gardes et avant la flotte : dernier instant où le tableau
  # est intact, premier où la surveillance est en place.
  # ATTENTION Le tableau est PASSE : sans lui, l export sauvegardait le miroir
  # quel que soit ce que la flotte travaille. Le 31/08 le jalon est parti sur le
  # fichier de production avec une reference prise sur le tableau d essai — donc
  # sans reprise possible, et la sortie affichait un succes avec une empreinte.
  # ⚠️ Le LOT que la flotte travaille, lu dans sa déclaration : le cran de
  # vacuité de l'export doit porter sur ce qu'il emporte, pas sur le tableau
  # entier. Le 31/08 il a refusé deux départs à cause de 39 lignes d'essais
  # antérieurs qui n'appartenaient pas au lot du jalon — un cran qui refuse
  # pour des lignes qu'il n'emporte pas protège d'un risque imaginaire.
  # ⚠️ REPRISE : un passage qui repart où il s'est arrêté garde SA référence.
  # Ré-exporter ferait échouer le cran de vacuité — à juste titre, puisque des
  # fiches sont déjà écrites — alors que la bonne référence existe déjà et
  # précède ces écritures. On la déclare, on vérifie qu'elle porte le bon
  # tableau, et on le DIT : un instantané repris n'est pas un instantané frais.
  if [ -n "${SOCLE_REPRIS:-}" ]; then
    [ -f "${SOCLE_REPRIS:-}" ] || { echo "⛔ instantané repris introuvable : $SOCLE_REPRIS"; exit 1; }
    _ns_repris=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('namespace') or '')" "${SOCLE_REPRIS:-}" 2>/dev/null)
    if [ "$_ns_repris" != "$NS_MIROIR" ]; then
      echo "⛔ l'instantané repris porte « $_ns_repris », la flotte travaille « $NS_MIROIR »"
      exit 1
    fi
    echo "instantané REPRIS (pas ré-exporté) : $(basename "${SOCLE_REPRIS:-}")"
    echo "   md5 : $(md5sum "${SOCLE_REPRIS:-}" | cut -d' ' -f1)"
    echo "   ⚠️ le bilan compare à CETTE référence, antérieure aux fiches déjà écrites."
  fi
  if [ -n "${SOCLE_REPRIS:-}" ]; then
    _socle="${SOCLE_REPRIS:-}"
  elif ! "$PY" "$RACINE/exporter-socle.py" "$NS_MIROIR" $_arg_lot; then
    echo "ABANDON : instantané non exporté — on ne lance pas sans référence."
    systemctl stop "$GARDE.timer" "$PROFILS.timer" 2>/dev/null
    exit 1
  fi
  _socle=$(ls -t "$RACINE"/socle-*.json 2>/dev/null | head -1)
  # ⚠️ PRÉ-VOL : l'instantané est-il celui du tableau que la flotte va traiter ?
  # Le 31/08 il ne l'était pas — le jalon partait sur la production avec une
  # référence prise sur le tableau d'essai, et la sortie affichait un succès.
  # Le nom est dans les deux ; les comparer coûte une ligne.
  _ns_socle=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(d.get('namespace') or '')" "$_socle" 2>/dev/null)
  if [ "$_ns_socle" != "$NS_MIROIR" ]; then
    echo "⛔ REFUS DE LANCER — l'instantané porte « $_ns_socle », la flotte"
    echo "   travaille « $NS_MIROIR ». Partir ainsi, c'est partir SANS RÉFÉRENCE :"
    echo "   aucune reprise ne serait possible, et on ne le saurait qu'en essayant."
    systemctl stop "$GARDE.timer" "$PROFILS.timer" 2>/dev/null
    exit 1
  fi
  echo "pré-vol : l'instantané est bien celui de « $NS_MIROIR » ✅"
  echo "socle exporté par le lancement : $(basename "$_socle")"
  echo "   md5 : $(md5sum "$_socle" | cut -d" " -f1)"
  echo "   ⚠️ à DÉPOSER dans le dossier partagé de la mission avec son empreinte :"
  echo "      c'est là, et seulement là, qu'un instantané est un fait."

  systemd-run --unit="$FLOTTE" --property=EnvironmentFile="$RACINE/.env" \
    --working-directory="$RACINE" "$PY" -m oto_runner.fleet "$yaml" >/dev/null || {
      echo "ABANDON : flotte non lancée — je retire la garde que je venais d'armer."
      systemctl stop "$GARDE.timer" 2>/dev/null; exit 1; }
  sleep 2
  echo "flotte $FLOTTE : $(systemctl is-active "$FLOTTE")"
  ;;

arreter)
  # L'ordre inverse : la flotte d'abord, sa garde ensuite. Retirer la garde en
  # premier laisserait la flotte tourner sans surveillance le temps de l'arrêt.
  systemctl stop "$FLOTTE" 2>/dev/null
  systemctl stop "$GARDE.timer" "$PROFILS.timer" 2>/dev/null
  for n in 1 2 3; do systemctl stop --no-block "oto-runner@$n"; done
  echo "flotte $(systemctl is-active "$FLOTTE") · garde retirée · agents en arrêt"

  # ⚠️ « Flotte arrêtée » N'EST PAS « zéro travail en vol ». Ce qui était déjà
  # parti finit après : le 29/08, un travail écrivait encore dix-sept secondes
  # après l'arrêt de la garde, et un autre est resté réservé cinquante secondes
  # après l'arrêt de la flotte. L'état de l'unité est un STOCK ; les travaux en
  # vol sont le DÉBIT, et c'est lui qui décide quand on peut lire ou repartir.
  #
  # Compte pour l'export de l'instantané : exporté trop tôt, il prendrait une
  # écriture née d'avant l'arrêt — la référence contaminée par ce qu'elle mesure.
  echo "attente du dernier travail en vol…"
  for _ in $(seq 1 60); do
    reserves=$("$PY" - <<'PY' 2>/dev/null
import json, os, urllib.request
h = {"Authorization": "Bearer " + os.environ["OTO_TOKEN"],
     "Content-Type": "application/json"}
r = urllib.request.Request("https://mcp.oto.cx/api/me/runner/jobs", headers=h,
                           data=json.dumps({"op": "list", "limit": 50}).encode())
print(sum(1 for j in json.load(urllib.request.urlopen(r, timeout=90))["jobs"]
          if j.get("status") == "claimed"))
PY
)
    actifs=$(systemctl is-active oto-runner@1 oto-runner@2 oto-runner@3 | grep -c '^active')
    if [ "${reserves:-1}" = "0" ] && [ "$actifs" = "0" ]; then
      echo "✅ zéro travail en vol, CONSTATÉ à $(date -u '+%H:%M:%S UTC')"
      break
    fi
    sleep 5
  done
  [ "${reserves:-1}" = "0" ] || echo "⚠️ des travaux sont ENCORE en vol — ne rien lire, ne rien exporter."
  echo "⚠️ La garde est SUPPRIMÉE, pas suspendue : la relancer demande 'lancer'."
  ;;

etat)
  echo "flotte  : $(systemctl is-active "$FLOTTE")"
  # ⚠️ « inactive » et « absente » ne sont PAS la même chose, et les confondre
  # est la faute que ce script combat : une garde supprimée se lit « inactive »
  # comme une garde simplement au repos. On regarde donc si l'unité EXISTE.
  charge=$(systemctl show "$GARDE.timer" -p LoadState --value 2>/dev/null)
  if [ "$charge" = "not-found" ] || [ -z "$charge" ]; then
    echo "garde   : ABSENTE — supprimée ou jamais armée. Une flotte ne se lance pas ainsi."
  else
    echo "garde   : $(systemctl is-active "$GARDE.timer")"
  fi
  echo "agents  : $(systemctl is-active oto-runner@1 oto-runner@2 oto-runner@3 | tr '\n' ' ')"
  echo "ordonn. : $(ps -eo args | grep -c '[o]to_runner.fleet') processus"
  ;;

*) echo "geste inconnu : $geste"; exit 2 ;;
esac
