"""Scénarios complets des commandes, avec de faux objets Discord."""

import asyncio
import itertools

import pytest

from draft_bot.bot import DraftBot
from draft_bot.storage import Store

ids = itertools.count(1000)


class FakeMessage:
    def __init__(self, channel, content):
        self.id = next(ids)
        self.channel = channel
        self.content = content


class FakePartial:
    def __init__(self, channel, message_id):
        self.channel, self.id = channel, message_id

    async def edit(self, content):
        self.channel.messages[self.id].content = content

    async def delete(self):
        self.channel.messages.pop(self.id, None)


class FakeChannel:
    def __init__(self, name):
        self.id = next(ids)
        self.name = name
        self.mention = f"#{name}"
        self.messages = {}

    async def send(self, content, **kwargs):
        message = FakeMessage(self, content)
        self.messages[message.id] = message
        return message

    def get_partial_message(self, message_id):
        return FakePartial(self, message_id)

    def texts(self):
        return [m.content for m in self.messages.values()]


class FakeRole:
    def __init__(self, name):
        self.id = next(ids)
        self.name = name
        self.mention = f"<@&{self.id}>"


class FakeGuild:
    def __init__(self, roles, channels):
        self.id = next(ids)
        self.roles = roles
        self.channels = {c.id: c for c in channels}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)

    def get_member(self, user_id):
        return None


class FakeMember:
    def __init__(self, guild, roles):
        self.id = next(ids)
        self.guild = guild
        self.roles = roles


class FakeResponse:
    def __init__(self, interaction):
        self.interaction = interaction
        self.done = False

    def is_done(self):
        return self.done

    async def send_message(self, content, **kwargs):
        self.done = True
        self.interaction.sent.append((content, kwargs.get("view")))

    async def edit_message(self, content, **kwargs):
        self.done = True
        self.interaction.sent.append((content, None))


class FakeFollowup:
    def __init__(self, interaction):
        self.interaction = interaction

    async def send(self, content, **kwargs):
        self.interaction.sent.append((content, kwargs.get("view")))


class FakeInteraction:
    def __init__(self, guild, user):
        self.guild = guild
        self.guild_id = guild.id
        self.user = user
        self.sent = []
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)

    async def edit_original_response(self, **kwargs):
        pass

    @property
    def last(self):
        return self.sent[-1][0]

    @property
    def view(self):
        return self.sent[-1][1]


@pytest.fixture
def league():
    store = Store(":memory:")
    bot = DraftBot(store, [], backup_dir=None)
    laval, granby = FakeRole("Laval"), FakeRole("Granby")
    liste, selections = FakeChannel("draft-liste"), FakeChannel("draft-selections")
    guild = FakeGuild([laval, granby], [liste, selections])
    store.set_channels(guild.id, liste.id, selections.id)
    store.replace_players(guild.id, [("Patrick Roy", "Granby", "G"), ("Mario Lemieux", "Laval", "C"),
                                     ("Ray Bourque", "Montréal", "D")])
    store.set_order(guild.id, ["laval", "granby", "granby", "laval"])
    gm_laval, gm_granby = FakeMember(guild, [laval]), FakeMember(guild, [granby])
    return bot, store, guild, gm_laval, gm_granby, selections


async def command(bot, name, interaction, *args):
    await bot.tree.get_command(name).callback(interaction, *args)
    return interaction


async def confirm(interaction):
    view = interaction.view
    assert view is not None, interaction.last
    await view.confirm.callback(interaction)
    return interaction


def run(coro):
    return asyncio.run(coro)


def test_gm_on_the_clock_picks_after_confirming(league):
    bot, store, guild, gm_laval, _, selections = league

    async def scenario():
        i = await command(bot, "draft", FakeInteraction(guild, gm_laval), "Roy")
        assert "Laval** repêche **Patrick Roy" in i.last
        assert store.made_picks_count(guild.id) == 0  # rien avant la confirmation
        await confirm(i)
        assert "✅" in i.last
    run(scenario())
    assert store.current_pick(guild.id).team == "Granby"
    announce = selections.texts()[-1]
    assert "Patrick Roy" in announce and "<@&" in announce  # le rôle suivant est mentionné


def test_other_team_cannot_pick(league):
    bot, store, guild, _, gm_granby, _ = league
    i = run(command(bot, "draft", FakeInteraction(guild, gm_granby), "Roy"))
    assert "pas ton tour" in i.last
    assert i.view is None


def test_cancel_button_changes_nothing(league):
    bot, store, guild, gm_laval, _, _ = league

    async def scenario():
        i = await command(bot, "draft", FakeInteraction(guild, gm_laval), "Roy")
        await i.view.cancel.callback(i)
        return i
    i = run(scenario())
    assert "Rien n'a été changé" in i.last
    assert store.made_picks_count(guild.id) == 0


def test_two_pending_confirmations_cannot_both_pick(league):
    bot, store, guild, gm_laval, _, _ = league

    async def scenario():
        first = await command(bot, "draft", FakeInteraction(guild, gm_laval), "Roy")
        second = await command(bot, "draft", FakeInteraction(guild, gm_laval), "Lemieux")
        await asyncio.gather(confirm(first), confirm(second))
        return first, second
    first, second = run(scenario())
    assert store.made_picks_count(guild.id) == 1
    assert "Rien n'a été enregistré" in second.last


def test_same_player_cannot_be_drafted_twice(league):
    bot, store, guild, gm_laval, gm_granby, _ = league

    async def scenario():
        granby_waiting = None
        i = await command(bot, "draft", FakeInteraction(guild, gm_laval), "Roy")
        await confirm(i)
        granby_waiting = await command(bot, "draft", FakeInteraction(guild, gm_granby), "Roy")
        return granby_waiting
    i = run(scenario())
    assert "Aucun joueur disponible" in i.last


def test_role_removed_before_confirm_is_refused(league):
    bot, store, guild, gm_laval, _, _ = league

    async def scenario():
        i = await command(bot, "draft", FakeInteraction(guild, gm_laval), "Roy")
        gm_laval.roles = []
        await confirm(i)
        return i
    i = run(scenario())
    assert "plus ton tour" in i.last
    assert store.made_picks_count(guild.id) == 0


def test_forcedraft_and_undo_are_logged(league):
    bot, store, guild, _, _, selections = league
    admin = FakeMember(guild, [])

    async def scenario():
        await confirm(await command(bot, "forcedraft", FakeInteraction(guild, admin), "Bourque"))
        await confirm(await command(bot, "draft-annuler", FakeInteraction(guild, admin)))
    run(scenario())
    assert store.made_picks_count(guild.id) == 0
    assert [e[2] for e in store.log_entries(guild.id)] == ["choix admin", "annulation"]
    assert "retourne dans la liste" in selections.texts()[-1]


def test_list_shrinks_after_each_pick(league):
    bot, store, guild, gm_laval, _, _ = league
    liste = guild.get_channel(store.get_channels(guild.id)[0])

    async def scenario():
        await bot.refresh_list(guild.id, liste, repost=True)
        assert "Patrick Roy" in liste.texts()[0]
        await confirm(await command(bot, "draft", FakeInteraction(guild, gm_laval), "Roy"))
    run(scenario())
    assert len(liste.messages) == 1
    assert "Patrick Roy" not in liste.texts()[0]
    assert "Joueurs disponibles : 2" in liste.texts()[0]


def test_time_up_alert_pings_admin_role_once(league):
    bot, store, guild, _, _, selections = league
    admins = FakeRole("Admins")
    guild.roles.append(admins)
    store.set_timer(guild.id, 60, admins.id)
    store.start_clock(guild.id, now=0)
    bot.get_guild = lambda guild_id: guild if guild_id == guild.id else None

    async def scenario():
        await bot.check_timers.coro(bot)
        await bot.check_timers.coro(bot)
    run(scenario())
    alerts = [t for t in selections.texts() if "Temps écoulé" in t]
    assert len(alerts) == 1 and admins.mention in alerts[0]


def test_admin_commands_check_permission_every_time():
    bot = DraftBot(Store(":memory:"), [], backup_dir=None)
    admin = {"draft-salons", "draft-liste", "draft-ordre", "draft-temps", "draft-echange",
             "forcedraft", "draft-annuler", "draft-journal"}
    for command in bot.tree.get_commands():
        if command.name in admin:
            assert command.default_permissions.manage_guild, command.name
            assert command.checks, command.name
        else:
            assert command.default_permissions is None, command.name


def test_unlisted_server_is_left():
    bot = DraftBot(Store(":memory:"), [111], backup_dir=None)
    left = []

    class Guild:
        def __init__(self, guild_id):
            self.id, self.name = guild_id, "x"

        async def leave(self):
            left.append(self.id)

    async def scenario():
        await bot.on_guild_join(Guild(111))
        await bot.on_guild_join(Guild(222))
    asyncio.run(scenario())
    assert left == [222]
