"""typecard pipeline: a kinetic caption card from words alone (needs_input = false).

Pillow draws every frame — user text never enters a filtergraph. ffmpeg then
encodes the ``frame_%04d.png`` sequence as H.264 and cook_clip budget-encodes
the GIF. This is the offline fallback when /gif has an intention but no
photo, and the recipe bare /gif opens with.
"""
from __future__ import annotations

import hashlib
import math
import os

from PIL import Image, ImageDraw, ImageFont

from clipwright import caption, ffmpeg, recipe
from clipwright.pipelines import CookContext, CookResult, common

DEFAULT_DURATION = 2.5
DEFAULT_FPS = 12
DEFAULT_WIDTH = 480
ASPECT = 3 / 4          # 480×360-ish; even() later
PAD = 28

# Saturated-enough backgrounds so white Impact type reads; picked by a hash of the text.
BG_RGB = (
    (18, 18, 32),
    (32, 14, 14),
    (14, 28, 18),
    (14, 18, 36),
    (36, 24, 10),
    (28, 12, 30),
    (10, 26, 28),
    (30, 18, 12),
)


def run(inst: dict, ctx: CookContext) -> CookResult:
    text = str(recipe.get(inst, "caption.text") or "").strip() or " "
    duration = float(recipe.get(inst, "duration", DEFAULT_DURATION) or DEFAULT_DURATION)
    duration = min(max(0.5, duration), common.SEGMENT_CAP_S)
    fps = float(recipe.get(inst, "fps", DEFAULT_FPS) or DEFAULT_FPS)
    width = ffmpeg.even(int(recipe.get(inst, "width", DEFAULT_WIDTH) or DEFAULT_WIDTH))
    height = ffmpeg.even(max(2, int(width * ASPECT)))
    color = str(recipe.get(inst, "caption.color", "#ffffff") or "#ffffff")
    size = int(recipe.get(inst, "caption.size", 64) or 64)
    bg = _bg(inst, text)
    n = max(2, int(round(fps * duration)))
    pattern = os.path.join(ctx.workdir, "frame_%04d.png")
    for i in range(1, n + 1):
        t = (i - 1) / (n - 1)
        _paint(text, width, height, size, color, bg, t).save(pattern % i, format="PNG")
    clip = os.path.join(ctx.workdir, "typecard.mp4")
    ffmpeg.run(ffmpeg.image_seq_argv(pattern, clip, fps=fps), log=ctx.log)
    clip_inst = {**inst, "input": clip,
                 "from": recipe.fmt_time(0.0), "to": recipe.fmt_time(duration)}
    return common.cook_clip(clip_inst, ctx, loop="none")


def _bg(inst: dict, text: str) -> tuple[int, int, int]:
    raw = recipe.get(inst, "bg")
    if isinstance(raw, str) and raw.startswith("#") and len(raw) == 7:
        try:
            v = int(raw[1:], 16)
            return (v >> 16 & 0xFF, v >> 8 & 0xFF, v & 0xFF)
        except ValueError:
            pass
    digest = hashlib.sha1(text.encode("utf-8")).digest()
    return BG_RGB[digest[0] % len(BG_RGB)]


def _paint(text: str, width: int, height: int, size: int, color: str,
           bg: tuple[int, int, int], t: float) -> Image.Image:
    """One frame: scale-in over the first 40 %, then a tiny sine bob."""
    canvas = Image.new("RGB", (width, height), bg)
    ease = _smooth(min(1.0, t / 0.4))
    scale = 0.82 + 0.18 * ease
    bob = int(round(6 * math.sin(t * math.pi * 2) * ease))
    px = max(caption.MIN_SIZE, int(round(size * scale * width / common.DEFAULT_WIDTH)))
    band = _band(text, width - 2 * PAD, px, color, height - 2 * PAD)
    x = (width - band.size[0]) // 2
    y = (height - band.size[1]) // 2 + bob
    canvas.paste(band, (x, y), band)
    return canvas


def _band(text: str, width: int, size: int, color: str, max_height: int) -> Image.Image:
    """Impact caption if the face can draw the words; otherwise a decorative fallback."""
    clean = caption.sanitize_text(text)
    if clean:
        return caption.render_caption(clean, width, size=size, color=color,
                                      style="impact-outline", max_height=max_height)
    # emoji-only / undecodable: concentric discs + whatever the default face will show
    band = Image.new("RGBA", (width, max(max_height, 64)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(band)
    cx, cy = width // 2, min(max_height, width) // 2
    for i, fill in enumerate(((220, 60, 80, 255), (240, 200, 60, 255), (60, 160, 220, 255))):
        r = max(8, cx // 3 - i * 18)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=fill, width=6)
    label = text.strip()[:24] or "?"
    try:
        font = ImageFont.truetype(caption.pick_font(bold=True), max(caption.MIN_SIZE, size // 2))
    except (OSError, RuntimeError):
        font = ImageFont.load_default()
    draw.text((cx, min(max_height - 8, cy + cx // 3)), label, font=font,
              fill=(255, 255, 255, 255), anchor="ms")
    return band


def _smooth(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)
