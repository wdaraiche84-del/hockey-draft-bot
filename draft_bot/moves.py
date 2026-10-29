"""Les annonces du ballottage SHM : un joueur soumis, puis réclamé ou pas.

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
