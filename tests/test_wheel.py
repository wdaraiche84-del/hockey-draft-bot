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
