"""Scénarios complets des commandes, avec de faux objets Discord."""

import asyncio
import itertools

import pytest

import draft_bot.bot as bot_module
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
        self.deleted = False

    async def edit_original_response(self, **kwargs):
        self.sent.append((kwargs.get("content"), kwargs.get("view")))

    async def delete_original_response(self):
        self.deleted = True

    @property
    def last(self):
        return self.sent[-1][0]

    @property
    def view(self):
        return self.sent[-1][1]


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(bot_module, "PICK_REPLY_SECONDS", 0)


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
        return i
    i = run(scenario())
    assert i.deleted  # la réponse privée s'efface : rien ne reste à fermer
    assert store.current_pick(guild.id).team == "Granby"
    announce = selections.texts()[-1]
    assert "**Laval** est fier de sélectionner **Patrick Roy** (Granby · G) au **1er rang**" in announce
    assert "<@&" in announce  # le rôle suivant est mentionné


def test_announcement_uses_role_name(league):
    bot, store, guild, gm_laval, _, selections = league
    guild.roles[0].name = "Les Bruins de Boston"
    store.set_order(guild.id, ["les bruins de boston", "granby"])

    async def scenario():
        await confirm(await command(bot, "draft", FakeInteraction(guild, gm_laval), "Lemieux"))
    run(scenario())
    assert "**Les Bruins de Boston** sont fiers de sélectionner **Mario Lemieux**" in selections.texts()[-1]


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
        await confirm(await command(bot, "annuler-choix", FakeInteraction(guild, admin)))
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
    admin = {"salons", "joueurs", "ordre", "delai", "echange",
             "forcedraft", "annuler-choix", "journal"}
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


def test_new_order_is_announced_publicly_with_role_ping(league):
    bot, store, guild, _, _, selections = league
    admin = FakeMember(guild, [])
    bot.backup = lambda reason: None

    async def scenario():
        i = await command(bot, "ordre", FakeInteraction(guild, admin), "granby, laval", 2, True)
        await confirm(i)
        return i
    i = run(scenario())
    public = selections.texts()[-1]
    assert "Le draft commence" in public
    granby = next(r for r in guild.roles if r.name == "Granby")
    assert granby.mention in public and "**Granby**" in public
    assert "Annoncé dans" in i.last


def test_trade_of_current_pick_pings_new_owner(league):
    bot, store, guild, _, _, selections = league
    admin = FakeMember(guild, [])
    granby = next(r for r in guild.roles if r.name == "Granby")

    async def scenario():
        await confirm(await command(bot, "echange", FakeInteraction(guild, admin), 1, "granby"))
    run(scenario())
    public = selections.texts()[-1]
    assert "Échange" in public and granby.mention in public
    assert store.current_pick(guild.id).team == "Granby"


def test_new_timer_is_announced_during_draft(league):
    bot, store, guild, _, _, selections = league
    admin = FakeMember(guild, [])

    class Deferring(FakeInteraction):
        def __init__(self, *args):
            super().__init__(*args)

            async def defer(**kwargs):
                self.response.done = True
            self.response.defer = defer

    i = run(command(bot, "delai", Deferring(guild, admin), "8h", None))
    assert "Nouveau délai : **8 h**" in selections.texts()[-1]
    assert "Temps limite" in selections.texts()[-1]
    assert "Annoncé dans" in i.last


def test_paths_on_pc_stay_next_to_the_bot():
    from pathlib import Path
    from draft_bot.bot import resolve_paths
    db, backups = resolve_paths({})
    assert db == Path("draft.db")
    assert backups == Path("draft.db").resolve().parent / "sauvegardes"


def test_paths_on_railway_go_to_the_volume():
    from pathlib import Path
    from draft_bot.bot import resolve_paths
    env = {"RAILWAY_ENVIRONMENT": "production", "RAILWAY_VOLUME_MOUNT_PATH": "/data"}
    assert resolve_paths(env) == (Path("/data/draft.db"), Path("/data/sauvegardes"))


def test_railway_without_volume_refuses_to_start():
    from draft_bot.bot import ConfigError, resolve_paths
    with pytest.raises(ConfigError, match="volume"):
        resolve_paths({"RAILWAY_ENVIRONMENT": "production"})


def test_guild_ids_parsing():
    from draft_bot.bot import ConfigError, parse_guild_ids
    assert parse_guild_ids({"GUILD_IDS": "111, 222"}) == [111, 222]
    assert parse_guild_ids({"GUILD_ID": "333"}) == [333]
    assert parse_guild_ids({}) == []
    with pytest.raises(ConfigError):
        parse_guild_ids({"GUILD_IDS": "mon serveur"})


def test_second_copy_of_the_bot_cannot_open_the_same_database(tmp_path):
    from draft_bot.bot import ConfigError, open_store
    path = tmp_path / "draft.db"
    first = open_store(path)
    with pytest.raises(ConfigError, match="autre copie"):
        open_store(path, wait_seconds=0)
    first.set_channels(1, 10, 20)  # la première copie fonctionne toujours
    first.db.close()
    assert open_store(path, wait_seconds=0).get_channels(1) == (10, 20)


class SyncRecorder:
    """Remplace tree.sync : note ce qui serait publié, global (None) ou par serveur."""

    def __init__(self, tree):
        self.tree = tree
        self.published = {}

    async def __call__(self, guild=None):
        key = guild.id if guild else None
        self.published[key] = sorted(c.name for c in self.tree.get_commands(guild=guild))


def test_with_guild_ids_global_commands_are_emptied():
    bot = DraftBot(Store(":memory:"), [111, 222], backup_dir=None)
    recorder = SyncRecorder(bot.tree)
    bot.tree.sync = recorder
    asyncio.run(bot.sync_commands())
    assert recorder.published[None] == []  # les anciennes commandes globales disparaissent
    assert "draft" in recorder.published[111] and "draft" in recorder.published[222]
    assert "draft-dg" not in recorder.published[111]


def test_without_guild_ids_old_guild_commands_are_removed():
    bot = DraftBot(Store(":memory:"), [], backup_dir=None)
    recorder = SyncRecorder(bot.tree)
    bot.tree.sync = recorder

    class Guild:
        id = 333
    bot._connection._guilds = {}
    type(bot).guilds = property(lambda self: [Guild()])
    try:
        asyncio.run(bot.sync_commands())
        asyncio.run(bot.clear_stale_guild_commands())
        asyncio.run(bot.clear_stale_guild_commands())  # une seule fois
    finally:
        del type(bot).guilds
    assert "draft" in recorder.published[None]
    assert recorder.published[333] == []


def test_only_two_commands_start_with_draft():
    bot = DraftBot(Store(":memory:"), [], backup_dir=None)
    names = sorted(c.name for c in bot.tree.get_commands())
    assert [n for n in names if n.startswith("draft")] == ["draft"]
    assert names == sorted(["draft", "forcedraft", "statut", "annuler-choix", "echange", "ordre",
                            "joueurs", "delai", "salons", "journal", "roue"])


def test_renamed_commands_replace_old_ones_on_each_server():
    bot = DraftBot(Store(":memory:"), [111], backup_dir=None)
    recorder = SyncRecorder(bot.tree)
    bot.tree.sync = recorder
    asyncio.run(bot.sync_commands())
    # Discord remplace toute la liste du serveur : les anciens noms (draft-ordre…) disparaissent.
    assert not [n for n in recorder.published[111] if n.startswith("draft-")]



def test_wheel_is_public_and_logged(league, monkeypatch):
    bot, store, guild, gm_laval, _, _ = league
    monkeypatch.setattr(bot_module, "WHEEL_SUSPENSE", 0)
    i = run(command(bot, "roue", FakeInteraction(guild, gm_laval), "ott, stl", "gambrell"))
    spinning, result = i.sent[-2][0], i.sent[-1][0]
    assert "La roue tourne" in spinning and "gambrell" in spinning
    winner = result.split("→ **")[1].split("**")[0]
    assert result.startswith("🎡 **gambrell** → ") and winner in ("ott", "stl")
    entry = store.log_entries(guild.id)[-1]
    assert entry[2] == "roue" and entry[3] == f"gambrell : ott, stl → {winner}"  # le journal dit le même gagnant


def test_wheel_rejects_bad_options(league):
    bot, store, guild, gm_laval, _, _ = league
    i = run(command(bot, "roue", FakeInteraction(guild, gm_laval), "ott", None))
    assert "au moins deux options" in i.last
    assert not [e for e in store.log_entries(guild.id) if e[2] == "roue"]
