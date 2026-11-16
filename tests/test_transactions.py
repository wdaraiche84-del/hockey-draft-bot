"""Les transactions SHM : les rôles Discord des équipes, les annonces et leur empreinte."""

import sqlite3

import pytest

from draft_bot import transactions
from draft_bot.storage import Store
from draft_bot.transactions import Item, Logo, Side, Trade


class Role:
    def __init__(self, role_id, name):
        self.id, self.name, self.mention = role_id, name, f"<@&{role_id}>"


def roles(*names):
    return [Role(i, name) for i, name in enumerate(names, start=1)]


def tarasov_trade():
    """Calgary reçoit Daniil Tarasov, Edmonton reçoit le choix de 2e ronde 2020 des Blues."""
    return Trade([
        Side(Logo("CGY"), [Item("Daniil Tarasov")], name="Calgary Flames"),
        Side(Logo("EDM"), [Item("Choix de 2e ronde", 2, "2020", Logo("STL"))], name="Edmonton Oilers"),
    ])


# Les rôles Discord des équipes

@pytest.mark.parametrize("name", ["Bruins", "Boston", "BOS", "Boston Bruins", "Bruins de Boston",
                                  "Les Bruins de Boston", "bruins", "🐻 Bruins"])
def test_team_role_found_by_any_usual_name(name):
    assert transactions.role_for_team("BOS", roles("Admin", name, "Canadiens")).name == name


def test_french_and_tricky_names():
    found = roles("Canadiens de Montréal", "Sénateurs d'Ottawa", "Blues de Saint-Louis", "Leafs", "Kings",
                  "Rangers", "Islanders", "Lightning", "Avalanche")
    assert transactions.role_for_team("MTL", found).name == "Canadiens de Montréal"
    assert transactions.role_for_team("OTT", found).name == "Sénateurs d'Ottawa"
    assert transactions.role_for_team("STL", found).name == "Blues de Saint-Louis"
    assert transactions.role_for_team("TOR", found).name == "Leafs"
    assert transactions.role_for_team("LAK", found).name == "Kings"
    assert transactions.role_for_team("NYR", found).name == "Rangers"
    assert transactions.role_for_team("NYI", found).name == "Islanders"
    assert transactions.role_for_team("TBL", found).name == "Lightning"
    assert transactions.role_for_team("COL", found).name == "Avalanche"


def test_unclear_roles_are_not_guessed():
    # « New York » pourrait être les Rangers ou les Islanders.
    assert transactions.role_for_team("NYR", roles("New York")) is None
    # Deux rôles portent le nom de la même équipe : aucun n'est choisi.
    assert transactions.role_for_team("BOS", roles("Bruins", "Boston")) is None
    # Un rôle qui nomme deux équipes n'est pas choisi.
    assert transactions.role_for_team("ANA", roles("Anaheim - Los Angeles")) is None
    assert transactions.role_for_team("BOS", roles("Laval", "Granby")) is None


def test_exact_role_wins_over_a_role_that_only_contains_the_name():
    assert transactions.role_for_team("BOS", roles("DG Bruins", "Bruins")).name == "Bruins"


def test_every_team_has_a_role_name():
    assert len(transactions.TEAMS) == len(transactions.TEAM_NAMES) == 33
    for code, english, *_ in transactions.TEAMS:
        assert transactions.role_for_team(code, roles(english)).name == english


# Les annonces

def test_announcement_mentions_both_teams():
    trade = tarasov_trade()
    server_roles = roles("Flames", "Oilers", "Blues")
    transactions.resolve(trade, server_roles)
    text = transactions.announcement(trade, {r.id: r for r in server_roles})
    assert text == ("🔁 **Transaction**\n"
                    "➡️ <@&1> reçoit : **Daniil Tarasov**\n"
                    "➡️ <@&2> reçoit : **Choix de 2e ronde 2020 (Blues)**")


def test_team_without_role_is_named_and_gets_its_emote():
    trade = tarasov_trade()
    transactions.resolve(trade, [])
    text = transactions.announcement(trade, {}, {"CGY": "<:CGY:1>"})
    assert text.splitlines()[1] == "➡️ <:CGY:1> **Calgary Flames** reçoit : **Daniil Tarasov**"
    assert text.splitlines()[2] == "➡️ **Edmonton Oilers** reçoit : **Choix de 2e ronde 2020 (St. Louis Blues)**"


def test_summary_is_short():
    trade = tarasov_trade()
    transactions.resolve(trade, [])
    assert transactions.summary(trade, {}, 30).endswith("…")
    assert len(transactions.summary(trade, {}, 30)) == 30


def test_same_trade_same_fingerprint():
    a, b = tarasov_trade(), tarasov_trade()
    for trade in (a, b):
        transactions.resolve(trade, roles("Flames", "Oilers"))
    assert transactions.fingerprint(a) == transactions.fingerprint(b)
    b.texts[0] = ["Autre joueur"]
    assert transactions.fingerprint(a) != transactions.fingerprint(b)


# Ce que le bot retient par serveur

def test_store_remembers_announced_trades():
    store = Store(":memory:")
    store.mark_announced(1, "empreinte")
    assert store.is_announced(1, "empreinte") and not store.is_announced(2, "empreinte")


def test_database_from_the_capture_days_still_opens(tmp_path):
    """Une base qui a servi à l'ancien bouton « Annoncer une transaction » : son salon des
    transactions devient celui des annonces SHM, et ses logos appris ne gênent pas."""
    path = tmp_path / "draft.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE config (guild_id INTEGER PRIMARY KEY, list_channel_id INTEGER, "
                "picks_channel_id INTEGER, trades_channel_id INTEGER)")
    old.execute("INSERT INTO config VALUES (1, 2, 3, 4)")
    old.execute("CREATE TABLE trade_logos (guild_id INTEGER NOT NULL, role_id INTEGER NOT NULL, "
                "descriptor BLOB NOT NULL, at REAL NOT NULL)")
    old.execute("INSERT INTO trade_logos VALUES (1, 7, x'616263', 0)")
    old.commit()
    old.close()
    store = Store(str(path))
    assert store.get_channels(1) == (2, 3)
    assert store.shm_channels(1)["trades"] == 4
