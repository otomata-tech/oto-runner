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

C'est `scripts/flotte.sh lancer` qui le fait, et pas autrement (cf. plus bas). Ce
qu'il pose, en substance :

```bash
# 1. déclarer la campagne HORS de l'unité — l'id seul sur la sortie standard
FID=$(/opt/oto-runner/.venv/bin/python -m oto_runner.fleet --declarer "$FLEET.yaml")
# 2. l'unité reçoit l'id : une relance REPREND la campagne, elle n'en ouvre pas une autre
systemd-run --unit="oto-fleet-$FLEET" --working-directory=/opt/oto-runner \
  --property=EnvironmentFile=/opt/oto-runner/.env \
  --property=Restart=on-failure --property=RestartSec=10min \
  --property=StartLimitIntervalSec=6h --property=StartLimitBurst=6 \
  --property=RestartPreventExitStatus=3 \
  /opt/oto-runner/.venv/bin/python -m oto_runner.fleet "$FLEET.yaml" "#$FID"
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

**Depuis, la règle n'est plus à tenir : `scripts/flotte.sh` la rend mécanique.**
`flotte.sh lancer` arme la garde **avant** la flotte et renonce au lancement si elle
n'a pas pu l'armer ; `flotte.sh arreter` arrête la flotte **puis** sa garde. On ne
peut plus obtenir l'un sans l'autre en se trompant de geste.

⚠️ **Mettre une garde en pause n'existe pas, et ce n'est pas un choix de style.**
Ses unités sont **transitoires** : `systemctl stop` ne les suspend pas, il les
**supprime**. Le 2026-08-29, des gardes mises « en pause » le temps d'essais ont été
détruites — sans conséquence, rien ne tournant, mais rien ne le disait avant de le
faire, et rien ne l'aurait dit après. Il n'existe donc qu'un geste : **arrêter la
campagne**, qui emporte sa garde. Vouloir désarmer seul, c'est déjà se tromper.

## Relance automatique de l'ordonnanceur

Depuis le 21/09/2026, l'unité de l'ordonnanceur (`oto-fleet-<nom>`) se **relance
seule** après une panne passagère. ⚠️ **Seules les flottes lancées par un
`flotte.sh` postérieur à ce changement en bénéficient** : les propriétés d'une
unité transitoire sont fixées à sa création, et une flotte déjà en cours garde les
siennes — aucune relance.

| sortie de l'ordonnanceur | relancé ? |
|---|---|
| exit 0 — file vide, volume atteint, budget atteint, **arrêt demandé** (`op=stop` obéi) | non : fin normale |
| exit 1 — panne : transport, 5xx, `no_runner_armed`, backend indisponible, échecs consécutifs, outil critique en échec, budget non suivable | **oui**, 10 min après |
| exit 3 — abandon définitif : campagne arrêtée, `404`, `409 fleet_not_serving`, armement ou prise refusés pour une cause que le serveur nomme, campagne tenue par un autre ordonnanceur (`409 held_by_other`, `409 not_the_holder`), `OTO_FLEET_HOLDER` absent | non (`RestartPreventExitStatus=3`) |
| `systemctl stop` — `flotte.sh arreter`, les gardes | non — et l'arrêt annule aussi une relance en attente |

**Pourquoi ces valeurs.** `RestartSec=10min` dépasse la tolérance interne du
driver (~3-4 min de panne dense avant qu'il sorte) et le bail d'une ligne : les
travaux en vol de la vie précédente — que l'ordonnanceur relancé ne suit plus —
ont fini avant qu'il ré-enfile, la concurrence n'est pas doublée.
`StartLimitBurst=6` sur `StartLimitIntervalSec=6h` : au plus **6 démarrages en
6 h, le premier compris**, soit 5 relances et au moins une heure de panne continue
couverte. L'intervalle doit dépasser 5 × `RestartSec`, sinon la limite ne mord
jamais ; un test le tient.

**Au-delà de la limite**, l'unité reste `failed` et le journal dit « Start request
repeated too quickly » : un humain regarde. Une fois la cause corrigée,
`systemctl start oto-fleet-<nom>` rejoue la même commande — donc **reprend la même
campagne** — mais seulement une fois la fenêtre de 6 h écoulée. `systemctl
reset-failed` lève la limite tout de suite, mais **décharge** l'unité transitoire :
il ne reste alors que `flotte.sh lancer`, qui déclare une campagne **neuve**.

**La reprise.** Relancé, l'ordonnanceur rejoue `launch` (refusé `not_launchable`,
toléré) puis `take`, que le backend **accepte** parce que la campagne est tenue par
le même preneur — `OTO_FLEET_HOLDER`, figé dans l'unité à `<machine>/oto-fleet-<nom>`,
identique à chaque relance — et repart. Tenue par un autre, la prise est refusée
`409 held_by_other` : abandon définitif, rien d'enfilé. Ce qui ne survit pas à la relance : les travaux en vol
(plus suivis, leur coût n'entre plus dans le bilan), le compte de jetons du
budget et la base du volume, recomptés depuis zéro — la borne `budget_tokens`
vaut donc **par vie** de l'ordonnanceur, pas par campagne. Les lignes, elles, ne
sont pas travaillées deux fois : un travail n'est jamais attaché à une ligne à
l'enfilement, c'est l'agent qui en réserve une sous bail.

**L'ordre de déploiement du preneur** (oto-backend#1032, 21/09/2026). Les deux
versions ne se parlent pas : un runner qui envoie `taken_by` à un backend qui ne
le déclare pas est refusé (`400 unknown_fields`) ; un runner qui ne l'envoie pas à
un backend qui l'exige l'est aussi (`400 missing_fields`) — et là, c'est pire
qu'un refus de départ : ses battements échouent en « état muet », sa campagne
tourne **en aveugle** et `op=stop` n'est plus lu. D'où l'ordre, sans exception :

1. la migration `0003_runner_fleets_preneur` jouée à la main sur la base (avant la
   fusion du backend : le code qui la lit en dépend) ;
2. oto-backend#1032 fusionné et déployé ;
3. seulement alors cette version du runner, et les flottes relancées par le
   nouveau `flotte.sh`.

⚠️ **Au moment du tag de prod du backend, aucun ordonnanceur de l'ancienne version
ne doit tourner** : arrêter (`flotte.sh arreter`) ou laisser finir les flottes en
cours avant le tag, les relancer après. Et une unité posée par un `flotte.sh`
antérieur ne porte pas `OTO_FLEET_HOLDER` : relancée par systemd sur le nouveau
code, elle refuse de démarrer (exit 3, sans relance) — la relancer par
`flotte.sh lancer`.

**Arrêter proprement, sans relance** : `flotte.sh arreter <nom>` (un `systemctl
stop` ne relance jamais — vérifié, y compris pendant l'attente d'une relance), ou
`op=stop` depuis la plateforme : l'ordonnanceur cesse d'enfiler, laisse finir ce
qui est en vol, accuse l'arrêt et sort en 0. ⚠️ Un `kill -9` n'est **pas** un
arrêt : SIGKILL compte comme une panne, et l'unité relance l'ordonnanceur.

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

## Mesurer : emprunter le client du produit

⚠️ **Un outil de mesure qui refait son propre client refait les bugs déjà corrigés
dans le produit — et les impute au produit.** `scripts/sonde.py` porte le client à
emprunter ; un script d'analyse n'en écrit pas un autre.

Trois fois en deux jours (28-29/08), un instrument a rapporté un défaut qu'il
**fabriquait lui-même** :

| l'instrument | ce qu'il affirmait | ce qui était vrai |
|---|---|---|
| une garde | « flotte ARRÊTÉE », cinq fois | rien n'était arrêté |
| une vérification | « ordonnanceur vivant » | la commande se comptait elle-même |
| quatre scripts d'analyse | « 203 outils servent du charabia » | ils lisaient `r.text` |

Le dernier mérite son détail, parce qu'il est le plus facile à refaire : le flux MCP
annonce `text/event-stream` **sans charset**. La bibliothèque HTTP retombe alors sur
latin-1, et un UTF-8 valide devient du mojibake. Le produit fait
`r.content.decode("utf-8")` **et le commente** ; le script jetable, écrit à côté, ne
le savait pas. L'alerte a été diffusée avant vérification des octets bruts.

**Mesurer avec le même client que le produit n'est pas une commodité : c'est ce qui
rend la mesure opposable.**

Corollaire pour les empreintes : elles se calculent sur du texte **décodé**. Une
empreinte calculée sur du charabia reste comparable — le charabia est déterministe —
mais le jour où le décodage se corrige, **des centaines d'outils paraissent modifiés
d'un coup** sans qu'aucun n'ait changé. C'est arrivé : 253 « modifications », dont
244 dues à la seule correction de lecture.

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
