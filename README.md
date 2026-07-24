# MLB Value Bot — Étapes 0 & 1

Ce dossier contient le tout début du projet : un bot qui récupère le
programme MLB du jour + les pitchers probables, et te l'envoie sur
Telegram. Pas encore d'analyse, pas encore de value — on valide juste
que toute la chaîne fonctionne. Les étapes suivantes (stats avancées,
cotes, scoring, value) viendront se brancher par-dessus.

## Contenu du dossier

- `telegram_bot.py` — la fonction qui envoie un message sur Telegram
- `get_schedule.py` — récupère le programme MLB du jour et l'envoie
- `requirements.txt` — les librairies Python nécessaires
- `.env.example` — modèle pour tes identifiants (à copier en `.env`)

## Étape A — Créer ton bot Telegram (5 minutes)

1. Ouvre Telegram, cherche le compte **@BotFather** (c'est le bot officiel
   pour créer des bots).
2. Envoie-lui `/newbot`.
3. Il te demande un nom (affiché aux utilisateurs) puis un identifiant
   unique qui doit finir par `bot`, ex : `MonMLBValueBot`.
4. BotFather te donne un **token**, une longue chaîne du type
   `123456789:ABCdefGhIJKlmNoPQRstuVWxyz`. C'est ta clé secrète —
   ne la partage jamais publiquement.

## Étape B — Créer le channel/groupe qui recevra les messages

**Option la plus simple : un channel public**
1. Dans Telegram, crée un nouveau channel (public ou privé).
2. Si tu le rends **public**, donne-lui un nom d'utilisateur, ex :
   `@mon_mlb_value_bot_feed`. Ce nom d'utilisateur sera directement ton
   `TELEGRAM_CHAT_ID` (avec le `@`).
3. Ajoute ton bot comme **administrateur** du channel (Gérer le
   channel → Administrateurs → Ajouter → cherche ton bot par son nom).

**Si tu préfères un channel/groupe privé** (pas de nom d'utilisateur public) :
1. Ajoute quand même le bot comme administrateur.
2. Envoie un message quelconque dans le channel/groupe.
3. Dans ton navigateur, va sur :
   `https://api.telegram.org/bot<TON_TOKEN>/getUpdates`
   (remplace `<TON_TOKEN>` par ton vrai token).
4. Tu verras du JSON contenant un champ `"chat":{"id": -1001234567890, ...}`.
   Ce nombre (avec le signe `-`) est ton `TELEGRAM_CHAT_ID`.

## Étape C — Installer Python et les dépendances

Sur Windows, ouvre PowerShell et vérifie que Python est bien installé :

```powershell
python --version
```

Si la commande n'est pas reconnue, c'est probablement un souci de PATH
(le même genre de souci que tu as eu avec Node.js) — dis-le-moi si ça
bloque, on le réglera ensemble.

Une fois Python confirmé, place-toi dans le dossier du projet et installe
les dépendances :

```powershell
cd mlb-value-bot
python -m pip install -r requirements.txt
```

## Étape D — Configurer tes identifiants

1. Copie `.env.example` en `.env` :

```powershell
copy .env.example .env
```

2. Ouvre `.env` avec le Bloc-notes (ou VS Code) et remplace les valeurs
   par ton vrai token et ton vrai chat_id :

```
TELEGRAM_TOKEN=123456789:ABCdefGhIJKlmNoPQRstuVWxyz
TELEGRAM_CHAT_ID=@mon_mlb_value_bot_feed
```

3. **Important** : ce fichier `.env` ne doit jamais être partagé ni
   poussé sur GitHub (on ajoutera un `.gitignore` quand on créera le
   repo GitHub, pour l'automatisation).

## Étape E — Premier test : le bot sait-il t'écrire ?

```powershell
python telegram_bot.py
```

Si tout est bien configuré, tu dois recevoir sur ton channel/groupe
Telegram : *"✅ Le bot MLB est bien connecté à Telegram."*

Si tu as une erreur, elle t'indique généralement quoi corriger
(token invalide, chat_id incorrect, bot pas admin du channel...).

## Étape F — Récupérer le programme MLB du jour

```powershell
python get_schedule.py
```

Tu dois recevoir la liste des matchs MLB du jour, avec pour chacun :
le stade, l'heure (en UTC pour l'instant), et les deux pitchers
probables (ou "Titulaire non encore annoncé" si ce n'est pas encore
sorti côté MLB).

Tu peux aussi tester une date précise :

```powershell
python get_schedule.py 2026-07-21
```

## Une fois que ça fonctionne

Dis-moi quand tu reçois bien les matchs sur Telegram — on passera à
l'étape 2 : brancher les stats avancées (pitchers, bullpen) sur chaque
match via `pybaseball`, pour commencer à construire le scoring.

---

## Étape 2 — Stats du titulaire + offense adverse

Cette étape ajoute, pour chaque match : les stats du titulaire probable
de chaque équipe, et les stats offensives de l'équipe qu'il affronte.
C'est l'équivalent MLB du bloc "% de victoires vs style adverse" de ta
capture tennis.

**Note importante** : la première version de ce script utilisait
`pybaseball` pour aller chercher des stats sur FanGraphs. FanGraphs
bloque désormais les accès automatisés (erreur 403) — un problème connu
depuis mi-2025, non résolu par les mainteneurs à ce jour, et qui ne se
corrige pas de notre côté. On utilise donc uniquement l'**API officielle
MLB** (les mêmes serveurs que `get_schedule.py`), qui reste stable et
sans blocage.

**Petit glossaire rapide** (pas besoin de les retenir, juste pour situer) :
- **ERA** : moyenne de points encaissés — plus bas = meilleur
- **WHIP** : nombre de coureurs autorisés par manche (hits + buts sur
  balles) — plus bas = meilleur
- **FIP** (calculé nous-mêmes à partir des K/BB/HR/IP officiels, formule
  sabermétrique standard) : version "ajustée" de l'ERA qui isole ce que
  le pitcher contrôle vraiment, indépendamment de sa défense — plus bas
  = meilleur, comparable directement à l'ERA
- **AVG / OBP / SLG / OPS** : indicateurs offensifs classiques d'une
  équipe (moyenne au bâton, % d'arrivée sur but, puissance, et OPS =
  OBP+SLG, l'indicateur combiné le plus utilisé) — plus haut = meilleur

### Installer les dépendances

```powershell
cd C:\mlb-value-bot
python -m pip install -r requirements.txt
```

(Rien de nouveau à installer si tu avais déjà fait l'étape 1 — ce script
n'utilise que `requests`, déjà en place.)

### Lancer le script

```powershell
python get_matchup_stats.py
```

Tu dois recevoir, pour chaque match, un message avec les stats du
titulaire de chaque équipe et les stats offensives de l'équipe adverse.

**Limitation connue à ce stade** (normal, on la corrige à l'étape 3) :
les stats offensives sont celles de l'équipe entière, pas encore
filtrées "vs gaucher/vs droitier" spécifiquement contre la main du
titulaire du jour — et le bullpen n'est pas encore inclus.

### Une fois que ça fonctionne

Dis-moi si les stats arrivent bien et si les chiffres te semblent
cohérents (tu es bien placé pour le vérifier). On passera ensuite à
l'étape 3 : park factor, météo, repos/voyage, et H2H — puis à l'étape 4,
les cotes et la détection de value.

---

## Étape 3 — Park factor, météo, repos et H2H

Cette étape ajoute le contexte autour de chaque match : la tendance du
stade (favorable lanceurs ou frappeurs), la météo prévue, le repos de
chaque équipe, et l'historique des confrontations cette saison.

**Aucune nouvelle dépendance à installer** — tout passe par l'API MLB
(déjà utilisée) et Open-Meteo (gratuite, sans clé).

**Note sur le park factor** : les chiffres exacts varient pas mal selon
la source et l'année (on l'a vérifié en cherchant plusieurs références).
Plutôt que de scraper un site tiers — et prendre le risque de retomber
sur un blocage comme avec FanGraphs — le script utilise une table
statique par paliers (très favorable lanceurs / neutre / très favorable
frappeurs), à corriger toi-même à l'occasion si tu vois un chiffre qui
te semble dépassé. Elle est en haut du fichier `get_context_factors.py`,
dans `STADIUM_INFO`.

### Lancer le script

```powershell
python get_context_factors.py
```

Tu dois recevoir, pour chaque match, un message avec :
- le park factor du stade
- la météo prévue (ou une mention "stade avec toit" si la météo n'a pas
  d'impact — Diamondbacks, Astros, Marlins, Brewers, Mariners, Rangers,
  Blue Jays)
- le repos de chaque équipe avant ce match
- le bilan des confrontances de la saison entre les deux équipes

**Cas particulier à surveiller** : les Athletics jouent temporairement à
Sutter Health Park (en attendant leur futur stade de Las Vegas), et les
Rays ont pu être délogés de Tropicana Field après des dégâts de toit. Si
l'un de ces deux cas a changé d'ici que tu lances le bot, corrige juste
la ligne correspondante dans `STADIUM_INFO`.

### Une fois que ça fonctionne

Dis-moi si tout s'affiche correctement. On passera ensuite à l'étape 4 :
les cotes des bookmakers et la détection de value — le cœur de ce que tu
recherches.

---

## Étape 4 — Modèle de probabilité + détection de value (Pinnacle vs Betclic)

C'est le cœur du bot. Deux choses nouvelles :

1. **Un vrai modèle de probabilité**, qu'on n'avait pas encore construit —
   les étapes 2-3 affichaient des stats brutes, sans les combiner. Ici on
   les transforme en un pourcentage de victoire par équipe :
   - **Base** : méthode **Log5** (Bill James), sur le bilan **domicile** de
     l'équipe qui reçoit et le bilan **extérieur** de l'équipe qui visite —
     ça intègre naturellement l'avantage du terrain, sans constante à
     inventer.
   - **Forme récente** : bilan des 10 derniers matchs de chaque équipe, EN
     PLUS du bilan de saison utilisé dans le Log5 (pas à sa place).
   - **Qualité du titulaire** : écart de FIP de chaque titulaire vs la
     moyenne de la ligue.
   - **Adversité** : l'avantage du titulaire est modulé par la force de la
     ligne de batteurs qu'il affronte CE SOIR — en priorité le **line-up
     réel** s'il est déjà publié (généralement 1 à 3h avant le match),
     sinon l'OPS de saison de l'équipe.
   - **Repos** (back-to-back) et **fatigue du bullpen** (lancers des
     relievers sur les 2-3 derniers jours, hors titulaire).
   - **H2H de la saison**, avec un poids qui grossit avec le nombre de
     matchs déjà joués entre les deux équipes (jusqu'à 12) — les rivaux de
     division se recroisent 13 à 19 fois par saison, donc l'échantillon
     devient vite plus solide qu'au tennis par exemple.

**⚠️ Point d'attention sur le line-up réel** : ce point du script dépend
d'un champ de l'API MLB (`hydrate=lineups`) que je n'ai pas pu tester en
conditions réelles. S'il ne remonte jamais rien de grave ne se passe — le
modèle retombe automatiquement sur l'OPS de saison — mais dis-le moi si tu
vois toujours `[line-up réel : ...]` absent des messages même lancé en
soirée, on corrigera le nom du champ ensemble.

**Note sur la vitesse** : la fatigue du bullpen ajoute pas mal d'appels
réseau (jusqu'à 3 boxscores par équipe). Le script est donc sensiblement
plus lent qu'avant — c'est normal, pas un bug.
2. **Les cotes**, via [The Odds API](https://the-odds-api.com) — **⚠️ pas
   theoddsapi.com, un service différent et beaucoup plus restrictif sur
   son offre gratuite.** On récupère Pinnacle (référence "sharp" du
   marché, utilisée pour calculer la value) et un book réellement jouable
   pour toi. **Betclic ne couvre pas le MLB sur cette API** (vérifié en
   listant tous les bookmakers renvoyés pour un soir de programme complet
   -- aucune trace de Betclic, tous matchs confondus) : le script utilise
   donc **Unibet** comme prix jouable de référence, Betclic restant en
   secours si jamais il apparaît un jour.
   peux réellement jouer) en un seul appel gratuit.

**Important à garder en tête** : les poids de l'ajustement titulaire/repos/
H2H sont un point de départ raisonnable, pas un résultat validé. Le
marché moneyline MLB est assez efficient — ce modèle ne vaut que ce que
le débrief quotidien va progressivement lui apprendre à valoir.

### Créer ta clé The Odds API (2 minutes, gratuit)

1. Va sur **https://the-odds-api.com** (vérifie bien l'URL — un site au
   nom presque identique existe et est beaucoup plus limité en gratuit)
2. Crée un compte gratuit (aucune carte bancaire demandée)
3. Récupère ta clé API sur ton tableau de bord
4. Ajoute-la dans ton `.env` :
   ```
   ODDS_API_KEY=ta_clé
   ```

### Lancer le script

```powershell
python get_value_bets.py
```

Tu dois recevoir, pour chaque match : la proba du modèle, le duel des
titulaires, un **détail vulgarisé** (en langage courant plutôt qu'en
chiffres bruts) expliquant ce qui pèse vraiment ce soir, le favori, la
cote Unibet/Betclic, la proba de marché dévigorée de Pinnacle, la value
avec son sens en une phrase, et un **bloc avis + suggestion** codé par
couleur selon la fiabilité du signal :
- 🟢 **vert** : value fiable (favori et value peuvent être d'accord ou
  pas — ça n'a pas d'importance, ce qui compte c'est que le signal est solide)
- 🟠 **orange** : value détectée mais suspecte (edge très large, non
  confirmé) — méfiance, mise automatiquement réduite
- ⚪ **gris** : aucune value, rien à jouer ce soir sur ce match

**Cas normaux à ne pas confondre avec un bug** :
- "Cotes indisponibles" : le marché n'est pas encore ouvert chez les
  bookmakers pour ce match (fréquent si tu lances le script tôt le matin)
- "Non listée" : Betclic ou Unibet ne propose pas ce match précis

### Splits lanceur supplémentaires -- Vague 1 (expérimental)

Quatre nouveaux facteurs, tous via le même mécanisme `statSplits`/`sitCodes`
de l'API MLB (non testé en direct pendant l'écriture -- même prudence que
pour le platoon et les line-ups) :

- **Domicile/extérieur** : ERA du titulaire à domicile ou en déplacement, comparée à son ERA de saison
- **Jour/nuit** : même principe, poids volontairement limité
- **Platoon** : OPS de l'équipe adverse spécifiquement contre la main du
  titulaire (gauche/droite), comparé à son OPS de saison -- plutôt que
  d'estimer la main de chaque batteur individuellement (donnée qu'on n'a
  pas), on utilise directement la vraie stat d'équipe existante
- **Vs cette équipe** : ERA du titulaire face à l'équipe adverse cette
  saison, avec un seuil de fiabilité plus bas (l'échantillon y est
  structurellement petit, 2-3 confrontations par saison au mieux)

Chacun retombe silencieusement sur 0.0 si les données sont indisponibles
ou l'échantillon trop petit (`MIN_SPLIT_IP` / `MIN_VS_TEAM_IP` dans
`get_value_bets.py`) -- un souci sur l'un n'affecte jamais les autres.

**Météo** : récupérée et affichée sur chaque match (température, vent,
humidité), mais **pas encore pondérée** dans le calcul du moneyline --
son effet réel joue sur le nombre de runs marqués, pas directement sur
qui gagne. Elle est prête pour le jour où on construira le marché Total.

### Style de jeu et batteurs déjà rencontrés -- Vague 2

Deux ajouts supplémentaires, construits à partir de stats déjà fiables
(pas de nouvelle source à risque) :

- **Style de jeu de la ligne adverse** : K% et BB% (approche au bâton --
  agressive/contact tôt vs patiente) et volume de vols de but sur la
  saison (pression sur les bases). Une ligne qui prend beaucoup de
  strikeouts et peu de buts sur balles est un peu plus facile à négocier
  pour un lanceur ; une ligne patiente et qui court beaucoup est plus coriace.
- **Historique des batteurs face à ce titulaire précis** : moyenne des
  confrontations passées cette saison entre les batteurs du line-up réel
  et le titulaire adverse. Poids volontairement très faible
  (`VS_PITCHER_SENSITIVITY`) et seuil minimum de passages au bâton
  (`MIN_VS_PITCHER_PA`) -- l'échantillon par duel individuel est presque
  toujours minuscule, donc peu fiable en soi. Ne s'active que si le
  line-up réel est publié (mêmes contraintes que l'arsenal).

**Non retenu pour l'instant** : la qualité du bullpen (distincte de sa
fatigue récente, déjà présente). L'isoler proprement demanderait de
dépouiller les boxscores de toute la saison, et une approximation
grossière ferait doublon avec ce que le Log5 capture déjà via le bilan
global de l'équipe.

### Ajustement arsenal (expérimental)

En plus de l'OPS de saison ("adversité"), le modèle croise maintenant
l'**arsenal précis du titulaire** (quels types de lancers, à quelle
fréquence) avec la performance de **chaque batteur du line-up réel**
contre chacun de ces types. Beaucoup plus fin qu'une moyenne OPS — un
lanceur avec une slider dominante face à une ligne qui slugge mal contre
les sliders, ça se voit maintenant dans le détail vulgarisé quand c'est
le facteur qui pèse le plus.

**Deux limites à connaître** :
1. Ne s'active QUE si le **line-up réel** est publié (on a besoin des
   vrais batteurs, pas d'une moyenne d'équipe) — sinon `arsenal +0.0`, et
   le modèle retombe sur l'adversité OPS classique. Lance le script dans
   ta fenêtre habituelle de 2h avant les matchs pour en profiter.
2. C'est la brique **la plus expérimentale** du modèle. Les données
   viennent de Baseball Savant (un domaine différent de FanGraphs, donc
   pas concerné par son blocage), mais je n'ai pas pu tester en conditions
   réelles pendant l'écriture. Si `arsenal` reste à `+0.0` sur TOUS les
   matchs (même ceux avec line-up réel confirmé), regarde les diagnostics
   affichés dans le terminal au premier chargement (`Chargement de
   l'arsenal des lanceurs...` puis la liste de colonnes) et colle-les-moi
   — ça voudra dire qu'un nom de colonne ne correspond pas à ce que
   Baseball Savant renvoie vraiment, et on corrigera ensemble dans
   `pitch_arsenal.py`.

### Une fois que ça fonctionne

Lance-le chaque soir avec ta bankroll de test, et fais un point le
lendemain : les probas du modèle collent-elles à ce qui s'est passé ? Les
"value" détectées se vérifient-elles dans la durée ? C'est ce débrief qui
va nous dire quoi ajuster — les poids du modèle, les seuils de value, ou
des facteurs à ajouter. On regardera aussi la couverture du marché Total
(Over/Under) une fois que tu as ta clé, pour voir si Betclic/Pinnacle le
proposent en pratique.

### Résumé de fin de soirée (système d'unités + euros)

Le dernier message envoyé chaque lancement de `get_value_bets.py` est un
résumé avec les **5 picks les plus fiables** ce soir-là (`MAX_SUMMARY_PICKS`)
— les messages par match, eux, gardent l'analyse complète de tous les
matchs, résumé ou pas. Chaque pick est dimensionné en **unités (u)** et
traduit directement en **euros** selon ta grille personnelle :

| Unités | Euros |
|---|---|
| 2u | 5€ |
| 1u | 2.5€ |
| 0.5u | 2.5€ ou 0€ (à ta discrétion) |

(`EUR_PER_UNIT = 2.5` dans `get_value_bets.py` et `debrief.py` si tu veux
changer l'échelle plus tard — 1u = 1% d'une bankroll de suivi fictive de
100 qui sert de référence pour le tracking, indépendamment de tes vraies
mises en euros.)

La mise n'est **pas** proportionnelle à la taille de l'edge — elle reflète
la fiabilité perçue du signal (voir la discussion Blue Jays/Rays plus
haut : un edge énorme contre Pinnacle seul est souvent le signe d'une
erreur, pas d'une pépite) :
- **0.5u** : edge très large (>15 pts) — suspect, mise plafonnée par précaution
- **1u** : edge modéré (3-8 pts), sans confirmation supplémentaire
- **2u** : edge fort (≥8 pts), sans confirmation supplémentaire
- **+1u** en plus si Betclic/Unibet et Pinnacle sont d'accord entre eux
- **+0.5u** en plus si le line-up réel est confirmé côté du pick
- **2u maximum**, quel que soit le cumul

Les poids exacts (`MAX_UNITS_PER_PICK` et la logique dans `stake_units()`)
sont un point de départ, à ajuster avec les débriefs.

### Débrief automatique (fini le copier-coller)

`debrief.py` compare les prédictions loggées la veille aux résultats
réels (récupérés automatiquement sur l'API MLB), règle les picks avec
mise, met à jour la bankroll de suivi, et t'envoie tout sur Telegram —
sans que tu aies à recopier quoi que ce soit depuis le chat.

```powershell
python debrief.py                # débrief d'hier
python debrief.py 2026-07-21     # débrief d'une date précise
```

Le message contient :
- le bilan du **favori du modèle sur TOUS les matchs** de la soirée (ex: 11/15)
- le détail match par match (✅/❌)
- le règlement des picks avec mise (unités gagnées/perdues, prix utilisé)
- la **bankroll de suivi** à jour (persistée dans `data/bankroll.json`)

C'est un suivi automatique du **modèle**, pas forcément identique à tes
mises réelles sur Betclic — compare les deux toi-même dans ton débrief
personnel. Le débrief est rejouable sans risque : relancer la même date
ne règle jamais deux fois les mêmes picks.

## Analyse de performance (à lancer dans quelques semaines)

`analyze_performance.py` agrège tous les logs (`data/*.json`) avec les
résultats réels, pour voir ce qui fonctionne vraiment dans le modèle :

```powershell
python analyze_performance.py
```

Il ventile la réussite par composante dominante (titulaire, adversité,
arsenal, forme...), par palier de mise (0.5u/1u/1.5u/2u), par accord de
books, et par présence du line-up réel. Depuis le 23 juillet, chaque
prédiction loggée garde le détail des 13 composantes individuelles
(`log_entry["components"]`) -- les logs antérieurs à cette date sont
comptés dans les totaux globaux mais ignorés dans la ventilation par
composante, faute de détail conservé à l'époque.

**Ne pas sur-interpréter avec un petit échantillon** : le script le
rappelle lui-même en bas de sortie -- sous une centaine de mises par
catégorie, les pourcentages affichés restent indicatifs, pas des
conclusions fiables. Utile pour repérer des tendances à surveiller au
fil des semaines, pas pour trancher après quelques jours.



