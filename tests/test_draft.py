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
    store.record_pick(1, pick.number, roy.id)
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


def test_gm_team_names_are_case_insensitive():
    store = make_store()
    store.set_gm(1, "mtl", 42)
    assert store.gm_for(1, "MTL") == 42
    assert store.gm_for(1, "TOR") is None


def test_team_names_get_capitalized():
    store = make_store(order=["laval", "granby", "mtl", "Trois-Rivières"])
    assert [p.team for p in store.upcoming_picks(1, 4)] == ["Laval", "Granby", "MTL", "Trois-Rivières"]
    store.transfer_pick(1, 1, "sherbrooke")
    assert store.get_pick(1, 1).team == "Sherbrooke"
