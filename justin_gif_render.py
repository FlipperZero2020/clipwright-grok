#!/usr/bin/env python3
"""The Ballad of @topo_chino — an animated GIF built from real benchod_tg facts."""
import math, os, subprocess
from PIL import Image, ImageDraw, ImageFont
from style_common import scratch_dir

ROOT = os.path.dirname(os.path.abspath(__file__))

W, H = 640, 640
F = "/usr/share/fonts/truetype/dejavu/"
def font(n, b=False, mono=False):
    if mono:
        return ImageFont.truetype(F + ("DejaVuSansMono-Bold.ttf" if b else "DejaVuSansMono.ttf"), n)
    return ImageFont.truetype(F + ("DejaVuSans-Bold.ttf" if b else "DejaVuSans.ttf"), n)

BG      = (23, 33, 43)
PANEL   = (24, 37, 51)
OUT     = (43, 82, 120)
TXT     = (236, 242, 248)
DIM     = (138, 158, 176)
ACCENT  = (100, 181, 239)
RED     = (229, 57, 53)
GOLD    = (255, 209, 102)

frames, delays = [], []
def emit(img, ms): frames.append(img.convert("P", palette=Image.ADAPTIVE, colors=128)); delays.append(ms)
def ease(t): return 1 - (1 - t) ** 3

def base():
    return Image.new("RGB", (W, H), BG)

def wrap(d, text, f, maxw):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f) <= maxw: cur = t
        else: lines.append(cur); cur = w
    if cur: lines.append(cur)
    return lines

def rrect(d, box, r, fill):
    d.rounded_rectangle(box, radius=r, fill=fill)

def unicycle(d, cx, cy, R, ang, color=GOLD):
    """Vector unicycle: wheel + rotating spokes + cranks + seat."""
    d.ellipse([cx-R, cy-R, cx+R, cy+R], outline=color, width=5)
    for i in range(8):
        a = ang + i * math.pi / 4
        d.line([cx, cy, cx + R*0.86*math.cos(a), cy + R*0.86*math.sin(a)], fill=color, width=2)
    d.ellipse([cx-5, cy-5, cx+5, cy+5], fill=color)
    # cranks + pedals, opposed
    for s in (0, math.pi):
        a = ang + s
        px, py = cx + R*0.42*math.cos(a), cy + R*0.42*math.sin(a)
        d.line([cx, cy, px, py], fill=color, width=4)
        d.line([px-7, py, px+7, py], fill=color, width=4)
    # fork + seat post + saddle
    d.line([cx, cy, cx, cy-R-38], fill=color, width=5)
    d.rounded_rectangle([cx-26, cy-R-52, cx+26, cy-R-36], radius=7, fill=color)

def title(d, s, y, f, col=TXT, cx=W//2):
    w = d.textlength(s, font=f)
    d.text((cx - w/2, y), s, font=f, fill=col)


# ── S1 · title ────────────────────────────────────────────────────────────────
f_big, f_sub, f_tiny = font(38, True), font(19), font(15)
def s1(d, t, tail=False):
    title(d, "THE BALLAD OF", 74, font(22, True), ACCENT)
    title(d, "@topo_chino", 108, f_big, TXT)
    if t > .35 or tail:
        title(d, "one man.  one unicycle.", 432, f_sub, DIM)
        title(d, "one denied FEMA claim.", 460, f_sub, DIM)
    if tail:
        title(d, "benchod_tg · est. whenever", 528, f_tiny, (90, 108, 124))
for i in range(26):
    t = ease(i/25)
    img = base(); d = ImageDraw.Draw(img)
    s1(d, t)
    unicycle(d, int(-90 + (W//2 + 90) * t), 320, 58, t * 7.5)
    emit(img, 55)
img = base(); d = ImageDraw.Draw(img)
s1(d, 1, tail=True); unicycle(d, W//2, 320, 58, 7.5)
emit(img, 1500)

# ── S2 · the chat (verbatim quotes) ───────────────────────────────────────────
f_msg, f_who = font(20), font(14, True)
CHAT = [
    ("Justin", "Devin I got rejected the fema money.", False),
    ("Justin", "I think I’m only supposed to have one unicycle for now", False),
    ("Devin",  "With the amount you spend to pimp out your van and rollerblades could’ve gotten one!", True),
]
def draw_chat(d, upto, slide=0.0):
    d.text((28, 26), "benchod_tg", font=font(18, True), fill=TXT)
    d.text((28, 50), "3 members, 2 unicycles pending", font=f_tiny, fill=DIM)
    d.line([20, 76, W-20, 76], fill=(38, 52, 66), width=2)
    y = 108
    for i, (who, msg, mine) in enumerate(CHAT[:upto]):
        tmp = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        lines = wrap(tmp, msg, f_msg, 372)
        bh = 30 + len(lines) * 27
        off = int(46 * (1 - slide)) if i == upto - 1 else 0
        bw = max(max(tmp.textlength(l, font=f_msg) for l in lines), 110) + 34
        x = (W - 28 - bw) if mine else 28
        rrect(d, [x, y + off, x + bw, y + off + bh], 16, OUT if mine else PANEL)
        d.text((x + 17, y + off + 7), who, font=f_who, fill=GOLD if not mine else ACCENT)
        for j, l in enumerate(lines):
            d.text((x + 17, y + off + 28 + j * 27), l, font=f_msg, fill=TXT)
        y += bh + 16
    return y

for step in range(1, 4):
    for i in range(9):
        img = base(); d = ImageDraw.Draw(img)
        draw_chat(d, step, ease(i/8)); emit(img, 45)
    img = base(); d = ImageDraw.Draw(img); draw_chat(d, step, 1.0)
    emit(img, 1500 if step < 3 else 2300)

# ── S3 · the denial ───────────────────────────────────────────────────────────
def fema(d, st1, st2):
    rrect(d, [52, 96, W-52, H-150], 14, (238, 238, 232))
    d.text((84, 130), "FEMA", font=font(30, True), fill=(40, 48, 58))
    d.text((84, 172), "DISASTER ASSISTANCE · HURRICANE BERYL", font=font(13, True), fill=(110, 120, 132))
    d.line([84, 200, W-84, 200], fill=(200, 202, 200), width=2)
    for i, (lab, sc) in enumerate((("HOUSING ASSISTANCE", st1), ("PERSONAL PROPERTY", st2))):
        yy = 236 + i * 118
        d.text((84, yy), lab, font=font(17), fill=(60, 68, 78))
        if sc <= 0: continue
        s = 1 + 1.9 * (1 - ease(min(sc, 1)))
        fs = max(8, int(34 * s))
        stamp = Image.new("RGBA", (330, 92), (0, 0, 0, 0)); sd = ImageDraw.Draw(stamp)
        ff = font(fs, True); tw = sd.textlength("DENIED", font=ff)
        sd.rectangle([8, 8, tw + 34, fs + 34], outline=RED + (255,), width=4)
        sd.text((21, 18), "DENIED", font=ff, fill=RED + (255,))
        stamp = stamp.rotate(-9, resample=Image.BICUBIC, expand=False)
        d._image.paste(stamp, (250, yy - 22), stamp)
    d.text((84, H-214), "“Devin I got rejected the fema money.”", font=font(15), fill=(120, 128, 138))

for i in range(8):
    img = base(); d = ImageDraw.Draw(img); fema(d, (i+1)/8, 0); emit(img, 45)
img = base(); d = ImageDraw.Draw(img); fema(d, 1, 0); emit(img, 500)
for i in range(8):
    img = base(); d = ImageDraw.Draw(img); fema(d, 1, (i+1)/8); emit(img, 45)
img = base(); d = ImageDraw.Draw(img); fema(d, 1, 1)
d.text((84, H-186), "unicycle #2: cancelled", font=font(16, True), fill=RED)
emit(img, 2500)

# ── S4 · the dossier ──────────────────────────────────────────────────────────
DOSSIER = [
    ("UNICYCLES OWNED",      "1  (FEMA permitting)"),
    ("ROLLERBLADES",         "yes — ask him about bearings"),
    ("VAN",                  "30 yrs old, endlessly pimped"),
    ("CAT",                  "adopted off death row"),
    ("WORK EMAIL",           "@fluenceanalytics.com"),
    ("LUNCHES OWED BY divan", "1  (USA beat Canada)"),
    ("AFFIDAVITS SWORN",     "1, under penalty of perjury"),
    ("ACOMP QUESTIONS",      "→ ask Justin"),
]
f_k, f_v = font(15, True, mono=True), font(15, mono=True)
def dossier(d, n):
    d.text((36, 34), "SUBJECT DOSSIER", font=font(24, True), fill=TXT)
    d.text((36, 68), "Justin · Stafford, TX · Fluence/Yokogawa", font=font(14), fill=ACCENT)
    d.line([32, 98, W-32, 98], fill=(46, 62, 78), width=2)
    for i, (k, v) in enumerate(DOSSIER[:n]):
        y = 124 + i * 52
        d.text((36, y), k, font=f_k, fill=DIM)
        d.text((36, y + 21), v, font=f_v, fill=GOLD if "1" in v[:2] or "yes" in v else TXT)
for n in range(1, len(DOSSIER) + 1):
    img = base(); d = ImageDraw.Draw(img); dossier(d, n); emit(img, 300)
img = base(); d = ImageDraw.Draw(img); dossier(d, len(DOSSIER)); emit(img, 2600)

# ── S5 · the counter ──────────────────────────────────────────────────────────
for i in range(30):
    img = base(); d = ImageDraw.Draw(img)
    unicycle(d, W//2, 250, 78, -i * 0.26)
    title(d, "UNICYCLE COUNT", 400, font(19, True), DIM)
    title(d, "1", 428, font(88, True), GOLD)
    title(d, "still emailing from @fluenceanalytics.com", 560, font(15), (96, 114, 130))
    emit(img, 55)
img = base(); d = ImageDraw.Draw(img)
unicycle(d, W//2, 250, 78, -30 * 0.26)
title(d, "UNICYCLE COUNT", 400, font(19, True), DIM)
title(d, "1", 428, font(88, True), GOLD)
title(d, "still emailing from @fluenceanalytics.com", 560, font(15), (96, 114, 130))
emit(img, 2600)

raw = scratch_dir() + "justin_raw.gif"
out = os.path.join(ROOT, "justin_ballad_of_topo_chino.gif")
frames[0].save(raw, save_all=True, append_images=frames[1:], duration=delays,
               loop=0, optimize=False, disposal=1)
subprocess.run(["gifsicle", "-O2", "--careful", "--colors", "100", raw, "-o", out], check=True)
print(out, len(frames), "frames")
