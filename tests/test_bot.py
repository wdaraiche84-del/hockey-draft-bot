"""Scénarios complets des commandes, boutons et fenêtres, avec de faux objets Discord."""

import asyncio
import itertools

import discord
import pytest

import draft_bot.bot as bot_module
from draft_bot import formatting as fmt
from draft_bot.bot import (AdminPanel, ChannelsModal, DelayModal, DraftBot, OrderModal, PickButtonView,
                           PlayerMenu, PlayersModal, SearchModal, TradeModal)
from draft_bot.storage import Store

ids = itertools.count(1000)


class FakeMessage:
    def __init__(self, channel, content, view=None):
        self.id = next(ids)
        self.channel = channel
        self.content = content
        self.view = view


class FakeHTTPResponse:
    status, reason = 404, "Not Found"


class FakePartial:
    def __init__(self, channel, message_id):
        self.channel, self.id = channel, message_id

    async def edit(self, **kwargs):
        if self.id not in self.channel.messages:
            raise discord.NotFound(FakeHTTPResponse(), "message effacé")
        message = self.channel.messages[self.id]
        if "content" in kwargs:
            message.content = kwargs["content"]
        if "view" in kwargs:
            message.view = kwargs["view"]

    async def delete(self):
        self.channel.messages.pop(self.id, None)


class FakePermissions:
    def __init__(self, allowed=True, manage_guild=False):
        self.view_channel = self.send_messages = self.read_message_history = allowed
        self.manage_guild = manage_guild


class FakeChannel:
    def __init__(self, name, bot_allowed=True):
        self.id = next(ids)
        self.name = name
        self.mention = f"#{name}"
        self.messages = {}
        self.bot_allowed = bot_allowed

    async def send(self, content, **kwargs):
        message = FakeMessage(self, content, kwargs.get("view"))
        self.messages[message.id] = message
        return message

    def get_partial_message(self, message_id):
        return FakePartial(self, message_id)

    def permissions_for(self, member):
        return FakePermissions(self.bot_allowed)

    def texts(self):
        return [m.content for m in self.messages.values()]

    def with_button(self):
        return [m for m in self.messages.values() if isinstance(m.view, PickButtonView)]


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
        self.me = object()

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)

    def get_member(self, user_id):
        return None


class FakeMember:
    def __init__(self, guild, roles, admin=False):
        self.id = next(ids)
        self.guild = guild
        self.roles = roles
        self.guild_permissions = FakePermissions(manage_guild=admin)


class FakeAttachment:
    def __init__(self, text, size=None):
        self.data = text.encode("utf-8")
        self.size = len(self.data) if size is None else size

    async def read(self):
        return self.data


class FakeResponse:
    def __init__(self, interaction):
        self.interaction = interaction
        self.done = False
        self.modal = None

    def is_done(self):
        return self.done

    async def send_message(self, content, **kwargs):
        self.done = True
        self.interaction.sent.append((content, kwargs.get("view")))

    async def edit_message(self, content, **kwargs):
        self.done = True
        self.interaction.sent.append((content, kwargs.get("view")))

    async def send_modal(self, modal):
        self.done = True
        self.modal = modal

    async def defer(self, **kwargs):
        self.done = True


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
        self.edits = []
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)
        self.deleted = False

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs.get("content"))
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
    bot.backup = lambda reason: None
    laval, granby = FakeRole("Laval"), FakeRole("Granby")
    liste, selections = FakeChannel("draft-liste"), FakeChannel("draft-selections")
    guild = FakeGuild([laval, granby], [liste, selections])
    store.set_channels(guild.id, liste.id, selections.id)
    store.replace_players(guild.id, [("Patrick Roy", "Granby", "G"), ("Mario Lemieux", "Laval", "C"),
                                     ("Ray Bourque", "Montréal", "D")])
    store.set_order(guild.id, ["laval", "granby", "granby", "laval"])
    gm_laval, gm_granby = FakeMember(guild, [laval]), FakeMember(guild, [granby])
    return bot, store, guild, gm_laval, gm_granby, selections


@pytest.fixture
def empty_league():
    """Un serveur où rien n'est encore préparé, avec un admin."""
    store = Store(":memory:")
    bot = DraftBot(store, [], backup_dir=None)
    bot.backup = lambda reason: None
    laval, granby, admins = FakeRole("Laval"), FakeRole("Granby"), FakeRole("Admins")
    liste, selections = FakeChannel("draft-liste"), FakeChannel("draft-selections")
    guild = FakeGuild([laval, granby, admins], [liste, selections])
    admin = FakeMember(guild, [], admin=True)
    return bot, store, guild, admin, liste, selections


def admin_of(guild):
    return FakeMember(guild, [], admin=True)


async def command(bot, name, interaction, *args):
    await bot.tree.get_command(name).callback(interaction, *args)
    return interaction


async def confirm(interaction):
    view = interaction.view
    assert view is not None, interaction.last
    await view.confirm.callback(interaction)
    return interaction


async def click(view, button, interaction):
    """Un clic sur un bouton, en passant par la même vérification que Discord."""
    if await view.interaction_check(interaction):
        await button.callback(interaction)
    return interaction


async def choose(interaction, player_name):
    """Choisit un joueur dans le menu reçu par ce membre."""
    menu = interaction.view
    assert isinstance(menu, PlayerMenu), interaction.last
    option = next(o for o in menu.menu.options if o.label == player_name)
    menu.menu._values = [option.value]
    await menu.chosen(interaction)
    return interaction


def fill(modal, **values):
    """Remplit les cases d'une fenêtre comme le ferait le membre."""
    for name, value in values.items():
        item = getattr(modal, name)
        if isinstance(value, str):
            item._value = value
        else:
            item._values = value
    return modal


async def open_panel(bot, guild, admin):
    i = await command(bot, "admin", FakeInteraction(guild, admin))
    assert isinstance(i.view, AdminPanel)
    return i


def run(coro):
    return asyncio.run(coro)


# Le bouton « Faire mon choix »

def test_gm_picks_with_the_button_menu_and_confirm(league):
    bot, store, guild, gm_laval, _, selections = league

    async def scenario():
        i = await click(PickButtonView(bot), PickButtonView(bot).pick, FakeInteraction(guild, gm_laval))
        assert "c'est ton tour" in i.last and "menu" in i.last
        assert [o.label for o in i.view.menu.options] == ["Patrick Roy", "Mario Lemieux", "Ray Bourque"]
        await choose(i, "Mario Lemieux")
        assert "Laval** repêche **Mario Lemieux" in i.last
        assert store.made_picks_count(guild.id) == 0  # rien avant la confirmation
        await confirm(i)
        return i
    i = run(scenario())
    assert i.deleted
    assert store.current_pick(guild.id).team == "Granby"
    announce = selections.texts()[-1]
    assert "**Laval** est fier de sélectionner **Mario Lemieux**" in announce
    assert "Au tour de **Granby**" in announce


def test_announcement_carries_the_button_and_only_the_latest_keeps_it(league):
    bot, store, guild, gm_laval, gm_granby, selections = league

    async def scenario():
        await confirm(await command(bot, "draft", FakeInteraction(guild, gm_laval), "Roy"))
        first = selections.with_button()
        await confirm(await command(bot, "draft", FakeInteraction(guild, gm_granby), "Lemieux"))
        return first
    first = run(scenario())
    assert len(first) == 1
    buttons = selections.with_button()
    assert len(buttons) == 1 and buttons[0].id != first[0].id
    assert store.button_message(guild.id) == buttons[0].id


def test_last_pick_has_no_button(league):
    bot, store, guild, gm_laval, _, selections = league
    store.set_order(guild.id, ["laval"])

    async def scenario():
        await confirm(await command(bot, "draft", FakeInteraction(guild, gm_laval), "Roy"))
    run(scenario())
    assert "Le draft est terminé" in selections.texts()[-1]
    assert selections.with_button() == []
    assert store.button_message(guild.id) is None


def test_button_refuses_other_team_and_hints_admins(league):
    bot, store, guild, _, gm_granby, _ = league
    view = PickButtonView(bot)
    i = run(click(view, view.pick, FakeInteraction(guild, gm_granby)))
    assert "pas ton tour" in i.last and i.view is None
    assert "Admin" not in i.last
    i = run(click(view, view.pick, FakeInteraction(guild, admin_of(guild))))
    assert "Choisir pour l'équipe au tour" in i.last


def test_button_before_setup_explains(empty_league):
    bot, store, guild, admin, _, _ = empty_league
    gm = FakeMember(guild, [guild.roles[0]])
    view = PickButtonView(bot)
    i = run(click(view, view.pick, FakeInteraction(guild, gm)))
    assert "/admin" in i.last


def test_many_players_open_a_search_window(league):
    bot, store, guild, gm_laval, _, _ = league
    rows = [(f"Joueur {n}", "Club", "AD") for n in range(30)] + [("Mario Lemieux", "Laval", "C")]
    store.replace_players(guild.id, rows)

    async def scenario():
        view = PickButtonView(bot)
        i = await click(view, view.pick, FakeInteraction(guild, gm_laval))
        modal = i.response.modal
        assert isinstance(modal, SearchModal)
        # Un seul joueur trouvé : directement la confirmation.
        one = FakeInteraction(guild, gm_laval)
        await fill(modal, query="lemi").on_submit(one)
        assert "repêche **Mario Lemieux" in one.last
        # Plusieurs : un menu pour choisir.
        several = FakeInteraction(guild, gm_laval)
        await fill(SearchModal(bot, False), query="joueur 1").on_submit(several)
        assert "11 joueurs correspondent" in several.last
        # Trop : les 25 premiers et un conseil.
        many = FakeInteraction(guild, gm_laval)
        await fill(SearchModal(bot, False), query="joueur").on_submit(many)
        assert "Plus de 25" in many.last and len(many.view.menu.options) == 25
        # Aucun.
        none = FakeInteraction(guild, gm_laval)
        await fill(SearchModal(bot, False), query="gretzky").on_submit(none)
        assert "Aucun joueur disponible" in none.last and none.view is None
        await confirm(one)
    run(scenario())
    assert store.last_made_pick(guild.id).number == 1


def test_menu_choice_after_turn_changed_is_refused(league):
    bot, store, guild, gm_laval, _, _ = league

    async def scenario():
        view = PickButtonView(bot)
        i = await click(view, view.pick, FakeInteraction(guild, gm_laval))
        # Un admin choisit pour Laval pendant que le DG regarde le menu.
        await confirm(await command(bot, "draft", FakeInteraction(guild, gm_laval), "Bourque"))
        await choose(i, "Patrick Roy")
        return i
    i = run(scenario())
    assert "plus ton tour" in i.last and i.view is None
    assert store.made_picks_count(guild.id) == 1


def test_button_is_permanent():
    async def scenario():
        view = PickButtonView(DraftBot(Store(":memory:"), [], backup_dir=None))
        assert view.timeout is None and view.is_persistent()
        assert view.pick.custom_id == bot_module.PICK_BUTTON_ID
    run(scenario())


def test_statut_shows_the_button_to_the_team_on_the_clock(league):
    bot, store, guild, gm_laval, gm_granby, _ = league
    i = run(command(bot, "statut", FakeInteraction(guild, gm_laval)))
    assert isinstance(i.view, PickButtonView)
    i = run(command(bot, "statut", FakeInteraction(guild, gm_granby)))
    assert i.view is None and "Ensuite" in i.last


# /draft, toujours là pour ceux qui préfèrent taper

def test_gm_on_the_clock_picks_after_confirming(league):
    bot, store, guild, gm_laval, _, selections = league

    async def scenario():
        i = await command(bot, "draft", FakeInteraction(guild, gm_laval), "Roy")
        assert "Laval** repêche **Patrick Roy" in i.last
        assert store.made_picks_count(guild.id) == 0
        await confirm(i)
        assert "✅" in i.last
        return i
    i = run(scenario())
    assert i.deleted
    assert store.current_pick(guild.id).team == "Granby"
    announce = selections.texts()[-1]
    assert "**Laval** est fier de sélectionner **Patrick Roy** (Granby · G) au **1er rang**" in announce
    assert "<@&" in announce


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
        view = PickButtonView(bot)
        second = await click(view, view.pick, FakeInteraction(guild, gm_laval))
        await choose(second, "Mario Lemieux")
        await asyncio.gather(confirm(first), confirm(second))
        return first, second
    first, second = run(scenario())
    assert store.made_picks_count(guild.id) == 1
    assert "Rien n'a été enregistré" in second.last


def test_same_player_cannot_be_drafted_twice(league):
    bot, store, guild, gm_laval, gm_granby, _ = league

    async def scenario():
        await confirm(await command(bot, "draft", FakeInteraction(guild, gm_laval), "Roy"))
        return await command(bot, "draft", FakeInteraction(guild, gm_granby), "Roy")
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


def test_time_up_alert_pings_admin_role_once_and_points_to_the_panel(league):
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
    assert "/admin" in alerts[0] and "forcedraft" not in alerts[0]


# Les commandes

def test_only_five_simple_commands():
    bot = DraftBot(Store(":memory:"), [], backup_dir=None)
    assert sorted(c.name for c in bot.tree.get_commands()) == ["admin", "aide", "draft", "roue", "statut"]


def test_only_admin_command_is_restricted_and_rechecked():
    bot = DraftBot(Store(":memory:"), [], backup_dir=None)
    for command in bot.tree.get_commands():
        if command.name == "admin":
            assert command.default_permissions.manage_guild
            assert command.checks
        else:
            assert command.default_permissions is None, command.name


def test_help_is_short_and_admins_see_more(league):
    bot, store, guild, gm_laval, _, _ = league
    gm = run(command(bot, "aide", FakeInteraction(guild, gm_laval))).last
    admin = run(command(bot, "aide", FakeInteraction(guild, admin_of(guild)))).last
    assert "Faire mon choix" in gm and "/admin" not in gm
    assert "/admin" in admin


def test_wheel_is_public_and_logged(league, monkeypatch):
    bot, store, guild, gm_laval, _, _ = league
    monkeypatch.setattr(bot_module, "WHEEL_SUSPENSE", 0)
    i = run(command(bot, "roue", FakeInteraction(guild, gm_laval), "ott, stl", "gambrell"))
    spinning, result = i.sent[-2][0], i.sent[-1][0]
    assert "La roue tourne" in spinning and "gambrell" in spinning
    winner = result.split("→ **")[1].split("**")[0]
    assert result.startswith("🎡 **gambrell** → ") and winner in ("ott", "stl")
    entry = store.log_entries(guild.id)[-1]
    assert entry[2] == "roue" and entry[3] == f"gambrell : ott, stl → {winner}"


def test_wheel_rejects_bad_options(league):
    bot, store, guild, gm_laval, _, _ = league
    i = run(command(bot, "roue", FakeInteraction(guild, gm_laval), "ott", None))
    assert "au moins deux options" in i.last
    assert not [e for e in store.log_entries(guild.id) if e[2] == "roue"]


# Le panneau /admin

def test_full_setup_from_the_panel(empty_league):
    bot, store, guild, admin, liste, selections = empty_league
    laval, granby, admins = guild.roles

    async def scenario():
        panel_i = await open_panel(bot, guild, admin)
        panel = panel_i.view
        text = panel_i.last
        assert text.count("❌") == 3 and "Prochaine étape : clique sur **1. Salons**" in text

        # 1. Salons
        i = await click(panel, panel.channels, FakeInteraction(guild, admin))
        modal = i.response.modal
        assert isinstance(modal, ChannelsModal)
        done = FakeInteraction(guild, admin)
        await fill(modal, list_channel=[liste], picks_channel=[selections]).on_submit(done)
        assert "✅ Liste dans #draft-liste" in done.last and "2. Joueurs" in done.last
        assert "✅ **1. Salons**" in panel_i.edits[-1]  # le panneau s'est mis à jour tout seul

        # 2. Joueurs
        i = await click(panel, panel.players, FakeInteraction(guild, admin))
        assert isinstance(i.response.modal, PlayersModal)
        done = FakeInteraction(guild, admin)
        await fill(i.response.modal, file=[FakeAttachment("Nom;Équipe;Position\nPatrick Roy;Granby;G\n"
                                                          "Mario Lemieux;Laval;C\n")]).on_submit(done)
        assert "Importer **2 joueurs**" in done.last
        await confirm(done)
        assert "✅ 2 joueurs publiés" in done.last and "3. Ordre" in done.last
        assert "Patrick Roy" in liste.texts()[0]

        # 3. Ordre
        i = await click(panel, panel.order, FakeInteraction(guild, admin))
        done = FakeInteraction(guild, admin)
        await fill(i.response.modal, teams="Laval\nGranby", rounds="2", snake=["oui"]).on_submit(done)
        assert "**4 choix**" in done.last
        await confirm(done)
        assert "2 équipes par ronde" in done.last
        assert [p.team for p in store.upcoming_picks(guild.id, 10)] == ["Laval", "Granby", "Granby", "Laval"]
        assert "Le draft commence" in selections.texts()[-1]
        assert len(selections.with_button()) == 1

        # 4. Délai
        i = await click(panel, panel.delay, FakeInteraction(guild, admin))
        done = FakeInteraction(guild, admin)
        await fill(i.response.modal, delay="8h", admin_role=[admins]).on_submit(done)
        assert "Délai de **8 h**" in done.last and admins.mention in done.last
        assert store.timer(guild.id) == (8 * 3600, admins.id)
        return panel_i
    panel_i = run(scenario())
    final = panel_i.edits[-1]
    assert "❌" not in final and "Tout est prêt" in final and "8 h par choix" in final
    assert [e[2] for e in store.log_entries(guild.id)] == ["salons", "liste", "ordre", "délai"]


def test_channels_must_differ_and_bot_must_have_access(empty_league):
    bot, store, guild, admin, liste, selections = empty_league
    hidden = FakeChannel("cache", bot_allowed=False)
    guild.channels[hidden.id] = hidden

    async def scenario():
        same = FakeInteraction(guild, admin)
        await fill(ChannelsModal(bot, None), list_channel=[liste], picks_channel=[liste]).on_submit(same)
        blocked = FakeInteraction(guild, admin)
        await fill(ChannelsModal(bot, None), list_channel=[liste], picks_channel=[hidden]).on_submit(blocked)
        return same, blocked
    same, blocked = run(scenario())
    assert "deux salons différents" in same.last
    assert "🔒" in blocked.last and "#cache" in blocked.last
    assert store.get_channels(guild.id) is None


def test_players_step_needs_channels_first(empty_league):
    bot, store, guild, admin, _, _ = empty_league

    async def scenario():
        panel = (await open_panel(bot, guild, admin)).view
        return await click(panel, panel.players, FakeInteraction(guild, admin))
    i = run(scenario())
    assert i.response.modal is None and "1. Salons" in i.last


def test_bad_player_files_are_refused(league):
    bot, store, guild, _, _, _ = league
    admin = admin_of(guild)

    async def scenario():
        empty = FakeInteraction(guild, admin)
        await fill(PlayersModal(bot), file=[FakeAttachment("\n\n")]).on_submit(empty)
        huge = FakeInteraction(guild, admin)
        await fill(PlayersModal(bot), file=[FakeAttachment("a;b;c", size=50_000_000)]).on_submit(huge)
        return empty, huge
    empty, huge = run(scenario())
    assert "Aucun joueur trouvé" in empty.last and empty.view is None
    assert "trop gros" in huge.last
    assert store.player_count(guild.id) == 3


def test_reimport_warns_about_made_picks(league):
    bot, store, guild, gm_laval, _, _ = league

    async def scenario():
        await confirm(await command(bot, "draft", FakeInteraction(guild, gm_laval), "Roy"))
        i = FakeInteraction(guild, admin_of(guild))
        await fill(PlayersModal(bot), file=[FakeAttachment("Wayne Gretzky;Brantford;C")]).on_submit(i)
        return i
    assert "1 choix déjà faits seront effacés" in run(scenario()).last


def test_order_window_checks_rounds_and_missing_roles(league):
    bot, store, guild, _, _, _ = league
    admin = admin_of(guild)

    async def scenario():
        bad = FakeInteraction(guild, admin)
        await fill(OrderModal(bot), teams="Laval, Granby", rounds="beaucoup", snake=["non"]).on_submit(bad)
        nothing = FakeInteraction(guild, admin)
        await fill(OrderModal(bot), teams=" , \n", rounds="1", snake=["non"]).on_submit(nothing)
        typo = FakeInteraction(guild, admin)
        await fill(OrderModal(bot), teams="Lavall, Granby", rounds="", snake=["non"]).on_submit(typo)
        return bad, nothing, typo
    bad, nothing, typo = run(scenario())
    assert "entre 1 et 50" in bad.last and bad.view is None
    assert "Aucune équipe trouvée" in nothing.last
    assert "**2 choix**" in typo.last and "Lavall" in typo.last and "Vérifie l'orthographe" in typo.last


def test_full_order_with_traded_picks_in_one_round(league):
    bot, store, guild, _, _, _ = league

    async def scenario():
        i = FakeInteraction(guild, admin_of(guild))
        await fill(OrderModal(bot), teams="Laval\nGranby\nGranby\nGranby", rounds="1", snake=["non"]).on_submit(i)
        await confirm(i)
    run(scenario())
    assert [p.team for p in store.upcoming_picks(guild.id, 10)] == ["Laval", "Granby", "Granby", "Granby"]


def test_new_timer_is_announced_during_draft(league):
    bot, store, guild, _, _, selections = league

    async def scenario():
        i = FakeInteraction(guild, admin_of(guild))
        await fill(DelayModal(bot, 0, None), delay="8h", admin_role=[]).on_submit(i)
        return i
    i = run(scenario())
    assert "Nouveau délai : **8 h**" in selections.texts()[-1]
    assert "Temps limite" in selections.texts()[-1]
    assert "Annoncé dans" in i.last and "Aucun rôle choisi" in i.last


def test_bad_or_zero_delay(league):
    bot, store, guild, _, _, _ = league
    store.set_timer(guild.id, 3600, None)

    async def scenario():
        bad = FakeInteraction(guild, admin_of(guild))
        await fill(DelayModal(bot, 3600, None), delay="demain", admin_role=[]).on_submit(bad)
        zero = FakeInteraction(guild, admin_of(guild))
        await fill(DelayModal(bot, 3600, None), delay="0", admin_role=[]).on_submit(zero)
        return bad, zero
    bad, zero = run(scenario())
    assert "Délai non reconnu" in bad.last
    assert "Aucun délai" in zero.last and store.timer(guild.id)[0] == 0


def test_delay_window_shows_the_current_delay():
    async def scenario():
        modal = DelayModal(DraftBot(Store(":memory:"), [], backup_dir=None), 5400, None)
        assert modal.delay.default == "1h30" and fmt.parse_duration(modal.delay.default) == 5400
    run(scenario())


def test_trade_of_current_pick_pings_new_owner(league):
    bot, store, guild, _, _, selections = league
    granby = next(r for r in guild.roles if r.name == "Granby")

    async def scenario():
        i = FakeInteraction(guild, admin_of(guild))
        await fill(TradeModal(bot), number="#1", team=[granby]).on_submit(i)
        await confirm(i)
    run(scenario())
    public = selections.texts()[-1]
    assert "Échange" in public and granby.mention in public
    assert store.current_pick(guild.id).team == "Granby"
    assert len(selections.with_button()) == 1


def test_bad_trades_are_refused(league):
    bot, store, guild, _, _, _ = league
    laval, granby = guild.roles[:2]

    async def scenario():
        results = []
        for number, role in (("douze", granby), ("99", granby), ("1", laval)):
            i = FakeInteraction(guild, admin_of(guild))
            await fill(TradeModal(bot), number=number, team=[role]).on_submit(i)
            results.append(i)
        return results
    words, unknown, same = run(scenario())
    assert "Écris le numéro" in words.last
    assert "n'existe pas" in unknown.last
    assert "appartient déjà à **Laval**" in same.last
    assert store.current_pick(guild.id).team == "Laval"


def test_admin_picks_for_the_team_and_undoes_from_the_panel(league):
    bot, store, guild, _, _, selections = league
    admin = admin_of(guild)

    async def scenario():
        panel = (await open_panel(bot, guild, admin)).view
        i = await click(panel, panel.force_pick, FakeInteraction(guild, admin))
        assert "pour **Laval**" in i.last
        await choose(i, "Ray Bourque")
        await confirm(i)
        assert "choix fait par un admin" in selections.texts()[-1]
        undo = await click(panel, panel.undo, FakeInteraction(guild, admin))
        await confirm(undo)
        journal = await click(panel, panel.journal, FakeInteraction(guild, admin))
        return journal
    journal = run(scenario())
    assert store.made_picks_count(guild.id) == 0
    assert [e[2] for e in store.log_entries(guild.id)] == ["choix admin", "annulation"]
    assert "retourne dans la liste" in selections.texts()[-1]
    assert len(selections.with_button()) == 1
    assert "2 actions" in journal.last


def test_panel_is_only_for_its_admin_and_rechecks_permission(league):
    bot, store, guild, gm_laval, _, _ = league
    admin = admin_of(guild)

    async def scenario():
        panel = (await open_panel(bot, guild, admin)).view
        stranger = await click(panel, panel.undo, FakeInteraction(guild, admin_of(guild)))
        admin.guild_permissions.manage_guild = False  # la permission est retirée
        demoted = await click(panel, panel.undo, FakeInteraction(guild, admin))
        return stranger, demoted
    stranger, demoted = run(scenario())
    assert stranger.sent == []
    assert "réservé aux admins" in demoted.last


def test_panel_warns_about_teams_without_role(league):
    bot, store, guild, _, _, _ = league
    store.set_order(guild.id, ["laval", "boston"])
    text = run(open_panel(bot, guild, admin_of(guild))).last
    assert "Aucun rôle Discord pour : Boston" in text


def test_panel_says_when_draft_is_over(league):
    bot, store, guild, gm_laval, _, _ = league
    store.set_order(guild.id, ["laval"])

    async def scenario():
        await confirm(await command(bot, "draft", FakeInteraction(guild, gm_laval), "Roy"))
        panel_i = await open_panel(bot, guild, admin_of(guild))
        reload = FakeInteraction(guild, panel_i.user)
        await click(panel_i.view, panel_i.view.reload, reload)
        return panel_i, reload
    panel_i, reload = run(scenario())
    assert "draft est terminé" in panel_i.last and "draft est terminé" in reload.last


def test_closed_panel_stops_being_refreshed(league):
    bot, store, guild, gm_laval, _, _ = league

    async def scenario():
        panel_i = await open_panel(bot, guild, admin_of(guild))
        await panel_i.view.on_timeout()
        await confirm(await command(bot, "draft", FakeInteraction(guild, gm_laval), "Roy"))
        return panel_i
    panel_i = run(scenario())
    assert "Panneau fermé" in panel_i.edits[-1]
    assert not bot.panels[guild.id]


def test_windows_respect_discord_limits():
    async def scenario():
        bot = DraftBot(Store(":memory:"), [], backup_dir=None)
        for modal in (SearchModal(bot, False), SearchModal(bot, True), ChannelsModal(bot, (1, 2)),
                      ChannelsModal(bot, None), PlayersModal(bot), OrderModal(bot), DelayModal(bot, 3600, 5),
                      DelayModal(bot, 0, None), TradeModal(bot)):
            data = modal.to_dict()
            assert len(data["title"]) <= 45 and len(data["components"]) <= 5
            for row in data["components"]:
                assert len(row["label"]) <= 45, row["label"]
                assert len(row.get("description") or "") <= 100, row["description"]
        for view in (AdminPanel(bot, 1, 2), PickButtonView(bot)):
            for row in view.to_components():
                assert len(row["components"]) <= 5
                assert all(len(c.get("label") or "") <= 80 for c in row["components"])
    run(scenario())


# Démarrage et configuration

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


def test_old_admin_commands_disappear_from_each_server():
    bot = DraftBot(Store(":memory:"), [111], backup_dir=None)
    recorder = SyncRecorder(bot.tree)
    bot.tree.sync = recorder
    asyncio.run(bot.sync_commands())
    # Discord remplace toute la liste du serveur : /salons, /forcedraft… disparaissent.
    assert recorder.published[111] == ["admin", "aide", "draft", "roue", "statut"]


def test_old_discord_library_gives_a_clear_message(monkeypatch):
    from draft_bot.bot import ConfigError, check_library
    check_library()
    monkeypatch.delattr(discord.ui, "FileUpload")
    with pytest.raises(ConfigError, match="pip install -U"):
        check_library()
