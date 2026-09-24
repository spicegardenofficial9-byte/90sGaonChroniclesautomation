"""Builds a 1280x720 YouTube thumbnail with a warm retro tint and bold text."""

from __future__ import annotations

import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from .editor import find_font

SIZE = (1280, 720)
MAX_BYTES = 2 * 1024 * 1024  # YouTube's thumbnail limit


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = find_font()
    return ImageFont.truetype(str(path), size) if path else ImageFont.load_default()


def build_thumbnail(base_image: Path, text: str, brand: str, dest: Path) -> Path:
    img = Image.open(base_image).convert("RGB")
    img = ImageOps.fit(img, SIZE, Image.LANCZOS)

    # warm sepia wash + punchier contrast so it reads at small sizes
    img = ImageEnhance.Contrast(img).enhance(1.2)
    img = ImageEnhance.Color(img).enhance(1.15)
    sepia = ImageOps.colorize(ImageOps.grayscale(img), "#2b1a0e", "#f3d9a4")
    img = Image.blend(img, sepia, 0.3)

    # dark gradient at the bottom for the text
    overlay = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for y in range(SIZE[1] // 2, SIZE[1]):
        alpha = int(200 * (y - SIZE[1] / 2) / (SIZE[1] / 2))
        draw.line([(0, y), (SIZE[0], y)], fill=(0, 0, 0, alpha))
    img = Image.alpha_composite(img.convert("RGBA"), overlay)

    draw = ImageDraw.Draw(img)
    text = (text or "").upper()
    font_size = 110
    lines = textwrap.wrap(text, width=16)[:3]
    font = _font(font_size)
    while lines and font_size > 50 and max(draw.textlength(l, font=font) for l in lines) > SIZE[0] - 100:
        font_size -= 6
        font = _font(font_size)

    y = SIZE[1] - 60 - len(lines) * (font_size + 10)
    for line in lines:
        w = draw.textlength(line, font=font)
        draw.text(((SIZE[0] - w) / 2, y), line, font=font, fill="#FFD84A",
                  stroke_width=6, stroke_fill="black")
        y += font_size + 10

    if brand:
        small = _font(36)
        draw.rounded_rectangle([30, 30, 60 + draw.textlength(brand, font=small), 90],
                               radius=12, fill=(180, 40, 30, 230))
        draw.text((45, 38), brand, font=small, fill="white")

    img = img.convert("RGB")
    quality = 92
    img.save(dest, "JPEG", quality=quality)
    while dest.stat().st_size > MAX_BYTES and quality > 50:
        quality -= 8
        img.save(dest, "JPEG", quality=quality)
    return dest
