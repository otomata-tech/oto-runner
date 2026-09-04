# oto-runner

Le **worker externe** des runs hébergés oto (chantier runner, R2) : il réclame des
jobs, joue la boucle agent (modèle ↔ outils), et appose chaque tour au **fil** du
run. Un process = un worker ; N processes = la batterie.

## Le design en une règle

Le runner est un **client pur** — trois contrats, zéro import du backend :

1. **l'API de fil et de jobs** (REST `POST /api/me/runs/thread`, `/api/me/runner/jobs`) ;
2. **la face MCP** (`mcp.oto.cx/mcp`, jeton `oto_`) — credential, RBAC, activation,
   rédaction et journal s'appliquent CÔTÉ SERVEUR au passage de chaque appel,
   précisément parce que le worker est un client comme un autre ;
3. **une clé de modèle** (env — V1 : un worker = une org, c'est la clé qui décide
   qui paie).

Si ces contrats tiennent, le worker est remplaçable. La mort d'un worker n'est
jamais un événement : le bail du job expire, un pair re-claime, **recharge le fil**
(`provider_raw` rejoués verbatim) et continue.

## ⚠️ Cran d'armement

Sans `OTO_RUNNER_ARMED=1`, le worker refuse de démarrer. Le premier run hébergé
réel est gaté par une relecture d'architecture — ce cran rend la gate mécanique.

## Environnement

```
OTO_BASE=https://mcp.oto.cx          # REST (fil + jobs)
OTO_MCP_URL=https://mcp.oto.cx/mcp   # face MCP (outils)
OTO_TOKEN=oto_…                      # jeton non porté du compte worker (une org)
ANTHROPIC_API_KEY=…                  # la clé de modèle = qui paie
OTO_RUNNER_MODEL=claude-sonnet-5     # défaut assumé (coût) ; Opus par flotte si justifié
OTO_RUNNER_ARMED=1                   # cf. ci-dessus
OTO_RUNNER_FAUX_DEPARTS_DIR=…        # optionnel : où déposer la sortie d'un faux départ
OTO_RUNNER_RELANCES_MAX=0            # relances d'un fil qui rend un appel au client
```

Le dépôt des faux départs est un outil de diagnostic qu'on arme : variable
absente, rien n'est écrit. Posée, chaque job conclu en faux départ y laisse un
`<job_id>.json` — outils appelés, **texte final intégral** de l'agent, et les
entrées brutes du fournisseur quand il en rend : de quoi dire s'il a rédigé sa
fiche en prose sans appeler l'écriture, s'il a renoncé, ou où le fil s'est
arrêté. ⚠️ Ce fichier porte de la **donnée de la file de travail** : il est
écrit en 0600, et **se purge après lecture**.

En mode Conversations, les outils sont exécutés par le connecteur : un appel
d'outil qui REVIENT au client interrompt le fil, et le job conclut sans avoir
rien écrit après avoir payé le run entier. `OTO_RUNNER_RELANCES_MAX` (défaut
**0**, donc inactif) autorise autant de relances du fil — on répond à l'appel
rendu et on repart de là.

## Cache de prompt (Anthropic)

Le modèle est sans état : chaque tour lui renvoie la procédure, les schémas
d'outils et tous les résultats d'outils déjà lus. Le provider Anthropic pose donc
**trois points de cache** (limite dure : 4) — dernier bloc du `system`, dernière
définition d'`tools`, dernier bloc du dernier message — et une lecture en cache
se facture ~0,1× le prix d'entrée. ⚠️ Un préfixe sous le minimum du modèle (1024
jetons sur Sonnet 5) n'est **pas** caché, sans erreur ni avertissement : le seul
juge est `usage_cache_read` au résultat du job.

## Un job

`start` : `{procedure, project_id, tools: […], input?, label?, max_steps?}` —
le worker ouvre le run, le lie au job, charge la procédure d'oto (jamais copiée),
joue la boucle sur un fil neuf. `continue` : `{run_id, input?}` — il recharge le
fil et continue ; `input` absent = reprise pure après une mort en plein tour.
Un job porte des **références, jamais un secret**.

### Session MCP perdue, et le job qui n'a rien écrit

Une session MCP ne survit pas au **redéploiement** du service : le serveur ne la
connaît plus (`-32600` « Session not found ») et tous les appels d'outils
suivants échouent d'un coup. L'agent lit ça comme une réponse, l'annonce
proprement et conclut — job « done » sans une écriture, donc **jamais rejoué**,
et la ligne reste « à traiter » sans que personne ne le sache (2 fiches perdues
en silence le 28/08).

Deux crans. Le client MCP **rouvre** la session et rejoue l'appel — une seule
fois par appel, journalisée, jamais en boucle ; un `-32600` n'ayant pas été
exécuté, le rejeu ne peut pas doubler une écriture. Si la réouverture échoue, il
**lève**. Et le worker **échoue le job** (`ok=False`) dès lors qu'il a réservé
une ligne, n'a rien écrit, et porte des appels morts au transport : le backend
le rejoue. Il n'existe pas d'issue légitime « conclu, rien écrit ».

## La flotte

`python -m oto_runner.fleet flotte.yaml` enfile des jobs `start` sur une file de
travail datastore et s'arrête sur une **borne** (déclaration complète :
`docs/fleet-example.yaml`). Le **nom du fichier** sans son extension est le nom
de la flotte : il est apposé à chaque job (`fleet`), et c'est par lui qu'on
retrouve les jobs d'une campagne — plus par « id ≥ N ».

Bornes **normales** (exit 0) : file vide, volume atteint, budget atteint. Toute
autre borne est une **panne** — exit 1, pour que systemd relance la campagne
quand la panne passe : échecs consécutifs, backend indisponible, outil critique
en échec, faux départs en série, **rendement effondré**.

Le plafond par LIGNE borne le prix d'un passage là où il dérape vraiment — une
ligne seule a coûté **65 571 jetons** le 01/09 :

```yaml
max_tokens_per_row: 40000   # plafond de jetons d'UNE ligne (absent ⟹ pas de borne)
```

Il est **descendu dans chaque travail enfilé**, donc appliqué par l'agent
lui-même : il vaut quel que soit le chemin qui a mis la ligne en file, y compris
sans ordonnanceur. L'agent s'arrête sur `stopped: max_tokens`, conclut, et la
ligne suivante repart à zéro. ⚠️ Ce qui compte dans ce total est ce qui est
**facturé** — entrée non cachée + sortie + écriture de cache : les jetons *lus*
en cache coûtent une fraction et gonfleraient le compteur d'un facteur trois sur
un déroulé bien caché, coupant les passages les plus économes.

Effet mesuré sur une campagne réelle (02/09) : sans borne, médiane 59 123 jetons
et **maximum 265 658**, dispersion ×48 ; borné à 80 000, médiane 86 303 et
maximum 107 204 — plus aucune ligne folle.

> ⚠️ **Corrigé le 02/09.** Ce paragraphe a décrit du 27/08 au 02/09 une borne de
> « rendement » — coût rapporté aux écritures produites, sur une fenêtre
> glissante — sous les noms `jetons_par_ecriture_max` et `rendement_fenetre`.
> **Ces deux réglages n'ont jamais existé dans le code** : le mécanisme a été
> conçu puis remplacé par la borne par ligne, sans que ce README suive. Une
> déclaration écrite depuis cette page repartait donc **sans aucune borne**, en
> croyant en avoir une — et le runner avertissait par-dessus que
> `max_tokens_per_row` était « ignoré », ce qui achevait de convaincre. Ce qui
> attrape « ça tourne à vide », ce sont les **faux départs en série**, décrits
> ci-dessus.

Chaque job conclu déclare son coût et sa sortie (`usage_tokens`,
`usage_cache_read`, `usage_cache_write`, `tool_counts`, `claims`, `writes`,
`claim_vide`, `faux_depart`, `model`) : c'est ce que l'ordonnanceur lit, sans jamais ouvrir un
fil. `usage_tokens` reste **input + output** — la base des bornes de flotte
(budget, rendement) ne bouge pas ; le cache se compte à côté.

## Il n'y a PAS d'instruction par défaut

Le worker **exécute** une instruction, il n'en **compose** pas. Il ne sait pas ce
que l'instruction contient ni ce que l'agent va faire — donc il ne peut pas en
écrire une qui vaille. Une campagne sans instruction est **refusée** au chargement.

⚠️ Il en existait une, en dur, sept lignes : *« ta file est ce tableau, réserve
chaque ligne, traite-les selon la procédure, puis conclus »*. Deux défauts, et le
second a coûté cher :

**① Du métier dans le transport** — un tableau, des lignes, une réservation,
alors que le worker ne sait rien de tout ça. Même faute que les crochets retirés
le 03/09.

**② Sa FORME enseignait un court-circuit.** *Réserve → traite → conclus* est une
partition en trois temps où « chercher » n'apparaît nulle part, sinon caché dans
« selon la procédure ». Mesuré dans la nuit du 03 au 04/09 sur des vagues
réelles : **7 jobs sur 11 n'appelaient AUCUN outil** et écrivaient quand même une
fiche complète — le modèle *racontait* les appels au lieu de les émettre, avec
dirigeants et dates inventés, dans un compte rendu parfaitement structuré. Avec
une instruction qui dit d'où viennent les données : **1 sur 9, puis 1 sur 20**, à
consigne et modèle identiques.

⚠️ **Et la garde d'alors visait à côté** : « n'invente jamais une ligne ni un
identifiant » protège l'EXISTENCE d'une ligne, pas le CONTENU d'une fiche.
Inventer un dirigeant ne violait aucune consigne.

### Ce qu'une instruction d'enrichissement doit faire — quatre gestes

⚠️ **Ceci est un savoir de MÉTIER, pas une règle du worker** : il vaut pour les
passages qui enrichissent des fiches depuis des sources ouvertes, pas pour tous.
Il vit ici et dans ce que le serveur DÉRIVE, jamais dans le code du runner — et
le refus de démarrage ne le récite pas, il dit seulement ce qui manque.

**1. Nommer ce que la ligne porte DÉJÀ.** Sans ça l'agent ne distingue pas ce
qu'il a reçu de ce qu'il a trouvé — et les fabrications mesurées sont toutes des
embellissements de données reçues.

**2. Dire que RECOPIER ne compte pas.** Sinon c'est la sortie de moindre effort,
et elle a l'air d'un travail fini.

**3. Nommer les outils comme la SOURCE, pas comme une étape** : « ce que tu
ajoutes vient d'une source ouverte pendant ce run ». C'est le cœur — l'ancienne
version faisait de la recherche une étape d'une partition ; celle-ci en fait la
condition d'existence de la donnée.

**4. Viser la PLAUSIBILITÉ**, qui est le vrai piège : « une fiche écrite sans
avoir appelé ces outils est inventée, même quand elle est plausible, et surtout
quand elle est plausible. » Le court-circuit ne produit pas du délire, il produit
du **crédible**.

⚠️ **Une consigne plus DÉTAILLÉE aggrave le défaut** : à écrire des règles de
vérification de plus en plus fines, on obtient des simulations de plus en plus
convaincantes — la règle récitée mot pour mot pour justifier deux noms inventés.
Ce qui corrige n'est pas une règle de plus, c'est de retirer l'ambiguïté sur *d'où
viennent les données*.

## Ce qu'une campagne déclare est LU, ou son absence est justifiée

Trois champs servis par le serveur étaient **ignorés** par le runner sans que
rien ne le dise : `provider`, `model`, `max_consecutive_failures`.

⚠️ **Les deux premiers sont des étiquettes ; le troisième était une garde.**
Quelqu'un déclare « arrête après N échecs d'affilée », le serveur *valide* la
valeur — et le runner appliquait sa constante. *Une borne déclarée qui n'est pas
appliquée ne se découvre que le jour où on comptait dessus* : elle ne fausse pas
un relevé, elle laisse tourner une campagne qu'on croyait bornée. Et la
validation côté serveur achevait de convaincre qu'elle était prise en compte.

**La borne est désormais lue et appliquée** (`_MAX_FAILED_CONSECUTIFS` n'est plus
qu'un défaut). Les deux étiquettes restent non lues, **avec leur raison écrite** :
le worker est un pool homogène, son fournisseur et son modèle viennent de son
environnement, et un fil commencé chez un fournisseur ne se continue pas chez un
autre.

⚠️ **Et un contrôle tient la classe** (`tests/test_champs_servis_et_lus.py`) :
tout champ servi doit être lu **ou** inscrit comme non lu **avec sa raison**. Un
champ ajouté demain côté serveur et oublié ici fait rougir le contrôle — au lieu
d'attendre qu'un utilisateur s'aperçoive que sa déclaration n'a aucun effet. Avec
le contrôle symétrique : *un champ qu'on finit par lire doit sortir de la liste
des exclus*, sinon elle devient un cimetière qu'on ne relit plus.

*Mesuré avant de corriger : les 14 campagnes déclarées laissaient la borne à
`null`. Personne ne s'était cru protégé — c'est ce qui distingue ce cas d'un
incident.*

## L'état d'un passage, et ce qu'on n'a pas pu dire

La séquence est **déclarer → armer → prendre**. Une flotte naît `draft` ; la
prendre exige `armed`. ⚠️ **L'armement manquait jusqu'au 03/09** : le runner
déclarait puis tentait de prendre, le refus était systématique, rattrapé et
poursuivi. Mesure du jour : **14 campagnes en base, toutes `draft`, aucune jamais
`running`** — alors que huit vagues avaient réellement tourné.

⚠️ **Le défaut n'était pas l'armement manquant, c'était l'échec avalé.** Le
journal disait la vérité à chaque battement — *« le passage tourne quand même,
mais son état ne dira pas en cours »* — et personne ne l'a lue pendant huit
vagues. Une ligne de journal de plus n'y aurait rien changé.

D'où `etat_muet` dans le bilan : **combien de fois le passage n'a pas pu dire où
il en était** (déclaration, armement, prise, battement, accusé d'arrêt). Le
passage continue — perdre l'observabilité vaut mieux qu'une campagne qui refuse
de partir — mais il **conclut** dessus, en erreur, là où on lit le résultat :

```
⚠️ ce passage a tourné EN AVEUGLE : N geste(s) d'état n'ont pas pu être posés.
```

Un passage sain ne dit rien : *une alerte qui se déclenche toujours ne se
distingue pas d'un décor.*

`model` porte ce que le fournisseur **dit avoir servi**, relevé sur le tour
lui-même (le dernier tour fait foi si un alias bascule en cours de déroulé) : un
alias flotte quand le fournisseur le décide, et sans ce champ une anomalie de
campagne ne se date pas — on ignore quels jobs ont tourné avant la bascule et
lesquels après. À défaut de réponse du fournisseur, le worker estampille ce
qu'il a **demandé** : une estampille approchée vaut mieux qu'un `null`, qui ne se
distingue pas d'un job qui n'a jamais tourné.

> ⚠️ **Corrigé le 02/09.** Ce champ était déclaré, lu par le worker et compté par
> le bilan — mais **aucun transport ne le posait** : trois consommateurs, zéro
> producteur, `null` sur 100 % des jobs. Le défaut s'est vu quand la question
> « quelles lignes viennent de quel modèle ? » a été posée à froid sur une
> campagne réelle, et qu'il a fallu passer par l'horodatage du journal des
> écritures pour y répondre.

Le verdict de faux départ (réserver une ligne sans rien écrire) appartient au
worker, qui a vu les appels — un résultat qui ne le porte pas vient d'un worker
trop ancien, et la flotte lève plutôt que de le redeviner.

⚠️ **Un claim à VIDE n'est pas un faux départ.** Une réservation qui ne rend
aucune ligne (`row: null`) n'a rien réservé, et en fin de file il y a **toujours
plus d'agents que de lignes** : compter ces jobs a fait échouer une campagne
ABOUTIE (28/08 — 18 lignes sur 20, les 2 dernières sous bail chez des pairs
encore en vol ⟹ 5 jobs à un seul appel ⟹ borne mordue, `exit 1`), et aurait
arrêté à tort une montée par paliers. Le worker déclare donc `claim_vide` et le
driver l'**ignore** : ni +1 au compteur de faux départs (la borne du 28/08), ni
remise à zéro (elle rendrait la borne contournable par alternance), ni point de
rendement — aucune écriture n'était attendue de ce job.

Deux règles selon le chemin, parce que le worker n'y voit pas la même chose :
la **boucle locale** lit la sortie du claim et s'y fie ; en **Conversations** la
boucle tourne chez le fournisseur et aucune sortie ne remonte — la règle de
repli porte alors sur la NATURE des appels, *un job dont tous les appels sont
des gestes de tenue* (`data_claim_next`, `data_release`, `run_start`,
`run_finish`) *n'a fait aucun travail, donc il n'a rien réservé* ; un seul appel
d'outil métier, et il compte. ⚠️ Compter les appels ne suffit pas : sur une file
vide l'agent en fait **deux** — il réserve, reçoit `row: null`, relâche, puis
conclut proprement. Le **bilan** applique la même règle que la borne, sur la
même liste importée : les deux parlent du même job, ils ne peuvent pas se
contredire.

### Le bilan de la campagne

Le driver rend lui-même le **bilan** de sa flotte : à la cadence
`bilan_periode_s` (600 s par défaut) pendant qu'elle tourne, et une fois à la
fin **quelle que soit la borne** — panne et interruption comprises, parce que
c'est précisément ces jours-là qu'on le lit.

```
bilan flotte prospects-demo : abouties 12/30 · faux départs 2 · 1,8 M jetons · 150,0 k/aboutie · data_write 14 appels, 0 refusé
```

Le même bilan se pose en JSON à côté de la déclaration (`flotte.yaml` →
`flotte.bilan.json`, réécrit atomiquement à chaque fois) : lignes
(départ / restantes / abouties), jobs (terminés / échoués / faux départs),
jetons (total, par job, par ligne aboutie), écritures (réservations et
écritures), et refus d'écriture.

⚠️ **« Aboutie » se lit au TABLEAU, jamais aux jobs** : c'est une ligne qui ne
correspond PLUS au `filter` de réservation. Le coût par ligne aboutie vaut
`null` tant que rien n'a abouti — jamais un chiffre calculé sur zéro. Les refus
d'écriture (`data_write` refusé par RBAC, quota ou schéma : le job conclut
« done » sans une ligne écrite) se lisent au journal des appels de l'org, ce qui
demande un `org:` dans la déclaration ; sans lui le poste est omis, et le bilan
dit pourquoi.

## Tests

```bash
pip install -e .[dev] && pytest -q
```

La boucle se teste à sec (faux provider, faux transport) : allowlist fail-closed,
troncature marquée, plafond de tours, refus terminal, fil apposé en double étage
(projection neutre + brut provider), continuation sans tour inventé.
