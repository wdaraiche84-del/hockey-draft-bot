"""Les annonces SHM autres que les transactions : ballottage, rachats et classement.

Ce module écrit les messages. Il ne parle ni à SHM ni à Discord : il reçoit ce que le module
`shm` a lu et les rôles du serveur, ce qui le rend facile à tester.
"""

from discord.utils import escape_markdown

from . import capture, shm


def team_label(code: str | None, teams: dict[str, str], roles: list, emojis: dict[str, str]) -> str:
    """L'équipe d'un code SHM : son emote et la mention de son rôle, sinon son nom en gras."""
    if not code:
        return "**❓ équipe inconnue**"
    name = teams.get(code)
    team = shm.known_team(code, name)
    role = capture.role_for_team(team, roles) if team else None
    if role is not None:
        label = role.mention
    else:
        label = f"**{escape_markdown(name or (capture.TEAM_NAMES.get(team) if team else None) or code)}**"
    return capture.emoji_prefix(team, emojis) + label


def details(waiver: shm.ShmWaiver) -> str:
    """« C · 25 ans · 825 000 $ · 2 ans de contrat » : seulement ce que SHM donne."""
    parts = [waiver.position]
    if waiver.age:
        parts.append(f"{waiver.age} ans")
    if waiver.salary:
        parts.append(f"{waiver.salary} $")
    if waiver.years:
        parts.append(f"{waiver.years} an{'s' if waiver.years != '1' else ''} de contrat")
    return " · ".join(escape_markdown(p) for p in parts if p)


def where_unclaimed(waiver: shm.ShmWaiver) -> str:
    dest = waiver.dest.upper()
    if dest == "UFA":
        return "il devient joueur autonome"
    if dest in ("AHL", "LAH"):
        return "il est envoyé dans la LAH"
    return ""


def placed(waiver: shm.ShmWaiver, teams: dict[str, str], roles: list, emojis: dict[str, str]) -> str:
    lines = [f"⚠️ **Ballottage** : {team_label(waiver.team, teams, roles, emojis)} place "
             f"**{escape_markdown(waiver.name)}** au ballottage."]
    if details(waiver):
        lines.append(details(waiver))
    end = []
    if waiver.ends:
        end.append(f"⏳ Se termine {escape_markdown(waiver.ends)}.")
    if where_unclaimed(waiver):
        end.append(f"S'il n'est pas réclamé, {where_unclaimed(waiver)}.")
    if end:
        lines.append(" ".join(end))
    return "\n".join(lines)


def claimed(waiver: shm.ShmWaiver, claimer: str, teams: dict[str, str], roles: list, emojis: dict[str, str]) -> str:
    return (f"✅ **Réclamé au ballottage** : {team_label(claimer, teams, roles, emojis)} obtient "
            f"**{escape_markdown(waiver.name)}** (venant de {team_label(waiver.team, teams, roles, emojis)}).")


def unclaimed(waiver: shm.ShmWaiver, teams: dict[str, str], roles: list, emojis: dict[str, str]) -> str:
    where = where_unclaimed(waiver)
    return (f"↩️ **Pas réclamé** : personne n'a réclamé **{escape_markdown(waiver.name)}** "
            f"({team_label(waiver.team, teams, roles, emojis)})" + (f", {where}." if where else "."))


def bought_out(buyout: shm.Buyout, teams: dict[str, str], roles: list, emojis: dict[str, str]) -> str:
    if shm.known_team(buyout.team, teams.get(buyout.team)) is None:
        # Un club-école dont l'équipe LNH est inconnue : on le nomme sans deviner.
        who = f"Le club-école **{escape_markdown(teams.get(buyout.team) or buyout.team)}**"
    else:
        who = team_label(buyout.team, teams, roles, emojis)
    return (f"💸 **Rachat** : {who} rachète le contrat de **{escape_markdown(buyout.player)}** "
            f"(salaire du rachat : {escape_markdown(buyout.salary)} $).")


# Le classement : quelques messages que le bot modifie après chaque simulation.

STANDINGS_TITLE = "📊 **Classement**\n-# PJ : parties jouées · V-D-DP : victoires, défaites, défaites en prolongation"
STANDINGS_FOOTER = "-# Mis à jour tout seul par le bot après chaque simulation sur SHM."
MESSAGE_LIMIT = 1900


def standing_line(rank: int, row: shm.Standing, teams: dict[str, str], emojis: dict[str, str]) -> str:
    name = teams.get(row.team)
    team = shm.known_team(row.team, name)
    name = name or (capture.TEAM_NAMES.get(team) if team else None) or row.team
    points = row.points or "0"
    return (f"`{rank:>2}.` {capture.emoji_prefix(team, emojis)}{escape_markdown(name)} — "
            f"**{points} pt{'s' if points not in ('0', '1') else ''}** · {row.games or 0} PJ · "
            f"{row.wins or 0}-{row.losses or 0}-{row.otl or 0}")


def standings(page: shm.StandingsPage, emojis: dict[str, str]) -> list[str]:
    """Le classement en messages de moins de 2000 caractères, une division ne coupant jamais en deux."""
    sections = [STANDINGS_TITLE]
    for title, rows in page.groups:
        lines = [f"**{escape_markdown(title)}**"]
        lines += [standing_line(rank, row, page.teams, emojis) for rank, row in enumerate(rows, 1)]
        sections.append("\n".join(lines))
    sections.append(STANDINGS_FOOTER)
    messages = [""]
    for section in sections:
        if messages[-1] and len(messages[-1]) + 2 + len(section) > MESSAGE_LIMIT:
            messages.append("")
        messages[-1] = f"{messages[-1]}\n\n{section}" if messages[-1] else section
    return messages
