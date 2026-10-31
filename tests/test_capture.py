"""Lecture des captures de transactions SHM : le tableau, les logos et les rôles des équipes."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from draft_bot import capture
from draft_bot.storage import Store

CAPTURES = Path(__file__).parent / "captures"
ROOT = Path(__file__).parent.parent

# Ce que montre la vraie capture SHM envoyée par William : (équipe, ce qu'elle reçoit) des deux côtés.
EXPECTED = [
    (("CGY", ["Daniil Tarasov"]),
     ("EDM", ["Choix de 2e ronde 2020 (St. Louis Blues)"])),
    (("SJS", ["Andrew Peeke"]),
     ("DAL", ["Choix de 2e ronde 2019 (San Jose Sharks)"])),
    (("OTT", ["Jordan Greenway", "Jake Evans"]),
     ("TOR", ["Choix de 2e ronde 2019 (Detroit Red Wings)", "Choix de 2e ronde 2021 (Ottawa Senators)",
              "Choix de 1re ronde 2021 (Ottawa Senators)"])),
    (("FLA", ["Mason Marchment", "Dante Fabbro", "Choix de 1re ronde 2021 (Tampa Bay Lightning)"]),
     ("TBL", ["Jakob Chychrun"])),
    (("PHI", ["Ilya Kovalchuk"]),
     ("LAK", ["Choix de 2e ronde 2020 (Philadelphia Flyers)"])),
    (("CAR", ["Tyler Bertuzzi"]),
     ("OTT", ["Brett Pesce", "Andreas Johnsson"])),
    (("FLA", ["Curtis Douglas"]),
     ("BOS", ["Nils Lundkvist"])),
    (("FLA", ["Yakov Trenin", "Jeremy Lauzon", "Choix de 1re ronde 2018 (St. Louis Blues)",
              "Choix de 1re ronde 2019 (Los Angeles Kings)"]),
     ("PIT", ["Charlie McAvoy"])),
]

reader_installed = all(importlib.util.find_spec(m) for m in ("rapidocr", "onnxruntime", "cv2", "numpy"))
needs_reader = pytest.mark.skipif(not reader_installed, reason="RapidOCR n'est pas installé")


class Role:
    def __init__(self, role_id, name):
        self.id, self.name, self.mention = role_id, name, f"<@&{role_id}>"


def roles(*names):
    return [Role(i, name) for i, name in enumerate(names, start=1)]


def read(found):
    trades = capture.parse_table(found["lines"], found["logos"])
    for trade in trades:
        capture.resolve(trade, [], [])
    return [tuple((side.logo.team if side.logo else None, texts) for side, texts in zip(t.sides, t.texts))
            for t in trades]


# Le tableau, à partir de ce que le lecteur a trouvé dans la vraie capture

def recorded():
    return json.loads((CAPTURES / "transactions-shm.json").read_text())


def test_every_trade_of_the_real_capture_is_read_exactly():
    assert read(recorded()) == EXPECTED


def test_reading_does_not_depend_on_the_order_of_the_text():
    found = recorded()
    found["lines"].reverse()
    found["logos"].reverse()
    assert read(found) == EXPECTED


def test_unsure_logo_gives_no_team():
    found = recorded()
    for logo in found["logos"]:
        logo["distance"] = 80.0
    trades = capture.parse_table(found["lines"], found["logos"])
    assert all(side.logo.team is None for t in trades for side in t.sides)
    capture.resolve(trades[0], roles("Flames"), [])
    assert capture.problems(trades[0], {}) == ["équipe inconnue", "équipe inconnue"]


def test_close_second_logo_gives_no_team():
    found = recorded()
    found["logos"][0]["second"] = found["logos"][0]["distance"] + 1
    trades = capture.parse_table(found["lines"], found["logos"])
    assert trades[0].sides[0].logo.team is None


def test_picture_without_logos_or_with_one_column_is_explained():
    found = recorded()
    with pytest.raises(capture.CaptureError, match="aucun logo"):
        capture.parse_table(found["lines"], [])
    left_only = [l for l in found["logos"] if l["box"][0] < 100]
    with pytest.raises(capture.CaptureError, match="une seule colonne"):
        capture.parse_table(found["lines"], left_only)
    with pytest.raises(capture.CaptureError):
        capture.parse_table([], found["logos"])


def test_header_and_dates_are_not_items():
    texts = [t for trade in read(recorded()) for _, items in trade for t in items]
    assert not any("il y a" in t or t.lower() in ("vers", "élements", "éléments") for t in texts)


def test_pick_written_in_english_or_with_noise():
    line = capture.Line("Choix de 1re ronde [ 2019", (0, 0, 100, 10))
    assert capture.parse_item(line, []).text == "Choix de 1re ronde"
    assert capture.parse_item(line, []).year == "2019"
    english = capture.parse_item(capture.Line("2nd round pick 2021", (0, 0, 100, 10)), [])
    assert (english.text, english.round, english.year) == ("Choix de 2e ronde", 2, "2021")
    assert capture.parse_item(capture.Line("Patrick Roy", (0, 0, 100, 10)), []).round is None


# Les rôles Discord des équipes

@pytest.mark.parametrize("name", ["Bruins", "Boston", "BOS", "Boston Bruins", "Bruins de Boston",
                                  "Les Bruins de Boston", "bruins", "🐻 Bruins"])
def test_team_role_found_by_any_usual_name(name):
    assert capture.role_for_team("BOS", roles("Admin", name, "Canadiens")).name == name


def test_french_and_tricky_names():
    found = roles("Canadiens de Montréal", "Sénateurs d'Ottawa", "Blues de Saint-Louis", "Leafs", "Kings",
                  "Rangers", "Islanders", "Lightning", "Avalanche")
    assert capture.role_for_team("MTL", found).name == "Canadiens de Montréal"
    assert capture.role_for_team("OTT", found).name == "Sénateurs d'Ottawa"
    assert capture.role_for_team("STL", found).name == "Blues de Saint-Louis"
    assert capture.role_for_team("TOR", found).name == "Leafs"
    assert capture.role_for_team("LAK", found).name == "Kings"
    assert capture.role_for_team("NYR", found).name == "Rangers"
    assert capture.role_for_team("NYI", found).name == "Islanders"
    assert capture.role_for_team("TBL", found).name == "Lightning"
    assert capture.role_for_team("COL", found).name == "Avalanche"


def test_unclear_roles_are_not_guessed():
    # « New York » pourrait être les Rangers ou les Islanders.
    assert capture.role_for_team("NYR", roles("New York")) is None
    # Deux rôles portent le nom de la même équipe : l'admin choisira.
    assert capture.role_for_team("BOS", roles("Bruins", "Boston")) is None
    # Un rôle qui nomme deux équipes n'est pas choisi.
    assert capture.role_for_team("ANA", roles("Anaheim - Los Angeles")) is None
    assert capture.role_for_team("BOS", roles("Laval", "Granby")) is None


def test_exact_role_wins_over_a_role_that_only_contains_the_name():
    assert capture.role_for_team("BOS", roles("DG Bruins", "Bruins")).name == "Bruins"


def test_every_team_has_a_role_name_and_the_30_shm_logos_are_known():
    logos = {path.stem for path in (ROOT / "draft_bot" / "logos").glob("*.png")}
    assert logos <= {code for code, *_ in capture.TEAMS}
    assert len(logos) == 30
    for code, english, *_ in capture.TEAMS:
        assert capture.role_for_team(code, roles(english)).name == english


def test_learned_logo_wins_over_the_role_name():
    found = recorded()
    trades = capture.parse_table(found["lines"], found["logos"])
    flames = trades[0].sides[0].logo
    server_roles = roles("Flames", "Mon équipe")
    capture.resolve(trades[0], server_roles, [(2, flames.descriptor)])
    assert trades[0].sides[0].role_id == 2


def test_announcement_mentions_both_teams():
    found = recorded()
    trade = capture.parse_table(found["lines"], found["logos"])[0]
    server_roles = roles("Flames", "Oilers", "Blues")
    capture.resolve(trade, server_roles, [])
    text = capture.announcement(trade, {r.id: r for r in server_roles})
    assert text == ("🔁 **Transaction**\n"
                    "➡️ <@&1> reçoit : **Daniil Tarasov**\n"
                    "➡️ <@&2> reçoit : **Choix de 2e ronde 2020 (Blues)**")


def test_same_trade_same_fingerprint():
    a, b = (capture.parse_table(f["lines"], f["logos"])[0] for f in (recorded(), recorded()))
    for trade in (a, b):
        capture.resolve(trade, roles("Flames", "Oilers"), [])
    assert capture.fingerprint(a) == capture.fingerprint(b)
    b.texts[0] = ["Autre joueur"]
    assert capture.fingerprint(a) != capture.fingerprint(b)


# Ce que le bot retient par serveur

def test_store_remembers_channel_logos_and_announced_trades():
    store = Store(":memory:")
    assert store.trades_channel(1) is None and store.learned_logos(1) == []
    store.set_trades_channel(1, 55)
    store.learn_logo(1, 7, b"abc")
    store.learn_logo(1, 8, b"abc")  # la dernière correction l'emporte
    store.learn_logo(2, 9, b"xyz")
    store.mark_announced(1, "empreinte")
    assert store.trades_channel(1) == 55
    assert store.learned_logos(1) == [(8, b"abc")]
    assert store.is_announced(1, "empreinte") and not store.is_announced(2, "empreinte")


def test_learned_logos_are_capped():
    from draft_bot import storage
    store = Store(":memory:")
    for i in range(storage.LEARNED_LOGOS_KEPT + 5):
        store.learn_logo(1, i, bytes([i % 256, i // 256]))
    kept = store.learned_logos(1)
    assert len(kept) == storage.LEARNED_LOGOS_KEPT and kept[0][0] == storage.LEARNED_LOGOS_KEPT + 4


def test_old_database_gets_the_new_tables(tmp_path):
    import sqlite3
    path = tmp_path / "draft.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE config (guild_id INTEGER PRIMARY KEY, list_channel_id INTEGER, picks_channel_id INTEGER)")
    old.execute("INSERT INTO config VALUES (1, 2, 3)")
    old.commit()
    old.close()
    store = Store(str(path))
    store.set_trades_channel(1, 4)
    assert store.get_channels(1) == (2, 3) and store.trades_channel(1) == 4


# La vraie lecture de l'image (gratuite, sur l'ordinateur du bot)

def run_reader(path):
    result = subprocess.run([sys.executable, "-m", "draft_bot.capture_reader", str(path)], cwd=ROOT,
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@needs_reader
def test_real_capture_is_read_from_the_image():
    assert read(run_reader(CAPTURES / "transactions-shm.png")) == EXPECTED


@needs_reader
@pytest.mark.parametrize("scale, kind", [(0.8, "png"), (1.3, "png"), (1.5, "png"), (1.0, "jpg"), (1.25, "jpg")])
def test_bigger_smaller_or_jpeg_capture_is_read_too(tmp_path, scale, kind):
    from PIL import Image
    image = Image.open(CAPTURES / "transactions-shm.png").convert("RGB")
    image = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
    image.save(tmp_path / f"capture.{kind}", quality=90)
    assert read(run_reader(tmp_path / f"capture.{kind}")) == EXPECTED


@needs_reader
def test_all_30_logos_of_the_shm_list_are_recognized():
    from PIL import Image
    from draft_bot import capture_reader as reader
    image = reader.load_image(str(CAPTURES / "logos-shm.png"))
    refs = reader.reference_logos()
    codes = [code for code, *_ in capture.TEAMS]
    boxes = sorted(reader.find_logos(image), key=lambda box: box[1])
    assert len(boxes) == 30
    for scale in (1.0, 1.6, 0.9):
        scaled = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
        for code, box in zip(codes, boxes):
            desc = reader.descriptor(scaled, tuple(round(v * scale) for v in box))
            best = min(refs, key=lambda key: reader.distance(desc, refs[key]))
            assert best == code, (scale, code, best)


@needs_reader
def test_reader_refuses_a_file_that_is_not_an_image(tmp_path):
    path = tmp_path / "capture.img"
    path.write_text("pas une image")
    result = subprocess.run([sys.executable, "-m", "draft_bot.capture_reader", str(path)], cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode != 0


@needs_reader
def test_reader_check_says_everything_is_installed():
    result = subprocess.run([sys.executable, "-m", "draft_bot.capture_reader", "--check"], cwd=ROOT,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0 and json.loads(result.stdout) == {"ok": True}


@needs_reader
@pytest.mark.parametrize("box, rows", [
    ((0, 127, 1778, 306), slice(2, 4)),   # deux rangées, sans les titres des colonnes
    ((0, 0, 1300, 581), slice(0, 8)),     # sans la colonne « Dernière mise à jour »
    ((0, 422, 1778, 470), slice(6, 7)),   # une seule transaction
])
def test_partial_capture_is_read(tmp_path, box, rows):
    from PIL import Image
    Image.open(CAPTURES / "transactions-shm.png").crop(box).save(tmp_path / "capture.png")
    assert read(run_reader(tmp_path / "capture.png")) == EXPECTED[rows]


@needs_reader
def test_bot_reads_a_capture_in_a_separate_process():
    import asyncio
    from draft_bot.bot import DraftBot, read_capture
    bot = DraftBot(Store(":memory:"), backup_dir=None)
    found = asyncio.run(read_capture(bot, (CAPTURES / "transactions-shm.png").read_bytes()))
    assert read(found) == EXPECTED
    with pytest.raises(capture.CaptureError, match="PNG ou JPG"):
        asyncio.run(read_capture(bot, b"pas une image"))
