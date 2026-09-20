#!/usr/bin/env python3
"""tabloid_kit — the shared visual style for newsprint-style episodes.
Generalized from tyler_01_enquirer.py. One page that assembles itself,
then a STOP PRESS band slams in. See alex_01_register.py for usage.
"""
import math, random, warnings
from PIL import Image, ImageDraw

from style_common import GifRenderer, center_text, ease, load_font, wrap as _common_wrap

W, H = 600, 780
PAPER = (245, 241, 232)
INK   = (26, 24, 22)
RED   = (198, 32, 38)
GREY  = (108, 104, 98)
EDGE  = (206, 200, 188)


def fs(n, b=True):
    return load_font(n, bold=b, family="serif")


def fa(n, b=True):
    return load_font(n, bold=b, family="sans")


def clamp(t):
    return max(0.0, min(1.0, t))


def _make_paper():
    img = Image.new("RGB", (W, H), (232, 226, 214))
    d = ImageDraw.Draw(img)
    d.rectangle([16, 16, W - 16, H - 16], fill=PAPER, outline=EDGE, width=2)
    rnd = random.Random(7)
    for y in range(20, H - 20, 5):
        for x in range(20, W - 20, 5):
            if rnd.random() < .30:
                v = rnd.randint(222, 238)
                d.point((x + rnd.randint(0, 2), y + rnd.randint(0, 2)), fill=(v, v - 4, v - 12))
    return img


PAPER_BG = _make_paper()
probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))


def _ctr(d, s, y, f, col=INK, cx=W // 2):
    center_text(d, s, y, f, col, cx)


def _wrap(text, f, maxw):
    return _common_wrap(probe, text, f, maxw)


def _warn_overflow(what, lines, cap):
    """Text that wraps past what its slot can show is dropped (strips) or drawn
    out of view (columns' stagger cap, the footer band). Say so loudly, naming
    the slot and the words the reader will never see, instead of failing quietly."""
    if len(lines) > cap:
        warnings.warn("tabloid %s wraps to %d lines but only %d fit; not shown: %r"
                      % (what, len(lines), cap, " ".join(lines[cap:])), stacklevel=2)


def _slam(img, text, size, color, cx, cy, prog):
    p = ease(clamp(prog)); sc = 1 + 1.35 * (1 - p)
    f = fa(max(6, int(size * sc)))
    tw, th = probe.textlength(text, font=f), size * sc * 1.25
    lay = Image.new("RGBA", (int(tw) + 60, int(th) + 60), (0, 0, 0, 0))
    ImageDraw.Draw(lay).text((30, 20), text, font=f, fill=color + (int(255 * p),))
    img.paste(lay, (int(cx - lay.width / 2), int(cy - lay.height / 2)), lay)


def _starburst(img, lines, cx, cy, r, prog):
    p = ease(clamp(prog))
    if p <= 0 or not lines:
        return
    lay = Image.new("RGBA", (r * 4, r * 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    C = r * 2
    pts = []
    for i in range(28):
        a = i * math.pi / 14
        rad = r * (1.0 if i % 2 == 0 else 0.72)
        pts.append((C + rad * math.cos(a), C + rad * math.sin(a)))
    d.polygon(pts, fill=RED + (255,))
    safe_w = 2 * r * 0.72 * 0.82  # stay inside the star's inner "valleys", not just its spike tips
    widest = max(probe.textlength(s, font=fa(sz)) for s, sz in lines)
    scale = min(1.0, safe_w / widest) if widest else 1.0
    y0 = C - 13 * len(lines)
    for j, (s, sz) in enumerate(lines):
        ff = fa(max(8, int(sz * scale)))
        d.text((C - d.textlength(s, font=ff) / 2, y0 + j * 24), s, font=ff, fill=(255, 255, 255, 255))
    lay = lay.rotate(-14 + 22 * (1 - p), resample=Image.BICUBIC)
    lay = lay.resize((max(1, int(lay.width * (.35 + .65 * p))), max(1, int(lay.height * (.35 + .65 * p)))))
    img.paste(lay, (int(cx - lay.width / 2), int(cy - lay.height / 2)), lay)


class Tabloid(GifRenderer):
    emit_colors = 64

    def __init__(self, masthead, dateline, kicker, headline_lines, deck, col1, col2,
                 starburst_lines, starburst_pos, strips, also_inside, stop_press_lines):
        super().__init__()
        self.masthead, self.dateline, self.kicker = masthead, dateline, kicker
        self.headline_lines, self.deck = headline_lines, deck
        self.col1, self.col2 = col1, col2
        self.starburst_lines, self.starburst_pos = starburst_lines, starburst_pos
        self.strips, self.also_inside, self.stop_press_lines = strips, also_inside, stop_press_lines

    def _timeline(self):
        """The three beats every later element hangs off, in seconds: deck and
        columns (dbase), starburst and strips (sbase), the ALSO INSIDE band
        (also_base). `_page` paints from these and `_page_end` stops the frame
        loop from them, so retuning one can't leave the other behind."""
        dbase = 2.9 + max(0, len(self.headline_lines) - 3) * .45
        sbase = dbase + 1.1
        also_base = sbase + .4 + len(self.strips) * .35 + .5
        return dbase, sbase, also_base

    def _page_end(self):
        return self._timeline()[2] + .6

    def _strip_gap(self, shift):
        """Vertical pitch of the quote strips: 58px, squeezed to no less than 40px
        so a tall deck still fits them above the ALSO INSIDE band. Warns when even
        40px isn't enough and the last strip would run under the footer."""
        n = len(self.strips)
        top, limit = 492 + shift, H - 92 - 12  # 12px breathing room above the footer
        gap = min(58, max(40, (limit - top - 46) / (n - 1))) if n > 1 else 58
        if n and top + (n - 1) * gap + 46 > limit:
            warnings.warn("tabloid strips: %d strips need %dpx but only %dpx fit above the footer"
                          % (n, (n - 1) * gap + 46, limit - top), stacklevel=2)
        return gap

    def _page(self, t, stop=0.0):
        img = PAPER_BG.copy(); d = ImageDraw.Draw(img)
        if t > 0:
            d.rectangle([30, 34, W - 30, 39], fill=RED)
            _ctr(d, self.masthead, 48, fs(31), INK)
            d.rectangle([30, 92, W - 30, 95], fill=RED)
            if t > .6:
                _ctr(d, self.dateline, 102, fa(11), GREY)
        if t > 1:
            _ctr(d, self.kicker, 128, fa(13), RED)
        for i, line in enumerate(self.headline_lines):
            if t > 1.2 + i * .45:
                _slam(img, line, 46, INK, W // 2, 175 + i * 50, (t - (1.2 + i * .45)) / .45)
        d = ImageDraw.Draw(img)

        dbase, sbase, also_base = self._timeline()
        deck_lines = _wrap(self.deck, fs(19, False), W - 92)
        deck_h = len(deck_lines) * 26
        if t > dbase:
            for j, l in enumerate(deck_lines):
                _ctr(d, l, 300 + j * 26, fs(19, False), GREY)
            d.line([46, 306 + deck_h, W - 46, 306 + deck_h], fill=INK, width=2)
        body_y = 318 + deck_h
        shift = deck_h - 26  # extra vertical room used by a multi-line deck
        if t > dbase + .4:
            n = int(clamp((t - (dbase + .4)) / .8) * 12)
            for ci, col in enumerate((self.col1, self.col2)):
                x = 46 + ci * 206
                lines = _wrap(col, fa(11, False), 196)
                cap = 12 - ci * 6  # the stagger reveal's cap: col1 shows 12 rows at most, col2 6
                if self.strips:
                    # The first quote strip is painted (opaque) over the columns at
                    # 492 + shift, i.e. 148px below body_y whatever the deck height,
                    # so only the 9 rows above it are ever visible.
                    cap = min(cap, (492 + shift - body_y) // 16)
                _warn_overflow("col%d" % (ci + 1), lines, cap)
                for j, l in enumerate(lines):
                    if j + ci * 6 < n:
                        d.text((x, body_y + j * 16), l, font=fa(11, False), fill=(58, 54, 50))

        if t > sbase:
            cx, cy = self.starburst_pos
            _starburst(img, self.starburst_lines, cx, cy + shift, 56, (t - sbase) / .6)
            d = ImageDraw.Draw(img)

        gap = self._strip_gap(shift)
        for i, (tag, quote) in enumerate(self.strips):
            base = sbase + .4 + i * .35
            if t > base:
                p = ease(clamp((t - base) / .35))
                y = 492 + shift + i * gap
                x0 = int(46 - 400 * (1 - p))
                tagf = fa(11)
                tagw = max(78, int(probe.textlength(tag, font=tagf)) + 22)
                d.rectangle([x0, y, x0 + tagw, y + 46], fill=RED)
                d.text((x0 + 11, y + 16), tag, font=tagf, fill=(255, 255, 255))
                d.rectangle([x0 + tagw, y, x0 + 508, y + 46], fill=(236, 231, 220), outline=EDGE)
                lines = _wrap(quote, fs(13, False), 500 - tagw - 24)
                _warn_overflow("strip %r" % tag, lines, 2)
                lines = lines[:2]
                for j, l in enumerate(lines):
                    d.text((x0 + tagw + 14, y + (14 if len(lines) == 1 else 6) + j * 18), l, font=fs(13, False), fill=INK)

        if t > also_base:
            d.rectangle([30, H - 92, W - 30, H - 46], fill=INK)
            also_lines = _wrap(self.also_inside, fa(11), 500)
            _warn_overflow("also_inside", also_lines, 2)
            for j, l in enumerate(also_lines):
                _ctr(d, l, H - 84 + j * 17, fa(11), (245, 241, 232))

        if stop > 0:
            p = ease(clamp(stop))
            band = Image.new("RGBA", (760, 150), (0, 0, 0, 0))
            bd = ImageDraw.Draw(band)
            bd.rectangle([0, 26, 760, 124], fill=RED + (245,))
            bd.rectangle([0, 26, 760, 32], fill=(255, 255, 255, 255))
            bd.rectangle([0, 118, 760, 124], fill=(255, 255, 255, 255))
            f1, f2 = fa(15), fa(28)
            l1, l2 = self.stop_press_lines
            bd.text((380 - bd.textlength(l1, font=f1) / 2, 40), l1, font=f1, fill=(255, 220, 220, 255))
            bd.text((380 - bd.textlength(l2, font=f2) / 2, 62), l2, font=f2, fill=(255, 255, 255, 255))
            band = band.rotate(9, resample=Image.BICUBIC, expand=True)
            yy = int(-200 + (H // 2 + 200) * p)
            img.paste(band, (int(W / 2 - band.width / 2), yy - band.height // 2), band)
        return img

    def render(self):
        t = 0.0
        while t < self._page_end():
            self._emit(self._page(t), 55); t += 0.13
        end = self._page_end()
        self._emit(self._page(end), 2400)
        for i in range(9):
            self._emit(self._page(end, (i + 1) / 9), 45)
        self._emit(self._page(end, 1.0), 3800)

    def save(self, raw, final, colors=72):
        self.render()
        return super().save(raw, final, colors)
