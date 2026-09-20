#!/usr/bin/env python3
"""style_common — plumbing shared by every *_kit.py (ballad, tabloid, meme).

Pure layout/encoding helpers only: font loading, easing, word-wrap, centered
text, and the frame-buffer -> two-pass-gifsicle GIF export every kit ends
with. No subject-specific palette, dimensions, or scene logic lives here —
that stays in each kit so they can keep diverging visually.
"""
import atexit
import os
import shutil
import subprocess
import tempfile
from PIL import Image, ImageFont

FONT_DIR = "/usr/share/fonts/truetype/dejavu/"

_FAMILIES = {
    "sans": ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
    "serif": ("DejaVuSerif.ttf", "DejaVuSerif-Bold.ttf"),
    "mono": ("DejaVuSansMono.ttf", "DejaVuSansMono-Bold.ttf"),
}


def load_font(size, bold=False, family="sans"):
    regular, bold_name = _FAMILIES[family]
    return ImageFont.truetype(FONT_DIR + (bold_name if bold else regular), size)


def ease(t):
    """Cubic ease-out — fast start, gentle settle. Used for every slide/grow/fade."""
    return 1 - (1 - t) ** 3


def wrap(d, text, f, maxw):
    """Greedy word-wrap of text into lines <= maxw px wide in font f, measured via d.
    A single word wider than maxw gets a line of its own — never a blank line first."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f) <= maxw:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def center_text(d, s, y, f, col, cx):
    d.text((cx - d.textlength(s, font=f) / 2, y), s, font=f, fill=col)


_scratch = None


def scratch_dir():
    """A private temp dir for an episode script's intermediate raw.gif before the
    gifsicle squeeze: a fresh owner-only mkdtemp, made once per process and removed
    at exit, so raw frames never sit at a predictable shared /tmp path and never
    pile up between runs. Scripts stay re-runnable because nothing here is tied to
    a session. Returns a path ending in '/', matching every call site's
    `S + "foo_raw.gif"` string-concat style.
    """
    global _scratch
    if _scratch is None:
        _scratch = tempfile.mkdtemp(prefix="clipwright_render_")
        atexit.register(shutil.rmtree, _scratch, ignore_errors=True)
    return _scratch + "/"


class GifRenderer:
    """Frame buffer plus the palette-convert -> gifsicle squeeze every kit shares.

    Subclasses that need a non-default per-frame palette size (tabloid_kit
    uses a tighter 64-color palette for its newsprint texture) override the
    `emit_colors` class attribute rather than re-implementing `_emit`.
    """
    emit_colors = 128

    def __init__(self):
        self.frames, self.delays = [], []

    def _emit(self, img, ms):
        self.frames.append(img.convert("P", palette=Image.ADAPTIVE, colors=self.emit_colors))
        self.delays.append(ms)

    def save(self, raw, final, colors=100):
        """Dump the frames to `raw`, squeeze them into `final` with gifsicle, and
        drop the throwaway `raw` once the squeeze has succeeded."""
        self.frames[0].save(raw, save_all=True, append_images=self.frames[1:],
                            duration=self.delays, loop=0, optimize=False, disposal=1)
        subprocess.run(["gifsicle", "-O2", "--careful", "--colors", str(colors),
                        raw, "-o", final], check=True)
        try:
            os.remove(raw)
        except OSError:
            pass
        return final
