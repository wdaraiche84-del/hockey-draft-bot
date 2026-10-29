# Bot de draft pour ligue de hockey simulée

Bot Discord qui gère le draft de ligues EHM / SHM. Ce projet est indépendant du site de la ligue : il ne partage aucun fichier avec lui. Un même bot peut servir plusieurs ligues : chaque serveur Discord a son propre draft, complètement séparé.

## Fonctionnement

Le bot est fait pour rester simple : les DG cliquent sur des boutons, et les admins font tout depuis un seul panneau.

- Chaque équipe a un **rôle Discord du même nom** (ex. rôle « Laval » pour l'équipe Laval). Les majuscules et les accents ne comptent pas.
- Le salon **liste** affiche les joueurs disponibles dans des messages du bot.
- Quand c'est le tour d'une équipe, le bot mentionne son rôle dans le salon **sélections**, avec un bouton vert **🏒 Faire mon choix**.
- Le DG clique sur le bouton, choisit le joueur dans le menu, puis clique **Confirmer**. S'il y a plus de 25 joueurs, une petite fenêtre lui demande quelques lettres du nom, puis le bot montre les joueurs trouvés.
- Le joueur disparaît de la liste et le choix est annoncé : « 🏒 **Les Bruins de Boston** sont fiers de sélectionner **David Pastrnak** (Boston · AD) au **5e rang** (ronde 1, choix 5). » L'équipe suivante est mentionnée, avec le bouton. Le bouton est retiré de l'annonce précédente : un seul bouton reste en vue.
- L'annonce reprend le nom du rôle tel quel. Un nom qui commence par « Les », « Le », « La » ou « L' » est accordé (« Le Canadien de Montréal est fier »). Un nom d'un seul mot donne « Laval est fier », et un nom de plusieurs mots sans article reçoit « Les » devant.
- Avec un délai, le temps limite s'affiche pour chaque choix. Une fois le délai écoulé, le bot mentionne le rôle admin, qui peut choisir depuis le panneau.

Les réponses du bot sont privées : les salons restent propres. Après un choix réussi, la réponse privée « ✅ repêché » s'efface toute seule après quelques secondes.

## Commandes

| Commande | Qui | Rôle |
| --- | --- | --- |
| `/admin` | Admin | Ouvre le panneau admin : préparer et gérer le draft |
| `/aide` | Tous | Explique en quelques lignes comment faire son choix |
| `/statut` | Tous | Qui est au tour, le temps limite et les prochains choix. L'équipe au tour y trouve aussi le bouton **Faire mon choix** |
| `/draft joueur` | Équipe au tour | Pour ceux qui préfèrent taper : repêcher un joueur par son nom |
| `/roue choix titre` | Tous | Tirer au sort une option, chances égales (ex. : `ott, stl`, titre `gambrell`). Résultat public et noté au journal |

« Admin » veut dire la permission *Gérer le serveur*, revérifiée à chaque clic. Toute action qui change le draft demande une confirmation par bouton.

### Le panneau `/admin`

Le panneau ne montre qu'un seul bouton à la fois : la prochaine chose à faire. Il se met à jour tout seul après chaque changement.

**Préparer le draft** (un bouton bleu par étape, dans l'ordre) :

1. **Choisir les salons** : choisir dans deux menus le salon de la liste et le salon des annonces.
2. **Importer les joueurs** : envoyer le fichier des joueurs (voir plus bas).
3. **Définir l'ordre du draft** : écrire les équipes de la 1re ronde, le nombre de rondes, et si l'ordre s'inverse à chaque ronde (serpentin).
4. **▶ Lancer le draft** : le draft commence et la première équipe est avertie. Personne ne peut choisir avant.

**Pendant le draft** : **Choisir pour l'équipe au tour** et **Annuler le dernier choix**.

**Plus d'options** (toujours là) : changer les salons, les joueurs ou l'ordre, **Délai par choix** (`8h`, `90m`, `1h30`, `0` pour aucun, et le rôle à avertir), **Échanger un choix** (numéro du choix + rôle de la nouvelle équipe) et **Journal** (fichier Excel/CSV de toutes les actions). Changer l'ordre pendant un draft le remet à zéro : il faut le relancer.

Le panneau se ferme après 14 minutes (limite de Discord) : refaire `/admin` pour le rouvrir.

### Annoncer une transaction

Le bouton **📣 Annoncer une transaction** est toujours dans `/admin`, avant, pendant et après le draft.

1. Faire une capture d'écran du tableau des transactions de SHM (colonnes « Vers » et « Éléments », avec les logos). Une partie du tableau suffit.
2. Cliquer sur **Annoncer une transaction**, ajouter la capture et choisir le salon des annonces de transactions (le bot s'en souvient).
3. Le bot montre ce qu'il a lu : qui reçoit quoi, pour chaque transaction. Tout est coché, sauf ce qui a déjà été annoncé. Décocher ce qu'il ne faut pas annoncer.
4. Une erreur ? **Corriger** : choisir le rôle de l'équipe et corriger les éléments (un par ligne). Le bot retient ce logo pour la prochaine fois.
5. **Annoncer** : une annonce par transaction, de la plus ancienne à la plus récente, qui mentionne le rôle de chaque équipe :

```
🔁 Transaction
➡️ @Flames reçoit : Daniil Tarasov
➡️ @Oilers reçoit : Choix de 2e ronde 2020 (Blues)
```

La lecture est **gratuite** et se fait sur l'ordinateur du bot (RapidOCR) : aucune image n'est envoyée ailleurs. Les logos des 30 équipes SHM sont reconnus. Le rôle d'une équipe est trouvé par son nom, son surnom, sa ville ou son code (« Bruins », « Les Bruins de Boston », « Boston », « BOS »). Si deux rôles conviennent, ou aucun, l'aperçu le signale et l'admin choisit avec **Corriger**. Rien n'est publié avant le clic sur **Annoncer**.

### Emotes d'équipe

Si le serveur a une emote pour une équipe, le bot la met devant le nom de l'équipe dans les annonces de transactions et de choix au draft. L'emote doit porter le code ou le nom de l'équipe : `:PIT:`, `:pit:`, `:Penguins:` ou `:Pittsburgh_Penguins:` (pas `:PIT2:`). Rien à régler : le bot les trouve tout seul.

### Transactions SHM automatiques

Le bot peut aussi lire lui-même les transactions sur SHM et les annoncer sans que personne n'ait rien à faire.

1. Le bot a son propre compte SHM (voir `SHM_USERNAME` et `SHM_PASSWORD` dans l'installation). Le commissaire de la ligue ajoute ce compte comme **membre de sa ligue, sans équipe**.
2. Dans `/admin` : **Plus d'options**, puis **🔄 Transactions SHM auto**. Coller l'adresse de la ligue sur SHM (ex. `https://simulatedhockeymanager.ca/lnhs_2`) et choisir le salon des annonces.
3. Le bot essaie tout de suite : s'il ne voit pas la ligue, il le dit et ne change rien. S'il la voit, les transactions déjà sur SHM ne sont **pas** réannoncées : seules les suivantes le seront.

Ensuite, le bot regarde la page **Historique des transactions** de la ligue toutes les 5 minutes et annonce chaque nouvelle transaction, de la plus ancienne à la plus récente, dans le même format que les captures. Une transaction déjà annoncée avec une capture n'est pas répétée, et renommer un rôle ne fait jamais réannoncer d'anciennes transactions. Après une longue panne du bot, il lit jusqu'à 3 pages pour rattraper son retard.

**Plus d'options** montre l'état : la ligue suivie, le salon et la dernière lecture réussie, ou l'erreur. Si SHM ne répond plus pendant une heure, le bot le dit une fois dans le salon, puis dit quand ça remarche. Laisser l'adresse vide arrête les annonces automatiques.

Le bot demande au plus une page SHM toutes les 2 secondes, et une ligue suivie par plusieurs serveurs n'est lue qu'une fois. Si SHM refuse le mot de passe, le bot n'insiste pas (nouvel essai après 30 minutes), pour ne pas faire bloquer le compte.

### Fichier des joueurs

Un fichier CSV ou texte, une ligne par joueur : nom, équipe actuelle, position. La virgule, le point-virgule et la tabulation fonctionnent, et une ligne d'en-tête est ignorée.

```
Nom;Équipe;Position
Patrick Roy;Granby;G
Mario Lemieux;Laval;C
```

### Ordre avec des choix échangés

Dans **Définir l'ordre du draft** (ou **Plus d'options**, **Changer l'ordre**), écrire tous les choix du draft dans l'ordre, une équipe par ligne, et mettre **1** ronde. Les choix échangés sont simplement écrits au nom de leur nouveau propriétaire.

```
Laval
Granby
Granby
Laval
```

Un choix peut aussi être échangé plus tard avec **Échanger un choix**.

Réimporter la liste ou redéfinir l'ordre remet le draft à zéro. Le bot le dit, demande une confirmation et fait une sauvegarde avant.

## Fiabilité

- **Un choix à la fois** : le bot revérifie, au moment du clic sur Confirmer, que c'est encore le tour de ce choix, que le membre a encore le rôle et que le joueur est encore disponible. Deux confirmations en même temps ne peuvent pas prendre le même joueur ni sauter un tour.
- **Rien ne se perd au redémarrage** : le draft, le chrono et le journal sont dans la base `draft.db`. Une alerte de délai manquée pendant que le bot était éteint part dès son retour.
- **Sauvegardes automatiques** dans le dossier `sauvegardes` : au démarrage, chaque heure, et avant chaque import de joueurs ou nouvel ordre. Les 48 plus récentes sont gardées. Pour revenir en arrière, arrêter le bot, remplacer `draft.db` par une sauvegarde renommée `draft.db`, puis relancer.
- **Plusieurs serveurs** : le bot marche sur tout serveur où il est invité, sans rien modifier. Chaque serveur a son propre draft. Les commandes sont publiées serveur par serveur (au démarrage et dès qu'il arrive sur un nouveau serveur), et les anciennes commandes globales sont effacées pour éviter les doublons. Le bot étant privé (*Public Bot* décoché), seul son propriétaire peut l'inviter.

## Installation

1. Sur le [portail des développeurs Discord](https://discord.com/developers/applications), créer une application, puis un bot, et copier son jeton. Décocher *Public Bot*.
2. Inviter le bot avec les portées `bot` et `applications.commands`, et les permissions *Voir les salons*, *Envoyer des messages*, *Lire l'historique des messages*, *Gérer les messages* et *Mentionner @everyone, @here et tous les rôles* (pour mentionner les rôles d'équipe).
3. Dans le salon de la liste, retirer *Envoyer des messages* aux membres mais leur laisser *Utiliser les commandes de l'application*.
4. Créer un rôle par équipe, au nom exact de l'équipe, et le donner à son DG.
5. Installer et lancer (Python 3.11 ou plus pour lire les captures de transactions ; 3.10 suffit pour le reste) :

```
pip install -r requirements.txt
copy .env.example .env        # puis remplir DISCORD_TOKEN
python -m draft_bot
```

6. Dans Discord : `/admin`, puis suivre les étapes 1, 2, 3 (et 4 pour un délai).
7. Facultatif, pour les transactions SHM automatiques : créer un compte SHM pour le bot, puis écrire son nom d'utilisateur et son mot de passe dans `.env` (`SHM_USERNAME` et `SHM_PASSWORD`) et relancer le bot.

### Mettre à jour le bot

Après avoir remplacé le dossier `draft_bot` et le fichier `requirements.txt` par la nouvelle version, mettre à jour les bibliothèques une fois (le panneau admin demande discord.py 2.7 ou plus, et la lecture des captures télécharge environ 100 Mo la première fois), puis relancer :

```
pip install -U -r requirements.txt
python -m draft_bot
```

Garder `.env` et `draft.db` : le draft en cours est conservé.

### Ajouter un serveur

Inviter le bot sur le serveur avec le même lien d'invitation (portées `bot` et `applications.commands`). Les commandes apparaissent en quelques secondes, sans redémarrer ni modifier `.env`. Si elles n'apparaissent pas, le bot a été invité sans la portée `applications.commands` : le réinviter avec le bon lien.

## Hébergement sur Railway

Le dépôt est prêt pour Railway : `railway.json` donne la commande de démarrage (`python -m draft_bot`) et redémarre le bot s'il plante, et `.python-version` fixe Python 3.12.

1. Sur Railway : **New Project** > **Deploy from GitHub repo** > `hockey-draft-bot`.
2. Dans le service : **Variables** > ajouter `DISCORD_TOKEN` (le jeton du bot), et `SHM_USERNAME` et `SHM_PASSWORD` pour les transactions SHM automatiques. `GUILD_IDS` n'est plus nécessaire : elle peut être supprimée, elle ne gêne pas.
3. Clic droit sur le service (ou **Ctrl+K**) > **Attach volume**, chemin de montage : `/data`. La base du draft, son journal et les sauvegardes y sont gardés entre les redéploiements.
4. Arrêter le bot sur le PC avant que Railway le démarre : un même jeton ne doit tourner qu'à un seul endroit.

Sans volume, le bot refuse de démarrer sur Railway, pour ne jamais perdre un draft au redéploiement. Une deuxième copie du bot ne peut pas ouvrir la même base : pendant un redéploiement, la nouvelle copie attend que l'ancienne s'arrête.

Chaque merge sur `main` redéploie le bot automatiquement.

La lecture d'une capture se fait dans un processus à part : elle utilise environ 400 Mo de mémoire pendant quelques secondes, puis les rend. Une seule capture est lue à la fois.

## Tests

```
pip install pytest
python -m pytest
```
