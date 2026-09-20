#!/usr/bin/env python3
"""meme_kit — the memegen-backed style: real quotes stamped onto stock meme
templates pulled live from api.memegen.link (github.com/jacebrowning/memegen),
then reeled into a short slideshow GIF with the series' own frame/kicker/
stinger treatment. Needs network access at render time — every other kit in
this project draws its own pixels and runs fully offline; this one doesn't.

Browse https://api.memegen.link/templates/ for template ids before writing
an episode. See an existing epNN_*.py using kit_module "meme_kit" for usage.
"""
import io
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw

from style_common import GifRenderer, center_text, ease, load_font, wrap as _wrap

W = H = 640
BG  = (18, 18, 20)
BAR = (198, 32, 38)
TXT = (240, 240, 240)
DIM = (170, 170, 176)

API = "https://api.memegen.link/images"


def font(n, b=False):
    return load_font(n, bold=b, family="sans")


def _ctr(d, s, y, f, col=TXT, cx=W // 2):
    center_text(d, s, y, f, col, cx)


def _encode(text):
    # memegen's path-segment escaping — order matters: literal _ and - first,
    # then space -> _, then the punctuation shorthands. See their API docs.
    text = text.replace("_", "__").replace("-", "--")
    text = text.replace("\n", "~n").replace('"', "''")
    text = text.replace("?", "~q").replace("%", "~p").replace("#", "~h").replace("/", "~s")
    text = text.replace(" ", "_")
    return urllib.parse.quote(text, safe="_-~'")


def fetch(template, top, bottom=None):
    """Fetch one rendered meme (real quote -> stock template) as an RGB Image."""
    path = "%s/%s" % (template, _encode(top))
    if bottom:
        path += "/%s" % _encode(bottom)
    url = "%s/%s.png" % (API, path)
    req = urllib.request.Request(url, headers={"User-Agent": "clipwright-plan/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
    except urllib.error.URLError as e:
        raise RuntimeError(
            "meme_kit needs network access to api.memegen.link and the fetch "
            "failed (template=%r): %s" % (template, e)
        ) from e
    return Image.open(io.BytesIO(data)).convert("RGB")


class MemeReel(GifRenderer):
    def __init__(self, kicker, footer=""):
        super().__init__()
        self.kicker, self.footer = kicker, footer

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _base(self):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, 54], fill=BAR)
        _ctr(d, self.kicker, 16, font(18, True), (255, 255, 255))
        return img, d

    # ── card: one real quote stamped on one template ────────────────────────
    def card(self, template, top, bottom="", cap="", hold=2200, intro_frames=10):
        meme = fetch(template, top, bottom)
        area_h = H - 132
        mw, mh = meme.size
        scale = min((W - 40) / mw, area_h / mh)
        nw, nh = max(1, int(mw * scale)), max(1, int(mh * scale))
        thumb = meme.resize((nw, nh), Image.LANCZOS)
        oy = 70 + (area_h - nh) // 2

        def paint(d, img, p, tail=False):
            sc = 1 + .5 * (1 - ease(min(p, 1)))
            t2 = thumb if p >= 1 else thumb.resize(
                (max(1, int(nw * sc)), max(1, int(nh * sc))), Image.LANCZOS)
            img.paste(t2, (W // 2 - t2.width // 2, oy + nh // 2 - t2.height // 2))
            if tail and cap:
                lines = _wrap(d, cap, font(16), W - 60)
                for i, l in enumerate(lines):
                    _ctr(d, l, H - 26 - (len(lines) - i) * 20, font(16), DIM)

        for i in range(intro_frames):
            img, d = self._base(); paint(d, img, i / max(1, intro_frames - 1)); self._emit(img, 45)
        img, d = self._base(); paint(d, img, 1, True); self._emit(img, hold)

    # ── stinger: closing beat, same job as ballad's counter / tabloid's STOP PRESS
    def stinger(self, line1, line2, hold=3000):
        def paint(d, p):
            _ctr(d, line1, 260, font(24, True), DIM)
            f = font(int(52 * (1 + 1.2 * (1 - ease(min(p, 1))))), True)
            _ctr(d, line2, 320, f, (255, 255, 255))
            if self.footer:
                _ctr(d, self.footer, H - 50, font(14), DIM)

        for i in range(12):
            img, d = self._base(); paint(d, i / 11); self._emit(img, 45)
        img, d = self._base(); paint(d, 1); self._emit(img, hold)
