#!/usr/bin/env python3
"""OPEN MIC — standalone one-off gag GIF, not part of the benchod_tg/weir_tg
roast series (no series_state.json entry, no epNN numbering). Illustrates a
user-supplied "below the line" statement about voice dictation and vibe
coding as a deliberately terrible hand-drawn office cartoon, cut with a few
real stock photos snapped in from Wikimedia Commons for the "match the
cartoon" bit. No real person is depicted — "GUY" is a generic invented
office character, not anyone from the group chats.

Photo credits (Wikimedia Commons, downloaded to this session's scratchpad,
not committed to the repo):
  - Call_Center_Agent.jpg — FiveOne51, CC BY-SA 3.0
  - Gulf Worldwide Sales & Marketing Team.jpg — MarkJaysonAranda, CC BY-SA 3.0
  - RustCodeOnScreen.jpg — Slashme, CC0 / public domain
"""
import math
import random
import subprocess

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

W, H = 720, 500
F = "/usr/share/fonts/truetype/dejavu/"

PAPER  = (244, 238, 222)
RULE   = (222, 213, 190)
MARGIN = (223, 150, 150)
INK    = (32, 28, 24)
RED    = (188, 40, 36)
SKIN   = (255, 213, 168)
HAIR   = (60, 42, 30)
DESK   = (150, 102, 58)
DESKTOP= (176, 128, 80)
SCREEN = (140, 205, 255)
SHIRT  = [(90, 130, 150), (150, 110, 140), (110, 140, 95), (150, 130, 80)]

PHOTOS = "/tmp/claude-1000/-home-tom-Documents-claude-CLIPWRIGHT-PLAN/c48d7999-3586-4ae7-926a-818b309a77d5/scratchpad/photos/"


def font(n, b=False, mono=False):
    if mono:
        return ImageFont.truetype(F + ("DejaVuSansMono-Bold.ttf" if b else "DejaVuSansMono.ttf"), n)
    return ImageFont.truetype(F + ("DejaVuSans-Bold.ttf" if b else "DejaVuSans.ttf"), n)


def wrap(d, text, f, maxw):
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


def jit(p, j):
    return (p[0] + random.uniform(-j, j), p[1] + random.uniform(-j, j))


def wline(d, p0, p1, fill=INK, width=3, j=2.2, segs=6):
    pts = []
    for i in range(segs + 1):
        t = i / segs
        pts.append(jit((p0[0] + (p1[0] - p0[0]) * t, p0[1] + (p1[1] - p0[1]) * t), j))
    d.line(pts, fill=fill, width=width, joint="curve")


def wellipse(d, cx, cy, rx, ry, fill=None, outline=None, width=3, j=2.0, steps=28):
    pts = [(cx + rx * math.cos(2 * math.pi * i / steps) + random.uniform(-j, j),
            cy + ry * math.sin(2 * math.pi * i / steps) + random.uniform(-j, j)) for i in range(steps)]
    if fill:
        d.polygon(pts, fill=fill)
    if outline:
        d.line(pts + [pts[0]], fill=outline, width=width, joint="curve")


def wpoly(d, pts, fill=None, outline=None, width=3, j=1.6):
    jp = [jit(p, j) for p in pts]
    if fill:
        d.polygon(jp, fill=fill)
    if outline:
        d.line(jp + [jp[0]], fill=outline, width=width, joint="curve")


def wrect(d, x0, y0, x1, y1, fill=None, outline=None, width=3, j=1.8):
    wpoly(d, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], fill=fill, outline=outline, width=width, j=j)


def ctext(d, s, cx, y, f, fill=INK):
    d.text((cx - d.textlength(s, font=f) / 2, y), s, font=f, fill=fill)


def shaky_ctext(d, s, cx, y, f, fill=INK, jitter=2.2, rot=6):
    tw = d.textlength(s, font=f)
    x = cx - tw / 2
    img = d._image if hasattr(d, "_image") else None
    for ch in s:
        cw = d.textlength(ch, font=f)
        if ch != " " and img is not None:
            tmp = Image.new("RGBA", (int(cw) + 14, f.size + 14), (0, 0, 0, 0))
            td = ImageDraw.Draw(tmp)
            td.text((7, 5), ch, font=f, fill=fill)
            tmp = tmp.rotate(random.uniform(-rot, rot), resample=Image.BICUBIC, expand=False)
            px = int(x - 7 + random.uniform(-jitter, jitter))
            py = int(y - 5 + random.uniform(-jitter, jitter))
            img.paste(tmp, (px, py), tmp)
        x += cw


def arrow(d, p0, p1, fill=INK, width=4):
    wline(d, p0, p1, fill, width=width, j=2.5, segs=8)
    ang = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
    for da in (2.5, -2.5):
        hx = p1[0] - 16 * math.cos(ang + da)
        hy = p1[1] - 16 * math.sin(ang + da)
        wline(d, p1, (hx, hy), fill, width=max(2, width - 1), j=1.2, segs=3)


def bubble(d, cx, top, w, h, lines, f, tail_to, style="speech", fill=(255, 255, 255)):
    wrect(d, cx - w / 2, top, cx + w / 2, top + h, fill=fill, outline=INK, width=3, j=2.2)
    if style == "speech":
        base = (cx, top + h - 6)
        wline(d, base, tail_to, INK, width=3, j=1.5, segs=4)
    else:
        mx, my = (cx + tail_to[0]) / 2, (top + h + tail_to[1]) / 2
        wellipse(d, mx, my, 7, 7, fill=fill, outline=INK, width=2)
        wellipse(d, tail_to[0], tail_to[1], 4, 4, fill=fill, outline=INK, width=2)
    lh = f.size + 5
    for i, l in enumerate(lines):
        ctext(d, l, cx, top + 9 + i * lh, f, INK)


def figure(d, cx, head_y, scale=1.0, mouth="closed", tears=0, sound=False, arm="type",
           headset=False, shirt=None, brow="flat", desk_y=None):
    r = 19 * scale
    shirt = shirt or SHIRT[0]
    neck = head_y + r * 0.95
    hem = desk_y if desk_y else neck + 60 * scale
    # torso
    wpoly(d, [(cx - 17 * scale, neck), (cx + 17 * scale, neck), (cx + 22 * scale, hem), (cx - 22 * scale, hem)],
          fill=shirt, outline=INK, width=3)
    # arms
    sh_y = neck + 6
    if arm == "type":
        wline(d, (cx - 14 * scale, sh_y), (cx - 30 * scale, hem - 4), width=4)
        wline(d, (cx + 14 * scale, sh_y), (cx + 30 * scale, hem - 4), width=4)
    elif arm == "mic":
        wline(d, (cx - 14 * scale, sh_y), (cx - 30 * scale, hem - 4), width=4)
        hx, hy = cx + 20 * scale, head_y + r * 0.35
        wline(d, (cx + 14 * scale, sh_y), (hx, hy), width=4)
        wline(d, (hx, hy), (hx + 3, hy - 14), fill=(50, 50, 55), width=5, j=0.8, segs=2)
        wellipse(d, hx + 4, hy - 17, 6, 7, fill=(35, 35, 40), outline=INK, width=2)
    elif arm == "point":
        wline(d, (cx - 14 * scale, sh_y), (cx - 30 * scale, hem - 4), width=4)
        wline(d, (cx + 14 * scale, sh_y), (cx + 44 * scale, sh_y - 26), width=4)
    # head
    wellipse(d, cx, head_y, r, r * 1.05, fill=SKIN, outline=INK, width=3)
    # hair
    wpoly(d, [(cx - r, head_y - r * 0.3), (cx - r * 0.8, head_y - r * 1.15),
              (cx + r * 0.8, head_y - r * 1.15), (cx + r, head_y - r * 0.3),
              (cx + r * 0.6, head_y - r * 0.7), (cx - r * 0.6, head_y - r * 0.7)],
          fill=HAIR, outline=INK, width=2)
    # brows
    by = head_y - r * 0.28
    ex = r * 0.38
    if brow == "angry":
        wline(d, (cx - ex - 6, by - 4), (cx - ex + 6, by + 3), width=3)
        wline(d, (cx + ex - 6, by + 3), (cx + ex + 6, by - 4), width=3)
    elif brow == "up":
        wline(d, (cx - ex - 6, by + 2), (cx - ex + 6, by - 5), width=3)
        wline(d, (cx + ex - 6, by - 5), (cx + ex + 6, by + 2), width=3)
    else:
        wline(d, (cx - ex - 6, by), (cx - ex + 6, by), width=3)
        wline(d, (cx + ex - 6, by), (cx + ex + 6, by), width=3)
    # eyes
    for s in (-1, 1):
        d.ellipse([cx + s * ex - 2.4, head_y - 3, cx + s * ex + 2.4, head_y + 3], fill=INK)
    # mouth
    my = head_y + r * 0.5
    if mouth == "yell":
        wellipse(d, cx, my, r * 0.42, r * 0.5, fill=(60, 25, 25), outline=INK, width=2)
    elif mouth == "laugh":
        pts = [(cx - r * 0.5, my - 3)]
        for i in range(1, 6):
            pts.append((cx - r * 0.5 + i * r * 0.2, my + (6 if i % 2 else -3)))
        d.line([jit(p, 1.4) for p in pts], fill=INK, width=3, joint="curve")
    elif mouth == "cry":
        pts = [(cx - r * 0.4, my + 4), (cx - r * 0.15, my - 2), (cx + r * 0.15, my - 2), (cx + r * 0.4, my + 4)]
        d.line([jit(p, 1.2) for p in pts], fill=INK, width=3, joint="curve")
    elif mouth == "o":
        wellipse(d, cx, my, r * 0.18, r * 0.22, fill=(60, 25, 25), outline=INK, width=2)
    else:
        wline(d, (cx - r * 0.28, my), (cx + r * 0.28, my), width=3)
    # tears
    for i in range(tears):
        tx = cx + (-1 if i % 2 == 0 else 1) * (r * 0.38)
        ty = head_y + 4 + i * 10
        wpoly(d, [(tx, ty), (tx - 4, ty + 10), (tx + 4, ty + 10)], fill=(120, 180, 230), outline=INK, width=2)
    # headset
    if headset:
        wline(d, (cx - r * 0.95, head_y - 2), (cx - r * 0.3, head_y - r * 1.05), width=3, j=1.5)
        wline(d, (cx - r * 0.3, head_y - r * 1.05), (cx + r * 0.5, head_y - r * 1.05), width=3, j=1.5)
        wellipse(d, cx - r * 0.95, head_y + 2, 7, 9, fill=(40, 40, 45), outline=INK, width=2)
    # sound burst
    if sound:
        for a in range(-2, 3):
            ang = math.radians(60 + a * 22)
            p0 = (cx + (r + 6) * math.cos(ang), my + (r + 6) * math.sin(ang) * 0.6)
            p1 = (cx + (r + 20 + random.uniform(-4, 4)) * math.cos(ang), my + (r + 20) * math.sin(ang) * 0.6)
            wline(d, p0, p1, fill=RED, width=3, j=1.5, segs=2)


def desk(d, cx, top_y, w=110, h=16, laptop=True, glow=False):
    wrect(d, cx - w / 2, top_y, cx + w / 2, top_y + h, fill=DESKTOP, outline=INK, width=3)
    wrect(d, cx - w / 2 + 6, top_y + h, cx - w / 2 + 16, top_y + h + 60, fill=DESK, outline=INK, width=2)
    wrect(d, cx + w / 2 - 16, top_y + h, cx + w / 2 - 6, top_y + h + 60, fill=DESK, outline=INK, width=2)
    if laptop:
        lw = 46
        wpoly(d, [(cx - lw / 2, top_y - 2), (cx + lw / 2, top_y - 2), (cx + lw / 2 + 5, top_y),
                  (cx - lw / 2 - 5, top_y)], fill=(70, 70, 76), outline=INK, width=2)
        wrect(d, cx - lw / 2, top_y - 38, cx + lw / 2, top_y - 2,
              fill=SCREEN if glow else (90, 90, 96), outline=INK, width=2)
        if glow:
            for i in range(3):
                yy = top_y - 33 + i * 10
                wline(d, (cx - lw / 2 + 5, yy), (cx + lw / 2 - 5 - i * 6, yy), fill=(30, 90, 60), width=1, j=0.5,
                      segs=2)


def paper_bg(d):
    d.rectangle([0, 0, W, H], fill=PAPER)
    for y in range(60, H, 26):
        d.line([(0, y), (W, y)], fill=RULE, width=1)
    d.line([(64, 0), (64, H)], fill=MARGIN, width=2)


def slug(d, text):
    d.text((78, 14), text, font=font(16, True, mono=True), fill=(90, 84, 74))
    d.line([(78, 36), (W - 30, 36)], fill=(90, 84, 74), width=1)


def caption_bar(d, lines, y0):
    for i, l in enumerate(lines):
        tw = d.textlength(l, font=font(17, True))
        d.rounded_rectangle([W / 2 - tw / 2 - 10, y0 + i * 24 - 3, W / 2 + tw / 2 + 10, y0 + i * 24 + 21],
                             radius=4, fill=(255, 255, 255))
        ctext(d, l, W / 2, y0 + i * 24, font(17, True), INK)


def scrapbook_photo(path, w, h, rot, sat=0.55):
    img = Image.open(path).convert("RGB")
    iw, ih = img.size
    scale = max(w / iw, h / ih)
    img = img.resize((max(1, int(iw * scale)), max(1, int(ih * scale))), Image.LANCZOS)
    iw, ih = img.size
    img = img.crop(((iw - w) // 2, (ih - h) // 2, (iw - w) // 2 + w, (ih - h) // 2 + h))
    img = ImageEnhance.Color(img).enhance(sat)
    img = ImageEnhance.Contrast(img).enhance(1.08)
    framed = Image.new("RGB", (w + 22, h + 22), (255, 255, 250))
    fd = ImageDraw.Draw(framed)
    framed.paste(img, (11, 11))
    wrect(fd, 4, 4, w + 18, h + 18, outline=INK, width=5, j=2.5)
    return framed.rotate(rot, resample=Image.BICUBIC, expand=True, fillcolor=PAPER)


def tape(w=46, h=18, rot=-18):
    tp = Image.new("RGBA", (w, h), (255, 244, 190, 210))
    td = ImageDraw.Draw(tp)
    td.rectangle([0, 0, w, h], outline=(180, 160, 90, 200), width=2)
    return tp.rotate(rot, expand=True)


class Reel:
    def __init__(self):
        self.frames, self.delays = [], []

    def base(self):
        img = Image.new("RGB", (W, H), PAPER)
        d = ImageDraw.Draw(img)
        paper_bg(d)
        return img, d

    def emit(self, img, ms):
        self.frames.append(img.convert("P", palette=Image.ADAPTIVE, colors=160))
        self.delays.append(ms)

    def paste_tape(self, img, cx, cy, rot=-18):
        tp = tape(rot=rot)
        img.paste(tp, (int(cx - tp.width / 2), int(cy - tp.height / 2)), tp)

    # ---- scene 1: title -------------------------------------------------
    def title(self):
        def paint(d, t):
            shaky_ctext(d, "OPEN MIC", W / 2, 140, font(74, True), INK, jitter=3, rot=8)
            if t > .4:
                ctext(d, "a screenplay, sort of, about a guy who vibe-codes very loudly", W / 2, 240, font(17), (80, 74, 64))
                ctext(d, "based on a true story someone told their computer", W / 2, 266, font(15), (120, 112, 98))
            wellipse(d, W / 2, 370, 40, 40, fill=(255, 255, 255), outline=INK, width=3)
            wline(d, (W / 2 - 4, 355), (W / 2 - 4, 340), width=3, j=1)
            for yy in (350, 358, 366):
                wline(d, (W / 2 - 14, yy), (W / 2 + 14, yy), width=2, j=1)
            wline(d, (W / 2 - 4, 400), (W / 2 - 4, 420), width=4, j=1)
        for i in range(9):
            img, d = self.base(); paint(d, i / 8); self.emit(img, 80)
        img, d = self.base(); paint(d, 1); self.emit(img, 1700)

    # ---- scene 2: establishing office -----------------------------------
    def office_establish(self):
        desks = [140, 300, 460, 600]
        def paint(d):
            slug(d, "INT. OPEN-PLAN OFFICE — DAY")
            for i, x in enumerate(desks):
                desk(d, x, 300, glow=True)
                figure(d, x, 236, 0.95, mouth="closed", arm="type", shirt=SHIRT[i % len(SHIRT)],
                       headset=(i == 1))
                for k in range(2):
                    wline(d, (x - 10 + k * 20, 200 - k * 6), (x - 6 + k * 20, 190 - k * 6), width=1, j=1, segs=2)
            ctext(d, "everybody's just quietly typing. a normal Tuesday.", W / 2, 420, font(16), (90, 84, 74))
        for i in range(7):
            img, d = self.base(); paint(d); self.emit(img, 90)
        img, d = self.base(); paint(d); self.emit(img, 1300)

    # ---- cutaway: real photo -------------------------------------------
    def cutaway(self, photo, caption, rot=-4, w=360, h=240, cy=250):
        img0 = scrapbook_photo(PHOTOS + photo, w, h, rot)
        def paint(d, img, scale):
            fw, fh = int(img0.width * scale), int(img0.height * scale)
            fw, fh = max(1, fw), max(1, fh)
            pic = img0.resize((fw, fh), Image.LANCZOS)
            img.paste(pic, (W // 2 - fw // 2, cy - fh // 2), pic if pic.mode == "RGBA" else None)
            self.paste_tape(img, W // 2 - w * 0.32, cy - h * 0.42, rot=-24)
            self.paste_tape(img, W // 2 + w * 0.32, cy - h * 0.42, rot=16)
            if scale >= 1:
                shaky_ctext(d, caption, W / 2, cy + h / 2 + 30, font(19, True), INK, jitter=1.6, rot=4)
        for i in range(6):
            img, d = self.base(); paint(d, img, 0.55 + 0.45 * (i + 1) / 6); self.emit(img, 55)
        img, d = self.base(); paint(d, img, 1); self.emit(img, 1900)

    # ---- scene: guy yells --------------------------------------------
    def yell(self):
        desks = [140, 300, 460, 600]
        def paint(d, mo):
            slug(d, "BACK IN THE OFFICE")
            for i, x in enumerate(desks):
                desk(d, x, 300, glow=True)
                if i == 1:
                    figure(d, x, 236, 1.05, mouth=("yell" if mo else "o"), brow="angry", arm="mic",
                           headset=True, shirt=SHIRT[i], sound=mo)
                else:
                    figure(d, x, 236, 0.95, mouth="o", brow="up", arm="type", shirt=SHIRT[i % len(SHIRT)])
            bubble(d, 300, 90, 300, 78,
                   wrap(d, "“EVERYONE WILL SLOWLY TRANSITION TO VOICE DICTATION!”", font(16, True), 270),
                   font(16, True), (300, 190), style="speech")
            for i, x in enumerate((140, 460, 600)):
                bubble(d, x, 60, 60, 34, ["?"], font(20, True), (x, 190), style="thought")
            caption_bar(d, ["“Everyone will slowly transition to voice dictation"], 445)
            caption_bar(d, ["over the next decade or two.”"], 469)
        for i in range(10):
            img, d = self.base(); paint(d, i % 2 == 0); self.emit(img, 90)
        img, d = self.base(); paint(d, True); self.emit(img, 1600)

    # ---- scene: coworkers react -----------------------------------------
    def react(self):
        def paint(d):
            slug(d, "CLOSE ON: THE REST OF THE ROOM")
            figure(d, 220, 260, 1.5, mouth="o", brow="up", arm="point", shirt=SHIRT[2])
            figure(d, 480, 260, 1.5, mouth="o", brow="up", arm="type", shirt=SHIRT[3])
            bubble(d, 350, 70, 320, 70, wrap(d, "why is he yelling into his mic", font(18), 290),
                   font(18), (350, 220), style="thought")
        for i in range(8):
            img, d = self.base(); paint(d); self.emit(img, 90)
        img, d = self.base(); paint(d); self.emit(img, 1700)

    # ---- scene: laughing while vibe coding -------------------------------
    def laugh(self):
        desks = [140, 300, 460, 600]
        def paint(d, burst):
            slug(d, "MEANWHILE, STILL VIBE CODING")
            for i, x in enumerate(desks):
                desk(d, x, 300, glow=True)
                if i == 1:
                    figure(d, x, 236, 1.05, mouth="laugh", brow="up", arm="mic", headset=True,
                           shirt=SHIRT[i], sound=burst)
                else:
                    figure(d, x, 236, 0.95, mouth="closed", arm="type", shirt=SHIRT[i % len(SHIRT)])
            if burst:
                shaky_ctext(d, "HA HA HA", 460, 130, font(30, True), RED, jitter=3, rot=10)
            caption_bar(d, ["“...these AI coding tools will start recognizing"], 445)
            caption_bar(d, ["tone and intonation, not just the raw words.”"], 469)
        for i in range(10):
            img, d = self.base(); paint(d, i % 2 == 0); self.emit(img, 90)
        img, d = self.base(); paint(d, True); self.emit(img, 1700)

    # ---- scene: crying while still vibe coding ---------------------------
    def cry(self):
        desks = [140, 300, 460, 600]
        def paint(d):
            slug(d, "SAME GUY. FIVE MINUTES LATER.")
            for i, x in enumerate(desks):
                desk(d, x, 300, glow=True)
                if i == 1:
                    figure(d, x, 236, 1.05, mouth="cry", brow="up", arm="mic", headset=True,
                           shirt=SHIRT[i], tears=2, sound=True)
                elif i == 0:
                    figure(d, x, 236, 0.95, mouth="laugh", arm="point", shirt=SHIRT[i % len(SHIRT)])
                else:
                    figure(d, x, 236, 0.95, mouth="o", brow="up", arm="type", shirt=SHIRT[i % len(SHIRT)])
            bubble(d, 140, 70, 130, 40, ["lol"], font(18, True), (140, 190), style="speech")
            caption_bar(d, ["“You can convey so much more context that way.”"], 445)
        for i in range(9):
            img, d = self.base(); paint(d); self.emit(img, 95)
        img, d = self.base(); paint(d); self.emit(img, 2100)

    # ---- scene: stinger ---------------------------------------------------
    def stinger(self):
        def paint(d, t, tail):
            shaky_ctext(d, "“AND HONESTLY, VIDEO INPUT", W / 2, 170, font(30, True), INK, jitter=2, rot=6)
            shaky_ctext(d, "IS COMING TOO.”", W / 2, 214, font(30, True), INK, jitter=2, rot=6)
            if tail:
                ctext(d, "the office has requested he keep his camera off.", W / 2, 300, font(17), (90, 84, 74))
                wline(d, (150, 400), (570, 400), width=3)
                ctext(d, "END", W / 2, 410, font(20, True), INK)
        for i in range(8):
            img, d = self.base(); paint(d, i / 7, False); self.emit(img, 90)
        img, d = self.base(); paint(d, 1, True); self.emit(img, 3200)

    def save(self, raw, final, colors=160):
        self.frames[0].save(raw, save_all=True, append_images=self.frames[1:],
                            duration=self.delays, loop=0, optimize=False, disposal=2)
        subprocess.run(["gifsicle", "-O2", "--careful", "--colors", str(colors), raw, "-o", final], check=True)
        return final


if __name__ == "__main__":
    r = Reel()
    r.title()
    r.office_establish()
    r.cutaway("office_cubicles.jpg", "(the actual office, for reference)", rot=-4, w=360, h=230, cy=250)
    r.yell()
    r.react()
    r.cutaway("call_center_agent.jpg", "(found this. close enough.)", rot=3, w=230, h=300, cy=260)
    r.laugh()
    r.cutaway("rust_code_screen.jpg", "(his screen, probably)", rot=-3, w=300, h=280, cy=250)
    r.cry()
    r.stinger()
    S = "/tmp/claude-1000/-home-tom-Documents-claude-CLIPWRIGHT-PLAN/c48d7999-3586-4ae7-926a-818b309a77d5/scratchpad/"
    print(r.save(S + "open_mic_raw.gif", "/home/tom/Documents/claude/CLIPWRIGHT_PLAN/open_mic_office.gif"))
