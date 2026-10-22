# Bot de draft pour ligue de hockey simulée

Bot Discord qui gère le draft d'une ligue EHM / SHM. Ce projet est indépendant du site de la ligue : il ne partage aucun fichier avec lui.

## Fonctionnement

- Le salon **liste** affiche les joueurs disponibles dans des messages du bot.
- Le DG dont c'est le tour fait `/draft <joueur>`. Le nom se complète automatiquement.
- Le joueur disparaît de la liste et le choix est annoncé dans le salon **sélections**, avec une mention du DG suivant.

Les réponses du bot aux commandes sont privées : le salon de la liste reste propre.

## Commandes

| Commande | Qui | Rôle |
| --- | --- | --- |
| `/draft-salons liste selections` | Admin | Choisir les deux salons |
| `/draft-liste fichier` | Admin | Importer les joueurs et publier la liste |
| `/draft-ordre equipes rondes serpentin` | Admin | Définir l'ordre (ou `fichier` avec l'ordre complet, une équipe par ligne) |
| `/draft-dg equipe membre` | Admin | Associer un DG à une équipe |
| `/draft-echange numero equipe` | Admin | Donner un choix à une autre équipe |
| `/draft joueur` | DG au tour | Repêcher un joueur |
| `/forcedraft joueur` | Admin | Repêcher pour l'équipe au tour |
| `/draft-annuler` | Admin | Annuler le dernier choix |
| `/draft-statut` | Tous | Voir qui est au tour et les prochains choix |

« Admin » veut dire la permission *Gérer le serveur*. On peut changer qui a accès à chaque commande dans Paramètres du serveur > Intégrations.

### Fichier des joueurs

Un fichier CSV ou texte, une ligne par joueur : nom, équipe actuelle, position. La virgule, le point-virgule et la tabulation fonctionnent, et une ligne d'en-tête est ignorée.

```
Nom;Équipe;Position
Patrick Roy;Granby;G
Mario Lemieux;Laval;C
```

Réimporter la liste ou redéfinir l'ordre remet le draft à zéro.

## Installation

1. Sur le [portail des développeurs Discord](https://discord.com/developers/applications), créer une application, puis un bot, et copier son jeton.
2. Inviter le bot avec les portées `bot` et `applications.commands`, et les permissions *Voir les salons*, *Envoyer des messages* et *Gérer les messages*.
3. Dans le salon de la liste, retirer *Envoyer des messages* aux membres mais leur laisser *Utiliser les commandes de l'application*.
4. Installer et lancer (Python 3.10 ou plus) :

```
python -m venv .venv
.venv\Scripts\activate        # Windows (sinon : source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env        # puis remplir DISCORD_TOKEN et GUILD_ID
python -m draft_bot
```

5. Dans Discord : `/draft-salons`, `/draft-liste`, `/draft-ordre`, puis `/draft-dg` pour chaque équipe.

## Tests

```
pip install pytest
python -m pytest
```
