"""La roue : un tirage au sort équitable entre plusieurs options, toutes avec les mêmes chances."""

import secrets

from discord.utils import escape_markdown

from .storage import normalize

MAX_OPTIONS = 50
MAX_LABEL = 80

# secrets s'appuie sur le hasard du système : impossible à prévoir ou à influencer.
_random = secrets.SystemRandom()


class WheelError(Exception):
    """Des options invalides. Le message s'affiche tel quel au membre."""


def parse_options(text: str) -> list[str]:
    """Lit « ott, stl » (virgules ou retours de ligne)."""
    options = []
    seen = set()
    for label in text.replace("\n", ",").split(","):
        label = " ".join(label.split())
        if not label:
            continue
        if len(label) > MAX_LABEL:
            raise WheelError(f"L'option « {label[:20]}… » est trop longue ({MAX_LABEL} caractères au plus).")
        key = normalize(label)
        if key in seen:
            raise WheelError(f"« {label} » est en double : chaque option doit être différente.")
        seen.add(key)
        options.append(label)
    if len(options) < 2:
        raise WheelError("Il faut au moins deux options, séparées par des virgules (ex. : `ott, stl`).")
    if len(options) > MAX_OPTIONS:
        raise WheelError(f"Il y a {len(options)} options : {MAX_OPTIONS} au plus.")
    return options


def spin(options: list[str], rng=_random) -> str:
    """Chaque option a exactement la même chance."""
    return rng.choice(options)


def options_text(options: list[str]) -> str:
    share = 100 / len(options)
    return " · ".join(escape_markdown(o) for o in options) + f"\n*Chances égales : {share:.0f} % chacune*"


def header(title: str | None) -> str:
    return f"🎡 **{escape_markdown(title)}**" if title else "🎡 **La roue**"


def spinning_message(title: str | None, options: list[str]) -> str:
    return f"{header(title)}\n{options_text(options)}\n\nLa roue tourne…"


def result_message(title: str | None, options: list[str], winner: str) -> str:
    return f"{header(title)} → **{escape_markdown(winner)}**\n{options_text(options)}"


def log_details(title: str | None, options: list[str], winner: str) -> str:
    return f"{title + ' : ' if title else ''}{', '.join(options)} → {winner}"
