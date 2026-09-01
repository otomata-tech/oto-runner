# Fleet comme produit — ce qui part avec les verbes

**Une flotte écrit dans le fichier d'un client.** Ce qui a évité le désastre
pendant les deux jours de mise au point n'était pas la plateforme : c'était la
discipline de l'équipe qui l'opérait — l'instantané d'avant, le périmètre
déclaré, la garde qui compare et restaure, le différentiel qui compte ce qui a
disparu, la destination vérifiée au lancement.

> **Si `fleet` devient un produit, le prochain utilisateur reçoit la puissance
> sans les deux jours de leçons. Il lancera trente agents sur un fichier client
> sans instantané, et il n'aura même pas l'idée que ça manque — parce que rien
> ne le lui dira.**

**Donc ce qui part avec la fonctionnalité, ce ne sont pas seulement des verbes :
ce sont les gardes.** Ce document fixe ce qui doit être dans le produit plutôt
que dans l'habitude d'une équipe. Il précède l'écriture des verbes.

---

## ① La capacité s'appelle `fleet`, et elle n'est pas une face agent sur la file

La capacité qui porte la file de travaux se déclare **worker-only** — *« la
plomberie d'exécution n'a pas de face agent »*. **Ce n'est pas un manque, c'est
une frontière posée exprès.** Lui ajouter une face agent brouillerait ce qu'elle
est : on ne saurait plus si l'objet servi est un rouage ou un produit.

**Une capacité `fleet` nomme ce qu'elle fait** — piloter un passage d'agents —
et laisse la plomberie être de la plomberie. C'est plus de surface ; **une
surface qui nomme la bonne chose coûte moins cher qu'une surface qui en
surcharge une autre**, parce que la seconde se paie à chaque lecture.

---

## ② Un lancement vise une CONFIGURATION DÉCLARÉE, jamais un tableau libre

**Un verbe généraliste rendrait accessible en un appel le geste qu'on a passé
deux jours à empêcher** : lancer des agents sur le fichier d'un client, sans
instantané, sans périmètre, sans témoin. *Le rendre facile suffit à le rendre
fréquent.*

Et il y a un argument structurel qui pèse plus que le risque :

> **Une configuration déclarée est l'endroit où les gardes VIVENT.** Un
> lancement libre n'a nulle part où accrocher un instantané de départ, un
> périmètre, une garde de restauration, un seuil d'arrêt.

Ce n'est donc pas une restriction de fonctionnalité : c'est lui donner un
domicile.

---

## ③ Les gardes qui partent avec le produit

**Chacune vient d'un incident payé, et aucune ne s'annonce d'elle-même.**

### La destination se constate, elle ne se suppose pas

Un lancement dont la destination n'est pas la production attendue **ne part
pas**, et le refus la nomme. *Toute la chaîne de contrôles vérifiait le quoi et
le combien — jamais le où.* Le jour où un correctif destructeur a été déployé en
préproduction, qui partage la base de la production, c'est le hasard qui a
protégé les données : les agents écrivaient ailleurs.

### Un passage sans instantané de départ se refuse

**Une garde qui n'a pas d'état d'avant ne peut rien attester.** Elle rendra
« aucune altération » — le résultat le plus convaincant et le plus faux. Un
lancement sans instantané se refuse ; un instantané pris après qu'un agent a
écrit **n'est plus un instantané** et se refuse aussi.

### L'instantané dit à quelle table il appartient

Une garde qui compare une fiche à l'état d'une **autre** table rend des verdicts
faux et crédibles. L'instantané porte le nom de sa table, la garde le lit, et
**refuse de comparer** si ce n'est pas la bonne — elle rend « non mesuré »,
jamais un verdict.

### Un verdict rendu sur un relevé plafonné DIT qu'il est plafonné

**Un relevé tronqué compte moins que la réalité, jamais plus — donc il rassure
exactement quand il ne faut pas.** Toute lecture paginée vérifie qu'elle a
atteint le total annoncé, et le déclare sinon.

### Trois états, jamais deux

Chaque poste qui peut ne pas savoir le **dit** :

```
une valeur accompagnée de sa référence   on a regardé
null accompagné de sa raison             on n'a pas pu regarder
un entier seul                           ne sait pas dire lequel il vaut
```

*Un zéro qui peut signifier « rien trouvé », « rien de mesurable » ou « personne
n'a regardé » est le défaut le plus coûteux de toute la mise au point.*

### Ce qui coupe, et ce qui se contente d'alerter

```
COUPE    une valeur du client altérée et SUBSISTANTE
         — mesurée au différentiel de DONNÉES, jamais à un compteur
COUPE    un élément perdu et non restauré
SUIVI    le taux de réparation — il compte le travail du dispositif,
         pas ce qui sort
```

⚠️ **Un cran qui se déclenche sur le bon fonctionnement finit ignoré — et il
l'aurait été au moment où il aurait eu raison.** Le premier seuil posé comptait
les réparations : il arrêtait une production dont le résultat était
irréprochable.

---

## ④ Le contrat déclare ce sur quoi on AGIT, laisse ouvert ce qu'on LIT

Aujourd'hui le résultat d'un travail ne déclare que quatre champs et s'ouvre au
reste. **Tout ce qui décide de quelque chose traverse sans être nommé** — les
postes de garde, ce que le modèle a tenté d'altérer, la référence de comparaison.
Le front consommateur les type dans un fichier temporaire.

> **Un écran qui devine ce que le serveur envoie n'est pas un produit, c'est une
> convention entre deux sessions** — et un fichier de types temporaire en est la
> trace matérielle. Elle survivra dix-huit mois.

**Règle** : ce sur quoi un consommateur **agit** se déclare ; ce qu'un humain
**lit** peut rester ouvert.

```
DÉCLARÉ    les postes de garde · ce que le modèle a tenté · la référence
           de comparaison · l'état du passage · le verdict
OUVERT     les compteurs d'observabilité — jetons, pas, comptes d'outils
```

---

## ⑤ L'ordre de construction

**La lecture d'abord.** L'opérateur d'une flotte a besoin, dans cet ordre :

```
① l'ÉTAT d'un passage        avancement · déclenchements de gardes ·
                             taux de réparation · travaux morts
② le VERDICT de vague        structuré, au même endroit
③ le LANCEMENT en verbe      plus tard — un shell suffit d'ici là
```

**Lancer est un geste rare et scriptable ; suivre est continu.** Aujourd'hui le
suivi n'existe que parce qu'une session pousse des messages à une autre.

⚠️ **Et l'ordre a une conséquence de conception** : ①② sont de la **lecture
seule**. Ils ne peuvent rien casser, ils se livrent vite, et ils rendent un
opérateur autonome. ③ écrit dans un fichier client — il attend que le reste ait
tourné.

---

### Une garde EXPIRE, ou se reprend par un fait daté

**Une garde armée qui n'est jamais rendue bloque un tableau jusqu'à on ne sait
quand.** Le passage suivant se heurte à un refus, quelqu'un désarme les gardes à
la main — **et à partir de là, désarmer devient le geste normal.**

*C'est exactement la dette qu'on solde ailleurs : des centaines de lignes
retenues par des traitements morts qui n'ont jamais rendu ce qu'ils tenaient.*

> **Les gardes d'un passage portent une échéance.** Passé ce délai, elles ne
> tiennent plus rien — et un passage qui reprend le tableau le dit dans son
> journal, avec la date de ce qu'il a trouvé.

### Un refus de concurrence distingue le vivant du résidu

**Deux situations n'ont rien à voir et ne se disent pas pareil :**

```
un passage VIVANT tient le tableau
   ⟹ dire QUI, DEPUIS QUAND, et QUOI FAIRE : attendre, ou prendre un
      périmètre disjoint. Un refus qui enseigne.

un passage MORT a laissé ses gardes armées
   ⟹ ce n'est pas une concurrence, c'est un RÉSIDU — et c'est ce cas qui
      fabrique la mauvaise habitude, parce qu'il n'a aucune raison d'attendre.
```

---

## ⑥ Ce qui doit résister, c'est le PASSAGE — pas le monde autour de lui

**Un passage ne gèle jamais rien.** Ni une publication, ni un déploiement, ni un
tag. **La résilience est une propriété du traitement — reprise, idempotence,
bail qui expire — pas du processus de livraison.**

⚠️ **C'est une correction, et elle porte sur ce document lui-même.** Une première
version décrivait comme « imprécise » une garde qui refuse de publier pendant
qu'un passage tourne, et proposait de l'affiner pour qu'elle distingue un
document d'un changement de code. **La bonne critique n'était pas là :**

> **Une garde de ce genre ne doit pas exister.** *« On ne peut pas se contraindre
> à ça — c'est la production qui doit résister. »*

**Un dispositif qui protège un passage en bloquant les livraisons déplace son
coût sur tout le monde**, et finit contourné par ceux qu'il gêne le plus. Ce
n'est pas un principe : c'est mesuré. Un gel des livraisons a été essayé fin
août puis levé — **deux rejeux, zéro perte sur neuf tags**. *Le coût réel d'une
coupure était plus petit que celui du gel.*

### Le même défaut, vu des deux bords

**Une garde qui reste armée après la mort de son passage** et **une garde qui
gèle les livraisons pendant sa vie** sont la même erreur de conception :

> **faire porter au reste du système la fragilité qu'on n'a pas traitée chez
> soi.**

**Donc `fleet` n'embarque aucune garde de ce genre** — ni « on ne déploie pas
pendant un passage », ni « on ne publie pas tant qu'un verdict n'est pas rendu ».

**Ce qu'elle embarque à la place** : un passage qui **survit** à un déploiement.

```
reprenable    un travail interrompu se reprend là où il en était
idempotent    le rejouer ne double rien
bail qui EXPIRE   il libère de lui-même, il ne bloque pas jusqu'à
                  ce que quelqu'un vienne le rendre
```

⚠️ **Et la conséquence pratique sur la mesure** : un passage coupé en deux par un
déploiement produit des relevés hétérogènes. **Ce n'est pas une raison de geler
— c'est une raison de DATER**, pour qu'un verdict puisse dire quelle moitié a
tourné sous quelle version.

---

## Ce que ce document ne tranche pas

- **Le nom des opérations** et la forme exacte de leurs arguments.
- **Qui a le droit de lancer** — un rôle, une option d'organisation, rien.
- **La durée d'une échéance de garde**, et ce qu'un passage écrit dans son
  journal quand il reprend un tableau tenu par un mort.
