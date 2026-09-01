"""Un travail-sonde : le MODÈLE dit quels outils il voit.

⚠️ Pourquoi cette sonde existe. Mon empreinte affichait « neuf outils servis » —
c'était MA DÉCLARATION, la liste d'inclusion envoyée au connecteur. Le 29/08,
`data_release`, retiré de cette liste, a été appelé 54 fois par le modèle : il
l'avait sous les yeux. **Une déclaration n'est pas une mesure**, et toutes mes
vérifications de fenêtre portaient sur une liste que personne n'avait constatée.

La sonde emprunte le MÊME chemin que la campagne — même harnais, même connecteur,
même liste d'inclusion — et sa seule tâche est de nommer ce qu'elle voit. Si elle
en voit 261 là où j'en déclare 9, on le sait AVANT de lancer, pas après.

Usage : sonde-outils-vus.py <fleet.yaml>
"""
import json
import os
import sys

sys.path.insert(0, "/opt/oto-runner")
from oto_runner import agent_conversations, fleet  # noqa: E402

# ⚠️ Le connecteur PRÉFIXE les noms : `oto-11aout_data_write` pour `data_write`.
# Comparer les chaînes brutes fait crier « neuf non déclarés » sur les neuf mêmes
# outils — c'est ce qui m'a fait annoncer à tort que la liste d'inclusion n'était
# pas appliquée. On compare donc sur le nom NU, en retirant tout préfixe.
def nu(nom):
    return nom.split("_", 1)[1] if "_" in nom and nom.split("_", 1)[0] not in (
        "data", "fr", "serper", "oto", "run") else nom



ORDRE = (
    "Ne fais AUCUN appel d'outil. Réponds uniquement par la liste des noms "
    "d'outils que tu as à ta disposition, un par ligne, sans commentaire, sans "
    "numérotation, sans phrase d'introduction ni de conclusion. Si tu n'en as "
    "aucun, réponds exactement : AUCUN."
)

spec = fleet.load_spec(sys.argv[1] if len(sys.argv) > 1
                     else "/opt/oto-runner/fleet-palier100.yaml")
# ⚠️ SANS OBJET hors de la voie ou les outils sont servis A DISTANCE.
#
# Cette sonde mesure l'ecart entre la liste que la flotte DECLARE et celle que
# le modele VOIT — un ecart qui n'existe que si un connecteur distant sert les
# outils. Sur la voie locale, le harnais les passe lui-meme a chaque tour : il
# n'y a pas de second acteur, donc pas d'ecart possible.
#
# Elle PLANTAIT au lieu de le dire, en envoyant le modele d'un fournisseur a
# l'API d'un autre. Un cran qui ne s'applique pas le declare ; il ne tombe pas
# en panne — une panne se lit comme un refus, et un refus comme un danger.
_voie = (os.environ.get("OTO_RUNNER_PROVIDER") or "anthropic").strip().lower()
if _voie != "conversations":
    print(f"sonde SANS OBJET sur la voie « {_voie} » : les outils sont passes "
          f"par le harnais a chaque tour, aucun tiers ne peut en servir "
          f"d'autres. Rien a mesurer, rien a refuser.")
    raise SystemExit(0)

declares = sorted(spec.tools or ())
print(f"DÉCLARÉS par la flotte : {len(declares)}")
for t in declares:
    print(f"   {t}")

# ⚠️ UN ÉCART SE CONFIRME AVANT DE BLOQUER.
#
# La sonde interroge un MODÈLE : sa réponse varie. Le 29/08, elle a refusé un
# lancement puis, relancée deux fois d'affilée, rendu un écart nul les deux fois.
# **Un garde-fou non déterministe qui bloque est pire qu'inutile** : il fait
# perdre un départ sur un aléa, et on finit par le contourner.
#
# Trois essais, et l'écart doit apparaître AU MOINS DEUX FOIS pour compter. Un
# vrai écart — une liste réellement différente — est stable ; une réponse mal
# formée ne l'est pas.
def _interroger():
    res = agent_conversations.run_once(
        instructions="Tu réponds à une question de diagnostic, rien d'autre.",
        inputs=ORDRE, tools=declares)
    lignes = [l.strip(" -•\t") for l in (res.reply or "").splitlines() if l.strip()]
    return [v for v in lignes if v and " " not in v.strip()], res


essais = []
for _tentative in range(3):
    vus, res = _interroger()
    ecart = bool({nu(t) for t in vus} ^ set(declares))
    essais.append((vus, ecart))
    print(f"  essai {_tentative + 1} : {len(vus)} outils vus · "
          f"{'ÉCART' if ecart else 'concordant'}")
    if not ecart:
        break

vus = essais[-1][0]
ecarts_confirmes = sum(1 for _, e in essais if e)
print(f"\nVUS PAR LE MODÈLE : {len(vus)}")
for t in sorted(vus)[:40]:
    marque = "" if nu(t) in declares else "   ⚠️ NON DÉCLARÉ"
    print(f"   {t}{marque}")
if len(vus) > 40:
    print(f"   … et {len(vus) - 40} autres")

vus_nus = {nu(t) for t in vus}
en_trop = sorted(vus_nus - set(declares))
manquants = sorted(set(declares) - vus_nus)
print(f"\n=== VERDICT ===")
print(f"  déclarés : {len(declares)} · vus : {len(vus)} "
      f"(comparés sur le nom nu, préfixe du connecteur retiré)")
print(f"  VUS MAIS NON DÉCLARÉS : {len(en_trop)}")
for t in en_trop[:15]:
    print(f"     ⚠️ {t}")
print(f"  déclarés mais NON VUS : {manquants or 'aucun'}")
if en_trop:
    print("\n  ⟹ la liste d'inclusion N'EST PAS appliquée : ma fenêtre ne mesure")
    print("     pas ce que le modèle voit, et un tag sur n'importe lequel de ces")
    print("     outils entre dans la campagne sans que mon contrôle le voie.")
else:
    print("\n  ✅ la liste d'inclusion est appliquée : la fenêtre mesure bien.")
    print("     Deux listes qui concordent = une fenêtre MESURÉE.")
    print("     Une seule = une fenêtre DÉCLARÉE.")
# On ne bloque que sur un écart CONFIRMÉ — au moins deux essais sur trois.
if (en_trop or manquants) and ecarts_confirmes >= 2:
    print(f"\n  ⟹ écart CONFIRMÉ sur {ecarts_confirmes} essais : on ne lance pas.")
    raise SystemExit(1)
if en_trop or manquants:
    print(f"\n  ⟹ écart vu une seule fois sur {len(essais)} : réponse instable du")
    print("     modèle, pas un changement d'outils. On laisse passer.")
raise SystemExit(0)
print(f"\n  jetons de la sonde : {(res.usage or {}).get('input_tokens', '?')} entrée / "
      f"{(res.usage or {}).get('output_tokens', '?')} sortie")
