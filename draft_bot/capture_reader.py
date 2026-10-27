"""Lit une capture d'écran des transactions SHM : le texte et les logos d'équipes.

Ce module tourne dans un processus séparé (`python -m draft_bot.capture_reader image.png`) :
la lecture du texte demande quelques centaines de Mo de mémoire pendant deux secondes,
et cette mémoire est rendue dès la fin. Une erreur ici ne peut pas arrêter le bot.

Tout est gratuit et local : RapidOCR lit le texte, et les logos sont reconnus en les
comparant aux logos SHM rangés dans le dossier `logos`. Aucune image ne quitte l'ordinateur.

Le résultat est imprimé en JSON : les lignes de texte et les logos trouvés, avec leur position.
`capture.parse_table` en tire ensuite les transactions.
"""

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

LOGO_DIR = Path(__file__).parent / "logos"

# Taille commune à laquelle chaque logo est réduit avant la comparaison (largeur, hauteur).
# Petite et un peu floue : une capture plus grande, plus petite ou compressée donne la même empreinte.
DESCRIPTOR_SIZE = (16, 10)
# Les bords arrondis changent avec le zoom : on compare l'intérieur du logo.
DESCRIPTOR_INSET = 0.08
# Un pixel est « du contenu » quand sa couleur s'éloigne autant du fond.
CONTENT_DISTANCE = 40
# Un logo SHM est un rectangle arrondi presque plein ; une lettre ou un mot l'est beaucoup moins.
MIN_LOGO_FILL = 0.75
# Une capture plus grande ne sert à rien et coûterait de la mémoire.
MAX_PIXELS = 25_000_000


def load_image(path: str) -> Image.Image:
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    image = Image.open(path)
    image.load()
    if image.mode in ("RGBA", "LA", "P"):
        # Une partie transparente devient blanche, comme sur la page SHM.
        image = image.convert("RGBA")
        background = Image.new("RGBA", image.size, (255, 255, 255, 255))
        image = Image.alpha_composite(background, image)
    return image.convert("RGB")


def descriptor(image: Image.Image, box: tuple[int, int, int, int]) -> bytes:
    """L'empreinte d'un logo : l'intérieur du logo réduit à 16 × 10 pixels, puis adouci."""
    x0, y0, x1, y1 = box
    dx, dy = (x1 - x0 + 1) * DESCRIPTOR_INSET, (y1 - y0 + 1) * DESCRIPTOR_INSET
    small = image.crop((x0 + dx, y0 + dy, x1 + 1 - dx, y1 + 1 - dy)).resize(DESCRIPTOR_SIZE, Image.BOX)
    return small.filter(ImageFilter.GaussianBlur(1)).tobytes()


def reference_logos() -> dict[str, bytes]:
    refs = {}
    for path in sorted(LOGO_DIR.glob("*.png")):
        image = load_image(str(path))
        refs[path.stem] = descriptor(image, (0, 0, image.width - 1, image.height - 1))
    return refs


def background_color(pixels: np.ndarray) -> np.ndarray:
    """La couleur la plus fréquente : le fond du tableau (blanc, ou foncé en mode sombre)."""
    flat = pixels.reshape(-1, 3)[::7]
    colors, counts = np.unique(flat, axis=0, return_counts=True)
    return colors[counts.argmax()].astype(int)


def find_logos(image: Image.Image) -> list[tuple[int, int, int, int]]:
    import cv2

    pixels = np.asarray(image)
    background = background_color(pixels)
    mask = (np.abs(pixels.astype(int) - background).sum(axis=2) > CONTENT_DISTANCE).astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    boxes = []
    for x, y, w, h, area in stats[1:count]:
        if w < 12 or h < 8 or not 1.2 <= w / h <= 2.3:
            continue
        if area / (w * h) < MIN_LOGO_FILL:
            continue
        boxes.append((int(x), int(y), int(x + w - 1), int(y + h - 1)))
    return boxes


def distance(a: bytes, b: bytes) -> float:
    return float(np.abs(np.frombuffer(a, np.uint8).astype(int) - np.frombuffer(b, np.uint8).astype(int)).mean())


def read_text(image: Image.Image) -> list[dict]:
    from rapidocr import RapidOCR

    engine = RapidOCR(params={"Global.log_level": "error"})
    result = engine(np.asarray(image))
    lines = []
    if result.boxes is None:
        return lines
    for box, text, score in zip(result.boxes, result.txts, result.scores):
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        lines.append({"text": str(text), "box": [min(xs), min(ys), max(xs), max(ys)], "score": float(score)})
    return lines


def read_capture(path: str) -> dict:
    image = load_image(path)
    refs = reference_logos()
    logos = []
    for box in find_logos(image):
        desc = descriptor(image, box)
        scores = sorted((distance(desc, ref), key) for key, ref in refs.items())
        logos.append({
            "box": list(box),
            "descriptor": desc.hex(),
            "team": scores[0][1] if scores else None,
            "distance": scores[0][0] if scores else None,
            "second": scores[1][0] if len(scores) > 1 else None,
        })
    return {"lines": read_text(image), "logos": logos}


def check() -> None:
    """Vérifie que tout est installé, sans lire de capture."""
    import cv2  # noqa: F401
    from rapidocr import RapidOCR  # noqa: F401

    assert len(reference_logos()) >= 30


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage : python -m draft_bot.capture_reader image.png | --check", file=sys.stderr)
        return 2
    if argv[1] == "--check":
        check()
        print(json.dumps({"ok": True}))
        return 0
    print(json.dumps(read_capture(argv[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
