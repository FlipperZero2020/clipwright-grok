"""Caption rendering: user text -> transparent RGBA band, drawn by Pillow.

This is the only place user-supplied text is turned into pixels. ffmpeg then
composites the resulting PNG with ``overlay``; the text itself never enters a
filtergraph string, which is what keeps caption text out of any shell or
filter-syntax reach (see docs/ENGINE_CONTRACT.md).

The band is exactly ``width`` pixels wide, fully transparent, and as tall as
``lines * line_h + 2 * pad``. Text is greedily word-wrapped to ``width - 2*pad``
and centred; if a single word is wider than that, the font shrinks stepwise
(down to ``MIN_SIZE``) before, as a last resort, the word is broken by
character so nothing is ever clipped at the band edges. ``max_height`` bounds
the band the same way: the font shrinks until the wrapped lines fit, and at
the floor size the lines that fit are kept and the last one is ellipsized, so
an overlay never grows taller than the frame it is composited on.

Only text a caption font can draw survives ``sanitize_text``: emoji and other
pictographic symbols are dropped rather than rendered as notdef boxes, because
every face ``pick_font`` can return is a plain text face.
"""
from __future__ import annotations

import glob
import math
import os
import re
import unicodedata
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

FONT_ROOT = "/usr/share/fonts"
MAX_CHARS = 200
MIN_SIZE = 18
SHRINK_STEP = 4
LINE_SPACING = 1.1
ELLIPSIS = "…"
STROKE_RGBA = (0, 0, 0, 255)
TRANSPARENT = (0, 0, 0, 0)

# style -> whether the text is upper-cased. Both styles get the black stroke.
STYLES: dict[str, bool] = {"impact-outline": True, "plain": False}

_HEX_COLOR = re.compile(r"#([0-9a-fA-F]{6})")

# What no text face draws: controls and surrogates, format characters (ZWJ
# and friends), enclosing marks (the keycap combiner), every symbol-other
# outside Latin-1 (emoji, dingbats, pictographs; © ® ° stay), the emoji
# skin-tone modifiers and the text/emoji variation selectors.
_DROPPED_CATEGORIES = frozenset({"Cc", "Cs", "Cf", "Me"})
_LATIN1_END = 0x100
_EMOJI_MODIFIERS = range(0x1F3FB, 0x1F400)
_VARIATION_SELECTORS = range(0xFE00, 0xFE10)


def pick_font(bold: bool = True) -> str:
    """Return the first caption font present, in the contract's order.

    bold: Impact, then any Impact/Anton face under FONT_ROOT, then Liberation
    Sans Bold, then DejaVu Sans Bold. Regular weight: Liberation Sans, DejaVu
    Sans (the display faces have no regular weight). Raises RuntimeError
    listing every searched path/pattern when nothing is installed.
    """
    searched = _font_search(bold)
    for pattern in searched:
        for hit in sorted(glob.glob(pattern, recursive=True)):
            if os.path.isfile(hit):
                return hit
    raise RuntimeError("no caption font found; searched: " + ", ".join(searched))


def _font_search(bold: bool) -> list[str]:
    truetype = os.path.join(FONT_ROOT, "truetype")
    if bold:
        return [
            os.path.join(truetype, "msttcorefonts", "Impact.ttf"),
            os.path.join(FONT_ROOT, "**", "*[Ii]mpact*.ttf"),
            os.path.join(FONT_ROOT, "**", "*[Aa]nton*.ttf"),
            os.path.join(truetype, "liberation", "LiberationSans-Bold.ttf"),
            os.path.join(truetype, "dejavu", "DejaVuSans-Bold.ttf"),
        ]
    return [
        os.path.join(truetype, "liberation", "LiberationSans-Regular.ttf"),
        os.path.join(truetype, "dejavu", "DejaVuSans.ttf"),
    ]


def sanitize_text(text: str) -> str:
    """Drop what no caption font can draw, collapse whitespace runs, cap at MAX_CHARS.

    Control characters go, and so do emoji and other pictographic symbols
    (see ``_drawable``): a caption with a 🙂 in it degrades to its words
    instead of showing a notdef box in the export.
    """
    kept = "".join(ch for ch in text if ch.isspace() or _drawable(ch))
    return " ".join(kept.split())[:MAX_CHARS].strip()


def _drawable(ch: str) -> bool:
    category = unicodedata.category(ch)
    if category in _DROPPED_CATEGORIES:
        return False
    code = ord(ch)
    if category == "So":
        return code < _LATIN1_END
    return code not in _EMOJI_MODIFIERS and code not in _VARIATION_SELECTORS


def render_caption(
    text: str,
    width: int,
    *,
    size: int = 64,
    color: str = "#ffffff",
    style: str = "impact-outline",
    font_path: str | None = None,
    pad: int = 12,
    max_height: int | None = None,
) -> Image.Image:
    """Draw ``text`` into a transparent RGBA band ``width`` px wide.

    Empty (or all-control/whitespace) text yields a 1 px tall transparent band.
    ``color`` must be ``#rrggbb``; ``style`` one of STYLES. Both raise
    ValueError otherwise, as does a ``max_height`` under 1.

    With ``max_height`` the band is at most that tall: the size ladder stops
    at the largest size whose wrapped lines fit, and when even MIN_SIZE is too
    tall the surplus lines are cut and the last kept line ends in ELLIPSIS.
    One line is always kept, so the bound holds whenever a single MIN_SIZE
    line plus padding fits inside it.
    """
    fill = _parse_color(color)
    if style not in STYLES:
        raise ValueError(f"unknown caption style {style!r} (known: {', '.join(STYLES)})")
    if width < 1 or size < 1 or pad < 0:
        raise ValueError(f"width and size must be >= 1 and pad >= 0 (got {width}, {size}, {pad})")
    if max_height is not None and max_height < 1:
        raise ValueError(f"max_height must be >= 1 or None, got {max_height}")

    clean = sanitize_text(text)
    if not clean:
        return Image.new("RGBA", (width, 1), TRANSPARENT)
    if STYLES[style]:
        clean = clean.upper()

    if font_path is None:
        font_path = pick_font(bold=True)
    elif not os.path.isfile(font_path):
        raise FileNotFoundError(f"caption font not found: {font_path}")

    max_w = width - 2 * pad
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    words = clean.split()
    for step in _size_ladder(size):
        font = ImageFont.truetype(font_path, step)
        measure = _measurer(probe, font)
        line_h, lead = _line_metrics(font)
        lines, overflow = _wrap(words, measure, max_w)
        if not overflow and _fits(len(lines), line_h, pad, max_height):
            break
    else:  # the floor size still does not fit: break long words, then cut lines
        if overflow:
            lines, _ = _wrap(_break_long(words, measure, max_w), measure, max_w)
        if not _fits(len(lines), line_h, pad, max_height):
            lines = _cut(lines, _max_lines(max_height, line_h, pad), measure, max_w)

    stroke = max(2, step // 16)
    band = Image.new("RGBA", (width, len(lines) * line_h + 2 * pad), TRANSPARENT)
    draw = ImageDraw.Draw(band)
    for i, line in enumerate(lines):
        x = (width - measure(line)) / 2
        y = pad + i * line_h + lead
        draw.text((x, y), line, font=font, fill=fill, anchor="la",
                  stroke_width=stroke, stroke_fill=STROKE_RGBA)
    return band


def write_caption_overlay(text: str, width: int, path: str, **kw) -> str:
    """Render the caption band and save it as a PNG at ``path``; returns ``path``."""
    render_caption(text, width, **kw).save(path, format="PNG")
    return path


def _parse_color(color: str) -> tuple[int, int, int, int]:
    match = _HEX_COLOR.fullmatch(color) if isinstance(color, str) else None
    if match is None:
        raise ValueError(f"colour must be '#rrggbb', got {color!r}")
    value = int(match.group(1), 16)
    return (value >> 16 & 0xFF, value >> 8 & 0xFF, value & 0xFF, 255)


def _size_ladder(size: int) -> list[int]:
    """Font sizes to try, largest first, always ending at MIN_SIZE (or below it)."""
    if size <= MIN_SIZE:
        return [size]
    return list(range(size, MIN_SIZE, -SHRINK_STEP)) + [MIN_SIZE]


def _measurer(probe: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont) -> Callable[[str], float]:
    return lambda s: probe.textlength(s, font=font)


def _line_metrics(font: ImageFont.FreeTypeFont) -> tuple[int, int]:
    """(line height, top lead): the font's natural height spaced by LINE_SPACING."""
    ascent, descent = font.getmetrics()
    natural = ascent + descent
    line_h = math.ceil(natural * LINE_SPACING)
    return line_h, (line_h - natural) // 2


def _fits(n_lines: int, line_h: int, pad: int, max_height: int | None) -> bool:
    return max_height is None or n_lines * line_h + 2 * pad <= max_height


def _max_lines(max_height: int, line_h: int, pad: int) -> int:
    return max(1, (max_height - 2 * pad) // line_h)


def _wrap(words: list[str], measure: Callable[[str], float], max_w: int) -> tuple[list[str], bool]:
    """Greedy word wrap. The flag is True when some single word exceeds max_w."""
    lines: list[str] = []
    current = ""
    overflow = False
    for word in words:
        candidate = f"{current} {word}" if current else word
        if measure(candidate) <= max_w:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        overflow = overflow or measure(word) > max_w
    if current:
        lines.append(current)
    return lines, overflow


def _break_long(words: list[str], measure: Callable[[str], float], max_w: int) -> list[str]:
    """Split any word wider than max_w into character runs that fit."""
    pieces: list[str] = []
    for word in words:
        run = ""
        for ch in word:
            if run and measure(run + ch) > max_w:
                pieces.append(run)
                run = ""
            run += ch
        pieces.append(run)
    return pieces


def _cut(lines: list[str], keep: int, measure: Callable[[str], float], max_w: int) -> list[str]:
    """The first ``keep`` lines, the last of them ellipsized to fit ``max_w``."""
    if len(lines) <= keep:
        return lines
    return lines[:keep - 1] + [_ellipsize(lines[keep - 1], measure, max_w)]


def _ellipsize(line: str, measure: Callable[[str], float], max_w: int) -> str:
    """``line`` shortened from its end until it fits ``max_w`` with ELLIPSIS appended."""
    text = line
    while text and measure(text.rstrip() + ELLIPSIS) > max_w:
        text = text[:-1]
    return text.rstrip() + ELLIPSIS
