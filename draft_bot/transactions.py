"""Les transactions SHM : retrouver le rôle Discord de chaque équipe et écrire les annonces.

`shm` lit les transactions sur le site SHM ; ce module trouve les rôles, les emotes d'équipe
et prépare les messages. Il sert aussi aux autres annonces SHM (ballottage, rachats, classement)
et au draft pour reconnaître une équipe par son nom.
"""

import hashlib
import re
from dataclasses import dataclass, field

from discord.utils import escape_markdown

from .storage import normalize

# Les équipes des logos SHM : (code, nom anglais, nom français, autres noms reconnus dans un rôle).
# Le premier « autre nom » est le surnom, le deuxième la ville.
TEAMS = [
    ("ANA", "Anaheim Ducks", "Ducks d'Anaheim", ["Ducks", "Anaheim"]),
    ("ARI", "Arizona Coyotes", "Coyotes de l'Arizona", ["Coyotes", "Arizona"]),
    ("BOS", "Boston Bruins", "Bruins de Boston", ["Bruins", "Boston"]),
    ("BUF", "Buffalo Sabres", "Sabres de Buffalo", ["Sabres", "Buffalo"]),
    ("CGY", "Calgary Flames", "Flames de Calgary", ["Flames", "Calgary"]),
    ("CAR", "Carolina Hurricanes", "Hurricanes de la Caroline", ["Hurricanes", "Carolina", "Caroline", "Canes"]),
    ("CHI", "Chicago Blackhawks", "Blackhawks de Chicago", ["Blackhawks", "Chicago"]),
    ("COL", "Colorado Avalanche", "Avalanche du Colorado", ["Avalanche", "Colorado", "Avalanches", "Avs"]),
    ("CBJ", "Columbus Blue Jackets", "Blue Jackets de Columbus", ["Blue Jackets", "Columbus", "CBS"]),
    ("DAL", "Dallas Stars", "Stars de Dallas", ["Stars", "Dallas"]),
    ("DET", "Detroit Red Wings", "Red Wings de Détroit", ["Red Wings", "Detroit"]),
    ("EDM", "Edmonton Oilers", "Oilers d'Edmonton", ["Oilers", "Edmonton"]),
    ("FLA", "Florida Panthers", "Panthers de la Floride", ["Panthers", "Florida", "Floride"]),
    ("LAK", "Los Angeles Kings", "Kings de Los Angeles", ["Kings", "Los Angeles", "LA"]),
    ("MIN", "Minnesota Wild", "Wild du Minnesota", ["Wild", "Minnesota"]),
    ("MTL", "Montreal Canadiens", "Canadiens de Montréal", ["Canadiens", "Montreal", "Canadien", "Habs"]),
    ("NSH", "Nashville Predators", "Predators de Nashville", ["Predators", "Nashville", "Preds", "NAS"]),
    ("NJD", "New Jersey Devils", "Devils du New Jersey", ["Devils", "New Jersey", "NJ"]),
    ("NYI", "New York Islanders", "Islanders de New York", ["Islanders"]),
    ("NYR", "New York Rangers", "Rangers de New York", ["Rangers"]),
    ("OTT", "Ottawa Senators", "Sénateurs d'Ottawa", ["Senators", "Ottawa", "Senateurs", "Sens"]),
    ("PHI", "Philadelphia Flyers", "Flyers de Philadelphie", ["Flyers", "Philadelphia", "Philadelphie"]),
    ("PIT", "Pittsburgh Penguins", "Penguins de Pittsburgh", ["Penguins", "Pittsburgh", "Pens"]),
    ("SJS", "San Jose Sharks", "Sharks de San José", ["Sharks", "San Jose", "SJ"]),
    ("STL", "St. Louis Blues", "Blues de St. Louis", ["Blues", "St Louis", "Saint Louis"]),
    ("TBL", "Tampa Bay Lightning", "Lightning de Tampa Bay", ["Lightning", "Tampa Bay", "Tampa", "TB"]),
    ("TOR", "Toronto Maple Leafs", "Maple Leafs de Toronto", ["Maple Leafs", "Toronto", "Leafs"]),
    ("VAN", "Vancouver Canucks", "Canucks de Vancouver", ["Canucks", "Vancouver"]),
    ("WSH", "Washington Capitals", "Capitals de Washington", ["Capitals", "Washington", "Caps", "WAS"]),
    ("WPG", "Winnipeg Jets", "Jets de Winnipeg", ["Jets", "Winnipeg"]),
    # Les équipes plus récentes.
    ("SEA", "Seattle Kraken", "Kraken de Seattle", ["Kraken", "Seattle"]),
    ("UTA", "Utah Mammoth", "Mammoth de l'Utah", ["Mammoth", "Utah", "Utah Hockey Club"]),
    ("VGK", "Vegas Golden Knights", "Golden Knights de Vegas", ["Golden Knights", "Vegas", "Las Vegas"]),
]
TEAM_NAMES = {code: english for code, english, _, _ in TEAMS}

# Une année de repêchage (« 2020 »).
YEAR = re.compile(r"\b((?:19|20)\d{2})\b")


@dataclass
class Logo:
    """Une équipe telle que SHM la montre (son logo). `team` : son code (ex. « BOS »), si elle est connue."""
    team: str | None = None


@dataclass
class Item:
    """Un élément reçu : un joueur, ou un choix de repêchage (ronde, année, équipe d'origine)."""
    text: str
    round: int | None = None
    year: str | None = None
    origin: Logo | None = None


@dataclass
class Side:
    """Une équipe d'une transaction et ce qu'elle reçoit."""
    logo: Logo | None
    items: list[Item] = field(default_factory=list)
    # Rempli par le bot : le rôle Discord de l'équipe, ou à défaut son nom.
    role_id: int | None = None
    name: str | None = None


@dataclass
class Trade:
    sides: list[Side]
    # Le texte des éléments une fois le nom des équipes d'origine connu, une ligne par élément.
    texts: list[list[str]] = field(default_factory=list)


def ordinal(round_number: int) -> str:
    return "1re" if round_number == 1 else f"{round_number}e"


# Les rôles Discord des équipes.

def words(text: str) -> str:
    """« St. Louis » et « Saint-Louis » deviennent « st louis » et « saint louis »."""
    return " " + " ".join(re.sub(r"[^0-9a-z]+", " ", normalize(text)).split()) + " "


def _names(code: str, english: str, french: str, others: list[str]) -> tuple[set[str], list[str]]:
    """Les noms qui désignent l'équipe quand ils sont tout le nom du rôle, et ceux qui suffisent
    quand le rôle les contient (« Les Bruins de Boston » contient « bruins »)."""
    exact = {words(name) for name in (code, english, french, *others)}
    # Les noms courts (« LA ») ou partagés (« New York ») ne comptent que s'ils sont tout le nom.
    contained = [words(name) for name in (english, french, *others)
                 if len(normalize(name)) > 3 and normalize(name) != "new york"]
    return exact, contained


TEAM_ROLE_NAMES = {code: _names(code, english, french, others) for code, english, french, others in TEAMS}


def team_score(role_name: str, team: str) -> int:
    """2 : le rôle porte exactement un nom de l'équipe. 1 : il en contient un. 0 : aucun."""
    key = words(role_name)
    exact, contained = TEAM_ROLE_NAMES[team]
    if key in exact:
        return 2
    return 1 if any(name in key for name in contained) else 0


def role_for_team(team: str, roles: list) -> object | None:
    """Le rôle d'une équipe : son nom, son surnom, sa ville ou son code (ex. « Bruins », « BOS »).

    Un rôle qui conviendrait aussi à une autre équipe, ou une équipe qui conviendrait à
    plusieurs rôles, n'est pas choisi : l'admin choisit lui-même le rôle dans l'aperçu.
    """
    if team not in TEAM_ROLE_NAMES:
        return None
    candidates = []
    for role in roles:
        score = team_score(role.name, team)
        if score and not any(team_score(role.name, other) >= score for other in TEAM_ROLE_NAMES if other != team):
            candidates.append((score, role))
    if not candidates:
        return None
    best = max(score for score, _ in candidates)
    found = [role for score, role in candidates if score == best]
    return found[0] if len(found) == 1 else None


def resolve(trade: Trade, roles: list) -> None:
    """Trouve le rôle de chaque équipe et écrit le texte des éléments."""
    by_id = {role.id: role for role in roles}

    def find(logo: Logo | None):
        if logo is not None and logo.team:
            return role_for_team(logo.team, roles)
        return None

    for side in trade.sides:
        if side.role_id is None:
            role = find(side.logo)
            side.role_id = role.id if role else None
        if side.name is None and side.logo is not None and side.logo.team:
            side.name = TEAM_NAMES[side.logo.team]
    if not trade.texts:
        trade.texts = [[item_text(item, find, by_id) for item in side.items] for side in trade.sides]


def item_text(item: Item, find, by_id) -> str:
    if item.round is None:
        return item.text
    text = item.text + (f" {item.year}" if item.year else "")
    if item.origin is not None:
        role = find(item.origin)
        origin = role.name if role else (TEAM_NAMES.get(item.origin.team) if item.origin.team else None)
        if origin:
            text += f" ({origin})"
    return text


# Les emotes d'équipe : une emote du serveur nommée comme l'équipe (« :PIT: », « :Penguins: »).

def team_for_name(name: str) -> str | None:
    """L'équipe LNH que désigne un nom (rôle, emote, nom SHM), si une seule convient le mieux."""
    best, found = 0, []
    for team in TEAM_ROLE_NAMES:
        score = team_score(name, team)
        if score > best:
            best, found = score, [team]
        elif score and score == best:
            found.append(team)
    return found[0] if len(found) == 1 else None


def team_emojis(emojis) -> dict[str, str]:
    """Code de l'équipe -> emote du serveur. Seul le nom complet de l'emote compte : « PIT », « pit »,
    « Penguins » ou « Pittsburgh_Penguins » conviennent, « PIT2 » non."""
    found: dict[str, str] = {}
    for emoji in sorted(emojis, key=lambda e: e.name.lower()):
        if not getattr(emoji, "available", True):
            continue
        name = emoji.name.replace("_", " ")
        team = next((t for t in TEAM_ROLE_NAMES if team_score(name, t) == 2), None)
        if team and team not in found:
            found[team] = str(emoji)
    return found


def emoji_prefix(team: str | None, emojis: dict[str, str]) -> str:
    return f"{emojis[team]} " if team in emojis else ""


# Les messages.

def side_label(side: Side, roles_by_id: dict, mention: bool) -> str:
    role = roles_by_id.get(side.role_id)
    if role is not None:
        return role.mention if mention else f"**{escape_markdown(role.name)}**"
    if side.name:
        return f"**{escape_markdown(side.name)}**"
    return "**❓ équipe inconnue**"


def announcement(trade: Trade, roles_by_id: dict, emojis: dict[str, str] | None = None) -> str:
    lines = ["🔁 **Transaction**"]
    for side, texts in zip(trade.sides, trade.texts):
        team = side.logo.team if side.logo is not None else None
        lines.append(f"➡️ {emoji_prefix(team, emojis or {})}{side_label(side, roles_by_id, mention=True)} reçoit : "
                     + ", ".join(f"**{escape_markdown(t)}**" for t in texts))
    return "\n".join(lines)


def summary(trade: Trade, roles_by_id: dict, limit: int = 200) -> str:
    parts = []
    for side, texts in zip(trade.sides, trade.texts):
        parts.append(f"{side_label(side, roles_by_id, mention=False)} reçoit "
                     + (", ".join(escape_markdown(t) for t in texts) or "rien"))
    text = " · ".join(parts)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def fingerprint(trade: Trade) -> str:
    """La même transaction donne toujours la même empreinte, pour ne pas l'annoncer deux fois."""
    sides = sorted(f"{side.role_id or side.name}:" + "|".join(sorted(normalize(t) for t in texts))
                   for side, texts in zip(trade.sides, trade.texts))
    return hashlib.sha256("#".join(sides).encode()).hexdigest()[:32]
