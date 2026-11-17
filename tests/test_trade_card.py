"""L'image d'une transaction : un bloc par équipe, avec les cotes SHM des joueurs."""

import io

from PIL import Image

from draft_bot import trade_card, transactions
from draft_bot.transactions import Item, Logo, PlayerLine, Side, Trade

LABELS = ["POT", "FI", "SH", "PL", "ST", "CH", "PO", "HI", "SK", "EN", "PE", "FA", "LE", "SR", "OFF", "DEF", "OA"]


def line(name, values="88 32 89 90 83 83 83 81 87 90 80 70 60 80 87 82 82", labels=LABELS):
    return PlayerLine(name, "AG/AD", "24", "4 250 000", "1", tuple(zip(labels, values.split())))


def trade(*sides):
    result = Trade([Side(Logo(code), items, name=code) for code, items in sides])
    transactions.resolve(result, [])
    return result


def open_png(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return Image.open(io.BytesIO(data))


def test_card_is_a_png_with_one_block_per_team():
    one = trade(("MTL", [Item("Kevin Fiala")]), ("EDM", [Item("Choix de 1re ronde", 1, "2027", Logo("EDM"))]))
    image = open_png(trade_card.render_png(one, {"kevin fiala": line("Kevin Fiala")}))
    assert image.width == trade_card.WIDTH
    # Les couleurs des deux équipes sont dans l'image (leurs bandeaux).
    colors = {color for _, color in image.convert("RGB").getcolors(1_000_000)}
    assert trade_card.hex_rgb(trade_card.TEAM_COLORS["MTL"][0]) in colors
    assert trade_card.hex_rgb(trade_card.TEAM_COLORS["EDM"][0]) in colors


def test_ratings_make_the_card_taller():
    one = trade(("MTL", [Item("Kevin Fiala")]), ("EDM", [Item("Evan Bouchard")]))
    without = open_png(trade_card.render_png(one, {}))
    with_lines = open_png(trade_card.render_png(one, {"kevin fiala": line("Kevin Fiala")}))
    assert with_lines.height == without.height + trade_card.RATINGS_H


def test_unusual_trades_still_give_an_image():
    goalie = line("Ilya Samsonov", "78 40 79 79 79 78 79 78 79 78 78 79 60 78 79 78 77",
                  ["POT", "FI", "GL", "BL", "ST", "PA", "AN", "AG", "SK", "EN", "PE", "FA", "LE", "SR", "OFF", "DEF", "OA"])
    cases = [
        # Équipe inconnue, nom avec emoji, côté vide, trois équipes.
        Trade([Side(Logo(None), [Item("Joueur 🏒 Étoile")], name="🐻 Ours"), Side(Logo("EDM"), [])]),
        trade(("MTL", [Item("Ilya Samsonov")]), ("EDM", [Item("A")]), ("TOR", [Item("B")])),
        # Un très long nom et beaucoup de joueurs : « + N autres ».
        trade(("MTL", [Item("X" * 120)] + [Item(f"Joueur {i}") for i in range(30)]), ("EDM", [Item("C")])),
        # Une cote qui n'est pas un nombre.
        trade(("MTL", [Item("Bizarre")]), ("EDM", [Item("D")])),
    ]
    players = {"ilya samsonov": goalie, "bizarre": line("Bizarre", "?? 50", ["POT", "OA"])}
    for case in cases:
        if not case.texts:
            transactions.resolve(case, [])
        open_png(trade_card.render_png(case, players))
    many = open_png(trade_card.render_png(cases[2], players))
    assert many.height < 1500


def test_pick_origin_is_shown_only_when_it_came_from_elsewhere():
    one = trade(("MTL", [Item("Choix de 2e ronde", 2, "2027", Logo("CBJ")),
                         Item("Choix de 1re ronde", 1, "2031", Logo("EDM"))]), ("EDM", [Item("A")]))
    entries = trade_card._entries(one, {})
    assert entries[0] == [("choix", "Choix de 2e ronde 2027", "Blue Jackets de Columbus"),
                          ("choix", "Choix de 1re ronde 2031", None)]


def test_helpers():
    assert trade_card.text_on("#FFB81C") == "#111111" and trade_card.text_on("#00205B") == "#FFFFFF"
    assert trade_card.clean("🐻 Bruins · Boston") == "Bruins · Boston"
    assert trade_card.contract_line(line("K")) == "AG/AD · 24 ans · 4 250 000 $ × 1 an"
    assert trade_card.contract_line(PlayerLine("K", "C", "", "", "", ())) == "C"
    assert trade_card.rating_color("90") != trade_card.rating_color("40") and trade_card.rating_color("x")
    assert set(trade_card.TEAM_COLORS) == set(transactions.TEAM_NAMES)
