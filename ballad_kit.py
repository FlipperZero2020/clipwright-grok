#!/usr/bin/env python3
"""ballad_kit — the shared visual style for the @topo_chino GIF series.

Five scenes, always in this order: title · chat · document · dossier · counter.
An episode is just data + an emblem drawing function. See justin_gif_render.py (the original) and ep06_aaron.py.
"""
import math
from PIL import Image, ImageDraw

from style_common import GifRenderer, center_text, ease, load_font, wrap

W = H = 640

BG     = (23, 33, 43)
PANEL  = (24, 37, 51)
OUT    = (43, 82, 120)
TXT    = (236, 242, 248)
DIM    = (138, 158, 176)
ACCENT = (100, 181, 239)
RED    = (229, 57, 53)
GOLD   = (255, 209, 102)
PAPER  = (238, 238, 232)
INK    = (40, 48, 58)
INK2   = (110, 120, 132)


def font(n, b=False, mono=False):
    return load_font(n, bold=b, family="mono" if mono else "sans")


class Ballad(GifRenderer):
    # ── plumbing ─────────────────────────────────────────────────────────────
    def _base(self):
        img = Image.new("RGB", (W, H), BG)
        return img, ImageDraw.Draw(img)

    def _ctr(self, d, s, y, f, col=TXT):
        center_text(d, s, y, f, col, W // 2)

    # ── scene 1 · title ──────────────────────────────────────────────────────
    def title(self, kicker, headline, tagline, footer, emblem):
        def paint(d, t, tail=False):
            self._ctr(d, kicker, 74, font(22, True), ACCENT)
            self._ctr(d, headline, 108, font(38, True), TXT)
            if t > .35 or tail:
                for i, l in enumerate(tagline):
                    self._ctr(d, l, 432 + i * 28, font(19), DIM)
            if tail:
                self._ctr(d, footer, 528, font(15), (90, 108, 124))
        for i in range(26):
            t = ease(i / 25)
            img, d = self._base(); paint(d, t)
            emblem(d, int(-90 + (W // 2 + 90) * t), 320, 58, t * 7.5)
            self._emit(img, 55)
        img, d = self._base(); paint(d, 1, True)
        emblem(d, W // 2, 320, 58, 7.5)
        self._emit(img, 1500)

    # ── scene 2 · chat ───────────────────────────────────────────────────────
    def chat(self, header, sub, msgs):
        f_msg, f_who = font(20), font(14, True)
        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))

        def paint(d, upto, slide):
            d.text((28, 26), header, font=font(18, True), fill=TXT)
            d.text((28, 50), sub, font=font(15), fill=DIM)
            d.line([20, 76, W - 20, 76], fill=(38, 52, 66), width=2)
            y = 108
            for i, (who, msg, mine) in enumerate(msgs[:upto]):
                lines = wrap(probe, msg, f_msg, 372)
                bh = 30 + len(lines) * 27
                off = int(46 * (1 - slide)) if i == upto - 1 else 0
                bw = max(max(probe.textlength(l, font=f_msg) for l in lines), 110) + 34
                x = (W - 28 - bw) if mine else 28
                d.rounded_rectangle([x, y + off, x + bw, y + off + bh], radius=16,
                                    fill=OUT if mine else PANEL)
                d.text((x + 17, y + off + 7), who, font=f_who, fill=ACCENT if mine else GOLD)
                for j, l in enumerate(lines):
                    d.text((x + 17, y + off + 28 + j * 27), l, font=f_msg, fill=TXT)
                y += bh + 16

        for step in range(1, len(msgs) + 1):
            for i in range(9):
                img, d = self._base(); paint(d, step, ease(i / 8)); self._emit(img, 45)
            img, d = self._base(); paint(d, step, 1.0)
            self._emit(img, 1500 if step < len(msgs) else 2300)

    # ── scene 3 · document ───────────────────────────────────────────────────
    def document(self, brand, sub, lede, rows, quote, verdict):
        def stamp_img(text, scale):
            base = 34 if len(text) <= 6 else max(16, int(34 * 6 / len(text)))
            fs = max(8, int(base * scale))
            ff = font(fs, True)
            probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
            tw = probe.textlength(text, font=ff)
            im = Image.new("RGBA", (int(tw) + 90, fs + 80), (0, 0, 0, 0))
            sd = ImageDraw.Draw(im)
            sd.rectangle([8, 8, tw + 34, fs + 34], outline=RED + (255,), width=4)
            sd.text((21, 18), text, font=ff, fill=RED + (255,))
            return im.rotate(-9, resample=Image.BICUBIC, expand=False)

        def paint(d, img, prog, done_verdict=False):
            d.rounded_rectangle([52, 96, W - 52, H - 120], radius=14, fill=PAPER)
            d.text((84, 130), brand, font=font(30, True), fill=INK)
            d.text((84, 172), sub, font=font(13, True), fill=INK2)
            d.line([84, 200, W - 84, 200], fill=(200, 202, 200), width=2)
            for i, l in enumerate(wrap(d, lede, font(13), W - 200)):
                d.text((84, 212 + i * 19), l, font=font(13), fill=INK2)
            for i, (lab, st) in enumerate(rows):
                yy = 268 + i * 102
                d.text((84, yy), lab, font=font(17), fill=(60, 68, 78))
                if prog[i] <= 0:
                    continue
                s = 1 + 1.9 * (1 - ease(min(prog[i], 1)))
                sti = stamp_img(st, s)
                img.paste(sti, (W - 76 - sti.width, yy - 22), sti)
            d.text((84, H - 184), quote, font=font(15), fill=(120, 128, 138))
            if done_verdict:
                d.text((84, H - 156), verdict, font=font(16, True), fill=RED)

        prog = [0.0] * len(rows)
        for r in range(len(rows)):
            for i in range(8):
                prog[r] = (i + 1) / 8
                img, d = self._base(); paint(d, img, prog); self._emit(img, 45)
            img, d = self._base(); paint(d, img, prog); self._emit(img, 500)
        img, d = self._base(); paint(d, img, prog, True); self._emit(img, 2500)

    # ── scene 4 · dossier ────────────────────────────────────────────────────
    def dossier(self, heading, sub, rows):
        f_k, f_v = font(15, True, mono=True), font(15, mono=True)

        def paint(d, n):
            d.text((36, 34), heading, font=font(24, True), fill=TXT)
            d.text((36, 68), sub, font=font(14), fill=ACCENT)
            d.line([32, 98, W - 32, 98], fill=(46, 62, 78), width=2)
            for i, (k, v, hot) in enumerate(rows[:n]):
                y = 124 + i * 52
                d.text((36, y), k, font=f_k, fill=DIM)
                d.text((36, y + 21), v, font=f_v, fill=GOLD if hot else TXT)

        for n in range(1, len(rows) + 1):
            img, d = self._base(); paint(d, n); self._emit(img, 300)
        img, d = self._base(); paint(d, len(rows)); self._emit(img, 2600)

    # ── scene 5 · counter ────────────────────────────────────────────────────
    def counter(self, label, value, footer, emblem):
        def paint(d, ang):
            emblem(d, W // 2, 250, 78, ang)
            self._ctr(d, label, 400, font(19, True), DIM)
            self._ctr(d, value, 428, font(88, True), GOLD)
            self._ctr(d, footer, 560, font(15), (96, 114, 130))
        for i in range(30):
            img, d = self._base(); paint(d, -i * 0.26); self._emit(img, 55)
        img, d = self._base(); paint(d, -30 * 0.26); self._emit(img, 2600)
