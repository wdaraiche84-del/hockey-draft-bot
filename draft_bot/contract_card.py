"""La carte d'une signature ou d'une prolongation de contrat en image (PNG).

Même style que l'image des transactions (`trade_card`) : un bandeau aux couleurs de l'équipe,
puis le joueur, son nouveau contrat bien en vue, sa position, son âge et ses cotes SHM.
Le bot l'envoie avec l'annonce ; si l'image ne peut pas être faite ou envoyée, l'annonce part
en texte (rien n'est bloqué).
"""

import io

from . import trade_card, transactions
from .trade_card import BACKGROUND, LINE, MARGIN, MUTED, PANEL, TEXT, WIDTH, clean, text_on

# Le mot de l'en-tête et du bandeau, et la couleur de l'étiquette, pour chaque sorte de contrat.
KINDS = {
    "signature": ("SIGNATURE", "SIGNE", "#7EE2A8"),
    "prolongation": ("PROLONGATION", "PROLONGE", "#8EC5FF"),
}

HEADER_H = 92
BAND_H = 60
PAD = 18
NAME_H = 40
CONTRACT_H = 58
INFO_H = 30
RATINGS_H = trade_card.RATINGS_H
FOOTER_H = 50


def years_text(years: str) -> str:
    return f"{years} an{'s' if years not in ('0', '1') else ''}"


def contract_text(salary: str, years: str) -> str:
    """« 4 250 000 $ par saison × 3 ans », avec seulement ce qui est connu."""
    if salary and years:
        return f"{salary} $ par saison × {years_text(years)}"
    if salary:
        return f"{salary} $ par saison"
    if years:
        return years_text(years)
    return ""


def info_text(line: transactions.PlayerLine | None) -> str:
    """« AG/AD · 24 ans », ou une phrase quand SHM n'a pas donné la ligne du joueur."""
    if line is None:
        return "Cotes non disponibles"
    parts = [line.position] if line.position else []
    if line.age:
        parts.append(f"{line.age} ans")
    return " · ".join(parts)


def render_png(kind: str, team: str | None, team_name: str | None, player: str, salary: str = "",
               years: str = "", line: transactions.PlayerLine | None = None) -> bytes:
    """L'image PNG d'une signature (`kind` = « signature ») ou d'une prolongation (« prolongation »).

    `team` : le code LNH de l'équipe (pour ses couleurs), ou None. `salary` et `years` : le nouveau
    contrat ; sinon, celui de la ligne SHM. `line` : la ligne SHM du joueur (position, âge, cotes)."""
    from PIL import Image, ImageDraw

    header, verb, accent = KINDS.get(kind, KINDS["signature"])
    if not salary and not years and line is not None:
        salary, years = line.salary, line.years
    fonts = trade_card._Fonts()
    contract = contract_text(salary, years)
    ratings = line.ratings if line is not None else ()

    block_h = BAND_H + 4 + PAD + NAME_H + INFO_H + (CONTRACT_H if contract else 0) + (RATINGS_H if ratings else 0) + PAD
    height = HEADER_H + block_h + FOOTER_H
    image = Image.new("RGB", (WIDTH, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    inner = WIDTH - 2 * MARGIN

    # L'en-tête : « SIGNATURE » ou « PROLONGATION », puis le code de l'équipe.
    label_w = int(draw.textlength(header, font=fonts.label)) + 28
    draw.rounded_rectangle((MARGIN, 26, MARGIN + label_w, 50), radius=5, fill=accent)
    draw.text((MARGIN + label_w // 2, 38), header, font=fonts.label, fill=text_on(accent), anchor="mm")
    draw.text((MARGIN + label_w + 18, 38), team or "?", font=fonts.title, fill=TEXT, anchor="lm")
    draw.line((MARGIN, HEADER_H - 16, WIDTH - MARGIN, HEADER_H - 16), fill=LINE, width=2)

    # Le bloc de l'équipe : bandeau à ses couleurs, comme pour les transactions.
    y = HEADER_H
    primary, secondary = trade_card.TEAM_COLORS.get(team, trade_card.UNKNOWN_COLORS)
    on_primary = text_on(primary)
    draw.rounded_rectangle((MARGIN, y, WIDTH - MARGIN, y + block_h), radius=14, fill=PANEL)
    draw.rounded_rectangle((MARGIN, y, WIDTH - MARGIN, y + BAND_H), radius=14, fill=primary)
    draw.rectangle((MARGIN, y + BAND_H - 14, WIDTH - MARGIN, y + BAND_H), fill=primary)
    draw.rectangle((MARGIN, y + BAND_H, WIDTH - MARGIN, y + BAND_H + 4), fill=secondary)
    cx, cy, r = MARGIN + 40, y + BAND_H // 2, 24
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=secondary, outline=on_primary, width=2)
    draw.text((cx, cy), (team or "?")[:3], font=fonts.badge, fill=text_on(secondary), anchor="mm")
    name = trade_card.FRENCH_NAMES.get(team) or clean(team_name or "") or "Équipe inconnue"
    draw.text((MARGIN + 80, cy), trade_card._fit(draw, name.upper(), fonts.team, inner - 220),
              font=fonts.team, fill=on_primary, anchor="lm")
    draw.text((WIDTH - MARGIN - 20, cy), verb, font=fonts.label, fill=on_primary, anchor="rm")

    x = MARGIN + 24
    ey = y + BAND_H + 4 + PAD
    draw.text((x, ey), trade_card._fit(draw, clean(player) or "Joueur inconnu", fonts.title, inner - 48),
              font=fonts.title, fill=TEXT)
    ey += NAME_H
    draw.text((x, ey), trade_card._fit(draw, info_text(line), fonts.info, inner - 48), font=fonts.info, fill=MUTED)
    ey += INFO_H
    if contract:
        # Le contrat : une bande discrète avec un liseré à la couleur de l'équipe.
        draw.rounded_rectangle((x, ey, WIDTH - MARGIN - 24, ey + CONTRACT_H - 12), radius=8, fill="#1F2530")
        draw.rectangle((x, ey, x + 5, ey + CONTRACT_H - 12), fill=accent)
        label = "NOUVEAU CONTRAT" if kind == "prolongation" else "CONTRAT"
        draw.text((x + 20, ey + (CONTRACT_H - 12) // 2), label, font=fonts.label, fill=MUTED, anchor="lm")
        offset = 20 + int(draw.textlength(label, font=fonts.label)) + 18
        draw.text((x + offset, ey + (CONTRACT_H - 12) // 2),
                  trade_card._fit(draw, contract, fonts.name, inner - 48 - offset - 20),
                  font=fonts.name, fill=TEXT, anchor="lm")
        ey += CONTRACT_H
    if ratings:
        trade_card._draw_ratings(draw, fonts, ratings, x, ey, inner - 48, primary)

    draw.text((WIDTH // 2, height - 24), "Hockey Sims Tools  ·  données SHM", font=fonts.footer,
              fill=MUTED, anchor="mm")
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()
