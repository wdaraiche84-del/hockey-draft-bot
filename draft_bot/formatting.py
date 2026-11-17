"""Lecture des fichiers importés et mise en forme des messages du bot."""

import csv
import re

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


def duplicate_names(rows: list[tuple[str, str, str]]) -> list[str]:
    seen, duplicates = set(), []
    for name, _, _ in rows:
        key = normalize(name)
        if key in seen and name not in duplicates:
            duplicates.append(name)
        seen.add(key)
    return duplicates


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


def deadline_text(deadline: float | None) -> str:
    # Discord affiche ces balises dans le fuseau horaire de chaque membre.
    if deadline is None:
        return ""
    return f"\n⌛ Temps limite : <t:{int(deadline)}:f> (<t:{int(deadline)}:R>)"


def ordinal(number: int) -> str:
    return "1er" if number == 1 else f"{number}e"


def proud_team(team: str) -> tuple[str, str]:
    """(« Les Bruins de Boston », « sont fiers »), accordé selon l'article du nom de l'équipe."""
    lowered = team.lower()
    capitalized = team[:1].upper() + team[1:]
    if lowered.startswith("les "):
        return capitalized, "sont fiers"
    if lowered.startswith("la "):
        return capitalized, "est fière"
    if lowered.startswith(("le ", "l'", "l’")):
        return capitalized, "est fier"
    if " " not in team:
        # Un seul mot, comme une ville : « Laval est fier ».
        return team, "est fier"
    return f"Les {team}", "sont fiers"


def announcement(pick: Pick, player: Player, forced: bool, team: str | None = None, emoji: str = "") -> str:
    """« Les Bruins de Boston sont fiers de sélectionner … au 5e rang. » L'emote de l'équipe, s'il y en
    a une, se place devant son nom."""
    subject, verb = proud_team(team or pick.team)
    details = " · ".join(escape_markdown(x) for x in (player.team, player.position) if x)
    text = (
        f"🏒 {emoji}**{escape_markdown(subject)}** {verb} de sélectionner **{escape_markdown(player.name)}**"
        + (f" ({details})" if details else "")
        + f" au **{ordinal(pick.number)} rang** (ronde {pick.round}, choix {pick.in_round})."
    )
    if forced:
        text += " *(choix fait par un admin)*"
    return text


def on_the_clock(pick: Pick | None, ping: str, deadline: float | None = None) -> str:
    """« Au tour de … », avec la mention du rôle de l'équipe et le temps limite."""
    if pick is None:
        return "🏁 Le draft est terminé !"
    return (
        f"⏱️ Au tour de **{escape_markdown(pick.team)}** {ping} ({pick_label(pick)})"
        + deadline_text(deadline)
    )


def time_up(pick: Pick, admin_ping: str) -> str:
    text = f"⏰ Temps écoulé pour **{escape_markdown(pick.team)}** ({pick_label(pick)})."
    text += " Un admin peut faire le choix : `/admin`, puis **Choisir pour l'équipe au tour**."
    return f"{admin_ping} {text}" if admin_ping else text


def parse_duration(text: str) -> int | None:
    """Lit un délai comme « 8h », « 90m », « 1h30 » ou « 2 » (heures). « 0 » désactive."""
    text = text.strip().lower().replace(" ", "")
    if not text:
        return None
    if text.replace(".", "", 1).replace(",", "", 1).isdigit():
        return int(float(text.replace(",", ".")) * 3600)
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)(?:m|min)?)?", text)
    if not match or not any(match.groups()):
        return None
    hours, minutes = (int(x) if x else 0 for x in match.groups())
    return hours * 3600 + minutes * 60


def duration_text(seconds: int) -> str:
    hours, minutes = divmod(seconds // 60, 60)
    if hours and minutes:
        return f"{hours} h {minutes:02d}"
    return f"{hours} h" if hours else f"{minutes} min"


# Les salons du repêchage : le tableau, la centrale, les attributions et le guide.

def split_messages(header: str, lines: list[str]) -> list[str]:
    """Découpe des lignes en messages Discord ; le titre est en tête du premier."""
    chunks, current = [], header
    for line in lines:
        if len(current) + 1 + len(line) > MAX_MESSAGE:
            chunks.append(current)
            current = line
        else:
            current += "\n" + line
    chunks.append(current)
    return chunks


def team_label(pick: Pick) -> str:
    """« **Détroit** (via Ottawa) » : l'équipe d'origine est rappelée après un échange."""
    text = f"**{escape_markdown(pick.team)}**"
    return text + f" (via {escape_markdown(pick.via)})" if pick.via else text


def short_player(player: Player | None) -> str:
    if player is None:
        return "?"
    return f"{escape_markdown(player.name)}" + (f" · {escape_markdown(player.position)}" if player.position else "")


BOARD_UPCOMING = 5
BOARD_RECENT = 3


def board(picks: list[Pick], players: dict[int, Player], started: bool, clock: str) -> str:
    """Le tableau du draft, dans le salon de la sélection des joueurs : qui est au tour (avec la mention
    et le temps limite, dans « clock »), les prochains choix et les derniers choix faits."""
    waiting = [p for p in picks if p.player_id is None]
    made = [p for p in picks if p.player_id is not None]
    if not started:
        lines = ["🏒 **Repêchage : bientôt !**", "Le draft n'a pas encore commencé. Les équipes seront averties ici."]
        if waiting:
            lines += ["", "**Premiers choix**"] + [f"#{p.number} {team_label(p)}" for p in waiting[:BOARD_UPCOMING]]
        return "\n".join(lines)
    if not waiting:
        lines = [f"🏁 **Le draft est terminé !** ({len(made)} choix)"]
    else:
        lines = [f"🏒 **Repêchage · Ronde {waiting[0].round}** ({len(made)} choix faits sur {len(picks)})", clock]
        if len(waiting) > 1:
            lines += ["", "**À venir**"]
            lines += [f"#{p.number} {team_label(p)} (ronde {p.round}, choix {p.in_round})"
                      for p in waiting[1:1 + BOARD_UPCOMING]]
    if made:
        lines += ["", "**Derniers choix**"]
        lines += [f"#{p.number} {team_label(p)} : {short_player(players.get(p.player_id))}"
                  for p in reversed(made[-BOARD_RECENT:])]
    text = "\n".join(lines)
    return text if len(text) <= MAX_MESSAGE else text[:MAX_MESSAGE - 1] + "…"


def recap(picks: list[Pick], players: dict[int, Player]) -> list[str]:
    """La centrale : les choix de chaque équipe, mis à jour après chaque choix."""
    made = [p for p in picks if p.player_id is not None]
    if not picks:
        return ["📋 **Choix de chaque équipe**\nAucun ordre de draft pour l'instant."]
    finished = len(made) == len(picks)
    header = ("🏁 **Repêchage terminé : les choix de chaque équipe**" if finished
              else f"📋 **Choix de chaque équipe** ({len(made)} sur {len(picks)})")
    if not made:
        return [header + "\nAucun choix pour l'instant. Cette liste se remplit pendant le draft."]
    teams: dict[str, list[Pick]] = {}
    for pick in made:
        teams.setdefault(pick.team, []).append(pick)
    lines = []
    for team in sorted(teams, key=normalize):
        lines.append(f"\n**{escape_markdown(team)}**")
        for pick in teams[team]:
            lines.append(f"　Ronde {pick.round}, #{pick.number} : {short_player(players.get(pick.player_id))}")
    return split_messages(header, lines)


def owners(picks: list[Pick], players: dict[int, Player]) -> list[str]:
    """Les attributions : qui possède quel choix, ronde par ronde, avec l'équipe d'origine après un échange."""
    header = "📑 **Choix du repêchage : qui choisit quand**"
    if not picks:
        return [header + "\nL'ordre n'est pas encore défini."]
    lines = []
    current_round = None
    for pick in picks:
        if pick.round != current_round:
            current_round = pick.round
            lines.append(f"\n__**Ronde {pick.round}**__")
        line = f"#{pick.number} {team_label(pick)}"
        if pick.via:
            line += " 🔁"
        if pick.player_id is not None:
            line += f" : {short_player(players.get(pick.player_id))}"
        lines.append(line)
    return split_messages(header, lines)


def guide(selection: str, players_list: str, announces: str, trades: str | None, seconds: int) -> str:
    """Le guide épinglé du salon « fonctionnement » : quoi faire, en 4 étapes simples."""
    lines = [
        "📖 **Comment fonctionne le repêchage**",
        f"1️⃣ Quand c'est ton tour, ton équipe est mentionnée dans {selection}.",
        "2️⃣ Clique sur le bouton vert **🏒 Faire mon choix** et choisis ton joueur "
        "(ou tape quelques lettres de son nom).",
        "3️⃣ Clique sur **Confirmer**. C'est tout ! Ton choix est annoncé dans " + announces + ".",
        f"📋 Les joueurs encore disponibles sont dans {players_list}.",
    ]
    if seconds:
        lines.append(f"⌛ Tu as **{duration_text(seconds)}** pour chaque choix. Après, un admin peut choisir pour toi.")
    if trades:
        lines.append(f"🔁 Les échanges de choix sont annoncés dans {trades}.")
    lines.append("❓ `/statut` montre qui est au tour. `/aide` explique tout.")
    return "\n".join(lines)
