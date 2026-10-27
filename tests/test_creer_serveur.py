"""Le programme qui remplit le serveur support et de test, avec un faux serveur Discord."""

import asyncio
import itertools

import discord

from draft_bot import creer_serveur, support
from draft_bot import formatting as fmt
from draft_bot.bot import DraftBot
from draft_bot.storage import Store

ids = itertools.count(5000)


class FakeHTTPResponse:
    status, reason = 403, "Forbidden"


class Perms:
    def __init__(self, allowed=True):
        self.manage_channels = self.manage_roles = self.manage_messages = allowed


class Me:
    def __init__(self, allowed=True):
        self.guild_permissions = Perms(allowed)


class Role:
    def __init__(self, name, **options):
        self.id = next(ids)
        self.name = name
        self.options = options


class Message:
    def __init__(self, content, file):
        self.content, self.file, self.pinned = content, file, False

    async def pin(self):
        self.pinned = True


class Channel:
    def __init__(self, name, category=None, topic=None, overwrites=None):
        self.id = next(ids)
        self.name = name
        self.category = category
        self.topic = topic
        self.overwrites = overwrites or {}
        self.mention = f"<#{self.id}>"
        self.messages = []
        self.last_message_id = None

    async def send(self, content, file=None, **kwargs):
        message = Message(content, file)
        self.messages.append(message)
        self.last_message_id = next(ids)
        return message


class Member:
    def __init__(self, name="William"):
        self.id = next(ids)
        self.display_name = name
        self.roles = []

    async def add_roles(self, *roles, reason=None):
        self.roles.extend(roles)


class Guild:
    def __init__(self, name="Support du bot", text=("général",), voice=("Général",), allowed=True, fail_after=None):
        self.id = next(ids)
        self.name = name
        self.owner = Member()
        self.owner_id = self.owner.id
        self.me = Me(allowed)
        self.default_role = Role("@everyone")
        self.roles = [self.default_role]
        self.categories = []
        self.text_channels = [Channel(n) for n in text]
        self.voice_channels = [Channel(n) for n in voice]
        self.calls = 0
        self.fail_after = fail_after

    def _count(self):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise discord.Forbidden(FakeHTTPResponse(), "refusé")

    async def create_role(self, name, reason=None, **options):
        self._count()
        role = Role(name, **options)
        self.roles.append(role)
        return role

    async def create_category(self, name, reason=None):
        self._count()
        category = Channel(name)
        self.categories.append(category)
        return category

    async def create_text_channel(self, name, category=None, topic=None, overwrites=None, reason=None):
        self._count()
        channel = Channel(name, category, topic, overwrites)
        self.text_channels.append(channel)
        return channel

    def channel(self, name):
        return next(c for c in self.text_channels if c.name == name)


class Client:
    def __init__(self, guilds):
        self.guilds = guilds
        self.application_id = 123456789012345678
        self.user = None


def answers(*replies):
    queue = list(replies)

    async def ask(prompt):
        return queue.pop(0) if queue else ""
    return ask


def run(client, *replies, guild_ids=()):
    out = []
    done = asyncio.run(creer_serveur.fill_server(client, list(guild_ids), answers(*replies), out.append))
    return done, "\n".join(out)


def all_channel_names():
    return [c.name for cat in support.BLUEPRINT for c in cat.channels]


def test_fills_an_empty_server():
    guild = Guild()
    done, out = run(Client([guild]), "1", "oui", guild_ids=[guild.id])
    assert done and "Terminé!" in out
    assert [c.name for c in guild.categories] == [cat.name for cat in support.BLUEPRINT]
    names = [c.name for c in guild.text_channels]
    assert all(name in names for name in all_channel_names())
    assert "général" in names  # le salon de départ n'est pas touché
    role_names = [r.name for r in guild.roles]
    for name in [support.SUPPORT_ROLE, support.TESTER_ROLE, *support.TEST_TEAMS]:
        assert role_names.count(name) == 1
    # Les rôles d'équipe se mentionnent : le bot doit pouvoir appeler l'équipe au tour.
    assert all(r.options.get("mentionable") for r in guild.roles if r.name in support.TEST_TEAMS)
    # Le propriétaire reçoit Support et les équipes de test, pas Testeurs.
    given = {r.name for r in guild.owner.roles}
    assert given == {support.SUPPORT_ROLE, *support.TEST_TEAMS}
    assert "GUILD_IDS" not in out


def test_channels_land_in_their_category_with_pinned_messages():
    guild = Guild()
    run(Client([guild]), "1", "oui")
    for category_plan in support.BLUEPRINT:
        category = next(c for c in guild.categories if c.name == category_plan.name)
        for plan in category_plan.channels:
            channel = guild.channel(plan.name)
            assert channel.category is category and channel.topic
            if plan.message:
                assert len(channel.messages) == 1 and channel.messages[0].pinned
                assert len(channel.messages[0].content) <= 2000
                assert "{" not in channel.messages[0].content  # tout a été rempli
            else:
                assert channel.messages == []
    welcome = guild.channel("👋-bienvenue").messages[0].content
    assert guild.channel("📜-règles").mention in welcome and guild.channel("🐛-bugs").mention in welcome


def test_read_only_channels_still_allow_bot_commands():
    guild = Guild()
    run(Client([guild]), "1", "oui")
    for name in ("👋-bienvenue", "📜-règles", "📢-annonces", "draft-liste", "draft-sélections"):
        overwrite = guild.channel(name).overwrites[guild.default_role]
        assert overwrite.send_messages is False and overwrite.use_application_commands is True
        assert guild.channel(name).overwrites[guild.me].send_messages is True
    for name in ("❓-aide", "🐛-bugs", "💡-suggestions", "test-commandes"):
        assert guild.channel(name).overwrites == {}


def test_test_message_explains_setup_and_carries_a_valid_players_file():
    guild = Guild()
    run(Client([guild]), "1", "oui")
    message = guild.channel("test-commandes").messages[0]
    assert guild.channel("draft-liste").mention in message.content
    assert guild.channel("draft-sélections").mention in message.content
    assert all(team in message.content for team in support.TEST_TEAMS)
    assert message.file.filename == support.PLAYERS_FILE
    # Le fichier joint passe tel quel par l'étape « 2. Joueurs » du panneau.
    rows = fmt.parse_players(fmt.decode(support.players_csv()))
    assert rows == support.TEST_PLAYERS and not fmt.duplicate_names(rows)
    # Les noms d'équipe collés à l'étape « 3. Ordre » donnent l'ordre attendu.
    order_text = message.content.split("```")[1]
    order = fmt.build_order(fmt.parse_order(order_text), support.TEST_ROUNDS, True)
    assert len(order) == len(support.TEST_TEAMS) * support.TEST_ROUNDS
    assert len(support.TEST_PLAYERS) > len(order)


def test_team_roles_match_the_draft_announcements():
    bot = DraftBot.__new__(DraftBot)
    guild = Guild()
    run(Client([guild]), "1", "oui")
    for team in support.TEST_TEAMS:
        assert DraftBot.team_role(bot, guild, team).name == team
    subject, verb = fmt.proud_team("Le Canadien de Montréal")
    assert (subject, verb) == ("Le Canadien de Montréal", "est fier")


def test_running_twice_creates_nothing_new():
    guild = Guild()
    run(Client([guild]), "1", "oui")
    before = (len(guild.roles), len(guild.categories), len(guild.text_channels),
              sum(len(c.messages) for c in guild.text_channels))
    done, out = run(Client([guild]), "1", "oui")
    after = (len(guild.roles), len(guild.categories), len(guild.text_channels),
             sum(len(c.messages) for c in guild.text_channels))
    assert done and before == after and "0 éléments créés" in out


def test_interrupted_build_is_finished_by_a_second_run():
    guild = Guild(fail_after=8)
    done, out = run(Client([guild]), "1", "oui")
    assert not done and "relance ce programme" in out
    guild.fail_after = None
    done, _ = run(Client([guild]), "1", "oui")
    assert done
    names = [c.name for c in guild.text_channels]
    assert all(names.count(name) == 1 for name in all_channel_names())
    assert len(guild.categories) == len(support.BLUEPRINT)
    # Le salon créé juste avant l'erreur reçoit quand même son message, une seule fois.
    for category_plan in support.BLUEPRINT:
        for plan in category_plan.channels:
            assert len(guild.channel(plan.name).messages) == (1 if plan.message else 0), plan.name


def test_a_channel_someone_wrote_in_gets_no_extra_message():
    guild = Guild(text=("général", "❓-aide"))
    guild.channel("❓-aide").last_message_id = 42
    run(Client([guild]), "1", "oui")
    assert guild.channel("❓-aide").messages == []
    assert guild.channel("🐛-bugs").messages


def test_refuses_a_real_league_server():
    league = Guild("Ma ligue", text=("général", "draft", "échanges", "signatures", "résultats"))
    done, out = run(Client([league]), "1", "oui")
    assert not done and "jamais un vrai serveur de ligue" in out
    assert league.calls == 0 and league.categories == []


def test_missing_permissions_give_an_invite_link():
    guild = Guild(allowed=False)
    done, out = run(Client([guild]), "1", "oui")
    assert not done and "Gérer les salons" in out and "permissions=8" in out
    assert guild.calls == 0


def test_cancel_or_wrong_number_changes_nothing():
    guild = Guild()
    for replies in (("",), ("7",), ("abc",), ("1", "non"), ("1", "")):
        done, out = run(Client([guild]), *replies)
        assert not done and "Annulé" in out
    assert guild.calls == 0


def test_picks_the_right_server_among_several():
    a, b = Guild("Zèbre"), Guild("Alpha")
    done, out = run(Client([a, b]), "2", "oui")
    assert done and "1. Alpha" in out and "2. Zèbre" in out
    assert a.categories and not b.categories


def test_no_server_gives_an_invite_link():
    done, out = run(Client([]))
    assert not done and "discord.com" in out and "permissions=8" in out


def test_warns_when_server_is_not_in_guild_ids():
    guild = Guild()
    done, out = run(Client([guild]), "1", "oui", guild_ids=[1])
    assert done and "GUILD_IDS" in out and str(guild.id) in out


def test_not_a_bot_command():
    # Les ligues ne voient jamais cet outil : les commandes du bot restent les mêmes.
    bot = DraftBot(Store(":memory:"), [], backup_dir=None)
    assert {c.name for c in bot.tree.get_commands()} == {"admin", "aide", "draft", "statut", "roue"}


def test_texts_fit_discord_limits():
    for category in support.BLUEPRINT:
        assert len(category.name) <= 100
        for plan in category.channels:
            assert len(plan.name) <= 100 and len(plan.topic) <= 1024
            assert plan.name == plan.name.lower() and " " not in plan.name
