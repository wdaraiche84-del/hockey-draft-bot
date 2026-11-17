"""La loterie du repêchage : chaque équipe a ses propres chances (la pire équipe en a le plus).

Le bot tire les premiers choix un à un, sans remise : une équipe tirée ne peut plus l'être. Les équipes
non tirées gardent ensuite l'ordre écrit par l'admin (de la pire à la meilleure).
"""

import re
import secrets

from discord.utils import escape_markdown

from .storage import normalize, team_display

MIN_TEAMS = 2
MAX_TEAMS = 40
MAX_DRAWS = 10

_random = secrets.SystemRandom()

# « San Jose 18,5 », « San Jose ; 18.5 % », « San Jose: 18 »
LINE = re.compile(r"^(?P<team>.*?)[\s;:,=\t-]+(?P<chance>\d+(?:[.,]\d+)?)\s*%?$")


class LotteryError(Exception):
    """Une loterie mal écrite. Le message s'affiche tel quel à l'admin."""


def parse_entries(text: str) -> list[tuple[str, float]]:
    """Lit une équipe par ligne avec ses chances : « San Jose 18,5 ». De la pire à la meilleure."""
    entries, seen = [], set()
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        match = LINE.match(line)
        if not match or not match["team"].strip(" ;:,=-"):
            raise LotteryError(f"Je ne comprends pas la ligne « {line[:40]} ». "
                               "Écris l'équipe puis ses chances, par exemple : `San Jose 18,5`.")
        team = team_display(match["team"].strip(" ;:,=-"))
        chance = float(match["chance"].replace(",", "."))
        if chance <= 0:
            raise LotteryError(f"**{team}** a 0 chance : enlève cette ligne ou donne-lui des chances.")
        key = normalize(team)
        if key in seen:
            raise LotteryError(f"**{team}** est écrite deux fois.")
        seen.add(key)
        entries.append((team, chance))
    if len(entries) < MIN_TEAMS:
        raise LotteryError("Il faut au moins deux équipes, une par ligne, avec leurs chances.")
    if len(entries) > MAX_TEAMS:
        raise LotteryError(f"Il y a {len(entries)} équipes : {MAX_TEAMS} au plus.")
    return entries


def parse_draws(text: str, teams: int) -> int:
    text = (text or "").strip()
    most = min(MAX_DRAWS, teams - 1)
    if not text.isdigit() or not 1 <= int(text) <= most:
        raise LotteryError(f"Le nombre de choix tirés doit être un nombre entre 1 et {most}.")
    return int(text)


def draw(entries: list[tuple[str, float]], draws: int, rng=_random) -> list[int]:
    """Les index des équipes gagnantes, dans l'ordre des tirages. Chaque tirage respecte les chances
    des équipes qui restent ; une équipe déjà tirée ne peut plus l'être."""
    left = list(range(len(entries)))
    winners = []
    for _ in range(draws):
        total = sum(entries[i][1] for i in left)
        point = rng.random() * total
        chosen = left[-1]
        for i in left:
            point -= entries[i][1]
            if point < 0:
                chosen = i
                break
        winners.append(chosen)
        left.remove(chosen)
    return winners


def final_order(entries: list[tuple[str, float]], winners: list[int]) -> list[str]:
    """Les gagnants d'abord, dans l'ordre des tirages, puis les autres dans l'ordre écrit."""
    rest = [i for i in range(len(entries)) if i not in winners]
    return [entries[i][0] for i in winners + rest]


def percent(value: float) -> str:
    text = f"{value:.1f}".replace(".", ",")
    return text[:-2] if text.endswith(",0") else text


def chances_text(entries: list[tuple[str, float]], left: list[int]) -> str:
    total = sum(entries[i][1] for i in left)
    return " · ".join(f"{escape_markdown(entries[i][0])} {percent(100 * entries[i][1] / total)} %" for i in left)


def ordinal(number: int) -> str:
    return "1er" if number == 1 else f"{number}e"


def opening_message(title: str, entries: list[tuple[str, float]], draws: int) -> str:
    what = "le 1er choix" if draws == 1 else f"les {draws} premiers choix"
    return (f"🎰 **{escape_markdown(title)}**\n{len(entries)} équipes, on tire {what}. "
            "Les autres équipes gardent leur rang.\n" + chances_text(entries, list(range(len(entries)))))


def spinning_message(entries: list[tuple[str, float]], left: list[int], rank: int) -> str:
    return f"🎰 **Tirage du {ordinal(rank)} choix**\n{chances_text(entries, left)}\n\nLa roue tourne…"


def winner_message(team: str, mention: str, rank: int) -> str:
    who = f"**{escape_markdown(team)}**" + (f" {mention}" if mention else "")
    return f"🎉 {who} obtient le **{ordinal(rank)} choix** !"


def result_message(title: str, entries: list[tuple[str, float]], order: list[str]) -> str:
    """L'ordre final, avec les rangs gagnés ou perdus par rapport à l'ordre de départ."""
    before = {normalize(team): i for i, (team, _) in enumerate(entries)}
    lines = [f"🏆 **Résultat : {escape_markdown(title)}**"]
    for rank, team in enumerate(order):
        move = before[normalize(team)] - rank
        arrow = f" ⬆️ +{move}" if move > 0 else f" ⬇️ {move}" if move < 0 else ""
        lines.append(f"{rank + 1}. {escape_markdown(team)}{arrow}")
    return "\n".join(lines)


def log_details(entries: list[tuple[str, float]], order: list[str]) -> str:
    return ", ".join(f"{t} {percent(c)}" for t, c in entries) + " → " + ", ".join(order)
