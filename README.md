# Bot de draft pour ligue de hockey simulée

Bot Discord qui gère le draft de ligues EHM / SHM. Ce projet est indépendant du site de la ligue : il ne partage aucun fichier avec lui. Un même bot peut servir plusieurs ligues : chaque serveur Discord a son propre draft, complètement séparé.

## Fonctionnement

- Le salon **liste** affiche les joueurs disponibles dans des messages du bot.
- Chaque équipe a un **rôle Discord du même nom** (ex. rôle « Laval » pour l'équipe Laval). Les majuscules et les accents ne comptent pas.
- Quand c'est le tour d'une équipe, le bot mentionne son rôle dans le salon **sélections**.
- Un membre qui a ce rôle fait `/draft <joueur>`, puis clique **Confirmer**.
- Le joueur disparaît de la liste, le choix est annoncé et l'équipe suivante est mentionnée.
- Avec `/draft-temps`, un délai s'affiche pour chaque choix. Une fois le délai écoulé, le bot mentionne le rôle admin, qui peut choisir avec `/forcedraft`.

Les réponses du bot aux commandes sont privées : le salon de la liste reste propre.

## Commandes

| Commande | Qui | Rôle |
| --- | --- | --- |
| `/draft-salons liste selections` | Admin | Choisir les deux salons |
| `/draft-liste fichier` | Admin | Importer les joueurs et publier la liste |
| `/draft-ordre equipes rondes serpentin` | Admin | Définir l'ordre (ou `fichier` avec l'ordre complet, une équipe par ligne) |
| `/draft-temps delai admin` | Admin | Délai par choix (`8h`, `90m`, `1h30`, `0` pour aucun) et rôle à avertir |
| `/draft-echange numero equipe` | Admin | Donner un choix à une autre équipe |
| `/draft joueur` | Équipe au tour | Repêcher un joueur |
| `/forcedraft joueur` | Admin | Repêcher pour l'équipe au tour |
| `/draft-annuler` | Admin | Annuler le dernier choix |
| `/draft-statut` | Tous | Voir qui est au tour, le temps limite et les prochains choix |
| `/draft-journal` | Admin | Télécharger le journal de toutes les actions (fichier Excel/CSV) |

« Admin » veut dire la permission *Gérer le serveur*, vérifiée à chaque utilisation. Toute action qui change le draft demande une confirmation par bouton.

### Fichier des joueurs

Un fichier CSV ou texte, une ligne par joueur : nom, équipe actuelle, position. La virgule, le point-virgule et la tabulation fonctionnent, et une ligne d'en-tête est ignorée.

```
Nom;Équipe;Position
Patrick Roy;Granby;G
Mario Lemieux;Laval;C
```

### Fichier d'ordre

Une équipe par ligne, dans l'ordre complet du draft. Les choix échangés sont simplement écrits au nom de leur nouveau propriétaire.

```
Laval
Granby
Granby
Laval
```

Réimporter la liste ou redéfinir l'ordre remet le draft à zéro. Le bot demande une confirmation et fait une sauvegarde avant.

## Fiabilité

- **Un choix à la fois** : le bot revérifie, au moment du clic sur Confirmer, que c'est encore le tour de ce choix, que le membre a encore le rôle et que le joueur est encore disponible. Deux confirmations en même temps ne peuvent pas prendre le même joueur ni sauter un tour.
- **Rien ne se perd au redémarrage** : le draft, le chrono et le journal sont dans la base `draft.db`. Une alerte de délai manquée pendant que le bot était éteint part dès son retour.
- **Sauvegardes automatiques** dans le dossier `sauvegardes` : au démarrage, chaque heure, et avant chaque `/draft-liste` ou `/draft-ordre`. Les 48 plus récentes sont gardées. Pour revenir en arrière, arrêter le bot, remplacer `draft.db` par une sauvegarde renommée `draft.db`, puis relancer.
- **Serveurs autorisés** : le bot quitte tout seul un serveur qui n'est pas dans `GUILD_IDS`.

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

6. Dans Discord : `/draft-salons`, `/draft-liste`, `/draft-ordre`, puis `/draft-temps` si on veut un délai.

### Ajouter un serveur autorisé

1. Dans Discord, *Paramètres utilisateur* > *Avancés* > activer le *Mode développeur*.
2. Clic droit sur l'icône du serveur > *Copier l'identifiant du serveur*.
3. Ajouter cet identifiant à `GUILD_IDS` dans `.env`, séparé des autres par une virgule.
4. Redémarrer le bot, puis l'inviter sur le serveur.

## Tests

```
pip install pytest
python -m pytest
```
