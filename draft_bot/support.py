"""Construction d'un serveur support et de test : catégories, salons, rôles et messages.

Le bot ne peut pas créer un serveur Discord : l'admin crée un serveur vide, y invite le bot,
puis lance une fois `python -m draft_bot.creer_serveur` (voir creer_serveur.py), qui construit le reste.
Ce n'est pas une commande du bot : les serveurs des ligues n'y ont jamais accès. Chaque élément déjà présent (même
nom) est gardé tel quel : recommencer après une erreur termine le travail sans rien doubler.
"""

import io
from dataclasses import dataclass, field

import discord

from .storage import normalize

# Un serveur neuf (« Créer le mien ») a un salon texte et un salon vocal. Au-delà de quelques
# salons, c'est sûrement un vrai serveur de ligue : le bot refuse d'y toucher.
MAX_FOREIGN_CHANNELS = 3

SUPPORT_ROLE = "Support"
TESTER_ROLE = "Testeurs"
# Fausses équipes pour tester le draft seul : le bot donne ces rôles à l'admin qui construit.
TEST_TEAMS = ["Les Bruins de Boston", "Le Canadien de Montréal", "Les Maple Leafs de Toronto",
              "Les Sénateurs d'Ottawa"]
TEST_ROUNDS = 3

# Joueurs inventés pour le draft de test : nom, équipe actuelle, position.
TEST_PLAYERS = [
    ("Alexis Tremblay", "Rimouski", "C"), ("Samuel Gagnon", "Halifax", "AG"),
    ("Liam Côté", "Québec", "D"), ("Nathan Bouchard", "Chicoutimi", "G"),
    ("Félix Roy", "Drummondville", "AD"), ("Thomas Pelletier", "Sherbrooke", "D"),
    ("William Lavoie", "Gatineau", "C"), ("Olivier Fortin", "Val-d'Or", "AG"),
    ("Jacob Morin", "Victoriaville", "AD"), ("Zachary Bélanger", "Baie-Comeau", "D"),
    ("Logan Gauthier", "Shawinigan", "G"), ("Raphaël Ouellet", "Moncton", "C"),
    ("Antoine Lefebvre", "Saint John", "AG"), ("Mathis Girard", "Charlottetown", "D"),
    ("Noah Caron", "Cap-Breton", "AD"), ("Édouard Simard", "Blainville", "C"),
    ("Charles Boucher", "Rouyn-Noranda", "D"), ("Émile Poirier", "Rimouski", "AG"),
    ("Hugo Dubé", "Québec", "AD"), ("Jérémy Paquette", "Halifax", "G"),
]

WELCOME = """\
👋 **Bienvenue sur le serveur du bot de draft!**

Ce bot aide les ligues de hockey simulé (EHM / SHM) à faire leur draft sur Discord.

**Pour commencer :**
• Lis les règles dans {rules}.
• Une question ? Écris dans {help}.
• Un problème avec le bot ? Explique-le dans {bugs}.
• Une idée ? Partage-la dans {ideas}.

Les nouvelles du bot sont publiées dans {news}."""

RULES = """\
📜 **Règles du serveur**

1. Reste respectueux avec tout le monde.
2. Une question = un message dans le bon salon : aide, bugs ou suggestions.
3. Pas de publicité ni de spam.
4. Ne partage jamais le jeton (token) d'un bot, même pour avoir de l'aide.

Merci et bon draft! 🏒"""

NEWS = """\
📢 **Le serveur support du bot de draft est ouvert!**
Les nouvelles versions du bot seront annoncées ici."""

HELP = """\
❓ **Besoin d'aide ?**
Écris ta question ici, avec le nom de ta ligue. Une capture d'écran aide beaucoup.

**Les commandes du bot :**
• `/aide` : comment faire son choix au draft
• `/statut` : qui est au tour et les prochains choix
• `/draft` : repêcher un joueur en tapant son nom
• `/roue` : tirer au sort une option
• `/admin` : le panneau des admins pour préparer et gérer le draft"""

BUGS = """\
🐛 **Signaler un bug**
Pour qu'on puisse le corriger vite, écris :
1. Ce que tu as fait (quelle commande ou quel bouton).
2. Ce qui s'est passé, avec une capture d'écran si possible.
3. Ce que tu attendais.
4. L'heure à peu près."""

IDEAS = """\
💡 **Suggestions**
Une idée pour améliorer le bot ? Écris-la ici.
Réagis avec 👍 aux idées des autres que tu aimes : les plus populaires passent en premier."""

TESTS = """\
🧪 **Tester le draft**
Tu as les rôles des {teams} équipes de test : tu peux donc faire tous les choix toi-même.

**Préparer le draft de test** (une seule fois) : tape `/admin`, puis :
1. **Salons** : liste = {liste}, annonces = {picks}
2. **Joueurs** : envoie le fichier **joueurs-test.csv** joint à ce message (télécharge-le d'abord)
3. **Ordre** : colle les équipes ci-dessous, mets **{rounds}** rondes et serpentin **Oui**
```
{team_lines}
```
Ensuite, va dans {picks} et clique sur le bouton vert **Faire mon choix**.
Pour tout recommencer : `/admin`, puis **3. Ordre** de nouveau.

Ce salon-ci sert à essayer les commandes (`/statut`, `/roue`, `/aide`) sans déranger les autres."""

PLAYERS_FILE = "joueurs-test.csv"


def players_csv() -> bytes:
    """Le fichier des joueurs de test, au format que l'étape « 2. Joueurs » attend."""
    lines = ["Nom;Équipe;Position"] + [";".join(row) for row in TEST_PLAYERS]
    # utf-8-sig : Excel affiche correctement les accents si on ouvre le fichier.
    return ("\n".join(lines) + "\n").encode("utf-8-sig")


@dataclass
class ChannelPlan:
    key: str
    name: str
    topic: str
    read_only: bool = False
    message: str = ""
    # Joindre le fichier des joueurs de test au message.
    players_file: bool = False


@dataclass
class CategoryPlan:
    name: str
    channels: list[ChannelPlan]


BLUEPRINT = [
    CategoryPlan("📌 Informations", [
        ChannelPlan("welcome", "👋-bienvenue", "Bienvenue! Commence ici.", read_only=True, message=WELCOME),
        ChannelPlan("rules", "📜-règles", "Les règles du serveur.", read_only=True, message=RULES),
        ChannelPlan("news", "📢-annonces", "Les nouvelles du bot.", read_only=True, message=NEWS),
    ]),
    CategoryPlan("🆘 Support", [
        ChannelPlan("help", "❓-aide", "Pose tes questions sur le bot ici.", message=HELP),
        ChannelPlan("bugs", "🐛-bugs", "Un problème avec le bot ? Explique-le ici.", message=BUGS),
        ChannelPlan("ideas", "💡-suggestions", "Tes idées pour améliorer le bot.", message=IDEAS),
    ]),
    CategoryPlan("🧪 Tests du draft", [
        ChannelPlan("liste", "draft-liste", "Les joueurs disponibles du draft de test.", read_only=True),
        ChannelPlan("picks", "draft-sélections", "Les choix du draft de test.", read_only=True),
        ChannelPlan("tests", "test-commandes", "Essaie les commandes du bot ici.", message=TESTS,
                    players_file=True),
    ]),
]

CHANNEL_NAMES = {normalize(c.name) for cat in BLUEPRINT for c in cat.channels}


@dataclass
class BuildReport:
    """Ce que la construction a fait, pour le résumé affiché à la fin."""
    created: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    channels: dict[str, discord.abc.GuildChannel] = field(default_factory=dict)
    roles_given: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def missing_permissions(guild: discord.Guild) -> list[str]:
    perms = guild.me.guild_permissions
    needed = [("manage_channels", "Gérer les salons"), ("manage_roles", "Gérer les rôles"),
              ("manage_messages", "Gérer les messages")]
    return [label for attr, label in needed if not getattr(perms, attr, False)]


def foreign_channels(guild: discord.Guild) -> list[str]:
    """Les salons qui ne viennent pas du plan : sur un vrai serveur de ligue, il y en a beaucoup."""
    channels = list(guild.text_channels) + list(guild.voice_channels)
    return [c.name for c in channels if normalize(c.name) not in CHANNEL_NAMES]


def refusal(guild: discord.Guild) -> str | None:
    """Pourquoi le bot refuse de construire sur ce serveur, ou None s'il peut y aller."""
    missing = missing_permissions(guild)
    if missing:
        return ("Il manque au bot ces permissions sur ce serveur : " + ", ".join(missing) + ".\n"
                "Le plus simple : invite le bot de nouveau avec le lien ci-dessous (permission Administrateur), "
                "puis relance ce programme.")
    foreign = foreign_channels(guild)
    if len(foreign) > MAX_FOREIGN_CHANNELS:
        return (f"Ce serveur a déjà {len(foreign)} salons. Par sécurité, le programme remplit seulement un "
                "serveur vide, créé pour le support et les tests : il ne touche jamais un vrai serveur de ligue.")
    return None


def find_role(guild: discord.Guild, name: str) -> discord.Role | None:
    key = normalize(name)
    return discord.utils.find(lambda role: normalize(role.name) == key, guild.roles)


def find_by_name(items, name: str):
    key = normalize(name)
    return next((item for item in items if normalize(item.name) == key), None)


def overwrites(guild: discord.Guild, read_only: bool) -> dict:
    if not read_only:
        return {}
    # Les membres lisent et utilisent les commandes du bot, mais n'écrivent pas : le salon reste propre.
    return {
        guild.default_role: discord.PermissionOverwrite(send_messages=False, add_reactions=True,
                                                        use_application_commands=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                              manage_messages=True),
    }


async def ensure_role(guild: discord.Guild, name: str, report: BuildReport, **options) -> discord.Role:
    role = find_role(guild, name)
    if role is not None:
        report.kept.append(f"rôle {name}")
        return role
    role = await guild.create_role(name=name, reason="Serveur de test du bot de draft", **options)
    report.created.append(f"rôle {name}")
    return role


async def build_structure(guild: discord.Guild, member: discord.Member) -> BuildReport:
    """Crée ce qui manque : rôles, catégories, salons, messages épinglés. Donne les rôles à l'admin.

    Une erreur de Discord arrête la construction : le rapport dit ce qui a été fait,
    et relancer le programme termine le reste.
    """
    report = BuildReport()
    support = await ensure_role(guild, SUPPORT_ROLE, report, colour=discord.Colour.blue(), hoist=True,
                                mentionable=True)
    await ensure_role(guild, TESTER_ROLE, report, colour=discord.Colour.green(), mentionable=True)
    teams = [await ensure_role(guild, team, report, mentionable=True) for team in TEST_TEAMS]

    for category_plan in BLUEPRINT:
        category = find_by_name(guild.categories, category_plan.name)
        if category is None:
            category = await guild.create_category(category_plan.name, reason="Serveur de test du bot de draft")
            report.created.append(f"catégorie {category_plan.name}")
        for plan in category_plan.channels:
            channel = find_by_name(guild.text_channels, plan.name)
            if channel is None:
                channel = await guild.create_text_channel(
                    plan.name, category=category, topic=plan.topic, overwrites=overwrites(guild, plan.read_only),
                    reason="Serveur de test du bot de draft")
                report.created.append(f"salon #{plan.name}")
            else:
                report.kept.append(f"salon #{plan.name}")
            report.channels[plan.key] = channel

    # Les messages vont seulement dans les salons encore vides : jamais de doublon, et un salon
    # créé juste avant une erreur reçoit son message au prochain lancement.
    mentions = {key: channel.mention for key, channel in report.channels.items()}
    mentions.update(teams=len(TEST_TEAMS), rounds=TEST_ROUNDS, team_lines="\n".join(TEST_TEAMS))
    for category_plan in BLUEPRINT:
        for plan in category_plan.channels:
            if plan.message and report.channels[plan.key].last_message_id is None:
                extra = {"file": discord.File(io.BytesIO(players_csv()), filename=PLAYERS_FILE)} if plan.players_file else {}
                message = await report.channels[plan.key].send(plan.message.format(**mentions),
                                                               allowed_mentions=discord.AllowedMentions.none(), **extra)
                try:
                    await message.pin()
                except discord.HTTPException:
                    report.problems.append(f"message de #{plan.name} non épinglé")

    to_give = [role for role in [support, *teams] if role not in member.roles]
    if to_give:
        try:
            await member.add_roles(*to_give, reason="Serveur de test du bot de draft")
            report.roles_given = [role.name for role in to_give]
        except discord.HTTPException:
            report.problems.append("rôles non donnés (le rôle du bot doit être plus haut dans la liste des rôles)")
    return report
