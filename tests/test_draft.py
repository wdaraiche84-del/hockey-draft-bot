from draft_bot import formatting as fmt
from draft_bot.storage import Player, Store


def make_store(players=None, order=None):
    store = Store(":memory:")
    store.replace_players(1, players or [("Sidney Crosby", "Halifax", "C"), ("Jérôme Iginla", "Kamloops", "AD"),
                                         ("Patrick Roy", "Granby", "G")])
    store.set_order(1, order or ["MTL", "TOR"])
    return store


def test_parse_players_semicolon_with_header():
    rows = fmt.parse_players("Nom;Équipe;Position\nPatrick Roy;Granby;G\n\nMario Lemieux;Laval;C\n")
    assert rows == [("Patrick Roy", "Granby", "G"), ("Mario Lemieux", "Laval", "C")]


def test_parse_players_missing_columns():
    assert fmt.parse_players("Patrick Roy\n") == [("Patrick Roy", "", "")]


def test_decode_latin1():
    assert fmt.decode("Jérôme".encode("latin-1")) == "Jérôme"


def test_build_order_snake():
    assert fmt.build_order(["A", "B", "C"], 2, snake=True) == ["A", "B", "C", "C", "B", "A"]
    assert fmt.build_order(["A", "B"], 2, snake=False) == ["A", "B", "A", "B"]


def test_render_list_splits_under_limit():
    players = [Player(i, f"Joueur numéro {i}", "Équipe junior", "AD") for i in range(300)]
    chunks = fmt.render_list(players)
    assert len(chunks) > 1
    assert all(len(c) <= fmt.MAX_MESSAGE for c in chunks)
    assert sum(c.count("\n") + 1 for c in chunks) == 300 + 1  # + l'en-tête


def test_resolve_ignores_accents_and_case():
    store = make_store()
    assert [p.name for p in store.resolve_available(1, "jerome iginla")] == ["Jérôme Iginla"]
    assert len(store.resolve_available(1, "ro")) == 3  # Crosby, Jérôme et Roy : ambigu


def test_pick_flow_and_undo():
    store = make_store()
    pick = store.current_pick(1)
    assert (pick.number, pick.team, pick.round, pick.in_round) == (1, "MTL", 1, 1)
    roy = store.resolve_available(1, "Patrick Roy")[0]
    store.make_pick(1, pick.number, roy.id)
    assert store.current_pick(1).team == "TOR"
    assert "Patrick Roy" not in [p.name for p in store.available_players(1)]
    assert store.resolve_available(1, "Patrick Roy") == []

    store.undo_pick(1, store.last_made_pick(1).number)
    assert store.current_pick(1).number == 1
    assert len(store.available_players(1)) == 3


def test_rounds_and_transfer():
    store = make_store(order=fmt.build_order(["MTL", "TOR"], 2, snake=False))
    assert [(p.number, p.round, p.in_round) for p in store.upcoming_picks(1, 4)] == [
        (1, 1, 1), (2, 1, 2), (3, 2, 1), (4, 2, 2)]
    assert store.transfer_pick(1, 3, "BOS")
    assert store.get_pick(1, 3).team == "BOS"


def test_team_names_get_capitalized():
    store = make_store(order=["laval", "granby", "mtl", "Trois-Rivières"])
    assert [p.team for p in store.upcoming_picks(1, 4)] == ["Laval", "Granby", "MTL", "Trois-Rivières"]
    store.transfer_pick(1, 1, "sherbrooke")
    assert store.get_pick(1, 1).team == "Sherbrooke"


def test_timer_alerts_once_per_pick():
    store = make_store()
    store.set_timer(1, 3600, admin_role_id=99)
    store.start_clock(1, now=1000)
    assert store.deadline(1) == 4600
    assert store.expired_pick(1, now=4599) is None
    assert store.expired_pick(1, now=4600).number == 1
    store.mark_alerted(1)
    assert store.expired_pick(1, now=9999) is None
    assert store.timed_guilds() == [1]


def test_timer_restarts_when_turn_changes():
    store = make_store()
    store.set_timer(1, 60, admin_role_id=None)
    store.start_clock(1, now=0)
    store.mark_alerted(1)
    store.make_pick(1, 1, store.available_players(1)[0].id)
    # Le chrono du choix #2 vient de démarrer : pas d'alerte, et pas encore expiré.
    assert store.current_pick(1).number == 2
    assert store.expired_pick(1) is None
    assert store.deadline(1) is not None


def test_no_timer_means_no_deadline():
    store = make_store()
    assert store.deadline(1) is None
    store.set_timer(1, 0, admin_role_id=None)
    assert store.deadline(1) is None
    assert store.timed_guilds() == []


def test_channels_and_timer_do_not_overwrite_each_other():
    store = make_store()
    store.set_channels(1, 10, 20)
    store.set_timer(1, 60, 99)
    store.set_channels(1, 11, 21)
    assert store.get_channels(1) == (11, 21)
    assert store.timer(1) == (60, 99)


def test_guilds_are_independent():
    store = make_store()
    store.replace_players(2, [("Autre Joueur", "X", "C")])
    store.set_order(2, ["A"])
    store.make_pick(2, 1, store.available_players(2)[0].id)
    assert len(store.available_players(1)) == 3
    assert store.current_pick(1).number == 1


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE config (guild_id INTEGER PRIMARY KEY, list_channel_id INTEGER, picks_channel_id INTEGER)")
    db.execute("INSERT INTO config VALUES (1, 10, 20)")
    db.commit()
    db.close()
    store = Store(str(path))
    assert store.get_channels(1) == (10, 20)
    assert store.timer(1) == (0, None)


def test_parse_duration():
    assert fmt.parse_duration("8h") == 8 * 3600
    assert fmt.parse_duration("90m") == 90 * 60
    assert fmt.parse_duration("1h30") == 5400
    assert fmt.parse_duration("2") == 7200
    assert fmt.parse_duration("0") == 0
    assert fmt.parse_duration("bientôt") is None
    assert fmt.duration_text(5400) == "1 h 30"


def test_make_pick_refuses_stale_turn_and_taken_player():
    import pytest
    from draft_bot.storage import PickError
    store = make_store()
    roy, iginla = store.resolve_available(1, "Roy")[0], store.resolve_available(1, "Iginla")[0]
    store.make_pick(1, 1, roy.id)
    with pytest.raises(PickError):
        store.make_pick(1, 1, iginla.id)  # le choix #1 est déjà fait
    with pytest.raises(PickError):
        store.make_pick(1, 2, roy.id)  # Roy est déjà repêché
    assert store.made_picks_count(1) == 1


def test_log_and_backup(tmp_path):
    store = make_store()
    store.make_pick(1, 1, store.available_players(1)[0].id, user_id=7)
    store.add_log(1, 7, "annulation", "#1")
    assert [(e[1], e[2]) for e in store.log_entries(1)] == [(7, "choix"), (7, "annulation")]
    path = tmp_path / "copie.db"
    store.backup(str(path))
    copy = Store(str(path))
    assert copy.made_picks_count(1) == 1
    assert len(copy.log_entries(1)) == 2
