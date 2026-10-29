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
        self.sent_mentions = []

    async def send(self, content, **kwargs):
        self.sent_mentions.append(kwargs.get("allowed_mentions"))
        message = FakeMessage(self, content, kwargs.get("view"))
        self.messages[message.id] = message
        return message

    def get_partial_message(self, message_id):
        return FakePartial(self, message_id)

    def permissions_for(self, member):
        return FakePermissions(self.bot_allowed)

    def texts(self):
        return [m.content for m in self.messages.values()]

    def mentions(self):
        return list(self.sent_mentions)

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
    bot = DraftBot(store, backup_dir=None)
    bot.backup = lambda reason: None
    laval, granby = FakeRole("Laval"), FakeRole("Granby")
    liste, selections = FakeChannel("draft-liste"), FakeChannel("draft-selections")
    guild = FakeGuild([laval, granby], [liste, selections])
    store.set_channels(guild.id, liste.id, selections.id)
    store.replace_players(guild.id, [("Patrick Roy", "Granby", "G"), ("Mario Lemieux", "Laval", "C"),
                                     ("Ray Bourque", "Montréal", "D")])
    store.set_order(guild.id, ["laval", "granby", "granby", "laval"])
    store.start_draft(guild.id)
    gm_laval, gm_granby = FakeMember(guild, [laval]), FakeMember(guild, [granby])
    return bot, store, guild, gm_laval, gm_granby, selections


@pytest.fixture
def empty_league():
    """Un serveur où rien n'est encore préparé, avec un admin."""
    store = Store(":memory:")
    bot = DraftBot(store, backup_dir=None)
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


def keys(panel):
    """Les boutons affichés dans le panneau, dans l'ordre."""
    return [item.key for item in panel.children]


def button(panel, key):
    found = panel.button(key)
    assert found is not None, f"bouton {key} absent : {keys(panel)}"
    return found


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
    store.start_draft(guild.id)

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
        view = PickButtonView(DraftBot(Store(":memory:"), backup_dir=None))
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
    store.start_draft(guild.id)

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
    bot = DraftBot(Store(":memory:"), backup_dir=None)
    assert sorted(c.name for c in bot.tree.get_commands()) == ["admin", "aide", "draft", "roue", "statut"]


def test_only_admin_command_is_restricted_and_rechecked():
    bot = DraftBot(Store(":memory:"), backup_dir=None)
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

def test_full_setup_from_the_panel_one_button_at_a_time(empty_league):
    bot, store, guild, admin, liste, selections = empty_league
    laval, granby, admins = guild.roles
    gm_laval = FakeMember(guild, [laval])

    async def scenario():
        panel_i = await open_panel(bot, guild, admin)
        panel = panel_i.view
        assert "étape 1 sur 4" in panel_i.last and "Choisir les salons" in panel_i.last
        assert keys(panel) == ["salons", "transaction", "options"]

        # Étape 1 : salons
        i = await click(panel, button(panel, "salons"), FakeInteraction(guild, admin))
        modal = i.response.modal
        assert isinstance(modal, ChannelsModal)
        done = FakeInteraction(guild, admin)
        await fill(modal, list_channel=[liste], picks_channel=[selections]).on_submit(done)
        assert "✅ Liste dans #draft-liste" in done.last and "Importer les joueurs" in done.last
        # le panneau s'est mis à jour tout seul : un seul bouton, celui de l'étape suivante
        assert "étape 2 sur 4" in panel_i.edits[-1] and keys(panel) == ["joueurs", "transaction", "options"]

        # Étape 2 : joueurs
        i = await click(panel, button(panel, "joueurs"), FakeInteraction(guild, admin))
        assert isinstance(i.response.modal, PlayersModal)
        done = FakeInteraction(guild, admin)
        await fill(i.response.modal, file=[FakeAttachment("Nom;Équipe;Position\nPatrick Roy;Granby;G\n"
                                                          "Mario Lemieux;Laval;C\n")]).on_submit(done)
        assert "Importer **2 joueurs**" in done.last
        await confirm(done)
        assert "✅ 2 joueurs publiés" in done.last and "Définir l'ordre du draft" in done.last
        assert "Patrick Roy" in liste.texts()[0]
        assert "étape 3 sur 4" in panel_i.edits[-1] and keys(panel) == ["ordre", "transaction", "options"]

        # Étape 3 : ordre. Le draft ne commence pas encore.
        i = await click(panel, button(panel, "ordre"), FakeInteraction(guild, admin))
        done = FakeInteraction(guild, admin)
        await fill(i.response.modal, teams="Laval\nGranby", rounds="2", snake=["oui"]).on_submit(done)
        assert "**4 choix**" in done.last
        await confirm(done)
        assert "2 équipes par ronde" in done.last and "Lancer le draft" in done.last
        assert [p.team for p in store.upcoming_picks(guild.id, 10)] == ["Laval", "Granby", "Granby", "Laval"]
        assert not any("Le draft commence" in t for t in selections.texts())
        assert selections.with_button() == []
        assert "Tout est prêt" in panel_i.edits[-1] and keys(panel) == ["lancer", "transaction", "options"]

        # Avant le lancement, personne ne peut choisir.
        early = await command(bot, "draft", FakeInteraction(guild, gm_laval), "Roy")
        assert "pas encore commencé" in early.last and early.view is None
        statut = await command(bot, "statut", FakeInteraction(guild, gm_laval))
        assert "pas encore commencé" in statut.last

        # Le délai, facultatif, se règle dans « Plus d'options » avant le lancement.
        await click(panel, button(panel, "options"), FakeInteraction(guild, admin))
        i = await click(panel, button(panel, "delai"), FakeInteraction(guild, admin))
        done = FakeInteraction(guild, admin)
        await fill(i.response.modal, delay="8h", admin_role=[admins]).on_submit(done)
        assert "Délai de **8 h**" in done.last and admins.mention in done.last
        assert store.timer(guild.id) == (8 * 3600, admins.id)
        assert selections.texts() == []  # rien d'annoncé avant le lancement
        assert keys(panel) == ["lancer", "transaction", "options"]  # retour à l'écran principal
        assert "8 h par choix" in panel_i.edits[-1]

        # Étape 4 : lancer
        launch = await click(panel, button(panel, "lancer"), FakeInteraction(guild, admin))
        assert "Lancer le draft maintenant" in launch.last and "Laval" in launch.last
        await confirm(launch)
        assert "✅ Le draft est lancé" in launch.last
        assert "Le draft commence" in selections.texts()[-1] and laval.mention in selections.texts()[-1]
        assert len(selections.with_button()) == 1
        assert store.deadline(guild.id) is not None
        assert keys(panel) == ["choisir", "annuler", "transaction", "options"]
        assert "Le draft est en cours" in panel_i.edits[-1]
        return panel_i
    run(scenario())
    assert [e[2] for e in store.log_entries(guild.id)] == ["salons", "liste", "ordre", "délai", "lancement"]


def test_launch_twice_announces_once(league):
    bot, store, guild, _, _, selections = league
    store.set_order(guild.id, ["laval", "granby"])
    admin = admin_of(guild)

    async def scenario():
        panel = (await open_panel(bot, guild, admin)).view
        first = await click(panel, button(panel, "lancer"), FakeInteraction(guild, admin))
        second = await click(panel, button(panel, "lancer"), FakeInteraction(guild, admin))
        await confirm(first)
        await confirm(second)
        late = FakeInteraction(guild, admin)
        await bot_module.launch_draft(bot, late)
        return second, late
    second, late = run(scenario())
    assert sum("Le draft commence" in t for t in selections.texts()) == 1
    assert "déjà lancé" in second.last and "déjà lancé" in late.last
    assert [e[2] for e in store.log_entries(guild.id)].count("lancement") == 1


def test_launch_needs_channels(league):
    bot, store, guild, _, _, selections = league
    store.set_order(guild.id, ["laval", "granby"])
    guild.channels.pop(selections.id)  # le salon des annonces a été effacé

    async def scenario():
        i = FakeInteraction(guild, admin_of(guild))
        await bot_module.launch_draft(bot, i)
        panel = (await open_panel(bot, guild, admin_of(guild))).view
        return i, panel
    i, panel = run(scenario())
    assert "pas encore prêt" in i.last and "Choisir les salons" in i.last and i.view is None
    assert not store.is_started(guild.id)
    assert keys(panel) == ["salons", "transaction", "options"]


def test_new_order_during_draft_waits_for_a_new_launch(league):
    bot, store, guild, gm_laval, _, selections = league
    admin = admin_of(guild)

    async def scenario():
        await confirm(await command(bot, "draft", FakeInteraction(guild, gm_laval), "Roy"))
        assert len(selections.with_button()) == 1
        i = FakeInteraction(guild, admin)
        await fill(OrderModal(bot), teams="Granby\nLaval", rounds="1", snake=["non"]).on_submit(i)
        assert "1 choix déjà faits seront effacés" in i.last
        await confirm(i)
        refused = await command(bot, "draft", FakeInteraction(guild, gm_laval), "Lemieux")
        panel = (await open_panel(bot, guild, admin)).view
        return refused, panel
    refused, panel = run(scenario())
    assert selections.with_button() == []  # l'ancien bouton « Faire mon choix » est retiré
    assert "pas encore commencé" in refused.last
    assert store.made_picks_count(guild.id) == 0
    assert keys(panel) == ["lancer", "transaction", "options"]


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


def test_players_need_channels_first(empty_league):
    bot, store, guild, admin, _, _ = empty_league

    async def scenario():
        panel = (await open_panel(bot, guild, admin)).view
        assert panel.button("joueurs") is None  # pas affiché avant les salons
        await click(panel, button(panel, "options"), FakeInteraction(guild, admin))
        return await click(panel, button(panel, "joueurs"), FakeInteraction(guild, admin))
    i = run(scenario())
    assert i.response.modal is None and "Choisis d'abord les salons" in i.last


def test_more_options_keeps_every_feature_and_back_returns(league):
    bot, store, guild, _, _, _ = league
    admin = admin_of(guild)

    async def scenario():
        panel_i = await open_panel(bot, guild, admin)
        panel = panel_i.view
        more = await click(panel, button(panel, "options"), FakeInteraction(guild, admin))
        assert keys(panel) == ["salons", "joueurs", "ordre", "delai", "echange", "journal", "shm", "retour"]
        assert "Plus d'options" in more.last and "#draft-liste" in more.last and "4 choix" in more.last
        modals = {}
        for key, modal in (("salons", ChannelsModal), ("joueurs", PlayersModal), ("ordre", OrderModal),
                           ("delai", DelayModal), ("echange", TradeModal)):
            i = await click(panel, button(panel, key), FakeInteraction(guild, admin))
            assert isinstance(i.response.modal, modal), key
            modals[key] = i.response.modal
        assert modals["salons"].list_channel.default_values  # les salons actuels sont déjà choisis
        back = await click(panel, button(panel, "retour"), FakeInteraction(guild, admin))
        assert keys(panel) == ["choisir", "annuler", "transaction", "options"] and "en cours" in back.last
    run(scenario())


def test_other_admins_panel_stays_in_options(league):
    bot, store, guild, gm_laval, _, _ = league
    admin, other = admin_of(guild), admin_of(guild)

    async def scenario():
        mine = (await open_panel(bot, guild, admin)).view
        theirs_i = await open_panel(bot, guild, other)
        theirs = theirs_i.view
        await click(theirs, button(theirs, "options"), FakeInteraction(guild, other))
        await click(mine, button(mine, "options"), FakeInteraction(guild, admin))
        # Un GM fait son choix : les panneaux se mettent à jour sans changer d'écran.
        await confirm(await command(bot, "draft", FakeInteraction(guild, gm_laval), "Roy"))
        assert "retour" in keys(theirs) and "1 déjà faits" in theirs_i.edits[-1]
        # L'admin qui agit revient à l'écran principal ; l'autre reste dans ses options.
        assert mine.button("annuler") is None  # cet admin est encore dans ses options
        await click(mine, button(mine, "retour"), FakeInteraction(guild, admin))
        undo = await click(mine, button(mine, "annuler"), FakeInteraction(guild, admin))
        await confirm(undo)
        assert "retour" in keys(theirs) and "choisir" in keys(mine)
    run(scenario())


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
        modal = DelayModal(DraftBot(Store(":memory:"), backup_dir=None), 5400, None)
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
        i = await click(panel, button(panel, "choisir"), FakeInteraction(guild, admin))
        assert "pour **Laval**" in i.last
        await choose(i, "Ray Bourque")
        await confirm(i)
        assert "choix fait par un admin" in selections.texts()[-1]
        undo = await click(panel, button(panel, "annuler"), FakeInteraction(guild, admin))
        await confirm(undo)
        await click(panel, button(panel, "options"), FakeInteraction(guild, admin))
        journal = await click(panel, button(panel, "journal"), FakeInteraction(guild, admin))
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
        stranger = await click(panel, button(panel, "annuler"), FakeInteraction(guild, admin_of(guild)))
        admin.guild_permissions.manage_guild = False  # la permission est retirée
        demoted = await click(panel, button(panel, "annuler"), FakeInteraction(guild, admin))
        return stranger, demoted
    stranger, demoted = run(scenario())
    assert stranger.sent == []
    assert "réservé aux admins" in demoted.last


def test_panel_warns_about_teams_without_role(league):
    bot, store, guild, _, _, _ = league
    store.set_order(guild.id, ["laval", "boston"])
    store.start_draft(guild.id)
    text = run(open_panel(bot, guild, admin_of(guild))).last
    assert "Aucun rôle Discord pour : Boston" in text


def test_panel_says_when_draft_is_over_and_last_pick_can_still_be_undone(league):
    bot, store, guild, gm_laval, _, _ = league
    store.set_order(guild.id, ["laval"])
    store.start_draft(guild.id)

    async def scenario():
        await confirm(await command(bot, "draft", FakeInteraction(guild, gm_laval), "Roy"))
        panel_i = await open_panel(bot, guild, admin_of(guild))
        panel = panel_i.view
        assert keys(panel) == ["annuler", "transaction", "options"]
        await click(panel, button(panel, "options"), FakeInteraction(guild, panel_i.user))
        back = await click(panel, button(panel, "retour"), FakeInteraction(guild, panel_i.user))
        undo = await click(panel, button(panel, "annuler"), FakeInteraction(guild, panel_i.user))
        await confirm(undo)
        return panel_i, back
    panel_i, back = run(scenario())
    assert "draft est terminé" in panel_i.sent[0][0] and "draft est terminé" in back.last
    assert store.made_picks_count(guild.id) == 0 and "en cours" in panel_i.edits[-1]


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
        bot = DraftBot(Store(":memory:"), backup_dir=None)
        for modal in (SearchModal(bot, False), SearchModal(bot, True), ChannelsModal(bot, (1, 2)),
                      ChannelsModal(bot, None), PlayersModal(bot), OrderModal(bot), DelayModal(bot, 3600, 5),
                      DelayModal(bot, 0, None), TradeModal(bot)):
            data = modal.to_dict()
            assert len(data["title"]) <= 45 and len(data["components"]) <= 5
            for row in data["components"]:
                assert len(row["label"]) <= 45, row["label"]
                assert len(row.get("description") or "") <= 100, row["description"]
        for view in (*all_panel_screens(), PickButtonView(bot)):
            for row in view.to_components():
                assert len(row["components"]) <= 5
                assert all(len(c.get("label") or "") <= 80 for c in row["components"])
    run(scenario())


def all_panel_screens():
    """Le panneau à chaque étape du draft, et ses options."""
    store = Store(":memory:")
    bot = DraftBot(store, backup_dir=None)
    liste, selections = FakeChannel("liste"), FakeChannel("selections")
    guild = FakeGuild([FakeRole("Laval")], [liste, selections])
    screens = []

    def snap(more=False):
        panel = AdminPanel(bot, 1, guild.id)
        panel.more = more
        assert panel.build(guild)
        screens.append(panel)

    snap()
    store.set_channels(guild.id, liste.id, selections.id)
    snap()
    store.replace_players(guild.id, [("Patrick Roy", "Granby", "G")])
    snap()
    store.set_order(guild.id, ["laval"])
    snap()
    store.start_draft(guild.id)
    snap()
    store.make_pick(guild.id, 1, store.available_players(guild.id)[0].id)
    snap()
    snap(more=True)
    return screens


def test_every_panel_screen_shows_few_buttons():
    phases = [[item.key for item in panel.children] for panel in all_panel_screens()]
    assert phases == [
        ["salons", "transaction", "options"], ["joueurs", "transaction", "options"], ["ordre", "transaction", "options"], ["lancer", "transaction", "options"],
        ["choisir", "annuler", "transaction", "options"], ["annuler", "transaction", "options"],
        ["salons", "joueurs", "ordre", "delai", "echange", "journal", "shm", "retour"],
    ]


# Démarrage et configuration

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


def test_old_guild_ids_setting_is_harmless(caplog):
    from draft_bot.bot import warn_old_guild_ids
    with caplog.at_level("INFO", logger="draft_bot"):
        warn_old_guild_ids({})
        assert not caplog.records
        warn_old_guild_ids({"GUILD_IDS": "mon serveur"})  # même une valeur invalide ne bloque plus le démarrage
    assert "n'est plus nécessaire" in caplog.text


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

    def __init__(self, tree, refuse=()):
        self.tree = tree
        self.published = {}
        self.calls = []
        self.refuse = dict(refuse)

    async def __call__(self, guild=None):
        key = guild.id if guild else None
        self.calls.append(key)
        if key in self.refuse:
            raise self.refuse[key](FakeHTTPResponse(), "refusé")
        self.published[key] = sorted(c.name for c in self.tree.get_commands(guild=guild))


class SyncGuild:
    def __init__(self, guild_id):
        self.id, self.name = guild_id, f"serveur {guild_id}"

    async def leave(self):
        raise AssertionError("le bot ne doit jamais quitter un serveur tout seul")


def sync_bot(guilds=(), refuse=()):
    bot = DraftBot(Store(":memory:"), backup_dir=None)
    recorder = SyncRecorder(bot.tree, refuse)
    bot.tree.sync = recorder
    joined = [SyncGuild(g) for g in guilds]
    bot.__dict__["joined"] = joined
    return bot, recorder, joined


@pytest.fixture
def fake_guilds(monkeypatch):
    monkeypatch.setattr(DraftBot, "guilds", property(lambda self: self.__dict__["joined"]))


EXPECTED = ["admin", "aide", "draft", "roue", "statut"]


def test_global_commands_are_emptied_but_still_answered():
    bot, recorder, _ = sync_bot()
    asyncio.run(bot.clear_global_commands())
    assert recorder.published[None] == []  # les anciennes commandes globales disparaissent chez Discord
    # Le bot garde ses commandes en mémoire pour répondre dans tous les serveurs.
    assert sorted(c.name for c in bot.tree.get_commands()) == EXPECTED


def test_global_cleanup_failure_does_not_lose_the_commands():
    bot, recorder, _ = sync_bot(refuse={None: discord.HTTPException})
    asyncio.run(bot.clear_global_commands())
    assert sorted(c.name for c in bot.tree.get_commands()) == EXPECTED


def test_every_server_gets_the_commands_at_startup(fake_guilds):
    bot, recorder, _ = sync_bot([111, 222, 333])
    asyncio.run(bot.on_ready())
    for guild_id in (111, 222, 333):
        # Discord remplace toute la liste du serveur : /salons, /forcedraft… disparaissent.
        assert recorder.published[guild_id] == EXPECTED
    assert None not in recorder.calls  # rien de global : pas de doublons
    assert bot.synced_guilds == {111, 222, 333}


def test_new_server_is_accepted_and_gets_the_commands(fake_guilds):
    bot, recorder, joined = sync_bot([111])
    asyncio.run(bot.on_ready())
    new = SyncGuild(999)
    joined.append(new)
    asyncio.run(bot.on_guild_join(new))  # SyncGuild.leave échouerait si le bot partait
    assert recorder.published[999] == EXPECTED
    assert recorder.calls.count(999) == 1


def test_reconnect_does_not_republish(fake_guilds):
    bot, recorder, _ = sync_bot([111, 222])
    asyncio.run(bot.on_ready())
    asyncio.run(bot.on_ready())  # Discord renvoie on_ready après une coupure
    assert recorder.calls == [111, 222]


def test_one_refusing_server_does_not_block_the_others(fake_guilds):
    bot, recorder, _ = sync_bot([111, 222, 333], refuse={222: discord.Forbidden})
    asyncio.run(bot.on_ready())
    assert recorder.published[111] == EXPECTED and recorder.published[333] == EXPECTED
    assert 222 not in recorder.published and bot.synced_guilds == {111, 333}
    # Au prochain on_ready (reconnexion), le serveur refusé est réessayé.
    recorder.refuse.clear()
    asyncio.run(bot.on_ready())
    assert recorder.published[222] == EXPECTED


def test_server_error_does_not_crash_startup(fake_guilds):
    bot, recorder, _ = sync_bot([111, 222], refuse={111: discord.HTTPException})
    asyncio.run(bot.on_ready())
    assert recorder.published[222] == EXPECTED


def test_removed_then_reinvited_server_gets_commands_again_and_keeps_its_draft(fake_guilds):
    bot, recorder, _ = sync_bot([111])
    bot.store.set_channels(111, 10, 20)
    asyncio.run(bot.on_ready())
    asyncio.run(bot.on_guild_remove(SyncGuild(111)))
    assert bot.tree.get_commands(guild=SyncGuild(111)) == []
    asyncio.run(bot.on_guild_join(SyncGuild(111)))
    assert recorder.calls.count(111) == 2 and recorder.published[111] == EXPECTED
    assert bot.store.get_channels(111) == (10, 20)  # le draft du serveur n'est pas effacé


def test_joins_during_startup_are_published_one_at_a_time(fake_guilds):
    bot, recorder, joined = sync_bot([111, 222])
    running, overlap = [0], [False]

    async def slow_sync(guild=None):
        running[0] += 1
        overlap[0] |= running[0] > 1
        await asyncio.sleep(0.01)
        running[0] -= 1
        await SyncRecorder.__call__(recorder, guild)
    bot.tree.sync = slow_sync

    async def scenario():
        new = SyncGuild(333)
        joined.append(new)
        await asyncio.gather(bot.on_ready(), bot.on_guild_join(new))
    asyncio.run(scenario())
    assert not overlap[0]
    assert all(recorder.published[g] == EXPECTED for g in (111, 222, 333))
    assert recorder.calls.count(333) == 1


def test_each_server_answers_its_own_commands(fake_guilds):
    """Une commande reçue d'un serveur est trouvée, qu'il ait déjà sa copie ou pas encore."""
    bot, recorder, _ = sync_bot([111])
    asyncio.run(bot.on_ready())
    for guild_id in ("111", "999"):  # 999 : serveur pas encore publié, la copie de secours répond
        command, _ = bot.tree._get_app_command_options({"name": "draft", "guild_id": guild_id})
        assert command.name == "draft"


def test_old_discord_library_gives_a_clear_message(monkeypatch):
    from draft_bot.bot import ConfigError, check_library
    check_library()
    monkeypatch.delattr(discord.ui, "FileUpload")
    with pytest.raises(ConfigError, match="pip install -U"):
        check_library()


def test_discord_finds_the_new_buttons_after_each_screen_change(league):
    """discord.py retrouve un clic par l'identifiant du bouton. Quand le panneau change de boutons,
    ceux qui ont disparu ne doivent plus répondre, et les nouveaux doivent répondre."""
    from discord.ui.view import ViewStore
    bot, store, guild, _, _, _ = league

    async def scenario():
        views = ViewStore(None)
        panel = AdminPanel(bot, 1, guild.id)
        panel.build(guild)
        views.add_view(panel, 42)  # comme à l'envoi du panneau
        main = set(views._views[42])
        panel.more = True
        panel.build(guild)
        views.add_view(panel, 42)  # comme à chaque modification du panneau
        more = set(views._views[42])
        panel.stop()
        return main, more, {(2, item.custom_id) for item in panel.children}
    main, more, current = run(scenario())
    assert main == {(2, "admin:choisir"), (2, "admin:annuler"), (2, "admin:transaction"),
                    (2, "admin:options")}
    assert more == current and (2, "admin:choisir") not in more and (2, "admin:retour") in more


# Annoncer une transaction à partir d'une capture SHM

import json
from pathlib import Path

from draft_bot import capture
from draft_bot.bot import CaptureModal, TradeFixModal, TradePreview

RECORDED = json.loads((Path(__file__).parent / "captures" / "transactions-shm.json").read_text())
NHL_ROLES = ["Flames", "Oilers", "Sharks", "Stars", "Sénateurs", "Maple Leafs", "Panthers", "Lightning", "Flyers",
             "Kings", "Hurricanes", "Bruins", "Penguins", "Blues", "Red Wings", "Admins"]


class FakeImage(FakeAttachment):
    def __init__(self, filename="capture.png", content_type="image/png", size=None):
        super().__init__("image", size)
        self.filename, self.content_type = filename, content_type


@pytest.fixture
def nhl(monkeypatch):
    """Un serveur de ligue avec un rôle par équipe et un salon des transactions."""
    monkeypatch.setattr(bot_module, "capture_installed", lambda: True)
    readings = []

    async def fake_read(bot, data):
        readings.append(data)
        return json.loads(json.dumps(RECORDED))
    monkeypatch.setattr(bot_module, "read_capture", fake_read)
    store = Store(":memory:")
    bot = DraftBot(store, backup_dir=None)
    roles = [FakeRole(name) for name in NHL_ROLES]
    trades = FakeChannel("transactions")
    guild = FakeGuild(roles, [trades])
    return bot, store, guild, admin_of(guild), trades, {r.name: r for r in roles}


async def send_capture(bot, guild, admin, channel, image=None):
    """L'admin clique sur « Annoncer une transaction » dans /admin, puis envoie sa capture."""
    panel = (await open_panel(bot, guild, admin)).view
    opened = await click(panel, button(panel, "transaction"), FakeInteraction(guild, admin))
    modal = opened.response.modal
    assert isinstance(modal, CaptureModal), opened.sent
    i = FakeInteraction(guild, admin)
    await fill(modal, file=[image or FakeImage()], channel=[channel]).on_submit(i)
    return i


def preview_button(view, label):
    return next(item for item in view.children if isinstance(item, discord.ui.Button) and label in item.label)


def preview_menu(view, row):
    return next(item for item in view.children if isinstance(item, discord.ui.Select) and item.row == row)


async def press(view, label, interaction):
    if await view.interaction_check(interaction):
        await preview_button(view, label).callback(interaction)
    return interaction


def test_capture_preview_then_announce_every_trade_with_team_roles(nhl):
    bot, store, guild, admin, channel, roles = nhl

    async def scenario():
        i = await send_capture(bot, guild, admin, channel)
        preview = i.view
        text = i.last
        await press(preview, "Annoncer", FakeInteraction(guild, admin))
        return text, preview
    text, preview = run(scenario())
    assert isinstance(preview, TradePreview)
    assert "8 transactions lues" in text and text.count("✅") == 8 and "⚠️" not in text
    assert "**Flames** reçoit Daniil Tarasov · **Oilers** reçoit Choix de 2e ronde 2020 (Blues)" in text
    posted = channel.texts()
    assert len(posted) == 8
    # SHM montre la plus récente en haut : la plus ancienne est annoncée en premier.
    assert posted[0] == ("🔁 **Transaction**\n"
                         f"➡️ {roles['Panthers'].mention} reçoit : **Yakov Trenin**, **Jeremy Lauzon**, "
                         "**Choix de 1re ronde 2018 (Blues)**, **Choix de 1re ronde 2019 (Kings)**\n"
                         f"➡️ {roles['Penguins'].mention} reçoit : **Charlie McAvoy**")
    assert posted[-1].startswith(f"🔁 **Transaction**\n➡️ {roles['Flames'].mention} reçoit : **Daniil Tarasov**")
    assert store.trades_channel(guild.id) == channel.id
    assert [a for _, _, a, _ in store.log_entries(guild.id)].count("transaction") == 8


def test_same_capture_twice_does_not_announce_twice(nhl):
    bot, store, guild, admin, channel, _ = nhl

    async def scenario():
        first = await send_capture(bot, guild, admin, channel)
        await press(first.view, "Annoncer", FakeInteraction(guild, admin))
        return await send_capture(bot, guild, admin, channel)
    again = run(scenario())
    assert again.last.count("(déjà annoncée)") == 8 and "✅" not in again.last
    assert preview_button(again.view, "Annoncer").disabled
    assert len(channel.texts()) == 8


def test_admin_chooses_which_trades_to_announce(nhl):
    bot, store, guild, admin, channel, roles = nhl

    async def scenario():
        i = await send_capture(bot, guild, admin, channel)
        menu = preview_menu(i.view, 0)
        assert len(menu.options) == 8 and all(o.default for o in menu.options)
        menu._values = ["0", "2"]
        changed = FakeInteraction(guild, admin)
        await i.view.select_changed(changed)
        assert changed.last.count("✅") == 2 and changed.last.count("⬜") == 6
        assert preview_button(changed.view, "Annoncer").label == "Annoncer les 2"
        await press(changed.view, "Annoncer", FakeInteraction(guild, admin))
    run(scenario())
    posted = channel.texts()
    assert len(posted) == 2
    assert roles["Sénateurs"].mention in posted[0] and roles["Flames"].mention in posted[1]


def test_team_without_role_must_be_corrected_and_the_logo_is_remembered(nhl):
    bot, store, guild, admin, channel, roles = nhl
    # Un rôle dont le nom ne dit pas de quelle équipe il s'agit.
    pittsburgh = roles["Penguins"]
    pittsburgh.name = "Équipe 13"

    async def scenario():
        i = await send_capture(bot, guild, admin, channel)
        assert "**Pittsburgh Penguins** reçoit Charlie McAvoy" in i.last and "équipe sans rôle" in i.last
        blocked = await press(i.view, "Annoncer", FakeInteraction(guild, admin))
        assert "À corriger d'abord : transaction 8" in blocked.last and channel.texts() == []
        fix = preview_menu(i.view, 1)
        fix._values = ["7"]
        opened = FakeInteraction(guild, admin)
        await i.view.fix_chosen(opened)
        modal = opened.response.modal
        assert isinstance(modal, TradeFixModal) and modal.title == "Corriger la transaction 8"
        assert modal.items[1].default == "Charlie McAvoy"
        modal.roles[1]._values = [pittsburgh]
        modal.items[1]._value = "Charlie McAvoy\nChoix de 3e ronde 2022"
        fixed = FakeInteraction(guild, admin)
        await modal.on_submit(fixed)
        assert "⚠️" not in fixed.last and "Choix de 3e ronde 2022" in fixed.last
        await press(fixed.view, "Annoncer", FakeInteraction(guild, admin))
        # La prochaine capture reconnaît ce logo toute seule.
        return await send_capture(bot, guild, admin, channel)
    again = run(scenario())
    assert len(channel.texts()) == 8
    assert f"{pittsburgh.mention} reçoit : **Charlie McAvoy**, **Choix de 3e ronde 2022**" in channel.texts()[0]
    assert len(store.learned_logos(guild.id)) == 1
    assert "**Équipe 13** reçoit Charlie McAvoy" in again.last


def test_capture_problems_are_explained(nhl, monkeypatch):
    bot, store, guild, admin, channel, _ = nhl
    locked = FakeChannel("verrouillé", bot_allowed=False)
    guild.channels[locked.id] = locked

    async def failing_read(bot, data):
        raise capture.CaptureError("Je n'arrive pas à lire cette image.")

    async def scenario():
        text = await send_capture(bot, guild, admin, channel, FakeImage("liste.csv", "text/csv"))
        huge = await send_capture(bot, guild, admin, channel, FakeImage(size=50_000_000))
        no_access = await send_capture(bot, guild, admin, locked)
        monkeypatch.setattr(bot_module, "read_capture", failing_read)
        unreadable = await send_capture(bot, guild, admin, channel)
        return text, huge, no_access, unreadable
    text, huge, no_access, unreadable = run(scenario())
    assert "pas une image" in text.last
    assert "trop grosse" in huge.last
    assert "ne peut pas écrire" in no_access.last
    assert "❌ Je n'arrive pas à lire cette image." in unreadable.last and unreadable.view is None
    assert channel.texts() == []


def test_not_a_trade_table_is_explained(nhl, monkeypatch):
    bot, store, guild, admin, channel, _ = nhl

    async def no_logos(bot, data):
        return {"lines": RECORDED["lines"], "logos": []}
    monkeypatch.setattr(bot_module, "read_capture", no_logos)
    i = run(send_capture(bot, guild, admin, channel))
    assert "aucun logo" in i.last


def test_capture_reading_not_installed(nhl, monkeypatch):
    bot, store, guild, admin, channel, _ = nhl
    monkeypatch.setattr(bot_module, "capture_installed", lambda: False)

    async def scenario():
        panel = (await open_panel(bot, guild, admin)).view
        return await click(panel, button(panel, "transaction"), FakeInteraction(guild, admin))
    i = run(scenario())
    assert i.response.modal is None and "pip install -U -r requirements.txt" in i.last


def test_preview_is_only_for_its_admin(nhl):
    bot, store, guild, admin, channel, _ = nhl

    async def scenario():
        i = await send_capture(bot, guild, admin, channel)
        other = await press(i.view, "Annoncer", FakeInteraction(guild, admin_of(guild)))
        admin.guild_permissions.manage_guild = False
        demoted = await press(i.view, "Annoncer", FakeInteraction(guild, admin))
        return other, demoted
    other, demoted = run(scenario())
    assert other.sent == [] and "réservé aux admins" in demoted.last.lower()
    assert channel.texts() == []


def test_discord_refusing_midway_says_how_many_were_announced(nhl):
    bot, store, guild, admin, channel, _ = nhl
    send = channel.send
    calls = []

    async def flaky(content, **kwargs):
        calls.append(content)
        if len(calls) > 2:
            raise discord.Forbidden(FakeHTTPResponse(), "interdit")
        return await send(content, **kwargs)
    channel.send = flaky

    async def scenario():
        i = await send_capture(bot, guild, admin, channel)
        done = FakeInteraction(guild, admin)
        await press(i.view, "Annoncer", done)
        return done
    done = run(scenario())
    assert "2 transaction(s) annoncée(s) sur 8" in done.last
    # Les deux déjà publiées ne seront pas annoncées une deuxième fois.
    again = run(send_capture(bot, guild, admin, channel))
    assert again.last.count("(déjà annoncée)") == 2


def test_capture_windows_and_preview_respect_discord_limits(nhl):
    bot, store, guild, admin, channel, _ = nhl

    async def scenario():
        many = {"lines": RECORDED["lines"], "logos": RECORDED["logos"]}
        trades = capture.parse_table(many["lines"], many["logos"]) * 4  # 32 transactions
        preview = TradePreview(bot, admin.id, guild, trades, channel.id)
        single = TradePreview(bot, admin.id, guild, trades[:1], channel.id)
        for modal in (CaptureModal(bot, None), CaptureModal(bot, 5), TradeFixModal(preview, 0, guild)):
            data = modal.to_dict()
            assert len(data["title"]) <= 45 and len(data["components"]) <= 5
            for row in data["components"]:
                assert len(row["label"]) <= 45, row["label"]
                assert len(row.get("description") or "") <= 100, row["description"]
        for view in (preview, single):
            assert len(view.text(guild)) <= 2000
            for row in view.to_components():
                assert len(row["components"]) <= 5
                for component in row["components"]:
                    assert len(component.get("label") or "") <= 80
                    assert len(component.get("options", [])) <= 25
                    assert all(len(o["label"]) <= 100 for o in component.get("options", []))
        assert "7 transactions plus anciennes ne sont pas montrées" in preview.text(guild)
        assert preview.text(guild).endswith("**Annoncer**.")
        assert [type(c).__name__ for c in single.children] == ["Button", "Button", "Button"]
    run(scenario())


# Transactions SHM automatiques : le bot lit lui-même l'historique de la ligue sur SHM

from draft_bot import shm
from draft_bot.bot import ShmModal

SHM_PAGE = (Path(__file__).parent / "fixtures" / "shm_historique.html").read_text(encoding="utf-8")
SHM_WAIVERS = (Path(__file__).parent / "fixtures" / "shm_ballottage.html").read_text(encoding="utf-8")
SHM_HOME = (Path(__file__).parent / "fixtures" / "shm_accueil.html").read_text(encoding="utf-8")
SHM_ROLES = ["Red Wings", "Rangers", "Hurricanes", "Blackhawks", "Islanders", "Sénateurs", "Sabres", "Stars",
             "Kings", "Predators", "Flyers", "Blues", "Golden Knights", "Oilers", "Canadiens", "Sharks", "Admins"]


class FakeShmClient:
    """Le site SHM vu par le bot : des pages qu'on peut changer pendant le test."""

    def __init__(self):
        self.pages = {1: shm.parse_history(SHM_PAGE)}
        self.waiver_page = shm.parse_waivers(SHM_WAIVERS)
        self.home = shm.parse_feed(SHM_HOME)
        self.error = self.waivers_error = self.home_error = None
        self.requests = []
        self.other_requests = []

    async def history(self, slug, page=1):
        self.requests.append((slug, page))
        if self.error:
            raise shm.ShmError(self.error)
        return self.pages.get(page, shm.ShmPage([], {}, False))

    async def waivers(self, slug):
        self.other_requests.append((slug, "ballottage"))
        if self.error or self.waivers_error:
            raise shm.ShmError(self.error or self.waivers_error)
        return shm.WaiversPage(list(self.waiver_page.waivers), self.waiver_page.teams)

    async def feed(self, slug):
        self.other_requests.append((slug, "accueil"))
        if self.error or self.home_error:
            raise shm.ShmError(self.error or self.home_error)
        return list(self.home)

    def add_home(self, team, action, text):
        """Une nouvelle ligne en haut des « Changements par équipe » ; la plus vieille disparaît."""
        self.home = [shm.FeedRow(team, action, text)] + self.home[:29]

    def waiver(self, name):
        return next(w for w in self.waiver_page.waivers if w.name == name)

    def end_waiver(self, name):
        self.waiver_page.waivers.remove(self.waiver(name))

    def add_trade(self, sides):
        """Une nouvelle transaction confirmée sur SHM : elle arrive en haut de la page."""
        page = self.pages[1]
        page.trades.insert(0, sides)


def new_trade(left, left_items, right, right_items):
    return [shm.ShmSide(left, [capture.Item(t) for t in left_items]),
            shm.ShmSide(right, [capture.Item(t) for t in right_items])]


@pytest.fixture
def shm_league():
    store = Store(":memory:")
    site = FakeShmClient()
    bot = DraftBot(store, backup_dir=None, shm_client=site)
    roles = [FakeRole(name) for name in SHM_ROLES]
    trades = FakeChannel("transactions")
    guild = FakeGuild(roles, [trades])
    bot.get_guild = lambda guild_id: guild if guild_id == guild.id else None
    return bot, store, guild, admin_of(guild), trades, {r.name: r for r in roles}, site


async def setup_shm_from_panel(bot, guild, admin, channel, address):
    panel = (await open_panel(bot, guild, admin)).view
    await click(panel, button(panel, "options"), FakeInteraction(guild, admin))
    opened = await click(panel, button(panel, "shm"), FakeInteraction(guild, admin))
    modal = opened.response.modal
    assert isinstance(modal, ShmModal), opened.sent
    i = FakeInteraction(guild, admin)
    await fill(modal, address=address, channel=[channel]).on_submit(i)
    return i, panel


def test_shm_setup_keeps_old_trades_quiet_then_announces_new_ones(shm_league):
    bot, store, guild, admin, channel, roles, site = shm_league

    async def scenario():
        i, panel = await setup_shm_from_panel(bot, guild, admin, channel,
                                              "https://simulatedhockeymanager.ca/lnhs_2/trades/history")
        quiet = list(channel.texts())
        await bot.check_shm()
        nothing_new = list(channel.texts())
        site.add_trade(new_trade("MTL", ["Cole Caufield"], "EDM", ["Choix de 1re ronde 2027"]))
        site.add_trade(new_trade("STL", ["Jordan Kyrou"], "VGK", ["Jack Eichel"]))
        await bot.check_shm()
        await bot.check_shm()
        return i, quiet, nothing_new
    i, quiet, nothing_new = run(scenario())
    assert "C'est branché" in i.last and "lnhs_2" in i.last and "15 transactions déjà sur SHM" in i.last
    assert store.shm_league(guild.id) == "lnhs_2" and store.trades_channel(guild.id) == channel.id
    assert quiet == [] and nothing_new == []
    posted = channel.texts()
    # Les deux nouvelles, une seule fois chacune, la plus ancienne en premier.
    assert posted == [
        f"🔁 **Transaction**\n➡️ {roles['Canadiens'].mention} reçoit : **Cole Caufield**\n"
        f"➡️ {roles['Oilers'].mention} reçoit : **Choix de 1re ronde 2027**",
        f"🔁 **Transaction**\n➡️ {roles['Blues'].mention} reçoit : **Jordan Kyrou**\n"
        f"➡️ {roles['Golden Knights'].mention} reçoit : **Jack Eichel**",
    ]
    assert [a for _, _, a, _ in store.log_entries(guild.id)].count("transaction SHM") == 2


def test_shm_status_is_shown_in_more_options(shm_league):
    bot, store, guild, admin, channel, roles, site = shm_league

    async def scenario():
        await setup_shm_from_panel(bot, guild, admin, channel, "lnhs_2")
        panel = (await open_panel(bot, guild, admin)).view
        ok = await click(panel, button(panel, "options"), FakeInteraction(guild, admin))
        site.error = "Je n'arrive pas à joindre SHM en ce moment."
        await bot.check_shm()
        await click(panel, button(panel, "retour"), FakeInteraction(guild, admin))
        failing = await click(panel, button(panel, "options"), FakeInteraction(guild, admin))
        return ok.last, failing.last
    ok, failing = run(scenario())
    assert "Transactions SHM automatiques : ligue `lnhs_2` → #transactions (✅ lu à l'instant)" in ok
    assert "⚠️ Je n'arrive pas à joindre SHM" in failing


def test_trade_already_announced_with_a_capture_is_not_repeated(shm_league):
    bot, store, guild, admin, channel, roles, site = shm_league

    async def scenario():
        await setup_shm_from_panel(bot, guild, admin, channel, "lnhs_2")
        sides = new_trade("MTL", ["Cole Caufield"], "EDM", ["Evan Bouchard"])
        trade = shm.to_trade(sides, site.pages[1].teams)
        capture.resolve(trade, guild.roles, [])
        store.mark_announced(guild.id, capture.fingerprint(trade))
        site.add_trade(sides)
        await bot.check_shm()
    run(scenario())
    assert channel.texts() == []


def test_renaming_a_role_does_not_announce_old_trades_again(shm_league):
    bot, store, guild, admin, channel, roles, site = shm_league

    async def scenario():
        await setup_shm_from_panel(bot, guild, admin, channel, "lnhs_2")
        roles["Red Wings"].name = "Les Red Wings de Détroit"
        await bot.check_shm()
    run(scenario())
    assert channel.texts() == []


def test_shm_long_outage_warns_once_then_says_it_is_back(shm_league, monkeypatch):
    bot, store, guild, admin, channel, roles, site = shm_league
    now = [1_000_000.0]
    monkeypatch.setattr(bot_module.time, "time", lambda: now[0])

    async def scenario():
        await setup_shm_from_panel(bot, guild, admin, channel, "lnhs_2")
        site.error = "SHM ne répond pas bien en ce moment (erreur 502)."
        await bot.check_shm()
        now[0] += 30 * 60
        await bot.check_shm()
        assert channel.texts() == []
        now[0] += 31 * 60
        await bot.check_shm()
        now[0] += 5 * 60
        await bot.check_shm()
        site.error = None
        await bot.check_shm()
        await bot.check_shm()
    run(scenario())
    posted = channel.texts()
    assert len(posted) == 2
    assert posted[0].startswith("⚠️ Je n'arrive plus à lire SHM depuis une heure")
    assert "erreur 502" in posted[0]
    assert posted[1] == "✅ Je lis de nouveau SHM : les annonces reprennent."


def test_after_a_long_break_the_next_pages_are_read_too(shm_league):
    bot, store, guild, admin, channel, roles, site = shm_league

    async def scenario():
        await setup_shm_from_panel(bot, guild, admin, channel, "lnhs_2")
        # Pendant que le bot était éteint, 15 transactions ont poussé les anciennes en page 2.
        old = site.pages[1]
        site.pages[2] = shm.ShmPage(old.trades[:5], old.teams, True)
        site.pages[1] = shm.ShmPage(
            [new_trade("MTL", [f"Joueur {n}"], "EDM", [f"Espoir {n}"]) for n in range(15, 0, -1)], old.teams, True)
        site.requests.clear()
        await bot.check_shm()
    run(scenario())
    assert site.requests == [("lnhs_2", 1), ("lnhs_2", 2)]
    posted = channel.texts()
    assert len(posted) == 15
    assert "Joueur 1**" in posted[0] and "Joueur 15**" in posted[-1]


def test_two_servers_on_the_same_league_read_it_once(shm_league):
    bot, store, guild, admin, channel, roles, site = shm_league
    other_channel = FakeChannel("transactions")
    other = FakeGuild([FakeRole("Canadiens"), FakeRole("Oilers")], [other_channel])
    guilds = {guild.id: guild, other.id: other}
    bot.get_guild = guilds.get

    async def scenario():
        await setup_shm_from_panel(bot, guild, admin, channel, "lnhs_2")
        await setup_shm_from_panel(bot, other, admin_of(other), other_channel, "lnhs_2")
        site.add_trade(new_trade("MTL", ["Cole Caufield"], "EDM", ["Evan Bouchard"]))
        site.requests.clear()
        site.other_requests.clear()
        await bot.check_shm()
    run(scenario())
    assert site.requests == [("lnhs_2", 1)]
    assert site.other_requests == [("lnhs_2", "ballottage"), ("lnhs_2", "accueil")]
    assert len(channel.texts()) == 1 and len(other_channel.texts()) == 1


def test_shm_setup_problems_are_explained(shm_league):
    bot, store, guild, admin, channel, roles, site = shm_league
    locked = FakeChannel("verrouille", bot_allowed=False)
    guild.channels[locked.id] = locked

    async def scenario():
        bad = (await setup_shm_from_panel(bot, guild, admin, channel, "https://exemple.com/lnhs_2"))[0]
        no_access = (await setup_shm_from_panel(bot, guild, admin, locked, "lnhs_2"))[0]
        site.error = "Je ne trouve pas le tableau des transactions sur SHM. Le compte SHM du bot est-il bien membre ?"
        not_member = (await setup_shm_from_panel(bot, guild, admin, channel, "lnhs_2"))[0]
        return bad.last, no_access.last, not_member.last
    bad, no_access, not_member = run(scenario())
    assert "simulatedhockeymanager.ca" in bad
    assert "ne peut pas écrire" in no_access
    assert "membre" in not_member and "Rien n'a été changé" in not_member
    assert store.shm_league(guild.id) is None


def test_shm_can_be_turned_off(shm_league):
    bot, store, guild, admin, channel, roles, site = shm_league

    async def scenario():
        await setup_shm_from_panel(bot, guild, admin, channel, "lnhs_2")
        off = (await setup_shm_from_panel(bot, guild, admin, channel, ""))[0]
        site.add_trade(new_trade("MTL", ["Cole Caufield"], "EDM", ["Evan Bouchard"]))
        await bot.check_shm()
        return off.last
    assert "ne sont plus annoncées" in run(scenario())
    assert store.shm_league(guild.id) is None and channel.texts() == []


def test_shm_button_explains_when_the_bot_has_no_shm_account(league):
    bot, store, guild, _, _, _ = league
    admin = admin_of(guild)

    async def scenario():
        panel = (await open_panel(bot, guild, admin)).view
        await click(panel, button(panel, "options"), FakeInteraction(guild, admin))
        return await click(panel, button(panel, "shm"), FakeInteraction(guild, admin))
    i = run(scenario())
    assert i.response.modal is None and "SHM_USERNAME" in i.last


def test_shm_account_comes_from_env():
    assert bot_module.shm_client_from_env({}) is None
    client = bot_module.shm_client_from_env({"SHM_USERNAME": " HockeySimsTools ", "SHM_PASSWORD": "secret"})
    assert client.username == "HockeySimsTools" and client.password == "secret"
    with pytest.raises(bot_module.ConfigError):
        bot_module.shm_client_from_env({"SHM_USERNAME": "HockeySimsTools"})


# Les emotes d'équipe

class FakeEmoji:
    def __init__(self, name, available=True):
        self.id, self.name, self.available = next(ids), name, available

    def __str__(self):
        return f"<:{self.name}:{self.id}>"


def test_team_emotes_are_found_by_code_or_nickname():
    emotes = [FakeEmoji("PIT"), FakeEmoji("canadiens"), FakeEmoji("Toronto_Maple_Leafs"), FakeEmoji("NAS"),
              FakeEmoji("PIT2"), FakeEmoji("hockey"), FakeEmoji("BOS", available=False)]
    found = capture.team_emojis(emotes)
    assert found == {"PIT": str(emotes[0]), "MTL": str(emotes[1]), "TOR": str(emotes[2]), "NSH": str(emotes[3])}


def test_shm_announcement_shows_team_emotes(shm_league):
    bot, store, guild, admin, channel, roles, site = shm_league
    mtl, edm = FakeEmoji("MTL"), FakeEmoji("oilers")
    guild.emojis = [mtl, edm]

    async def scenario():
        await setup_shm_from_panel(bot, guild, admin, channel, "lnhs_2")
        site.add_trade(new_trade("MTL", ["Cole Caufield"], "EDM", ["Evan Bouchard"]))
        await bot.check_shm()
    run(scenario())
    assert channel.texts() == [
        f"🔁 **Transaction**\n➡️ {mtl} {roles['Canadiens'].mention} reçoit : **Cole Caufield**\n"
        f"➡️ {edm} {roles['Oilers'].mention} reçoit : **Evan Bouchard**"]


# Le ballottage SHM : un joueur soumis, puis réclamé ou pas

def test_waivers_already_on_shm_are_quiet_then_new_ones_are_announced(shm_league):
    bot, store, guild, admin, channel, roles, site = shm_league
    mtl = FakeEmoji("MTL")
    guild.emojis = [mtl]

    async def scenario():
        i, _ = await setup_shm_from_panel(bot, guild, admin, channel, "lnhs_2")
        await bot.check_shm()
        quiet = list(channel.texts())
        site.waiver_page.waivers.append(shm.ShmWaiver(
            "999:MTL", "Brendan Gallagher", "MTL", None, "AD", "33", "6 500 000", "2", "UFA", "dans 1 jour"))
        site.waiver_page.waivers.append(shm.ShmWaiver("998:NYI", "Pierre Engvall", "NYI", dest="AHL"))
        await bot.check_shm()
        await bot.check_shm()
        return i, quiet
    i, quiet = run(scenario())
    assert "ballottage" in i.last
    assert quiet == []
    assert channel.texts() == [
        f"⚠️ **Ballottage** : {mtl} {roles['Canadiens'].mention} place **Brendan Gallagher** au ballottage.\n"
        "AD · 33 ans · 6 500 000 $ · 2 ans de contrat\n"
        "⏳ Se termine dans 1 jour. S'il n'est pas réclamé, il devient joueur autonome.",
        f"⚠️ **Ballottage** : {roles['Islanders'].mention} place **Pierre Engvall** au ballottage.\n"
        "S'il n'est pas réclamé, il est envoyé dans la LAH.",
    ]
    # Pas de notification pour un joueur soumis : la mention du rôle s'affiche sans sonner.
    assert all(m == bot_module.NO_PINGS for m in channel.mentions())
    assert [a for _, _, a, _ in store.log_entries(guild.id)].count("ballottage SHM") == 2


def test_claimed_waiver_is_announced_with_the_claiming_team(shm_league):
    bot, store, guild, admin, channel, roles, site = shm_league

    async def scenario():
        await setup_shm_from_panel(bot, guild, admin, channel, "lnhs_2")
        await bot.check_shm()
        # Nils Aman (EDM) était réclamé par NAS sur la page du ballottage.
        site.end_waiver("Nils Aman")
        site.add_home("NAS", "check", "Nils Aman a été réclamé au ballotage")
        await bot.check_shm()
        await bot.check_shm()
    run(scenario())
    assert channel.texts() == [
        f"✅ **Réclamé au ballottage** : {roles['Predators'].mention} obtient **Nils Aman** "
        f"(venant de {roles['Oilers'].mention})."]
    assert channel.mentions() == [bot_module.PINGS]


def test_the_home_page_decides_who_really_claimed_the_player(shm_league):
    bot, store, guild, admin, channel, roles, site = shm_league

    async def scenario():
        await setup_shm_from_panel(bot, guild, admin, channel, "lnhs_2")
        await bot.check_shm()
        # Personne ne le réclamait à la lecture d'avant, mais MTL l'a réclamé juste avant la fin.
        site.end_waiver("Kevin Lankinen")
        site.add_home("MTL", "check", "Kevin Lankinen a été réclamé au ballotage")
        await bot.check_shm()
    run(scenario())
    assert channel.texts() == [
        f"✅ **Réclamé au ballottage** : {roles['Canadiens'].mention} obtient **Kevin Lankinen** "
        "(venant de **Toronto Maple Leafs**)."]


def test_unclaimed_waiver_is_announced_only_when_nothing_was_missed(shm_league):
    bot, store, guild, admin, channel, roles, site = shm_league

    async def scenario():
        await setup_shm_from_panel(bot, guild, admin, channel, "lnhs_2")
        await bot.check_shm()
        site.end_waiver("Kevin Lankinen")
        site.add_home("TOR", "list", "Alignement mis à jour")
        await bot.check_shm()
        sure = list(channel.texts())
        # Plus de 30 actions entre deux lectures : l'accueil ne montre plus tout, on ne devine pas.
        site.end_waiver("Chris Tanev")
        for n in range(31):
            site.add_home("TOR", "list", f"Action {n}")
        await bot.check_shm()
        return sure
    sure = run(scenario())
    assert sure == ["↩️ **Pas réclamé** : personne n'a réclamé **Kevin Lankinen** (**Toronto Maple Leafs**), "
                    "il devient joueur autonome."]
    assert channel.texts() == sure


def test_waivers_and_trades_do_not_block_each_other(shm_league):
    bot, store, guild, admin, channel, roles, site = shm_league

    async def scenario():
        await setup_shm_from_panel(bot, guild, admin, channel, "lnhs_2")
        await bot.check_shm()
        site.waivers_error = "La page du ballottage a changé."
        site.add_trade(new_trade("MTL", ["Cole Caufield"], "EDM", ["Evan Bouchard"]))
        await bot.check_shm()
        trade_only = list(channel.texts())
        site.waivers_error = None
        site.home_error = "L'accueil a changé."
        site.waiver_page.waivers.append(shm.ShmWaiver("999:MTL", "Brendan Gallagher", "MTL"))
        site.end_waiver("Nils Aman")
        await bot.check_shm()
        return trade_only
    trade_only = run(scenario())
    assert len(trade_only) == 1 and "Cole Caufield" in trade_only[0]
    later = channel.texts()[1:]
    # Sans l'accueil, la page du ballottage suffit : NAS le réclamait à la lecture d'avant.
    assert len(later) == 2
    assert "obtient **Nils Aman**" in later[0] and "place **Brendan Gallagher**" in later[1]
    # L'accueil ne sert qu'à confirmer : son erreur n'est pas affichée aux admins.
    assert bot.shm_status[guild.id].error is None


def test_changing_league_forgets_the_old_waivers(shm_league):
    bot, store, guild, admin, channel, roles, site = shm_league

    async def scenario():
        await setup_shm_from_panel(bot, guild, admin, channel, "lnhs_2")
        site.waiver_page.waivers.clear()
        await setup_shm_from_panel(bot, guild, admin, channel, "autre_ligue")
        await bot.check_shm()
    run(scenario())
    # Les joueurs de l'ancienne ligue ne sont pas annoncés comme « pas réclamés ».
    assert channel.texts() == []
    assert store.shm_waivers(guild.id) == []


# Les rachats SHM, lus sur l'accueil de la ligue

def test_buyouts_already_on_shm_are_quiet_then_new_ones_are_announced(shm_league):
    bot, store, guild, admin, channel, roles, site = shm_league
    nyi = FakeEmoji("NYI")
    guild.emojis = [nyi]

    async def scenario():
        await setup_shm_from_panel(bot, guild, admin, channel, "lnhs_2")
        await bot.check_shm()
        quiet = list(channel.texts())
        site.add_home("MTL", "gavel", "Josh Anderson a été racheté au salaire de 2 750 000$")
        # Le logo du club-école de NYI : l'équipe LNH a été apprise plus tôt sur l'accueil.
        site.add_home("BRG", "gavel", "Pierre Engvall a été racheté au salaire de 1 000 000$")
        site.add_home("SPR", "gavel", "Jake Neighbours a été racheté au salaire de 900 000$")
        await bot.check_shm()
        await bot.check_shm()
        return quiet
    quiet = run(scenario())
    assert quiet == []
    assert channel.texts() == [
        f"💸 **Rachat** : {roles['Canadiens'].mention} rachète le contrat de **Josh Anderson** "
        "(salaire du rachat : 2 750 000 $).",
        f"💸 **Rachat** : {nyi} {roles['Islanders'].mention} rachète le contrat de **Pierre Engvall** "
        "(salaire du rachat : 1 000 000 $).",
        # Un club-école inconnu : nommé tel quel, sans deviner l'équipe.
        "💸 **Rachat** : Le club-école **SPR** rachète le contrat de **Jake Neighbours** "
        "(salaire du rachat : 900 000 $).",
    ]
    assert all(m == bot_module.NO_PINGS for m in channel.mentions())
    assert [a for _, _, a, _ in store.log_entries(guild.id)].count("rachat SHM") == 3


def test_buyout_still_on_the_home_page_after_a_restart_is_not_repeated(shm_league):
    bot, store, guild, admin, channel, roles, site = shm_league

    async def scenario():
        await setup_shm_from_panel(bot, guild, admin, channel, "lnhs_2")
        site.add_home("MTL", "gavel", "Josh Anderson a été racheté au salaire de 2 750 000$")
        await bot.check_shm()
        restarted = DraftBot(store, backup_dir=None, shm_client=site)
        restarted.get_guild = bot.get_guild
        await restarted.check_shm()
    run(scenario())
    assert len(channel.texts()) == 1


def test_draft_pick_announcement_shows_the_team_emote(league):
    bot, store, guild, gm_laval, gm_granby, selections = league
    penguins = FakeRole("Penguins")
    guild.roles.append(penguins)
    emote = FakeEmoji("PIT")
    guild.emojis = [emote]
    store.set_order(guild.id, ["Penguins", "laval"])
    store.start_draft(guild.id)
    gm = FakeMember(guild, [penguins])

    async def scenario():
        i = await command(bot, "draft", FakeInteraction(guild, gm), "Mario Lemieux")
        await confirm(i)
    run(scenario())
    assert any(t.startswith(f"🏒 {emote} **Penguins** est fier de sélectionner **Mario Lemieux**")
               for t in selections.texts())
