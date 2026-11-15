import io
import random
from collections import Counter

import pytest

from draft_bot import wheel


def test_parse_options():
    assert wheel.parse_options("ott, stl") == ["ott", "stl"]
    assert wheel.parse_options("Montréal\nQuébec,  Trois  Rivières ,") == ["Montréal", "Québec", "Trois Rivières"]
    assert wheel.parse_options("équipe: A, équipe: B") == ["équipe: A", "équipe: B"]  # pas de poids


@pytest.mark.parametrize("text, message", [
    ("ott", "au moins deux"),
    ("", "au moins deux"),
    ("ott, OTT", "en double"),
    ("Québec, quebec", "en double"),
    (", ".join(f"o{i}" for i in range(51)), "50 au plus"),
    ("x" * 81 + ", b", "trop longue"),
])
def test_parse_options_errors(text, message):
    with pytest.raises(wheel.WheelError, match=message):
        wheel.parse_options(text)


def test_spin_gives_equal_chances():
    rng = random.Random(42)
    counts = Counter(wheel.spin(["a", "b", "c"], rng) for _ in range(30000))
    assert set(counts) == {"a", "b", "c"}
    assert all(9500 < n < 10500 for n in counts.values())


def test_messages():
    assert "50 % chacune" in wheel.result_message("gambrell", ["ott", "stl"], "stl")
    assert "33 % chacune" in wheel.spinning_message(None, ["a", "b", "c"])
    assert wheel.result_message(None, ["a", "b"], "b").startswith("🎡 **La roue** → **b**")
    assert wheel.log_details(None, ["a", "b"], "a") == "a, b → a"


# La roue animée

@pytest.mark.parametrize("count", [2, 3, 7, 12, 50])
def test_the_wheel_always_stops_on_the_winner(count):
    rng = random.Random(count)
    for winner in range(count):
        for _ in range(20):
            angle = wheel.final_angle(count, winner, rng)
            assert angle >= wheel.TURNS * 360  # elle fait vraiment plusieurs tours
            assert wheel.winner_under_pointer(count, angle) == winner


def test_neighbour_slices_never_share_a_color():
    for count in range(2, wheel.MAX_OPTIONS + 1):
        colors = wheel.slice_colors(count)
        assert all(colors[i] != colors[(i + 1) % count] for i in range(count)), count


def test_gif_plays_once_and_ends_on_the_winner_with_accents_and_long_names():
    from PIL import Image
    options = ["Équipe de Montréal", "Québec", "x" * wheel.MAX_LABEL]
    data = wheel.render_gif(options, 1, random.Random(1))
    gif = Image.open(io.BytesIO(data))
    assert gif.n_frames == wheel.SPIN_FRAMES + 1 and "loop" not in gif.info
    gif.seek(gif.n_frames - 1)
    assert gif.info["duration"] >= 60_000  # la roue reste arrêtée sur le gagnant
    assert gif.size == (wheel.WHEEL_SIZE, wheel.WHEEL_SIZE + wheel.BANNER)


def test_gif_stays_small_even_with_the_most_options():
    options = [f"Option numéro {i}" for i in range(wheel.MAX_OPTIONS)]
    data = wheel.render_gif(options, 0, random.Random(2))
    assert len(data) < 8 * 1024 * 1024  # bien sous la limite d'envoi de Discord
