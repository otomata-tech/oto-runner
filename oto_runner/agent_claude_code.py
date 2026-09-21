"""Le moteur CLAUDE CODE : la boucle de Claude Code (Agent SDK), servie par le worker.

Pourquoi il existe : un run hébergé devait pouvoir faire ce que fait une session
`claude -p` lancée sur GitHub Actions contre la face MCP — un runner client en est
la référence. La boucle maison (`agent_runtime`) n'a ni délégation à des
sous-agents, ni compaction (elle TRONQUE à 60 messages), ni tours au-delà de 64 :
un sourcing qui pagine une centaine de candidats y perd la procédure en route ou
s'arrête `blocked`. Ce moteur délègue la boucle entière à Claude Code, qui a les
trois.

⚠️ Ce qu'il ne promet PAS : des déroulés de 400 tours. `PLAFOND_TOURS` est un toit,
le travail est servi à `spec.max_steps` (24 par défaut), et c'est la deadline murale
(900 s) qui coupe en pratique. Ce que ce moteur apporte vraiment, c'est la
DÉLÉGATION et la COMPACTION — pas une longueur.

Ce qui ne change PAS, et c'est la raison de la forme :

- **Les outils passent par la session MCP du travail** (`McpSession`), exposée à
  Claude Code comme un serveur MCP EN PROCESSUS. Claude Code ne parle jamais au
  backend directement : les jetons de contexte (`_org`, `_project`, `_run_id`)
  restent posés par le worker, les paramètres de cycle de vie restent retirés, la
  session perdue reste rouverte. Un Claude Code branché en HTTP sur `/mcp` aurait
  tout perdu d'un coup, sans rien casser de visible.
- **L'allowlist est le catalogue** : le serveur en processus n'expose QUE les
  outils du travail. Un sous-agent hérite de ce serveur ; il ne peut donc pas voir
  plus que son parent — la garantie ne dépend d'aucun réglage de permission.
- **Aucun outil intégré** hors la délégation à un sous-agent (`Agent`) : ni
  shell, ni fichiers, ni web. Aucun réglage disque lu (`setting_sources=[]`), aucun
  autre serveur MCP (`strict_mcp_config`), un répertoire de configuration NEUF par
  travail — rien ne passe d'une org à l'autre.
- **La clé est celle du travail** (dépôt `anthropic`), sinon celle du worker ;
  jamais un abonnement. Les secrets du worker sont effacés de l'environnement
  hérité par le sous-processus.

Ce qui change : c'est un chemin ONE-SHOT (comme Conversations) — pas de tours
apposés au fil, le verbatim va au journal du travail. Les appels d'outils sont
SÉRIALISÉS (la session MCP n'est pas réentrante, et sa deadline SIGALRM exige le
thread principal) : des sous-agents parallèles attendent leur tour d'outil.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import shutil
import tempfile
import time
from typing import Callable, Optional

from . import agent_llm
from .agent_runtime import AgentResult, AgentStep, _cap, max_tool_output
from .comptage import Compteur
from .deadline import DeadlineExceeded
from .llm_types import EFFORT_SANS_RAISONNEMENT, LlmUnavailable

logger = logging.getLogger("oto_runner")

ONE_SHOT = True
#: `run_once` reçoit la session MCP, le cadre et le workspace du travail.
OUTILS_LOCAUX = True
#: L'effort de réflexion du travail est servi (`ClaudeAgentOptions.effort`).
EFFORT_SERVI = True

SERVEUR = "oto"
PREFIXE = f"mcp__{SERVEUR}__"
#: Le seul outil intégré offert : la délégation à un sous-agent. Le CLI 2.1.273 le
#: sert sous le nom `Task` quand on demande `Agent` ; les deux sont autorisés.
OUTILS_INTEGRES = ("Agent",)
_NOMS_DELEGATION = ("Agent", "Task")

#: PLAFOND de tours du fil principal — un toit, jamais une promesse. Ce qui est
#: servi, c'est `spec.max_steps` (24 par défaut) ; ce nombre ne fait que dire
#: jusqu'où le moteur accepte d'aller, et une demande au-delà est DITE au journal
#: plutôt que rabotée en silence.
#:
#: ⚠️ Ce n'est PAS la borne qui mord. Un déroulé n'a que `wall_s()` secondes
#: (900 par défaut) pour jouer ses tours : bien avant 400, c'est le mur qui coupe,
#: en `DeadlineExceeded` — et un travail mort ainsi peut être rejoué depuis zéro,
#: donc payé deux fois. Les deux nombres partent ensemble au journal (`_options`).
#: ⚠️ Et il ne borne que le FIL PRINCIPAL : un sous-agent déroule ses propres
#: tours dessous. La seule borne de session est `OTO_RUNNER_CLAUDE_CODE_MAX_USD`.
PLAFOND_TOURS = 400

_ENV_WALL = "OTO_RUNNER_CLAUDE_CODE_WALL_S"
#: La borne de dépense de la SESSION (dollars), sous-agents compris. Non posée = pas
#: de borne : `max_turns` ne borne que le fil principal.
_ENV_MAX_USD = "OTO_RUNNER_CLAUDE_CODE_MAX_USD"
#: Sous la patience de systemd à l'arrêt (16 min, `docs/deploiement-et-arret.md`) : au-delà,
#: un déploiement tuerait un déroulé qui avait le droit de finir. La relever exige de
#: relever `TimeoutStopSec` avec elle.
_WALL_DEFAUT_S = 900
_BATTEMENT_S = 60

#: Les secrets du worker, effacés NOMMÉMENT — même absents de l'environnement au
#: moment de l'appel. La liste blanche ci-dessous les couvrirait ; les nommer garde
#: écrit ce qui ne doit jamais atteindre le CLI.
_SECRETS_DU_WORKER = ("OTO_WORKER_SECRET", "OTO_TOKEN", "OTO_RUNNER_OPENAI_API_KEY",
                      "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")

#: ⚠️ LISTE BLANCHE, et non liste noire. Le SDK fusionne TOUT `os.environ` sous
#: `options.env` (`_internal/transport/subprocess_cli.py` : `{**os.environ, ...,
#: **options.env}`), et `options.env` ne peut pas RETIRER une variable — seulement
#: l'écraser. Une liste noire laissait donc passer au CLI toute variable FUTURE du
#: `.env` du worker : il suffisait d'en ajouter une pour la lui offrir. Ici, tout ce
#: qui n'est pas nommé part VIDE.
#:
#: Chaque nom se justifie, sinon il sort :
_ENV_TRANSMIS = frozenset({
    "PATH",              # le SDK résout le CLI en absolu avant de lancer, mais le
                         # binaire natif et ses propres enfants en ont besoin
    "LANG", "LC_ALL",    # l'encodage de la sortie ; un CLI en C locale casse l'UTF-8
    "TZ",                # les dates que l'agent écrit sont celles de la box
    "TMPDIR",            # sans lui, /tmp — que `PrivateTmp` isole déjà
    "USER", "LOGNAME",   # ni secrets ni identifiants : des outils POSIX les lisent
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",  # l'autorité TLS de la box
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",                 # la sortie réseau de la box,
    "https_proxy", "http_proxy", "no_proxy",                 # aux deux casses (curl lit
                                                             # la minuscule, node la majuscule)
})
#: `LC_*` en entier : ce sont des réglages de locale, jamais des secrets.
_PREFIXES_TRANSMIS = ("LC_",)


def _transmis(nom: str) -> bool:
    return nom in _ENV_TRANSMIS or nom.startswith(_PREFIXES_TRANSMIS)


def model() -> str:
    return agent_llm.model()


def depot() -> str:
    return "anthropic"


def resolve_key() -> str:
    return agent_llm.resolve_key()


def _sdk():
    try:
        import claude_agent_sdk  # noqa: PLC0415 — import gardé : sans la lib, pas de moteur
    except ImportError as e:
        raise LlmUnavailable(
            "claude-agent-sdk absent : installe l'extra `claude-code` "
            "(`pip install oto-runner[claude-code]`)") from e
    return claude_agent_sdk


def max_usd() -> Optional[float]:
    """La borne de DÉPENSE de la session, ou `None`. Absente = pas de borne servie.

    `max_turns` ne borne que le fil principal : un sous-agent déroule ses propres
    tours dessous, et rien ne les comptait. Le SDK, lui, sait arrêter la session
    entière sur un montant (`max_budget_usd`) — c'est la borne honnête ici.
    """
    brut = os.environ.get(_ENV_MAX_USD, "").strip()
    if not brut:
        return None
    try:
        valeur = float(brut)
    except ValueError:
        valeur = 0.0
    if valeur <= 0:
        raise LlmUnavailable(f"{_ENV_MAX_USD} = {brut!r} : un montant > 0 est attendu")
    return valeur


def wall_s() -> int:
    brut = os.environ.get(_ENV_WALL, "").strip()
    if not brut:
        return _WALL_DEFAUT_S
    if not brut.isdigit() or int(brut) < 1:
        raise LlmUnavailable(f"{_ENV_WALL} = {brut!r} : un entier ≥ 1 est attendu")
    return int(brut)


@dataclasses.dataclass
class _Etat:
    steps: list = dataclasses.field(default_factory=list)
    panne: Optional[str] = None
    #: Le compte EN VOL, tenu HORS de la coroutine — `asyncio.wait_for` l'annule à la
    #: deadline, et tout ce qui vivait dedans partirait avec elle. C'est ce qui permet
    #: à un déroulé MORT (deadline, clé refusée, transport, signal) de dire ce qu'il a
    #: dépensé au lieu de le perdre.
    #:
    #: ⚠️ Ses `tours` comptent des MESSAGES, pas des tours d'API, et il ne sert QUE de
    #: repli : dès qu'un `ResultMessage` arrive, c'est le bilan de session qui fait foi
    #: (il couvre les sous-agents via `model_usage`). Les deux ne se fusionnent JAMAIS —
    #: un poste que les sous-agents ne déclarent pas ferait tomber tout le compte à
    #: `None` alors que le bilan, lui, le connaît.
    compteur: Compteur = dataclasses.field(default_factory=Compteur)
    vus: set = dataclasses.field(default_factory=set)
    servi: Optional[str] = None


def _outils(sdk, mcp, noms: frozenset, etat: _Etat, note) -> list:
    """L'allowlist du travail, en outils du serveur MCP en processus."""
    limite = max_tool_output()
    schemas = mcp.schemas(noms)
    manquants = sorted(noms - {s["name"] for s in schemas})
    if manquants:
        note("outils_absents", noms=manquants)
    return [_un_outil(sdk, mcp, s, limite, etat, note) for s in schemas]


def _un_outil(sdk, mcp, schema: dict, limite: int, etat: _Etat, note):
    nom = schema["name"]

    async def appeler(args):
        debut = time.monotonic()
        try:
            # Synchrone, sur le thread de la boucle : la deadline de la session est un
            # SIGALRM, et la session n'est pas réentrante — les appels s'enchaînent.
            texte, erreur = mcp.call(nom, dict(args or {}))
        except Exception as e:  # noqa: BLE001 — un transport mort arrête le TRAVAIL
            # Rendre l'exception au modèle la ferait lire comme une réponse métier :
            # il l'annoncerait et conclurait « done » sans écriture. Le travail échoue.
            etat.panne = f"{nom} : {type(e).__name__}: {e}"
            etat.steps.append(AgentStep(tool=nom, ok=False,
                                        duration_ms=int((time.monotonic() - debut) * 1000),
                                        error=str(e)[:300], transport_ko=True))
            return {"content": [{"type": "text",
                                 "text": "Transport indisponible : le travail s'arrête."}],
                    "is_error": True}
        lu, coupe = _cap(texte, limite)
        note("outil", nom=nom, erreur=erreur, sortie=texte, coupee=coupe)
        etat.steps.append(AgentStep(tool=nom, ok=not erreur,
                                    duration_ms=int((time.monotonic() - debut) * 1000),
                                    error=(texte[:300] if erreur else None)))
        return {"content": [{"type": "text", "text": lu}], "is_error": bool(erreur)}

    return sdk.tool(nom, schema.get("description") or "", schema["input_schema"])(appeler)


def _environnement(cle: str, workspace: Optional[str], spec, dossier: str) -> dict:
    """L'environnement du sous-processus : la liste blanche, et rien d'autre.

    Calculé À L'APPEL, sur l'`os.environ` du moment : une variable ajoutée au worker
    après le démarrage est effacée elle aussi.
    """
    env = {s: "" for s in _SECRETS_DU_WORKER}
    env.update({nom: "" for nom in os.environ if not _transmis(nom)})
    env.update({
        "ANTHROPIC_API_KEY": cle,
        # Le foyer du sous-processus est CELUI DU TRAVAIL, effacé avec lui : rien de
        # ce que le CLI écrirait « chez lui » ne vit sous le foyer du worker, ni ne
        # passe d'une org à la suivante.
        "HOME": dossier,
        "CLAUDE_CONFIG_DIR": os.path.join(dossier, "config"),
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        # Notre plafond en caractères est celui qui mord, et il le DIT au modèle
        # (`_cap`). Celui du CLI est en jetons ; un jeton ne fait jamais moins d'un
        # caractère, donc le même nombre le laisse toujours au-dessus.
        "MAX_MCP_OUTPUT_TOKENS": str(max_tool_output()),
    })
    if workspace:
        env["ANTHROPIC_CUSTOM_HEADERS"] = f"anthropic-workspace-id: {workspace}"
    if spec is not None and spec.max_output_tokens:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(int(spec.max_output_tokens))
    return env


def _options(sdk, *, instructions: str, serveur, noms: frozenset, spec, modele,
             env: dict, dossier: str, mur_s: int, note):
    demande = spec.max_steps if spec is not None else PLAFOND_TOURS
    tours = max(1, min(int(demande), PLAFOND_TOURS))
    if tours != demande:
        note("plafond_tours", demande=demande, servi=tours, plafond=PLAFOND_TOURS)
    effort = (spec.effort if spec is not None else None) or agent_llm.effort_hote()
    reglages = {
        # ⚠️ Le CADRE DU TRAVAIL, tel quel — pas le preset `claude_code` complété.
        # Le preset est un prompt système de plusieurs milliers de jetons que le
        # backend n'a pas envoyés : le servir, c'est composer à la place du
        # demandeur, et le worker ne compose pas (doctrine). Ce que ça coûte en
        # échange : le preset est aussi ce qui APPREND au modèle à déléguer à un
        # sous-agent. L'outil reste offert ; la délégation, elle, reste à mesurer
        # sur un essai réel.
        "system_prompt": instructions,
        "mcp_servers": {SERVEUR: serveur},
        "strict_mcp_config": True,
        "tools": list(OUTILS_INTEGRES),
        "allowed_tools": ([PREFIXE + n for n in sorted(noms)]
                          + list(OUTILS_INTEGRES) + ["Task"]),
        "permission_mode": "dontAsk",
        "setting_sources": [],
        "max_turns": tours,
        "model": modele,
        "cwd": dossier,
        "env": env,
    }
    if effort and effort != EFFORT_SANS_RAISONNEMENT:
        reglages["effort"] = effort
    plafond_usd = max_usd()
    if plafond_usd is not None:
        # La SEULE borne qui tienne sur toute la session : `max_turns` ne borne que le
        # fil principal, un sous-agent déroule ses propres tours dessous. Le SDK sort
        # en `error_max_budget_usd`, que `_ARRETS` conclut déjà en `max_tokens`.
        reglages["max_budget_usd"] = plafond_usd
    if spec is not None and spec.temperature is not None:
        # Claude Code n'expose pas de température : la servir en silence ferait croire
        # que deux passages comparés l'étaient à réglage égal.
        note("temperature_non_servie", temperature=spec.temperature)
        logger.warning("température %s déclarée et non servie par Claude Code",
                       spec.temperature)
    # ⚠️ Les deux bornes qui se contredisent, CÔTE À CÔTE dans le journal : un
    # déroulé de `tours` tours n'a que `mur_s` secondes pour les jouer, et ce qui
    # dépasse meurt en `DeadlineExceeded` — puis peut être rejoué depuis zéro, donc
    # payé deux fois. Les lire ensemble évite de relire un plafond de tours généreux
    # comme une promesse que le mur ne tiendra pas.
    note("claude_code", **{k: v for k, v in reglages.items()
                           if k not in ("env", "mcp_servers")},
         mur_s=mur_s, env=sorted(k for k in env if env[k]))
    return sdk.ClaudeAgentOptions(**reglages)


_CLE_REFUSEE = (401, 403)

_ARRETS = {"success": "end_turn", "error_max_turns": "max_steps",
           "error_max_budget_usd": "max_tokens"}


def _postes(entree, sortie, lus, ecrits) -> dict:
    """Les postes de `comptage`, sans jamais fabriquer un zéro : un poste que le
    fournisseur n'a pas déclaré reste ABSENT, et `Compteur` le rendra `None`."""
    usage = {"input_tokens": entree, "output_tokens": sortie,
             "cache_read_input_tokens": lus, "cache_creation_input_tokens": ecrits}
    if None not in (entree, lus, ecrits):
        # `input_tokens` est le non-caché exact ; l'entrée TOTALE ne se pose que
        # lorsque les deux caches sont connus (cf. la doctrine de `comptage`).
        usage["input_total_tokens"] = entree + lus + ecrits
    return {k: v for k, v in usage.items() if v is not None}


def _usage_du_resultat(resultat) -> dict:
    """L'usage de la SESSION, sous-agents compris : `model_usage` par modèle quand il
    est rendu (il couvre les sous-agents), sinon `usage`."""
    par_modele = getattr(resultat, "model_usage", None) or {}
    if par_modele:
        def somme(cle):
            valeurs = [m.get(cle) for m in par_modele.values() if isinstance(m, dict)]
            return None if any(v is None for v in valeurs) else sum(int(v) for v in valeurs)
        return _postes(somme("inputTokens"), somme("outputTokens"),
                       somme("cacheReadInputTokens"), somme("cacheCreationInputTokens"))
    u = getattr(resultat, "usage", None) or {}
    return _postes(u.get("input_tokens"), u.get("output_tokens"),
                   u.get("cache_read_input_tokens"), u.get("cache_creation_input_tokens"))


def _usage_du_message(message) -> dict:
    """L'usage d'UN message d'assistant, aux noms de l'API — la même forme que le
    bilan, pour que le compte en vol et le bilan de session se lisent pareil."""
    u = getattr(message, "usage", None) or {}
    return _postes(u.get("input_tokens"), u.get("output_tokens"),
                   u.get("cache_read_input_tokens"), u.get("cache_creation_input_tokens"))


def _compter(etat: _Etat, message, principal: bool, plafond, note) -> Optional[str]:
    """Compte UN message d'assistant, et rend l'arrêt qu'il déclenche — ou `None`.

    ⚠️ Le compte tourne MÊME SANS BORNE demandée. Il ne sert pas qu'à arrêter :
    c'est lui qui dit ce qu'a coûté un déroulé mort avant son bilan de session
    (deadline, clé refusée, transport mort, signal). Le tenir seulement « si
    plafond » laissait ces jetons nulle part.
    """
    manque = etat.compteur.ajouter(_usage_du_message(message))
    if plafond is None:
        return None
    if manque and principal:
        # ⚠️ Une borne DEMANDÉE ne se suit que sur des messages MESURÉS : un usage
        # absent compté pour 0 la rendrait muette. Même arbitrage que la boucle
        # maison (13/09/2026), même nom d'arrêt.
        note("borne_non_suivie", borne="max_tokens", max_tokens=plafond,
             manque=manque, jetons_bornes=etat.compteur.borne)
        return "max_tokens_non_mesurable"
    if manque:
        # Un SOUS-AGENT muet n'aveugle pas la borne : le bilan de session le couvre
        # (`model_usage`), et sans bilan la couverture dira le manque. S'arrêter là
        # couperait un déroulé sain — le fil principal, lui, DOIT être mesuré.
        note("usage_absent_sous_agent", manque=manque)
        return None
    if etat.compteur.borne >= plafond:
        note("budget_depasse", jetons=etat.compteur.borne, plafond=plafond)
        return "max_tokens"
    return None


async def _derouler(sdk, prompt: str, options, *, etat: _Etat, spec, note,
                    battre: Callable[[], None]):
    resultat, texte, arret = None, "", None
    plafond = spec.max_tokens if spec is not None else None
    flux = sdk.query(prompt=prompt, options=options).__aiter__()
    try:
        while True:
            try:
                message = await flux.__anext__()
            except StopAsyncIteration:
                break
            except Exception as e:
                # Un résultat en erreur (plafond de tours, clé refusée…) fait sortir le CLI
                # en code 1, et le SDK lève `ResultError` APRÈS avoir rendu le résultat.
                # Le résultat est déjà lu : c'est lui qui conclut, pas l'exception.
                if resultat is not None and type(e).__name__ == "ResultError":
                    break
                raise
            genre = type(message).__name__
            note("message", genre=genre, contenu=message)
            battre()
            if genre == "SystemMessage" and getattr(message, "subtype", "") == "api_retry":
                statut = (getattr(message, "data", None) or {}).get("error_status")
                if statut in _CLE_REFUSEE:
                    # Le CLI rejoue une clé refusée dix fois, en minutes d'attente croissante :
                    # une clé révoquée n'est pas un transitoire.
                    raise RuntimeError(f"clé de modèle refusée par Anthropic ({statut}) — "
                                       "vérifie la clé déposée par l'org (ou celle du worker)")
            if genre == "AssistantMessage":
                principal = getattr(message, "parent_tool_use_id", None) is None
                for bloc in getattr(message, "content", None) or ():
                    nom_bloc = type(bloc).__name__
                    if nom_bloc == "ToolUseBlock" and getattr(bloc, "name", "") in _NOMS_DELEGATION:
                        # Pas un pas : `tool_counts` compte des appels RÉUSSIS, et une
                        # délégation n'a pas encore d'issue quand elle part. Elle se dit
                        # au journal ; ses appels d'outils, eux, sont comptés un à un.
                        note("delegation", parent_principal=principal,
                             entree=getattr(bloc, "input", None))
                    elif nom_bloc == "TextBlock" and principal and getattr(bloc, "text", ""):
                        texte = bloc.text
                if principal and getattr(message, "model", None):
                    etat.servi = message.model
                # Un message d'API arrive découpé en plusieurs messages (un par bloc), qui
                # portent tous son usage : il ne se compte qu'une fois.
                #
                # ⚠️ Le compte tourne MÊME SANS BORNE demandée. Il ne sert pas qu'à
                # arrêter : c'est lui qui dit ce qu'a coûté un déroulé mort avant son
                # bilan de session (deadline, clé refusée, transport, signal). Le
                # compter seulement « si plafond » laissait ces jetons nulle part.
                identifiant = getattr(message, "message_id", None)
                if identifiant is None or identifiant not in etat.vus:
                    if identifiant is not None:
                        etat.vus.add(identifiant)
                    arret = _compter(etat, message, principal, plafond, note) or arret
                    if arret:
                        break
            elif genre == "ResultMessage":
                resultat = message
            if etat.panne:
                break
    finally:
        # Sortir tôt (budget, panne, clé refusée) doit arrêter le sous-processus, pas
        # le laisser dépenser derrière un travail déjà conclu.
        fermer = getattr(flux, "aclose", None)
        if fermer is not None:
            await fermer()
    return resultat, texte, arret


def _mort_nommee(e: BaseException) -> BaseException:
    """Traduit une mort de sous-processus en échec qui DIT ce qui s'est passé.

    Un `systemctl restart` sous le `KillMode` par défaut envoie SIGTERM à tout le
    groupe de contrôle, donc au `claude` fils : le SDK sort alors en `ProcessError`
    avec un code NÉGATIF (le signal), et le travail finissait sur « sorti sans
    message de résultat » — un symptôme qui n'accuse pas sa cause. Cf.
    `docs/deploiement-et-arret.md` (`KillMode=mixed`).
    """
    code = getattr(e, "exit_code", None)
    if isinstance(code, int) and code < 0:
        return RuntimeError(
            f"Claude Code tué par le signal {-code} avant de conclure — un "
            "redémarrage de l'unité tue le CLI fils tant qu'elle n'est pas en "
            "`KillMode=mixed` avec un `TimeoutStopSec` au-dessus de la deadline "
            f"({_ENV_WALL})")
    return e


def run_once(*, instructions: str, inputs: str, tools, api_key: Optional[str] = None,
             modele: Optional[str] = None, on_event=None, mcp=None, spec=None,
             workspace: Optional[str] = None,
             heartbeat: Optional[Callable[[], None]] = None) -> AgentResult:
    """UN déroulé Claude Code complet → AgentResult.

    Lève : `LlmUnavailable` (SDK ou clé absents), `DeadlineExceeded` (au-delà de
    `OTO_RUNNER_CLAUDE_CODE_WALL_S`), `RuntimeError` (transport MCP mort, CLI tué
    par un signal, ou Claude Code sorti sans résultat) — le retry de job décide.

    ⚠️ Ce qui SORT par une exception emporte ce qu'il a coûté : `usage_partiel`,
    `couverture_partielle`, `modele_partiel` et `pas_partiels` sont accrochés à
    l'exception, exactement comme `agent_runtime.run` le fait pour la boucle
    maison — c'est `conclusion.resultat_partiel` qui les lit, et sans eux un
    déroulé mort rendait ZÉRO jeton au serveur alors qu'il en avait dépensé.
    """
    etat = _Etat()
    try:
        return _deroule_complet(
            etat, instructions=instructions, inputs=inputs, tools=tools,
            api_key=api_key, modele=modele, on_event=on_event, mcp=mcp, spec=spec,
            workspace=workspace, heartbeat=heartbeat)
    except BaseException as e:
        e.usage_partiel = etat.compteur.usage()               # type: ignore[attr-defined]
        e.couverture_partielle = etat.compteur.couverture()   # type: ignore[attr-defined]
        e.modele_partiel = etat.servi                         # type: ignore[attr-defined]
        e.pas_partiels = len(etat.steps)                      # type: ignore[attr-defined]
        raise


def _deroule_complet(etat: _Etat, *, instructions: str, inputs: str, tools,
                     api_key: Optional[str] = None, modele: Optional[str] = None,
                     on_event=None, mcp=None, spec=None,
                     workspace: Optional[str] = None,
                     heartbeat: Optional[Callable[[], None]] = None) -> AgentResult:
    if mcp is None:
        raise LlmUnavailable("le moteur Claude Code exige la session MCP du travail")
    sdk = _sdk()
    cle = api_key or resolve_key()
    nom = modele or model()
    noms = frozenset(tools or ())

    def note(ev: str, **champs) -> None:
        if on_event:
            on_event(ev, champs)

    dernier: list = [None]

    def battre() -> None:
        # `None` : le premier battement part toujours. Une horloge monotone compte depuis
        # le démarrage de la machine, donc un zéro initial taisait le premier battement
        # sur une machine démarrée depuis moins d'une minute.
        if heartbeat is None or (dernier[0] is not None
                                 and time.monotonic() - dernier[0] < _BATTEMENT_S):
            return
        dernier[0] = time.monotonic()
        heartbeat()

    dossier = tempfile.mkdtemp(prefix="oto-claude-code-")
    try:
        serveur = sdk.create_sdk_mcp_server(SERVEUR, tools=_outils(sdk, mcp, noms, etat, note))
        limite = wall_s()
        options = _options(sdk, instructions=instructions, serveur=serveur, noms=noms,
                           spec=spec, modele=nom,
                           env=_environnement(cle, workspace, spec, dossier),
                           dossier=dossier, mur_s=limite, note=note)
        try:
            resultat, texte, arret = asyncio.run(asyncio.wait_for(
                _derouler(sdk, inputs, options, etat=etat, spec=spec, note=note,
                          battre=battre), timeout=limite))
        except asyncio.TimeoutError as e:
            raise DeadlineExceeded(
                f"déroulé Claude Code > {limite}s wall-clock ({_ENV_WALL})") from e
        except Exception as e:
            traduite = _mort_nommee(e)
            if traduite is not e:
                raise traduite from e
            raise
    finally:
        shutil.rmtree(dossier, ignore_errors=True)

    if etat.panne:
        raise RuntimeError(f"transport MCP mort pendant le déroulé — {etat.panne}")
    defaut = None
    if resultat is not None:
        # Le bilan de SESSION fait foi : il couvre les sous-agents (`model_usage`) et
        # se compte pour UN tour. Il ne se fusionne pas avec le compte en vol, dont
        # les `tours` comptent des messages — mêler les deux ferait tomber à `None`
        # des postes que le bilan connaît.
        compte = Compteur()
        compte.ajouter(_usage_du_resultat(resultat))
        note("resultat_claude_code", sous_type=resultat.subtype,
             tours=resultat.num_turns, cout_usd=resultat.total_cost_usd,
             duree_ms=resultat.duration_ms, refus_permission=resultat.permission_denials,
             # `model` du bilan ne nomme que le fil principal : les sous-agents peuvent
             # en servir d'autres, et c'est ici qu'ils se lisent.
             modeles=sorted((getattr(resultat, "model_usage", None) or {}).keys()))
    else:
        # Pas de bilan : le déroulé s'est arrêté AVANT (budget dépassé). Le compte en
        # vol est tout ce qu'on a — et il vaut mieux que le zéro qu'on rendait.
        compte = etat.compteur
        note("usage_en_vol", raison=arret, **compte.couverture())
    if arret is None:
        if resultat is None:
            raise RuntimeError("Claude Code s'est terminé sans message de résultat")
        if getattr(resultat, "stop_reason", None) == "refusal":
            arret = "refusal"
        else:
            arret = _ARRETS.get(resultat.subtype)
            if arret is None or (arret == "end_turn" and resultat.is_error):
                arret = "fin_anormale"
                defaut = {"forme": "fin_anormale", "finish_reason": resultat.subtype}
    reponse = (getattr(resultat, "result", None) or texte or "").strip()
    return AgentResult(reply=reponse, steps=list(etat.steps), stopped=arret,
                       usage=compte.usage(), couverture=compte.couverture(),
                       model=etat.servi or nom, defaut=defaut)
