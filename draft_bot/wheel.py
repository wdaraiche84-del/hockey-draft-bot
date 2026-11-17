"""La roue : un tirage au sort équitable entre plusieurs options, toutes avec les mêmes chances."""

import secrets
from pathlib import Path

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



# La roue animée : une image GIF qui tourne UNE fois et s'arrête sur l'option tirée.
# Le gagnant est tiré avant (spin) : l'animation ne fait que le montrer, elle ne décide rien.

WHEEL_SIZE = 380           # pixels (la roue) ; un bandeau dessous montre le gagnant à la fin
BANNER = 56
SPIN_FRAMES = 56           # images de la rotation
FRAME_MS = 60              # durée de chaque image (≈ 3,4 s de rotation)
FINAL_MS = 600_000         # la dernière image reste 10 minutes : la roue est arrêtée
TURNS = 4                  # tours complets avant de ralentir
FONT = Path(__file__).parent / "polices" / "DejaVuSans-Bold.ttf"
# Couleurs vives, bien différentes entre voisines.
COLORS = ["#e63946", "#457b9d", "#f4a261", "#2a9d8f", "#9b5de5", "#e9c46a", "#1d3557", "#f15bb5",
          "#06d6a0", "#ef476f"]


def spin_seconds() -> float:
    """Le temps que la roue met à s'arrêter, pour révéler le gagnant en texte juste après."""
    return SPIN_FRAMES * FRAME_MS / 1000


def slice_colors(count: int) -> list[str]:
    colors = [COLORS[i % len(COLORS)] for i in range(count)]
    # La dernière part touche la première : jamais deux voisines de la même couleur.
    if count > 1 and colors[-1] == colors[0]:
        colors[-1] = next(c for c in COLORS if c not in (colors[0], colors[-2]))
    return colors


def short_label(label: str, count: int) -> str:
    room = 16 if count <= 8 else 12 if count <= 16 else 9
    return label if len(label) <= room else label[:room - 1] + "…"


def final_angle(count: int, winner_index: int, rng=_random) -> float:
    """De combien de degrés (sens horaire) la roue tourne pour finir avec le gagnant sous la flèche.

    La part i va de i × part à (i+1) × part, comptée depuis le haut dans le sens horaire. On s'arrête
    à un endroit au hasard dans la part du gagnant, jamais tout près d'un bord (pas d'hésitation).
    """
    part = 360 / count
    target = winner_index * part + part * (0.15 + 0.7 * rng.random())
    return TURNS * 360 + (360 - target) % 360


def winner_under_pointer(count: int, angle: float) -> int:
    """Quelle part est sous la flèche (en haut) après avoir tourné de « angle » degrés."""
    part = 360 / count
    return int(((360 - angle % 360) % 360) // part) % count


def _ease_out(t: float) -> float:
    return 1 - (1 - t) ** 3


def render_gif(options: list[str], winner_index: int, rng=_random) -> bytes:
    """La roue qui tourne une fois et s'arrête sur options[winner_index], en GIF qui ne boucle pas."""
    import io
    import math

    from PIL import Image, ImageDraw, ImageFont

    count = len(options)
    size = WHEEL_SIZE
    scale = 2  # dessinée en grand puis réduite : bords plus lisses
    big, c = size * scale, size * scale / 2
    radius = c - 20 * scale
    part = 360 / count

    wheel = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(wheel)
    labels = [short_label(label, count) for label in options]
    # Le plus gros texte qui tient : assez court pour la longueur de la part, assez mince pour sa largeur.
    arc = 2 * math.pi * radius * 0.60 / count
    points = 26 * scale
    while points > 9 * scale:
        font = ImageFont.truetype(str(FONT), points)
        if max(font.getlength(t) for t in labels) <= radius * 0.66 and points <= arc * 0.75:
            break
        points -= scale
    for i, color in enumerate(slice_colors(count)):
        start = -90 + i * part
        draw.pieslice([c - radius, c - radius, c + radius, c + radius], start, start + part,
                      fill=color, outline="white", width=2 * scale)
    for i in range(count):
        middle = (i + 0.5) * part  # depuis midi, sens horaire
        layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        # Le texte est écrit à l'horizontale à droite du centre, puis tourné sur sa part.
        # Comme sur une vraie roue, chaque texte se lit du centre vers le bord.
        ImageDraw.Draw(layer).text((c + radius * 0.58, c), labels[i], font=font, fill="white",
                                   anchor="mm", stroke_width=scale, stroke_fill=(0, 0, 0, 110))
        wheel.alpha_composite(layer.rotate(90 - middle, center=(c, c), resample=Image.BICUBIC))
    wheel = wheel.resize((size, size), Image.LANCZOS)

    h = size / 2
    background = (49, 51, 56)  # le fond de Discord (thème sombre)
    winner_font = ImageFont.truetype(str(FONT), 24)

    def frame_at(angle: float, final: bool) -> Image.Image:
        frame = Image.new("RGBA", (size, size + BANNER), background + (255,))
        frame.alpha_composite(wheel.rotate(-angle, resample=Image.BICUBIC, center=(h, h)))
        d = ImageDraw.Draw(frame)
        d.ellipse([h - (h - 20) - 4, 16, h + (h - 20) + 4, size - 16], outline="#1e1f22", width=5)
        d.ellipse([h - 24, h - 24, h + 24, h + 24], fill="white", outline="#1e1f22", width=4)
        # La flèche, en haut, pointe vers le bas sur la part gagnante.
        d.polygon([(h - 18, 0), (h + 18, 0), (h, 42)], fill="#1e1f22")
        d.polygon([(h - 12, 4), (h + 12, 4), (h, 34)], fill="white")
        if final:
            text = options[winner_index]
            while winner_font.getlength(text) > size - 40 and len(text) > 2:
                text = text[:-2] + "…"
            d.rounded_rectangle([12, size + 4, size - 12, size + BANNER - 6], radius=12, fill="#f0b232")
            d.text((h, size + BANNER / 2 - 1), text, font=winner_font, fill="#1e1f22", anchor="mm")
        return frame.convert("RGB")

    total = final_angle(count, winner_index, rng)
    frames = [frame_at(total * _ease_out(n / SPIN_FRAMES), False) for n in range(SPIN_FRAMES)]
    frames.append(frame_at(total, True))
    # Une seule palette pour toutes les images : couleurs stables, fichier plus léger.
    palette = frames[-1].quantize(colors=96, method=Image.Quantize.MEDIANCUT)
    frames = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    out = io.BytesIO()
    # Pas de « loop » : le GIF joue une seule fois, puis reste sur la dernière image (le gagnant).
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:],
                   duration=[FRAME_MS] * SPIN_FRAMES + [FINAL_MS], disposal=1)
    return out.getvalue()
