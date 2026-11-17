"""La carte d'une transaction en image (PNG) : chaque équipe, ce qu'elle reçoit et les cotes SHM.

Une équipe par bloc, l'une sous l'autre : un bandeau à ses couleurs, puis ses joueurs (nom,
position, âge, contrat et cotes) et ses choix de repêchage. Le bot l'envoie avec l'annonce ;
si l'image ne peut pas être faite ou envoyée, l'annonce part en texte (rien n'est bloqué).
"""

import io
from pathlib import Path

from . import transactions
from .storage import normalize

POLICES = Path(__file__).parent / "polices"
BOLD = POLICES / "DejaVuSans-Bold.ttf"
REGULAR = POLICES / "DejaVuSans.ttf"

# Couleurs des équipes : (principale, secondaire). Inconnue : gris.
TEAM_COLORS = {
    "ANA": ("#F47A38", "#111111"), "ARI": ("#8C2633", "#E2D6B5"), "BOS": ("#FFB81C", "#111111"),
    "BUF": ("#003087", "#FFB81C"), "CGY": ("#C8102E", "#F1BE48"), "CAR": ("#CE1126", "#111111"),
    "CHI": ("#CF0A2C", "#111111"), "COL": ("#6F263D", "#236192"), "CBJ": ("#002654", "#CE1126"),
    "DAL": ("#006847", "#8F8F8C"), "DET": ("#CE1126", "#FFFFFF"), "EDM": ("#041E42", "#FF4C00"),
    "FLA": ("#C8102E", "#041E42"), "LAK": ("#111111", "#A2AAAD"), "MIN": ("#154734", "#A6192E"),
    "MTL": ("#AF1E2D", "#192168"), "NSH": ("#FFB81C", "#041E42"), "NJD": ("#CE1126", "#111111"),
    "NYI": ("#00539B", "#F47D30"), "NYR": ("#0038A8", "#CE1126"), "OTT": ("#C52032", "#C2912C"),
    "PHI": ("#F74902", "#111111"), "PIT": ("#111111", "#FCB514"), "SJS": ("#006D75", "#EA7200"),
    "STL": ("#002F87", "#FCB514"), "TBL": ("#002868", "#FFFFFF"), "TOR": ("#00205B", "#FFFFFF"),
    "VAN": ("#00205B", "#00843D"), "WSH": ("#C8102E", "#041E42"), "WPG": ("#041E42", "#AC162C"),
    "SEA": ("#001628", "#99D9D9"), "UTA": ("#6CACE4", "#111111"), "VGK": ("#B4975A", "#333F42"),
}
UNKNOWN_COLORS = ("#4A5568", "#A0AEC0")
FRENCH_NAMES = {code: french for code, _, french, _ in transactions.TEAMS}

WIDTH = 1000
MARGIN = 28
BACKGROUND = "#0E1116"
PANEL = "#161A21"
TEXT = "#F5F7FA"
MUTED = "#8B95A5"
LINE = "#262C36"
# Au-delà, « + N autres » : l'image reste lisible.
MAX_ITEMS = 12


def hex_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))


def text_on(color: str) -> str:
    """Noir ou blanc, selon ce qui se lit le mieux sur cette couleur."""
    r, g, b = hex_rgb(color)
    return "#111111" if (0.299 * r + 0.587 * g + 0.114 * b) > 160 else "#FFFFFF"


def clean(text: str) -> str:
    """Retire ce que la police ne sait pas dessiner (les emojis d'un nom de rôle, par exemple)."""
    return "".join(c for c in text if ord(c) < 0x2000 or c in "·×⇄…").strip()


def rating_color(value: str) -> str:
    try:
        number = int(value)
    except ValueError:
        return TEXT
    if number >= 85:
        return "#F6C453"
    if number >= 75:
        return "#7EE2A8"
    if number >= 60:
        return TEXT
    return MUTED


def side_name(side: transactions.Side) -> str:
    team = side.logo.team if side.logo is not None else None
    if team in FRENCH_NAMES:
        return FRENCH_NAMES[team]
    return clean(side.name or "") or "Équipe inconnue"


def contract_line(line: transactions.PlayerLine) -> str:
    parts = [line.position] if line.position else []
    if line.age:
        parts.append(f"{line.age} ans")
    if line.salary:
        contract = f"{line.salary} $"
        if line.years:
            contract += f" × {line.years} an{'s' if line.years not in ('0', '1') else ''}"
        parts.append(contract)
    return " · ".join(parts)


class _Fonts:
    def __init__(self):
        from PIL import ImageFont
        self.title = ImageFont.truetype(str(BOLD), 30)
        self.label = ImageFont.truetype(str(BOLD), 13)
        self.team = ImageFont.truetype(str(BOLD), 24)
        self.badge = ImageFont.truetype(str(BOLD), 14)
        self.name = ImageFont.truetype(str(BOLD), 23)
        self.info = ImageFont.truetype(str(REGULAR), 16)
        self.rating_label = ImageFont.truetype(str(BOLD), 11)
        self.rating = ImageFont.truetype(str(BOLD), 17)
        self.footer = ImageFont.truetype(str(REGULAR), 13)


def _fit(draw, text: str, font, width: int) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"


# Les hauteurs de chaque partie, pour connaître la taille de l'image avant de dessiner.
HEADER_H = 92
BAND_H = 60
PLAYER_H = 64
RATINGS_H = 46
PICK_H = 58
MORE_H = 34
PANEL_PAD = 16
GAP = 18
FOOTER_H = 50


def _entries(trade: transactions.Trade, players: dict) -> list[list[tuple]]:
    """Pour chaque équipe, ses éléments : ("joueur", nom, ligne SHM ou None) ou ("choix", texte, origine)."""
    result = []
    codes = [side.logo.team if side.logo is not None else None for side in trade.sides]
    for index, side in enumerate(trade.sides):
        entries = []
        others = {code for i, code in enumerate(codes) if i != index and code}
        for item in side.items:
            if item.round is None:
                entries.append(("joueur", clean(item.text), players.get(normalize(item.text))))
            else:
                text = item.text + (f" {item.year}" if item.year else "")
                origin = item.origin.team if item.origin is not None else None
                # La provenance n'est utile que si le choix n'appartenait pas déjà à l'autre équipe.
                via = FRENCH_NAMES.get(origin) if origin and origin not in others else None
                entries.append(("choix", clean(text), via))
        result.append(entries)
    return result


def _entry_height(entry) -> int:
    if entry[0] == "choix":
        return PICK_H
    line = entry[2]
    return PLAYER_H + (RATINGS_H if line is not None and line.ratings else 0)


def render_png(trade: transactions.Trade, players: dict | None = None) -> bytes:
    """L'image PNG de la transaction. `players` : nom normalisé -> ligne SHM (contrat et cotes)."""
    from PIL import Image, ImageDraw

    fonts = _Fonts()
    sides = _entries(trade, players or {})
    shown = [entries[:MAX_ITEMS] for entries in sides]

    height = HEADER_H
    for entries, full in zip(shown, sides):
        height += BAND_H + PANEL_PAD * 2 + sum(_entry_height(e) for e in entries)
        height += (MORE_H if len(full) > len(entries) else 0) + (0 if entries else PICK_H) + GAP
    height += FOOTER_H - GAP

    image = Image.new("RGB", (WIDTH, height), BACKGROUND)
    draw = ImageDraw.Draw(image)

    # L'en-tête : « TRANSACTION » et les codes des équipes.
    codes = [side.logo.team if side.logo is not None and side.logo.team else "?" for side in trade.sides]
    draw.rounded_rectangle((MARGIN, 26, MARGIN + 132, 50), radius=5, fill=TEXT)
    draw.text((MARGIN + 66, 38), "TRANSACTION", font=fonts.label, fill=BACKGROUND, anchor="mm")
    draw.text((MARGIN + 150, 38), "  ⇄  ".join(codes), font=fonts.title, fill=TEXT, anchor="lm")
    draw.line((MARGIN, HEADER_H - 16, WIDTH - MARGIN, HEADER_H - 16), fill=LINE, width=2)

    y = HEADER_H
    inner = WIDTH - 2 * MARGIN
    for side, entries, full in zip(trade.sides, shown, sides):
        team = side.logo.team if side.logo is not None else None
        primary, secondary = TEAM_COLORS.get(team, UNKNOWN_COLORS)
        on_primary = text_on(primary)
        block_h = (BAND_H + PANEL_PAD * 2 + sum(_entry_height(e) for e in entries)
                   + (MORE_H if len(full) > len(entries) else 0) + (0 if entries else PICK_H))
        # Le bloc : panneau sombre aux coins arrondis, bandeau aux couleurs de l'équipe en haut.
        draw.rounded_rectangle((MARGIN, y, WIDTH - MARGIN, y + block_h), radius=14, fill=PANEL)
        draw.rounded_rectangle((MARGIN, y, WIDTH - MARGIN, y + BAND_H), radius=14, fill=primary)
        draw.rectangle((MARGIN, y + BAND_H - 14, WIDTH - MARGIN, y + BAND_H), fill=primary)
        draw.rectangle((MARGIN, y + BAND_H, WIDTH - MARGIN, y + BAND_H + 4), fill=secondary)
        # La pastille de l'équipe : son code dans un cercle.
        cx, cy, r = MARGIN + 40, y + BAND_H // 2, 24
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=secondary, outline=on_primary, width=2)
        draw.text((cx, cy), (team or "?")[:3], font=fonts.badge, fill=text_on(secondary), anchor="mm")
        draw.text((MARGIN + 80, cy), _fit(draw, side_name(side).upper(), fonts.team, inner - 220),
                  font=fonts.team, fill=on_primary, anchor="lm")
        draw.text((WIDTH - MARGIN - 20, cy), "REÇOIT", font=fonts.label, fill=on_primary, anchor="rm")

        ey = y + BAND_H + 4 + PANEL_PAD
        x = MARGIN + 24
        if not entries:
            draw.text((x, ey + 12), "Rien", font=fonts.info, fill=MUTED)
        for entry in entries:
            if entry[0] == "choix":
                _, text, via = entry
                draw.text((x, ey + 6), _fit(draw, text, fonts.name, inner - 48), font=fonts.name, fill=TEXT)
                draw.text((x, ey + 34), f"Choix d'origine : {via}" if via else "Choix de repêchage",
                          font=fonts.info, fill=MUTED)
                ey += PICK_H
                continue
            _, name, line = entry
            draw.text((x, ey + 6), _fit(draw, name, fonts.name, inner - 48), font=fonts.name, fill=TEXT)
            info = contract_line(line) if line is not None else "Cotes non disponibles"
            draw.text((x, ey + 36), _fit(draw, info, fonts.info, inner - 48), font=fonts.info, fill=MUTED)
            ey += PLAYER_H
            if line is not None and line.ratings:
                _draw_ratings(draw, fonts, line.ratings, x, ey, inner - 48, primary)
                ey += RATINGS_H
        if len(full) > len(entries):
            draw.text((x, ey + 6), f"+ {len(full) - len(entries)} autres", font=fonts.info, fill=MUTED)
        y += block_h + GAP

    draw.text((WIDTH // 2, height - 24), "Hockey Sims Tools  ·  données SHM", font=fonts.footer,
              fill=MUTED, anchor="mm")
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _draw_ratings(draw, fonts, ratings, x: int, y: int, width: int, accent: str) -> None:
    """Une rangée de petites cases : le titre de la cote au-dessus, la valeur en dessous. OA ressort."""
    count = len(ratings)
    cell = min(56, width // max(count, 1))
    for i, (label, value) in enumerate(ratings):
        left = x + i * cell
        box = (left, y, left + cell - 4, y + RATINGS_H - 8)
        overall = normalize(label) in ("oa", "ovr")
        draw.rounded_rectangle(box, radius=6, fill=accent if overall else "#1F2530")
        middle = left + (cell - 4) // 2
        label_color = text_on(accent) if overall else MUTED
        value_color = text_on(accent) if overall else rating_color(value)
        draw.text((middle, y + 10), clean(label)[:4], font=fonts.rating_label, fill=label_color, anchor="mm")
        draw.text((middle, y + 27), clean(value)[:3], font=fonts.rating, fill=value_color, anchor="mm")
