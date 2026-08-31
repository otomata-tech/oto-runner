"""Le décodage : la valeur écrite dit-elle ce que le code du registre signifie ?

⚠️ La faute que ce contrôle cherche passe tous les autres. La source est réelle,
la provenance bien formée, la tranche citée exactement — et la traduction est
fausse d'un cran. Deux fois aujourd'hui : `NN` lu comme « zéro salarié », puis
`01` (1 ou 2 salariés) lu comme « sans salarié ».

> Nos contrôles vérifient qu'une valeur est SOURCÉE ; aucun ne vérifie qu'elle
> est BIEN TRADUITE. Le commentaire ne protège de rien s'il porte l'erreur.

Trois exigences, et elles ne sont pas décoratives :
  ① la table ENTIÈRE — les deux fautes connues sont à un cran l'une de l'autre,
    les autres crans existent aussi ;
  ② la case VIDE comme la pleine — 7 643 lignes sur 8 910 n'ont pas d'effectif
    déclaré, donc le cas majoritaire est celui que l'agent remplit ;
  ③ l'écart EN CLAIR — code rendu / tranche écrite / ce que le code signifie.
    Un compte de « décodages faux » ne dit pas quel cran a glissé.

Usage : controle-decodage.py <namespace> [lot]
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, "/opt/oto-runner")
NS = sys.argv[1] if len(sys.argv) > 1 else ""
if not NS:
    sys.exit("⛔ nomme le tableau à contrôler : aucun défaut n'est codé ici.")
LOT = sys.argv[2] if len(sys.argv) > 2 else ""
H = {"Authorization": "Bearer " + os.environ["OTO_TOKEN"], "X-Oto-Org": "226"}

# La nomenclature INSEE des tranches d'effectif salarié, en entier.
# ⚠️ `00` vaut ZÉRO salarié et `01` vaut UN OU DEUX : c'est ce cran-là qui a
# glissé. `NN` ne vaut pas zéro, il vaut « non renseigné » — l'autre faute.
TABLE = {
    "NN": ("non_renseigne", "non renseigné — PAS zéro"),
    "00": ("sans_salarie", "0 salarié"),
    "01": ("1_2", "1 ou 2 salariés"),
    "02": ("3_5", "3 à 5 salariés"),
    "03": ("6_9", "6 à 9 salariés"),
    "11": ("10_19", "10 à 19 salariés"),
    "12": ("20_49", "20 à 49 salariés"),
    "21": ("50_99", "50 à 99 salariés"),
    "22": ("100_199", "100 à 199 salariés"),
    "31": ("200_249", "200 à 249 salariés"),
    "32": ("250_499", "250 à 499 salariés"),
    "41": ("500_999", "500 à 999 salariés"),
    "42": ("1000_1999", "1 000 à 1 999 salariés"),
    "51": ("2000_4999", "2 000 à 4 999 salariés"),
    "52": ("5000_9999", "5 000 à 9 999 salariés"),
    "53": ("10000_plus", "10 000 salariés et plus"),
}

from oto_runner import mcp as mcp_mod  # noqa: E402
s = mcp_mod.McpSession(os.environ.get("OTO_MCP_URL", "https://mcp.oto.cx/mcp"),
                       os.environ["OTO_TOKEN"], org=226)


def v(x):
    return x.get("valeur") if isinstance(x, dict) and "valeur" in x else x


out, off = [], 0
while True:
    d = json.load(urllib.request.urlopen(urllib.request.Request(
        "https://mcp.oto.cx/api/datastore/namespaces/%s/rows?limit=500&offset=%d"
        % (NS, off), headers=H), timeout=180))
    p = d.get("rows") or []
    out += p
    if len(p) < 500:
        break
    off += 500
lignes = [r for r in out if not LOT or str(v(r.get("lot_test"))) == LOT]
# ⚠️ La case VIDE compte autant que la pleine : c'est là que l'agent écrit.
avec = [r for r in lignes if v(r.get("effectif")) not in (None, "")]
print("population : %d ligne(s) · portant un effectif : %d" % (len(lignes), len(avec)))

faux, justes, sans_code = [], 0, 0
import re
for r in avec:
    siren = str(v(r.get("siren")))
    ecrit = str(v(r.get("effectif")))
    try:
        rep = s.outil("fr_get", {"siren": siren, "_org": 226})
    except Exception:  # noqa: BLE001
        sans_code += 1
        continue
    m = re.search(r'"tranche_effectif_salarie"\s*:\s*"?([^",}]{0,6})',
                  json.dumps(rep, ensure_ascii=False))
    if not m:
        sans_code += 1
        continue
    code = (m.group(1) or "").strip().upper()
    attendu, sens = TABLE.get(code, (None, "code inconnu de la table"))
    if attendu is None:
        faux.append((siren, code, ecrit, sens))
    elif ecrit != attendu:
        faux.append((siren, code, ecrit, "%s ⟹ %s" % (sens, attendu)))
    else:
        justes += 1

print("\n=== LE DÉCODAGE, ÉCART PAR ÉCART ===")
print("  bien traduits            : %d" % justes)
print("  MAL TRADUITS             : %d" % len(faux))
print("  code du registre illisible : %d" % sans_code)
for siren, code, ecrit, sens in faux[:20]:
    print("  ⛔ %s · registre rend %-3s · fiche écrit %-14s · %s"
          % (siren, code, ecrit, sens))
if not faux and justes:
    print("  ✅ toutes les tranches écrites disent ce que le code signifie.")
if sans_code:
    print("  ⚠️ %d non mesurées — ce n'est pas « justes », c'est « pas su »."
          % sans_code)
