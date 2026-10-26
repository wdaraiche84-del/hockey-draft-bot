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

Le panneau montre l'état du draft avec des ✅ et des ❌, et la prochaine étape à faire. Il se met à jour tout seul après chaque changement.

**Préparer le draft** (dans l'ordre) :

1. **Salons** : choisir dans deux menus le salon de la liste et le salon des annonces.
2. **Joueurs** : envoyer le fichier des joueurs (voir plus bas).
3. **Ordre** : écrire les équipes de la 1re ronde, le nombre de rondes, et si l'ordre s'inverse à chaque ronde (serpentin).
4. **Délai** (facultatif) : le temps pour chaque choix (`8h`, `90m`, `1h30`, `0` pour aucun) et le rôle à avertir.

**Pendant le draft** : **Choisir pour l'équipe au tour**, **Annuler le dernier choix**, **Échanger un choix** (numéro du choix + rôle de la nouvelle équipe), **Journal** (fichier Excel/CSV de toutes les actions) et **Actualiser**.

Le panneau se ferme après 14 minutes (limite de Discord) : refaire `/admin` pour le rouvrir.

### Fichier des joueurs

Un fichier CSV ou texte, une ligne par joueur : nom, équipe actuelle, position. La virgule, le point-virgule et la tabulation fonctionnent, et une ligne d'en-tête est ignorée.

```
Nom;Équipe;Position
Patrick Roy;Granby;G
Mario Lemieux;Laval;C
```

### Ordre avec des choix échangés

Dans **3. Ordre**, écrire tous les choix du draft dans l'ordre, une équipe par ligne, et mettre **1** ronde. Les choix échangés sont simplement écrits au nom de leur nouveau propriétaire.

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
- **Serveurs autorisés** : le bot quitte tout seul un serveur qui n'est pas dans `GUILD_IDS`. Les commandes y sont publiées serveur par serveur, et les anciennes commandes globales sont effacées au démarrage pour éviter les doublons.

## Installation

1. Sur le [portail des développeurs Discord](https://discord.com/developers/applications), créer une application, puis un bot, et copier son jeton. Décocher *Public Bot*.
2. Inviter le bot avec les portées `bot` et `applications.commands`, et les permissions *Voir les salons*, *Envoyer des messages*, *Lire l'historique des messages*, *Gérer les messages* et *Mentionner @everyone, @here et tous les rôles* (pour mentionner les rôles d'équipe).
3. Dans le salon de la liste, retirer *Envoyer des messages* aux membres mais leur laisser *Utiliser les commandes de l'application*.
4. Créer un rôle par équipe, au nom exact de l'équipe, et le donner à son DG.
5. Installer et lancer (Python 3.10 ou plus) :

```
pip install -r requirements.txt
copy .env.example .env        # puis remplir DISCORD_TOKEN et GUILD_IDS
python -m draft_bot
```

6. Dans Discord : `/admin`, puis suivre les étapes 1, 2, 3 (et 4 pour un délai).

### Mettre à jour le bot

Après avoir remplacé le dossier `draft_bot` et le fichier `requirements.txt` par la nouvelle version, mettre à jour les bibliothèques une fois (le panneau admin demande discord.py 2.7 ou plus), puis relancer :

```
pip install -U -r requirements.txt
python -m draft_bot
```

Garder `.env` et `draft.db` : le draft en cours est conservé.

### Ajouter un serveur autorisé

1. Dans Discord, *Paramètres utilisateur* > *Avancés* > activer le *Mode développeur*.
2. Clic droit sur l'icône du serveur > *Copier l'identifiant du serveur*.
3. Ajouter cet identifiant à `GUILD_IDS` dans `.env`, séparé des autres par une virgule.
4. Redémarrer le bot, puis l'inviter sur le serveur.

## Hébergement sur Railway

Le dépôt est prêt pour Railway : `railway.json` donne la commande de démarrage (`python -m draft_bot`) et redémarre le bot s'il plante, et `.python-version` fixe Python 3.12.

1. Sur Railway : **New Project** > **Deploy from GitHub repo** > `hockey-draft-bot`.
2. Dans le service : **Variables** > ajouter `DISCORD_TOKEN` (le jeton du bot) et `GUILD_IDS` (les serveurs autorisés).
3. Clic droit sur le service (ou **Ctrl+K**) > **Attach volume**, chemin de montage : `/data`. La base du draft, son journal et les sauvegardes y sont gardés entre les redéploiements.
4. Arrêter le bot sur le PC avant que Railway le démarre : un même jeton ne doit tourner qu'à un seul endroit.

Sans volume, le bot refuse de démarrer sur Railway, pour ne jamais perdre un draft au redéploiement. Une deuxième copie du bot ne peut pas ouvrir la même base : pendant un redéploiement, la nouvelle copie attend que l'ancienne s'arrête.

Chaque merge sur `main` redéploie le bot automatiquement.

## Tests

```
pip install pytest
python -m pytest
```
