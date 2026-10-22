"""Lecture des fichiers importés et mise en forme des messages du bot."""

import csv
import io

from discord.utils import escape_markdown

from .storage import Pick, Player, normalize

# Discord refuse les messages de plus de 2000 caractères.
MAX_MESSAGE = 1900

HEADER_WORDS = {"nom", "name", "joueur", "player"}


def decode(data: bytes) -> str:
    # Les exports Windows (EHM, Excel) sont souvent en Latin-1 plutôt qu'en UTF-8.
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def parse_players(text: str) -> list[tuple[str, str, str]]:
    """Lit une liste « nom, équipe actuelle, position », une ligne par joueur.

    Accepte la virgule, le point-virgule ou la tabulation comme séparateur,
    et ignore une ligne d'en-tête si elle est présente.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    sample = "\n".join(lines[:20])
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        delimiter = ","
    rows = []
    for i, row in enumerate(csv.reader(lines, delimiter=delimiter)):
        cells = [cell.strip() for cell in row]
        if i == 0 and cells and normalize(cells[0]) in HEADER_WORDS:
            continue
        if not cells or not cells[0]:
            continue
        cells += [""] * (3 - len(cells))
        rows.append((cells[0], cells[1], cells[2]))
    return rows


def parse_order(text: str) -> list[str]:
    """Lit une liste d'équipes séparées par des virgules ou des retours de ligne."""
    parts = text.replace("\n", ",").split(",")
    return [part.strip() for part in parts if part.strip()]


def build_order(first_round: list[str], rounds: int, snake: bool) -> list[str]:
    order = []
    for r in range(rounds):
        teams = first_round[::-1] if snake and r % 2 == 1 else first_round
        order.extend(teams)
    return order


def player_line(player: Player) -> str:
    details = " · ".join(escape_markdown(x) for x in (player.team, player.position) if x)
    name = f"**{escape_markdown(player.name)}**"
    return f"{name} · {details}" if details else name


def render_list(players: list[Player]) -> list[str]:
    """Découpe la liste des joueurs disponibles en messages Discord."""
    header = f"📋 **Joueurs disponibles : {len(players)}**"
    if not players:
        return [header + "\nTous les joueurs ont été repêchés."]
    chunks = []
    current = header
    for player in players:
        line = player_line(player)
        if len(current) + 1 + len(line) > MAX_MESSAGE:
            chunks.append(current)
            current = line
        else:
            current += "\n" + line
    chunks.append(current)
    return chunks


def pick_label(pick: Pick) -> str:
    return f"Choix #{pick.number} (ronde {pick.round}, choix {pick.in_round})"


def mention(user_id: int | None) -> str:
    return f"<@{user_id}>" if user_id else "(aucun DG associé)"


def announcement(pick: Pick, player: Player, forced: bool) -> str:
    text = (
        f"🏒 **{pick_label(pick)}** : **{escape_markdown(pick.team)}** sélectionne "
        f"{player_line(player)}"
    )
    if forced:
        text += " *(choix fait par un admin)*"
    return text


def on_the_clock(pick: Pick | None, gm_id: int | None) -> str:
    if pick is None:
        return "🏁 Le draft est terminé!"
    return f"⏱️ Au tour de **{escape_markdown(pick.team)}** {mention(gm_id)} ({pick_label(pick)})"
