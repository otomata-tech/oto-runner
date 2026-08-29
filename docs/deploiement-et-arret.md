# Déployer sans tuer un travail en cours

Le runner n'est pas un service qu'on redémarre à la légère : ses agents tiennent des
**lignes sous bail** et paient des jetons. Un agent tué au milieu d'un travail laisse
sa ligne bloquée jusqu'à l'expiration du bail, et fait **repayer** le travail. Cette
page dit ce qui garantit que ça n'arrive plus, et à quel prix.

## Ce qui tourne

- **Trois agents permanents** (`oto-runner@{1,2,3}`), processus systemd, boucle
  « réserver un travail → le faire → recommencer ».
- **Un ordonnanceur de flotte** (`python -m oto_runner.fleet <flotte>.yaml`), lancé à
  la demande, qui dépose les travaux dans la file en gardant N en vol.

Déployer redémarre **les agents**, jamais l'ordonnanceur : celui-ci survit au
déploiement et continue d'alimenter la file pendant que les agents se relaient.

## L'arrêt en douceur

Au signal d'arrêt, un agent **ne s'interrompt pas** : il lève un drapeau, finit le
travail qu'il tient, et sort. Il ne réserve rien de plus. Le journal l'écrit —
`agent sorti proprement — aucun travail interrompu`.

Ce que ça change, mesuré sur une campagne réelle le 2026-08-29 :

| | avant | après |
|---|---|---|
| travaux tués par un déploiement | 3 | **0** |
| lignes laissées sous bail | 3 | **0** |
| travail repayé | 3 | **0** |
| flotte à l'arrêt | ~15 s | **2 min 47 s** |

**Le prix d'un déploiement n'est plus du travail perdu, c'est de l'attente.** Et cette
attente vaut exactement la durée du travail le plus long en vol — ici 167 secondes.
C'est ce qui rend une correction en pleine campagne acceptable : mieux vaut trois
minutes d'attente qu'une valeur fausse écrite dans un fichier client.

⚠️ **Le drapeau se vérifie aussi APRÈS la réservation.** Le signal peut tomber entre
la décision de réserver et le retour du serveur : dans cette fenêtre, l'agent rend la
ligne sans l'entamer plutôt que de la garder sous bail pendant qu'il s'éteint.

## La chaîne de nombres qui doit rester cohérente

Trois durées, dans cet ordre, et **l'ordre est la garantie** :

```
durée d'un travail  <  bail de la ligne (10 min)  <  patience de systemd (16 min)
```

- **Le bail** borne combien de temps une ligne reste bloquée si l'agent meurt vraiment.
- **La patience de systemd** (`TimeoutStopSec=16min`) doit dépasser le bail, sinon
  systemd tue un agent qui avait encore le droit de finir — et l'arrêt en douceur
  devient une promesse que la machine ne tient pas.

**Toucher à l'une des trois sans les autres casse la propriété en silence** : rien
n'échoue, les travaux se font tuer à nouveau, et le journal n'écrit plus la ligne de
sortie propre. C'est le seul témoin — son absence est le signal.

## Lancer une flotte : en unité, JAMAIS à la main

```bash
systemd-run --unit="oto-fleet-$FLEET" --working-directory=/opt/oto-runner \
  --property=EnvironmentFile=/opt/oto-runner/.env \
  /opt/oto-runner/.venv/bin/python -m oto_runner.fleet "/opt/oto-runner/$FLEET.yaml"
```

⚠️ **Un ordonnanceur lancé directement dans une connexion à distance MEURT avec
elle.** Le gestionnaire de session s'éteint dès qu'il ne reste plus aucune
connexion ouverte, et il emporte tout ce qui tournait dedans — sans borne, sans
bilan final, sans une ligne dans le journal qui dise pourquoi.

**Vécu le 2026-08-29** : une campagne de cent lignes lancée à la main a tourné trois
heures, puis s'est arrêtée à `03:17:42`, à trois lignes de la fin. Le journal système
dit tout :

```
03:17:42  Stopping User Manager for UID 0...
03:17:42  Stopped User Manager for UID 0.
```

Le gestionnaire s'est arrêté **81 fois entre 00:30 et 03:45** — une fois par
connexion refermée. La campagne n'a pas survécu trois heures parce qu'elle tenait :
elle a survécu parce que les connexions se succédaient assez vite. **Elle est morte
au premier trou.** Sur un lot de plusieurs jours, ce trou arrive la première nuit.

**Le même écart produit un second défaut, et c'est ce qui le rend traître** : les
gardes arrêtent la flotte *par le nom de son unité*. Pas d'unité, pas d'arrêt
possible — la garde a détecté la violation qu'elle surveillait, tenté d'arrêter une
unité inexistante, et **annoncé « flotte ARRÊTÉE »** toutes les deux minutes pendant
que les agents continuaient. Une flotte mortelle et une garde aveugle sur elle, pour
un seul `systemd-run` oublié.

**Le script existait.** `essai-reprise.sh` et `palier.sh` lancent en unité depuis le
début. Ce n'est pas une règle de plus : c'est le rappel que la règle était déjà
écrite, et qu'une exception « juste pour cette fois » a coûté une campagne.

## Ce qui fait foi

Un travail rend un **relevé d'exécution** — jetons, outils appelés, lignes réservées,
lignes écrites, et `ligne_abandonnee` quand le harnais a dû marquer un abandon.

⚠️ **Le relevé fait foi, jamais un champ de données ni un texte.** Deux façons de se
tromper, vécues à une semaine d'intervalle :

- **un champ écrit peut mentir** — une estampille posée à la main affirmait un modèle
  que le travail n'avait pas utilisé ; c'est le relevé qui disait vrai ;
- **un texte peut cesser de correspondre** — compter les abandons en cherchant une
  formule dans un motif marche jusqu'au jour où la formule change d'un mot. Ce
  jour-là le comptage rend zéro **sans rien signaler**, et un comptage qui ne trouve
  rien ressemble exactement à un comptage qui n'a rien à trouver.

Dans les deux cas la règle est la même : **ce qui a été enregistré en agissant prime
sur ce qu'un champ raconte après coup.** Le champ reste la vérification croisée — s'il
diverge du relevé, il y a autre chose à comprendre.

## Pièges vécus

⚠️ **Un déploiement en pleine campagne était interdit par DISCIPLINE, pas par le
système** (« ne pas pousser pendant une campagne »). Une règle qu'un humain doit tenir
finit par céder : elle a cédé le 2026-08-28, trois travaux tués, seize minutes perdues
à attendre l'expiration des baux. La règle n'a pas été renforcée — elle a été
**remplacée par une propriété de la machine**.

⚠️ **Une garde d'intégrité se réarme dans le geste qui modifie légitimement ce qu'elle
surveille**, jamais à la main. Le rechargement d'un tableau supprime et recrée des
lignes ; une garde qui compte les lignes y voit une hémorragie et coupe la flotte — ce
qu'elle a fait, une minute après un lancement, en ayant parfaitement raison sur les
faits et complètement tort sur la situation.

⚠️ **`Restart=on-failure`, et non `always`** : un agent qui sort proprement doit
RESTER sorti le temps que le déploiement installe la version neuve. Avec `always`,
systemd le relance aussitôt sur l'ancien code, et le déploiement redémarre un
processus qui ne s'était jamais vraiment arrêté.
