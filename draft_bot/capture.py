"""Des transactions SHM tirées d'une capture d'écran.

`capture_reader` trouve le texte et les logos d'une capture. Ce module en tire les transactions
(quelle équipe reçoit quoi), retrouve le rôle Discord de chaque équipe et prépare les annonces.
Il ne dépend d'aucune bibliothèque de lecture d'image : il se teste avec de simples positions.

Le tableau des transactions SHM a quatre colonnes : « Vers » (logo de l'équipe), « Éléments »
(ce qu'elle reçoit), puis la même chose pour l'autre équipe. Un choix de repêchage s'écrit
« Choix de 2e ronde [logo] 2020 », où le logo est l'équipe d'origine du choix.
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
    # Les équipes plus récentes : reconnues par leur nom sur la page SHM (pas encore de logo de capture).
    ("SEA", "Seattle Kraken", "Kraken de Seattle", ["Kraken", "Seattle"]),
    ("UTA", "Utah Mammoth", "Mammoth de l'Utah", ["Mammoth", "Utah", "Utah Hockey Club"]),
    ("VGK", "Vegas Golden Knights", "Golden Knights de Vegas", ["Golden Knights", "Vegas", "Las Vegas"]),
]
TEAM_NAMES = {code: english for code, english, _, _ in TEAMS}

# Un logo est reconnu quand il ressemble assez à un logo connu, et nettement plus qu'au deuxième.
# Mesuré sur les vraies captures SHM : le bon logo est à moins de 21, deux logos différents à plus de 25.
LOGO_MAX_DISTANCE = 24.0
LOGO_MIN_MARGIN = 4.0
# Un logo appris sur ce serveur (après une correction d'un admin) passe avant les logos connus.
LEARNED_MAX_DISTANCE = 22.0

# Les en-têtes et la colonne « Dernière mise à jour » ne sont pas des éléments échangés.
HEADER_WORDS = {"vers", "elements", "derniere mise a jour", "to", "from", "items", "assets", "last updated", "date"}
DATE_PATTERN = re.compile(r"^(il y a\b|\d+\s+\w+\s+ago$|aujourd|hier$|today$|yesterday$)")
PICK_FR = re.compile(r"choix\s*de\s*(\d{1,2})\s*(?:re|er|e|eme)?\s*ronde", re.IGNORECASE)
PICK_EN = re.compile(r"(\d{1,2})\s*(?:st|nd|rd|th)\s*round", re.IGNORECASE)
YEAR = re.compile(r"\b((?:19|20)\d{2})\b")


class CaptureError(Exception):
    """La capture ne ressemble pas au tableau des transactions. Le message s'affiche tel quel."""


@dataclass
class Logo:
    box: tuple[float, float, float, float]
    descriptor: bytes = b""
    team: str | None = None

    @property
    def cx(self) -> float:
        return (self.box[0] + self.box[2]) / 2

    @property
    def cy(self) -> float:
        return (self.box[1] + self.box[3]) / 2

    @property
    def height(self) -> float:
        return self.box[3] - self.box[1]


@dataclass
class Line:
    text: str
    box: tuple[float, float, float, float]

    @property
    def cx(self) -> float:
        return (self.box[0] + self.box[2]) / 2

    @property
    def cy(self) -> float:
        return (self.box[1] + self.box[3]) / 2

    @property
    def height(self) -> float:
        return self.box[3] - self.box[1]


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


def to_logos(raw: list[dict]) -> list[Logo]:
    """Les logos trouvés par capture_reader, avec l'équipe reconnue si elle est sûre."""
    logos = []
    for entry in raw:
        team = entry.get("team")
        dist, second = entry.get("distance"), entry.get("second")
        sure = (team in TEAM_NAMES and dist is not None and dist <= LOGO_MAX_DISTANCE
                and (second is None or second - dist >= LOGO_MIN_MARGIN))
        logos.append(Logo(tuple(entry["box"]), bytes.fromhex(entry.get("descriptor", "")), team if sure else None))
    return logos


def to_lines(raw: list[dict]) -> list[Line]:
    return [Line(" ".join(str(entry["text"]).split()), tuple(entry["box"])) for entry in raw
            if str(entry.get("text", "")).strip()]


def is_noise(text: str) -> bool:
    key = normalize(text).strip(" :")
    return key in HEADER_WORDS or bool(DATE_PATTERN.match(key))


def merge_lines(lines: list[Line]) -> list[Line]:
    """Réunit les morceaux d'une même ligne (« Choix de 2e ronde » et « 2020 »), de gauche à droite."""
    merged: list[list[Line]] = []
    for line in sorted(lines, key=lambda l: (l.cy, l.box[0])):
        for group in merged:
            ref = group[0]
            overlap = min(ref.box[3], line.box[3]) - max(ref.box[1], line.box[1])
            if overlap > 0.5 * min(ref.height, line.height):
                group.append(line)
                break
        else:
            merged.append([line])
    result = []
    for group in merged:
        group.sort(key=lambda l: l.box[0])
        box = (min(l.box[0] for l in group), min(l.box[1] for l in group),
               max(l.box[2] for l in group), max(l.box[3] for l in group))
        result.append(Line(" ".join(l.text for l in group), box))
    return sorted(result, key=lambda l: l.box[1])


def blocks(lines: list[Line], gap: float) -> list[list[Line]]:
    """Regroupe les lignes qui se suivent de près : les éléments d'une même transaction."""
    groups: list[list[Line]] = []
    for line in sorted(lines, key=lambda l: l.box[1]):
        if groups and line.box[1] - groups[-1][-1].box[3] <= gap:
            groups[-1].append(line)
        else:
            groups.append([line])
    return groups


def ordinal(round_number: int) -> str:
    return "1re" if round_number == 1 else f"{round_number}e"


def parse_item(line: Line, pick_logos: list[Logo]) -> Item:
    text = line.text
    match = PICK_FR.search(text) or PICK_EN.search(text)
    if not match:
        return Item(text.strip(" |[]"))
    round_number = int(match.group(1))
    year = YEAR.search(text[match.end():]) or YEAR.search(text)
    origin = None
    for logo in pick_logos:
        if line.box[1] - 4 <= logo.cy <= line.box[3] + 4 and line.box[0] - 4 <= logo.cx <= line.box[2] + logo.height * 3:
            origin = logo
            break
    return Item(f"Choix de {ordinal(round_number)} ronde", round_number, year.group(1) if year else None, origin)


def parse_table(raw_lines: list[dict], raw_logos: list[dict]) -> list[Trade]:
    """Les transactions du tableau, dans l'ordre de la capture (la plus récente en haut chez SHM)."""
    lines = [l for l in to_lines(raw_lines) if not is_noise(l.text)]
    logos = to_logos(raw_logos)
    if lines:
        # Une lettre très grasse peut ressembler à un petit logo : un logo est au moins presque aussi
        # haut qu'une ligne de texte.
        text_height = sorted(l.height for l in lines)[len(lines) // 2]
        logos = [logo for logo in logos if logo.height >= 0.6 * text_height]
    if not logos:
        raise CaptureError("Je ne trouve aucun logo d'équipe dans cette image. Envoie une capture du "
                           "tableau des transactions de SHM, avec les logos.")
    tallest = max(logo.height for logo in logos)
    team_logos = [logo for logo in logos if logo.height >= 0.8 * tallest]
    pick_logos = [logo for logo in logos if logo.height < 0.8 * tallest]

    # Les logos d'équipe forment deux colonnes : la séparation est le plus grand écart horizontal.
    xs = sorted(logo.cx for logo in team_logos)
    gaps = [(b - a, (a + b) / 2) for a, b in zip(xs, xs[1:])]
    if not gaps or max(gaps)[0] < tallest * 3:
        raise CaptureError("Je ne vois qu'une seule colonne d'équipes. La capture doit montrer les deux "
                           "colonnes « Vers » du tableau des transactions.")
    split = max(gaps)[1]
    left = sorted((l for l in team_logos if l.cx < split), key=lambda l: l.cy)
    right = sorted((l for l in team_logos if l.cx >= split), key=lambda l: l.cy)
    right_x = min(l.box[0] for l in right)
    left_x = max(l.box[2] for l in left)

    # Une transaction par rangée : un logo à gauche et un à droite, à la même hauteur.
    rows: list[list[Logo | None]] = []
    unused = list(right)
    for logo in left:
        partner = min(unused, key=lambda r: abs(r.cy - logo.cy), default=None)
        if partner is not None and abs(partner.cy - logo.cy) <= tallest * 0.6:
            unused.remove(partner)
            rows.append([logo, partner])
        else:
            rows.append([logo, None])
    rows += [[None, logo] for logo in unused]
    rows.sort(key=lambda row: next(l.cy for l in row if l is not None))
    centers = [next(l.cy for l in row if l is not None) for row in rows]

    right_end = max(l.box[2] for l in right)
    # Un logo est parfois lu comme une lettre (l'étoile de Dallas) : ce texte-là n'est pas un élément.
    text_lines = [l for l in lines if (left_x < l.cx < right_x or l.cx > right_end)
                  and not any(logo.box[0] - 2 <= l.cx <= logo.box[2] + 2 and logo.box[1] - 2 <= l.cy <= logo.box[3] + 2
                              for logo in logos)]
    if not text_lines:
        raise CaptureError("Je n'arrive pas à lire le texte de cette capture. Essaie une capture plus nette, "
                           "sans zoom réduit.")
    line_height = sorted(l.height for l in text_lines)[len(text_lines) // 2]
    trades = [Trade([Side(row[0]), Side(row[1])]) for row in rows]
    # Chaque colonne est lue à part : une ligne de gauche n'est jamais collée à une ligne de droite.
    for side_index, side_lines in enumerate((
            merge_lines([l for l in text_lines if l.cx < right_x]),
            merge_lines([l for l in text_lines if l.cx >= right_x]))):
        for group in blocks(side_lines, gap=line_height * 0.8):
            middle = (group[0].box[1] + group[-1].box[3]) / 2
            row = min(range(len(rows)), key=lambda i: abs(centers[i] - middle))
            trades[row].sides[side_index].items += [parse_item(l, pick_logos) for l in group]
    trades = [t for t in trades if any(side.items for side in t.sides)]
    if not trades:
        raise CaptureError("Je ne trouve aucune transaction dans cette capture.")
    return trades


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


def logo_distance(a: bytes, b: bytes) -> float:
    if not a or len(a) != len(b):
        return float("inf")
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def learned_role(logo: Logo | None, learned: list[tuple[int, bytes]]) -> int | None:
    """Le rôle qu'un admin a déjà associé à ce logo sur ce serveur."""
    if logo is None or not learned:
        return None
    dist, role_id = min((logo_distance(logo.descriptor, desc), role_id) for role_id, desc in learned)
    return role_id if dist <= LEARNED_MAX_DISTANCE else None


def resolve(trade: Trade, roles: list, learned: list[tuple[int, bytes]]) -> None:
    """Trouve le rôle de chaque équipe et écrit le texte des éléments."""
    by_id = {role.id: role for role in roles}

    def find(logo: Logo | None):
        role_id = learned_role(logo, learned)
        if role_id in by_id:
            return by_id[role_id]
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


def problems(trade: Trade, roles_by_id: dict) -> list[str]:
    found = []
    for side, texts in zip(trade.sides, trade.texts):
        if side.role_id not in roles_by_id:
            found.append("équipe sans rôle" if side.name else "équipe inconnue")
        if not texts:
            found.append("rien reçu")
    return found


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
