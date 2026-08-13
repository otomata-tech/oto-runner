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
```

## Un job

`start` : `{procedure, project_id, tools: […], input?, label?, max_steps?}` —
le worker ouvre le run, le lie au job, charge la procédure d'oto (jamais copiée),
joue la boucle sur un fil neuf. `continue` : `{run_id, input?}` — il recharge le
fil et continue ; `input` absent = reprise pure après une mort en plein tour.
Un job porte des **références, jamais un secret**.

## Tests

```bash
pip install -e .[dev] && pytest -q
```

La boucle se teste à sec (faux provider, faux transport) : allowlist fail-closed,
troncature marquée, plafond de tours, refus terminal, fil apposé en double étage
(projection neutre + brut provider), continuation sans tour inventé.
