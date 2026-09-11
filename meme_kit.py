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
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw, ImageFont

W = H = 640
F = "/usr/share/fonts/truetype/dejavu/"
BG  = (18, 18, 20)
BAR = (198, 32, 38)
TXT = (240, 240, 240)
DIM = (170, 170, 176)

API = "https://api.memegen.link/images"


def font(n, b=False):
    return ImageFont.truetype(F + ("DejaVuSans-Bold.ttf" if b else "DejaVuSans.ttf"), n)


def ease(t):
    return 1 - (1 - t) ** 3


def _ctr(d, s, y, f, col=TXT, cx=W // 2):
    d.text((cx - d.textlength(s, font=f) / 2, y), s, font=f, fill=col)


def _wrap(d, text, f, maxw):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f) <= maxw:
            cur = t
        else:
            lines.append(cur); cur = w
    if cur:
        lines.append(cur)
    return lines


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


class MemeReel:
    def __init__(self, kicker, footer=""):
        self.kicker, self.footer = kicker, footer
        self.frames, self.delays = [], []

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _emit(self, img, ms):
        self.frames.append(img.convert("P", palette=Image.ADAPTIVE, colors=128))
        self.delays.append(ms)

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
            img, d = self._base(); paint(d, img, ease(i / max(1, intro_frames - 1))); self._emit(img, 45)
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
            img, d = self._base(); paint(d, ease(i / 11)); self._emit(img, 45)
        img, d = self._base(); paint(d, 1); self._emit(img, hold)

    # ── output ───────────────────────────────────────────────────────────────
    def save(self, raw, final, colors=100):
        self.frames[0].save(raw, save_all=True, append_images=self.frames[1:],
                            duration=self.delays, loop=0, optimize=False, disposal=1)
        subprocess.run(["gifsicle", "-O2", "--careful", "--colors", str(colors),
                        raw, "-o", final], check=True)
        return final
