"""Les salons du repêchage : sélection des joueurs (tableau + bouton), rondes, guide, centrale,
attributions, échanges, loterie et « Remplir automatiquement »."""

import sqlite3

import discord
import pytest

import draft_bot.bot as bot_module
from draft_bot import formatting as fmt
from draft_bot import lottery, wheel
from draft_bot.bot import (ChannelsModal, ExtraChannelsModal, LotteryModal, OrderModal, PickButtonView,
                           guess_channels)
from draft_bot.storage import Store

from test_bot import (FakeChannel, FakeGuild, FakeInteraction, FakeMember, FakeRole, admin_of, button,
                      click, command, confirm, fill, keys, open_panel, run)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(bot_module, "PICK_REPLY_SECONDS", 0)
    monkeypatch.setattr(bot_module, "WHEEL_SUSPENSE", 0)
    monkeypatch.setattr(bot_module, "WHEEL_SUSPENSE_AFTER_GIF", 0)
    monkeypatch.setattr(wheel, "spin_seconds", lambda: 0)


class Category:
    def __init__(self, name):
        self.name = name


@pytest.fixture
def salons():
    """Une ligue comme celle de William : une catégorie « Repêchage » avec un salon par rôle."""
    store = Store(":memory:")
    bot = bot_module.DraftBot(store, backup_dir=None)
    bot.backup = lambda reason: None
    laval, granby = FakeRole("Laval"), FakeRole("Granby")
    names = ["fonctionnement", "centrale-de-repêchage", "attributions-picks", "loterie", "résultat-de-la-loterie",
             "liste-des-joueurs", "sélection-des-joueurs", "ronde-1", "ronde-2", "option-repêchage",
             "trade-durant-le-repêchage"]
    channels = {name: FakeChannel(name) for name in names}
    category = Category("Repêchage")
    for c in channels.values():
        c.category = category
    general = FakeChannel("général")
    guild = FakeGuild([laval, granby], [*channels.values(), general])
    store.approve_guild(guild.id, None)
    store.replace_players(guild.id, [("Patrick Roy", "Granby", "G"), ("Mario Lemieux", "Laval", "C"),
                                     ("Ray Bourque", "Montréal", "D"), ("Guy Lafleur", "Québec", "AD")])
    store.set_order(guild.id, ["laval", "granby", "granby", "laval"])
    gm_laval, gm_granby = FakeMember(guild, [laval]), FakeMember(guild, [granby])
    return bot, store, guild, channels, gm_laval, gm_granby


def use(store, guild, channels, **keys_to_names):
    """Choisit les salons sans passer par /admin (la liste et les annonces, puis les facultatifs)."""
    store.set_channels(guild.id, channels["liste-des-joueurs"].id, channels[keys_to_names.pop("picks", "ronde-1")].id)
    store.set_draft_channels(guild.id, {k: channels[v].id for k, v in keys_to_names.items()})


async def draft(bot, guild, gm, name):
    return await confirm(await command(bot, "draft", FakeInteraction(guild, gm), name))


# Remplir automatiquement

def test_guess_finds_williams_channels(salons):
    bot, store, guild, ch, _, _ = salons
    found = {k: c.name for k, c in guess_channels(guild).items()}
    assert found == {
        "list": "liste-des-joueurs", "board": "sélection-des-joueurs", "round1": "ronde-1", "round2": "ronde-2",
        "guide": "fonctionnement", "recap": "centrale-de-repêchage", "owners": "attributions-picks",
        "trades": "trade-durant-le-repêchage", "lottery": "loterie", "lottery_result": "résultat-de-la-loterie",
        # Pas de salon « annonces » à part : les annonces vont dans la ronde 1.
        "picks": "ronde-1",
    }


def test_guess_ignores_other_categories_and_other_spellings():
    names = ["première-ronde", "deuxième-ronde", "listes", "annonces-draft", "règlements"]
    channels = [FakeChannel(n) for n in names]
    for c in channels:
        c.category = Category("Draft 2026")
    outside = FakeChannel("loterie")  # hors de la catégorie du draft : ignoré
    outside.category = Category("Général")
    guild = FakeGuild([], [*channels, outside])
    found = {k: c.name for k, c in guess_channels(guild).items()}
    assert found == {"round1": "première-ronde", "round2": "deuxième-ronde", "list": "listes",
                     "picks": "annonces-draft", "guide": "règlements"}


def test_guess_without_draft_category_looks_everywhere():
    guild = FakeGuild([], [FakeChannel("draft-liste"), FakeChannel("draft-selections"), FakeChannel("blabla")])
    found = {k: c.name for k, c in guess_channels(guild).items()}
    assert found == {"list": "draft-liste", "board": "draft-selections"}


def test_autofill_from_the_panel_then_confirm(salons):
    bot, store, guild, ch, _, _ = salons
    admin = admin_of(guild)

    async def scenario():
        panel = (await open_panel(bot, guild, admin)).view
        await click(panel, button(panel, "options"), FakeInteraction(guild, admin))
        await click(panel, button(panel, "salons"), FakeInteraction(guild, admin))
        i = await click(panel, button(panel, "auto"), FakeInteraction(guild, admin))
        assert "Voici les salons que j'ai trouvés" in i.last and "#ronde-2" in i.last
        assert store.get_channels(guild.id) in (None, (None, None))  # rien avant la confirmation
        await confirm(i)
        return i, panel
    i, panel = run(scenario())
    assert "✅ Salons enregistrés" in i.last
    assert store.get_channels(guild.id) == (ch["liste-des-joueurs"].id, ch["ronde-1"].id)
    assert store.draft_channels(guild.id)["board"] == ch["sélection-des-joueurs"].id
    assert store.draft_channels(guild.id)["round2"] == ch["ronde-2"].id
    # Le guide est publié et épinglé ; la centrale et les attributions aussi.
    guide = list(ch["fonctionnement"].messages.values())
    assert len(guide) == 1 and guide[0].pinned and "Comment fonctionne le repêchage" in guide[0].content
    assert "#sélection-des-joueurs" in guide[0].content and "#ronde-1 et #ronde-2" in guide[0].content
    assert "qui choisit quand" in ch["attributions-picks"].texts()[0]
    assert "Choix de chaque équipe" in ch["centrale-de-repêchage"].texts()[0]
    # Le draft n'est pas lancé : le tableau l'annonce, sans bouton.
    board = ch["sélection-des-joueurs"].texts()
    assert len(board) == 1 and "pas encore commencé" in board[0]
    assert ch["sélection-des-joueurs"].with_button() == []
    # L'écran des salons montre tout.
    assert keys(panel) == ["principaux", "plus", "auto", "retour"]


def test_autofill_finds_nothing(salons):
    bot, store, guild, ch, _, _ = salons
    empty = FakeGuild([], [FakeChannel("général"), FakeChannel("blabla")])
    i = FakeInteraction(empty, admin_of(empty))
    run(bot_module.autofill_channels(bot, i))
    assert "aucun salon" in i.last


# Les salons principaux

def test_main_window_saves_optional_channels_and_can_clear_them(salons):
    bot, store, guild, ch, _, _ = salons
    admin = admin_of(guild)

    async def scenario():
        modal = ChannelsModal(bot, None)
        fill(modal, list_channel=[ch["liste-des-joueurs"]], picks_channel=[ch["ronde-1"]])
        modal.selects["board"]._values = [ch["sélection-des-joueurs"]]
        modal.selects["round2"]._values = [ch["ronde-2"]]
        i = FakeInteraction(guild, admin)
        await modal.on_submit(i)
        # Plus tard : la sélection des joueurs n'est plus utilisée (menu vidé).
        again = ChannelsModal(bot, store.get_channels(guild.id), store.draft_channels(guild.id))
        assert again.selects["board"].default_values
        fill(again, list_channel=[ch["liste-des-joueurs"]], picks_channel=[ch["ronde-1"]])
        again.selects["board"]._values = []
        again.selects["round2"]._values = [ch["ronde-2"]]
        cleared = FakeInteraction(guild, admin)
        await again.on_submit(cleared)
        return i, cleared
    i, cleared = run(scenario())
    assert "Sélection des joueurs (tableau + bouton) : #sélection-des-joueurs" in i.last
    assert store.draft_channels(guild.id)["board"] is None
    assert store.draft_channels(guild.id)["round2"] == ch["ronde-2"].id
    assert "Sélection des joueurs (tableau + bouton) : —" in cleared.last
    # Le tableau publié avant est effacé.
    assert ch["sélection-des-joueurs"].texts() == []


def test_list_channel_must_stay_alone(salons):
    bot, store, guild, ch, _, _ = salons

    async def scenario():
        modal = ChannelsModal(bot, None)
        fill(modal, list_channel=[ch["liste-des-joueurs"]], picks_channel=[ch["ronde-1"]])
        modal.selects["board"]._values = [ch["liste-des-joueurs"]]
        i = FakeInteraction(guild, admin_of(guild))
        await modal.on_submit(i)
        return i
    i = run(scenario())
    assert "seulement à la liste" in i.last and "Sélection des joueurs" in i.last
    assert store.get_channels(guild.id) in (None, (None, None))


def test_extra_channels_need_main_channels_and_access(salons):
    bot, store, guild, ch, _, _ = salons
    hidden = FakeChannel("cache", bot_allowed=False)
    guild.channels[hidden.id] = hidden

    async def scenario():
        first = FakeInteraction(guild, admin_of(guild))
        modal = ExtraChannelsModal(bot, {})
        modal.selects["guide"]._values = [ch["fonctionnement"]]
        await modal.on_submit(first)
        use(store, guild, ch)
        blocked = FakeInteraction(guild, admin_of(guild))
        modal = ExtraChannelsModal(bot, {})
        modal.selects["recap"]._values = [hidden]
        await modal.on_submit(blocked)
        return first, blocked
    first, blocked = run(scenario())
    assert "Salons principaux" in first.last
    assert "🔒" in blocked.last and "#cache" in blocked.last
    assert store.draft_channels(guild.id)["recap"] is None


def test_guide_without_pin_permission_is_still_published(salons):
    bot, store, guild, ch, _, _ = salons
    use(store, guild, ch)
    ch["fonctionnement"].can_pin = False

    async def scenario():
        modal = ExtraChannelsModal(bot, {})
        modal.selects["guide"]._values = [ch["fonctionnement"]]
        i = FakeInteraction(guild, admin_of(guild))
        await modal.on_submit(i)
        return i
    i = run(scenario())
    assert "✅" in i.last
    guide = list(ch["fonctionnement"].messages.values())
    assert len(guide) == 1 and not guide[0].pinned


# La sélection des joueurs : le tableau du draft, avec le bouton

def test_board_mode_announces_in_rounds_and_reposts_the_board(salons):
    bot, store, guild, ch, gm_laval, gm_granby = salons
    use(store, guild, ch, board="sélection-des-joueurs", round2="ronde-2")
    store.start_draft(guild.id)
    board = ch["sélection-des-joueurs"]

    async def scenario():
        await draft(bot, guild, gm_laval, "Lemieux")   # #1, ronde 1
        await draft(bot, guild, gm_granby, "Roy")      # #2, ronde 1
        first_board = list(board.messages.values())
        await draft(bot, guild, gm_granby, "Bourque")  # #3, ronde 2
        return first_board
    first_board = run(scenario())
    # Les annonces : chaque choix dans le salon de sa ronde, sans « Au tour de » ni bouton.
    r1, r2 = ch["ronde-1"].texts(), ch["ronde-2"].texts()
    assert len(r1) == 2 and "Mario Lemieux" in r1[0] and "Patrick Roy" in r1[1]
    assert len(r2) == 1 and "Ray Bourque" in r2[0]
    assert not any("Au tour de" in t for t in r1 + r2)
    assert ch["ronde-1"].with_button() == [] and ch["ronde-2"].with_button() == []
    # Le tableau : un seul message, republié en bas du salon, avec la mention et le bouton.
    assert len(first_board) == 1
    messages = list(board.messages.values())
    assert len(messages) == 1 and messages[0].id != first_board[0].id
    text = messages[0].content
    assert "Ronde 2" in text and "Au tour de **Laval**" in text and "<@&" in text
    assert "Derniers choix" in text and "Ray Bourque" in text
    assert board.with_button() == messages
    assert store.button_message(guild.id) == messages[0].id
    assert board.mentions()[-1] == bot_module.PINGS
    # Le bon salon est gardé pour chaque choix.
    assert store.get_pick(guild.id, 3).message_channel_id == ch["ronde-2"].id


def test_board_after_the_last_pick_has_no_button(salons):
    bot, store, guild, ch, gm_laval, _ = salons
    use(store, guild, ch, board="sélection-des-joueurs")
    store.set_order(guild.id, ["laval"])
    store.start_draft(guild.id)
    run(draft(bot, guild, gm_laval, "Lemieux"))
    board = ch["sélection-des-joueurs"]
    assert "Le draft est terminé" in board.texts()[-1]
    assert board.with_button() == [] and store.button_message(guild.id) is None


def test_board_button_works_like_before(salons):
    bot, store, guild, ch, gm_laval, _ = salons
    use(store, guild, ch, board="sélection-des-joueurs")

    async def scenario():
        i = FakeInteraction(guild, admin_of(guild))
        await bot_module.launch_draft(bot, i)
        await confirm(i)
        view = ch["sélection-des-joueurs"].with_button()[0].view
        return await click(view, view.pick, FakeInteraction(guild, gm_laval))
    i = run(scenario())
    assert "c'est ton tour" in i.last
    # « Le draft commence ! » est dit dans la sélection des joueurs, juste avant le tableau.
    texts = ch["sélection-des-joueurs"].texts()
    assert "Le draft commence" in texts[0] and "Au tour de **Laval**" in texts[-1]
    assert ch["ronde-1"].texts() == []


def test_removing_the_board_mid_draft_brings_the_button_back(salons):
    bot, store, guild, ch, gm_laval, _ = salons
    use(store, guild, ch, board="sélection-des-joueurs")
    store.start_draft(guild.id)
    run(draft(bot, guild, gm_laval, "Lemieux"))
    assert ch["sélection-des-joueurs"].with_button()

    async def scenario():
        modal = ChannelsModal(bot, store.get_channels(guild.id), store.draft_channels(guild.id))
        fill(modal, list_channel=[ch["liste-des-joueurs"]], picks_channel=[ch["ronde-1"]])
        modal.selects["board"]._values = []
        await modal.on_submit(FakeInteraction(guild, admin_of(guild)))
    run(scenario())
    assert ch["sélection-des-joueurs"].texts() == []
    buttons = ch["ronde-1"].with_button()
    assert len(buttons) == 1 and "Au tour de **Granby**" in buttons[0].content
    assert store.button_message(guild.id) == buttons[0].id


def test_adding_the_board_mid_draft_moves_the_button(salons):
    bot, store, guild, ch, gm_laval, _ = salons
    use(store, guild, ch)
    store.start_draft(guild.id)
    run(draft(bot, guild, gm_laval, "Lemieux"))
    assert ch["ronde-1"].with_button()

    async def scenario():
        modal = ChannelsModal(bot, store.get_channels(guild.id), store.draft_channels(guild.id))
        fill(modal, list_channel=[ch["liste-des-joueurs"]], picks_channel=[ch["ronde-1"]])
        modal.selects["board"]._values = [ch["sélection-des-joueurs"]]
        await modal.on_submit(FakeInteraction(guild, admin_of(guild)))
    run(scenario())
    assert ch["ronde-1"].with_button() == []
    assert len(ch["sélection-des-joueurs"].with_button()) == 1


def test_undo_deletes_the_announcement_in_its_round_channel(salons):
    bot, store, guild, ch, gm_laval, gm_granby = salons
    use(store, guild, ch, board="sélection-des-joueurs", round2="ronde-2")
    store.start_draft(guild.id)

    async def scenario():
        await draft(bot, guild, gm_laval, "Lemieux")
        await draft(bot, guild, gm_granby, "Roy")
        await draft(bot, guild, gm_granby, "Bourque")
        i = FakeInteraction(guild, admin_of(guild))
        await bot_module.undo_last_pick(bot, i)
        await confirm(i)
        return i
    i = run(scenario())
    assert "annulé" in i.last
    r2 = ch["ronde-2"].texts()
    assert not any("Ray Bourque**" in t and "sélectionner" in t for t in r2)
    assert any("retourne dans la liste" in t for t in r2)
    board = ch["sélection-des-joueurs"].texts()
    assert len(board) == 1 and "Au tour de **Granby**" in board[0]


def test_time_up_alert_goes_to_the_selection_channel(salons):
    bot, store, guild, ch, _, _ = salons
    use(store, guild, ch, board="sélection-des-joueurs")
    store.start_draft(guild.id)
    store.set_timer(guild.id, 60, None)
    store.start_clock(guild.id, now=0)
    bot.get_guild = lambda guild_id: guild
    run(bot.check_timers.coro(bot))
    assert any("Temps écoulé" in t for t in ch["sélection-des-joueurs"].texts())
    assert not any("Temps écoulé" in t for t in ch["ronde-1"].texts())


# Échanges, attributions et centrale

def test_trade_goes_to_its_channel_and_updates_owners_and_board(salons):
    bot, store, guild, ch, gm_laval, _ = salons
    use(store, guild, ch, board="sélection-des-joueurs", trades="trade-durant-le-repêchage",
        owners="attributions-picks")
    store.start_draft(guild.id)
    laval = guild.roles[0]
    board = ch["sélection-des-joueurs"]

    async def scenario():
        await draft(bot, guild, gm_laval, "Lemieux")
        before = list(board.messages)
        i = FakeInteraction(guild, admin_of(guild))
        await bot_module.trade_pick(bot, i, "3", laval)  # pas le choix en cours
        await confirm(i)
        return before, i
    before, i = run(scenario())
    assert "#trade-durant-le-repêchage" in i.last
    trades = ch["trade-durant-le-repêchage"].texts()
    assert len(trades) == 1 and "Échange" in trades[0] and "Au tour" not in trades[0]
    # Le tableau est modifié sur place (personne n'est mentionné de nouveau).
    assert list(board.messages) == before
    assert "#3 **Laval** (via Granby)" in board.texts()[0]
    assert "#3 **Laval** (via Granby) 🔁" in "\n".join(ch["attributions-picks"].texts())
    assert store.get_pick(guild.id, 3).via == "Granby"


def test_trade_of_the_current_pick_pings_the_new_team(salons):
    bot, store, guild, ch, _, _ = salons
    use(store, guild, ch, board="sélection-des-joueurs", trades="trade-durant-le-repêchage")
    store.start_draft(guild.id)
    granby = guild.roles[1]

    async def scenario():
        before = list(ch["sélection-des-joueurs"].messages)
        i = FakeInteraction(guild, admin_of(guild))
        await bot_module.trade_pick(bot, i, "1", granby)
        await confirm(i)
        return before
    before = run(scenario())
    after = list(ch["sélection-des-joueurs"].messages.values())
    assert len(after) == 1 and after[0].id not in before
    assert "Au tour de **Granby**" in after[0].content and "(via Laval)" not in after[0].content.split("\n")[1]


def test_recap_and_owners_follow_every_pick_and_undo(salons):
    bot, store, guild, ch, gm_laval, gm_granby = salons
    use(store, guild, ch, recap="centrale-de-repêchage", owners="attributions-picks")
    store.start_draft(guild.id)

    async def scenario():
        await draft(bot, guild, gm_laval, "Lemieux")
        await draft(bot, guild, gm_granby, "Roy")
        recap = "\n".join(ch["centrale-de-repêchage"].texts())
        i = FakeInteraction(guild, admin_of(guild))
        await bot_module.undo_last_pick(bot, i)
        await confirm(i)
        return recap
    recap = run(scenario())
    assert "**Laval**" in recap and "Mario Lemieux · C" in recap and "Patrick Roy · G" in recap
    after = "\n".join(ch["centrale-de-repêchage"].texts())
    assert "Mario Lemieux" in after and "Patrick Roy" not in after
    owners = "\n".join(ch["attributions-picks"].texts())
    assert "Ronde 1" in owners and "Ronde 2" in owners and "#1 **Laval** : Mario Lemieux" in owners
    # Toujours un seul message par salon : il est modifié, pas republié.
    assert len(ch["centrale-de-repêchage"].messages) == 1 and len(ch["attributions-picks"].messages) == 1


def test_recap_at_the_end_says_finished():
    picks = [bot_module.Pick(1, 1, 1, "Laval", 10, None), bot_module.Pick(2, 1, 2, "Granby", 11, None)]
    players = {10: bot_module.Player(10, "Mario Lemieux", "Laval", "C"),
               11: bot_module.Player(11, "Patrick Roy", "Granby", "G")}
    text = "\n".join(fmt.recap(picks, players))
    assert "Repêchage terminé" in text and text.index("Granby") < text.index("Laval")


def test_long_tables_are_split_under_discord_limit():
    picks = [bot_module.Pick(n, (n - 1) // 32 + 1, (n - 1) % 32 + 1, f"Équipe numéro {n % 32}", n, None,
                             f"Équipe d'origine {n % 7}") for n in range(1, 225)]
    players = {n: bot_module.Player(n, f"Joueur avec un long nom {n}", "Club", "AD") for n in range(1, 225)}
    for chunks in (fmt.owners(picks, players), fmt.recap(picks, players)):
        assert len(chunks) > 1 and all(len(c) <= 2000 for c in chunks)
    assert len(fmt.board(picks, players, True, "⏱️ Au tour de …")) <= 2000


def test_new_order_resets_board_owners_and_recap(salons):
    bot, store, guild, ch, gm_laval, _ = salons
    use(store, guild, ch, board="sélection-des-joueurs", recap="centrale-de-repêchage", owners="attributions-picks")
    store.start_draft(guild.id)

    async def scenario():
        await draft(bot, guild, gm_laval, "Lemieux")
        i = FakeInteraction(guild, admin_of(guild))
        await fill(OrderModal(bot), teams="Granby\nLaval", rounds="1", snake=["non"]).on_submit(i)
        await confirm(i)
    run(scenario())
    assert "pas encore commencé" in ch["sélection-des-joueurs"].texts()[0]
    assert ch["sélection-des-joueurs"].with_button() == []
    assert "Aucun choix pour l'instant" in ch["centrale-de-repêchage"].texts()[0]
    assert "#1 **Granby**" in ch["attributions-picks"].texts()[0]


def test_removed_extra_channel_is_emptied(salons):
    bot, store, guild, ch, _, _ = salons
    use(store, guild, ch, owners="attributions-picks", guide="fonctionnement")
    run(bot.refresh_extras(guild, guide=True))
    assert ch["attributions-picks"].texts() and ch["fonctionnement"].texts()

    async def scenario():
        modal = ExtraChannelsModal(bot, store.draft_channels(guild.id))
        for select in modal.selects.values():
            select._values = []
        await modal.on_submit(FakeInteraction(guild, admin_of(guild)))
    run(scenario())
    assert ch["attributions-picks"].texts() == [] and ch["fonctionnement"].texts() == []


def test_guide_follows_the_delay(salons):
    bot, store, guild, ch, _, _ = salons
    use(store, guild, ch, guide="fonctionnement")
    store.start_draft(guild.id)
    run(bot.refresh_extras(guild, guide=True))
    assert "⌛" not in ch["fonctionnement"].texts()[0]
    run(bot_module.set_delay(bot, FakeInteraction(guild, admin_of(guild)), "8h", None))
    guide = ch["fonctionnement"].texts()
    assert len(guide) == 1 and "**8 h** pour chaque choix" in guide[0]


def test_without_optional_channels_everything_stays_as_before(salons):
    """Aucun salon facultatif : les annonces, « Au tour de » et le bouton restent ensemble, comme avant."""
    bot, store, guild, ch, gm_laval, _ = salons
    use(store, guild, ch)
    store.start_draft(guild.id)
    run(draft(bot, guild, gm_laval, "Lemieux"))
    last = ch["ronde-1"].texts()[-1]
    assert "Mario Lemieux" in last and "Au tour de **Granby**" in last
    assert len(ch["ronde-1"].with_button()) == 1
    for name in ("sélection-des-joueurs", "fonctionnement", "centrale-de-repêchage", "attributions-picks"):
        assert ch[name].texts() == []


def test_failing_extra_channel_never_blocks_a_pick(salons):
    bot, store, guild, ch, gm_laval, _ = salons
    use(store, guild, ch, recap="centrale-de-repêchage")
    store.start_draft(guild.id)

    async def broken(*args, **kwargs):
        raise discord.HTTPException(type("R", (), {"status": 500, "reason": "x"})(), "panne")
    ch["centrale-de-repêchage"].send = broken
    i = run(draft(bot, guild, gm_laval, "Lemieux"))
    assert store.made_picks_count(guild.id) == 1
    assert "centrale" in i.last and "bien enregistré" in i.last
    assert "Mario Lemieux" in ch["ronde-1"].texts()[-1]


# La loterie

def test_lottery_lines_are_read_in_many_ways():
    entries = lottery.parse_entries("San Jose 18,5\nWinnipeg ; 13.5 %\n\nQuébec: 11\nTeam 2 10")
    assert entries == [("San Jose", 18.5), ("Winnipeg", 13.5), ("Québec", 11.0), ("Team 2", 10.0)]
    for bad, message in (("San Jose", "comprends pas"), ("A 10\na 5", "deux fois"), ("A 10", "au moins deux"),
                         ("A 0\nB 5", "0 chance")):
        with pytest.raises(lottery.LotteryError, match=message):
            lottery.parse_entries(bad)
    assert lottery.parse_draws("2", 4) == 2
    for bad in ("0", "4", "deux", ""):
        with pytest.raises(lottery.LotteryError):
            lottery.parse_draws(bad, 4)


class FixedRandom:
    def __init__(self, *values):
        self.values = list(values)

    def random(self):
        return self.values.pop(0)


def test_lottery_draw_respects_chances_without_replacement():
    entries = [("A", 50), ("B", 30), ("C", 20)]
    # 1er tirage : 0,9 × 100 = 90 → C (après 50 + 30). 2e : sur A + B = 80, 0,1 × 80 = 8 → A.
    winners = lottery.draw(entries, 2, FixedRandom(0.9, 0.1))
    assert winners == [2, 0]
    assert lottery.final_order(entries, winners) == ["C", "A", "B"]
    result = lottery.result_message("Loterie", entries, ["C", "A", "B"])
    assert "1. C ⬆️ +2" in result and "2. A ⬇️ -1" in result and "3. B ⬇️ -1" in result


def test_lottery_is_fair_over_many_draws():
    import random
    rng = random.Random(4)
    entries = [("A", 60), ("B", 30), ("C", 10)]
    counts = {0: 0, 1: 0, 2: 0}
    for _ in range(6000):
        counts[lottery.draw(entries, 1, rng)[0]] += 1
    assert abs(counts[0] / 6000 - 0.6) < 0.03 and abs(counts[2] / 6000 - 0.1) < 0.02


def test_weighted_wheel_stops_on_the_winner():
    import random
    rng = random.Random(1)
    weights = [18.5, 13.5, 11.5, 0.5, 56]
    for winner in range(len(weights)):
        for _ in range(20):
            angle = wheel.final_angle(len(weights), winner, rng, weights)
            assert wheel.winner_under_pointer(len(weights), angle, weights) == winner
    # Sans « weights », la roue de /roue ne change pas.
    assert wheel.winner_under_pointer(4, wheel.final_angle(4, 2, rng)) == 2


def test_weighted_wheel_draws_a_gif():
    gif = wheel.render_gif(["San Jose", "Winnipeg", "Minuscule"], 2, weights=[60, 39.5, 0.5])
    assert gif[:6] == b"GIF89a"


def test_lottery_from_the_panel(salons):
    bot, store, guild, ch, _, _ = salons
    use(store, guild, ch)
    store.set_order(guild.id, ["laval", "granby"])
    ch["loterie"].attach_files = True

    async def scenario():
        admin = admin_of(guild)
        modal = LotteryModal(bot, store.draft_channels(guild.id))
        fill(modal, teams="Laval 60\nGranby 30\nQuébec 10", draws="2")
        modal.lottery_channel._values = [ch["loterie"]]
        modal.result_channel._values = [ch["résultat-de-la-loterie"]]
        i = FakeInteraction(guild, admin)
        await modal.on_submit(i)
        assert "Faire la loterie" in i.last and "Laval : 60 %" in i.last
        assert "Québec" in i.last and "Aucun rôle" in i.last  # pas de rôle « Québec »
        await confirm(i)
        return i
    i = run(scenario())
    assert "Loterie terminée" in i.last
    texts = ch["loterie"].texts()
    assert "2 premiers choix" in texts[0]
    assert sum("La roue tourne" in t for t in texts) == 2 and sum("obtient le" in t for t in texts) == 2
    assert any(m.file is not None for m in ch["loterie"].messages.values())
    result = ch["résultat-de-la-loterie"].texts()
    assert len(result) == 1 and "Résultat" in result[0]
    order = store.lottery_order(guild.id)
    assert sorted(order) == ["Granby", "Laval", "Québec"]
    assert store.draft_channels(guild.id)["lottery"] == ch["loterie"].id
    # Le journal garde le résultat ; « Changer l'ordre » le propose déjà écrit.
    assert any(action == "loterie" for _, _, action, _ in store.log_entries(guild.id))
    modal = OrderModal(bot, store.lottery_order(guild.id))
    assert modal.teams.default == "\n".join(order)


def test_lottery_order_is_offered_until_used(salons):
    bot, store, guild, ch, _, _ = salons
    use(store, guild, ch)
    store.set_lottery_order(guild.id, ["Granby", "Laval"])
    admin = admin_of(guild)

    async def scenario():
        panel = (await open_panel(bot, guild, admin)).view
        await click(panel, button(panel, "options"), FakeInteraction(guild, admin))
        i = await click(panel, button(panel, "ordre"), FakeInteraction(guild, admin))
        modal = i.response.modal
        assert modal.teams.default == "Granby\nLaval"
        done = FakeInteraction(guild, admin)
        await fill(modal, teams="Granby\nLaval", rounds="2", snake=["non"]).on_submit(done)
        await confirm(done)
    run(scenario())
    assert store.lottery_order(guild.id) == []
    assert OrderModal(bot, store.lottery_order(guild.id)).teams.default is None


def test_lottery_without_files_permission_is_text_only(salons):
    bot, store, guild, ch, _, _ = salons
    use(store, guild, ch)

    async def scenario():
        modal = LotteryModal(bot, {})
        fill(modal, teams="Laval 70\nGranby 30", draws="1")
        modal.lottery_channel._values = [ch["loterie"]]
        modal.result_channel._values = []
        i = FakeInteraction(guild, admin_of(guild))
        await modal.on_submit(i)
        await confirm(i)
        return i
    i = run(scenario())
    assert "Loterie terminée" in i.last
    assert all(m.file is None for m in ch["loterie"].messages.values())
    # Sans salon de résultat, le résultat va dans le salon de la loterie.
    assert "Résultat" in ch["loterie"].texts()[-1]


def test_two_lotteries_at_once_are_refused(salons):
    bot, store, guild, ch, _, _ = salons
    use(store, guild, ch)
    bot.lotteries.add(guild.id)

    async def scenario():
        modal = LotteryModal(bot, {})
        fill(modal, teams="Laval 70\nGranby 30", draws="1")
        modal.lottery_channel._values = [ch["loterie"]]
        modal.result_channel._values = []
        i = FakeInteraction(guild, admin_of(guild))
        await modal.on_submit(i)
        await confirm(i)
        return i
    i = run(scenario())
    assert "déjà en cours" in i.last and ch["loterie"].texts() == []


def test_bad_lottery_explains(salons):
    bot, store, guild, ch, _, _ = salons

    async def scenario():
        modal = LotteryModal(bot, {})
        fill(modal, teams="Laval\nGranby 30", draws="1")
        modal.lottery_channel._values = [ch["loterie"]]
        modal.result_channel._values = []
        i = FakeInteraction(guild, admin_of(guild))
        await modal.on_submit(i)
        return i
    i = run(scenario())
    assert "❌" in i.last and "Loterie" in i.last and i.view is None


# La base de données d'avant cette mise à jour

def test_old_database_is_upgraded(tmp_path):
    path = tmp_path / "ancienne.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE config (guild_id INTEGER PRIMARY KEY, list_channel_id INTEGER, picks_channel_id INTEGER);
        CREATE TABLE picks (guild_id INTEGER NOT NULL, number INTEGER NOT NULL, round INTEGER NOT NULL,
                            in_round INTEGER NOT NULL, team TEXT NOT NULL, player_id INTEGER, message_id INTEGER,
                            PRIMARY KEY (guild_id, number));
        INSERT INTO config (guild_id, list_channel_id, picks_channel_id) VALUES (1, 10, 20);
        INSERT INTO picks VALUES (1, 1, 1, 1, 'Laval', NULL, NULL);
    """)
    db.commit()
    db.close()
    store = Store(str(path))
    assert store.get_channels(1) == (10, 20)
    assert store.draft_channels(1) == {k: None for k in store.draft_channels(1)}
    pick_ = store.get_pick(1, 1)
    assert pick_.team == "Laval" and pick_.via is None and pick_.message_channel_id is None
    assert store.button_channel(1) is None and store.lottery_order(1) == []
