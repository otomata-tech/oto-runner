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

case "$geste" in
lancer)
  yaml=${3:?fichier de flotte}; a=${4:?référence a}; b=${5:?référence b}; c=${6:?référence c}
  [ -f "$yaml" ] || { echo "fichier de flotte introuvable : $yaml"; exit 1; }
  systemctl reset-failed "$FLOTTE" "$GARDE" 2>/dev/null

  # La GARDE D'ABORD : une flotte qui tourne une seconde sans surveillance est
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
  systemctl stop "$GARDE.timer" 2>/dev/null
  for n in 1 2 3; do systemctl stop --no-block "oto-runner@$n"; done
  echo "flotte $(systemctl is-active "$FLOTTE") · garde retirée · agents en arrêt"
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
