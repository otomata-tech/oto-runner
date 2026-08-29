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
NS_MIROIR="${NS_MIROIR:-copie-eval-palier100}"

case "$geste" in
lancer)
  yaml=${3:?fichier de flotte}; a=${4:?référence a}; b=${5:?référence b}; c=${6:?référence c}
  [ -f "$yaml" ] || { echo "fichier de flotte introuvable : $yaml"; exit 1; }
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
    --setenv=GARDE_DEPUIS="$DEPUIS" \
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
