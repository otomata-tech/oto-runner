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
```

Le dépôt des faux départs est un outil de diagnostic qu'on arme : variable
absente, rien n'est écrit. Posée, chaque job conclu en faux départ y laisse un
`<job_id>.json` — outils appelés et **texte final intégral** de l'agent, la
seule pièce qui dise s'il a rédigé sa fiche en prose sans appeler l'écriture ou
s'il a renoncé. ⚠️ Ce fichier porte de la **donnée de la file de travail** : il
est écrit en 0600, et **se purge après lecture**.

## Un job

`start` : `{procedure, project_id, tools: […], input?, label?, max_steps?}` —
le worker ouvre le run, le lie au job, charge la procédure d'oto (jamais copiée),
joue la boucle sur un fil neuf. `continue` : `{run_id, input?}` — il recharge le
fil et continue ; `input` absent = reprise pure après une mort en plein tour.
Un job porte des **références, jamais un secret**.

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

Le rendement est la borne GÉNÉRALE du prix de la sortie. « Outil critique » ne
couvre qu'un outil qui répond en erreur ; une campagne peut payer le prix plein
sans rien produire pour dix autres raisons, et rien ne le voit puisque les jobs
concluent « done » :

```yaml
jetons_par_ecriture_max: 40000   # plafond de jetons par écriture (absent ⟹ inactif)
rendement_fenetre: 10            # fenêtre glissante de jobs conclus (défaut 10)
```

Sur les `rendement_fenetre` derniers jobs conclus, si la somme des jetons
dépasse `jetons_par_ecriture_max × max(1, écritures)`, la flotte s'arrête. La
fenêtre ne juge qu'une fois **pleine** : un début de vol n'a pas de verdict.

Chaque job conclu déclare son coût et sa sortie (`usage_tokens`, `tool_counts`,
`claims`, `writes`, `faux_depart`) : c'est ce que l'ordonnanceur lit, sans jamais
ouvrir un fil. Le verdict de faux départ (réserver une ligne sans rien écrire)
appartient au worker, qui a vu les appels — un résultat qui ne le porte pas vient
d'un worker trop ancien, et la flotte lève plutôt que de le redeviner.

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
