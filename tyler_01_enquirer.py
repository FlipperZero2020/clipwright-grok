#!/usr/bin/env python3
"""THE QUINLAN ENQUIRER — a tabloid front page that assembles itself.
Different series, different style: newsprint portrait, not the dark ballad format.
Every headline and pull-quote is verbatim Tyler, from weir_tg.
"""
import math, random, subprocess
from PIL import Image, ImageDraw, ImageFont

W, H = 600, 780
F = "/usr/share/fonts/truetype/dejavu/"
PAPER = (245, 241, 232)
INK   = (26, 24, 22)
RED   = (198, 32, 38)
GREY  = (108, 104, 98)
EDGE  = (206, 200, 188)

def fs(n, b=True):  # serif
    return ImageFont.truetype(F + ("DejaVuSerif-Bold.ttf" if b else "DejaVuSerif.ttf"), n)
def fa(n, b=True):  # sans
    return ImageFont.truetype(F + ("DejaVuSans-Bold.ttf" if b else "DejaVuSans.ttf"), n)

def ease(t): return 1 - (1 - t) ** 3
def clamp(t): return max(0.0, min(1.0, t))

# ── static newsprint ─────────────────────────────────────────────────────────
def make_paper():
    img = Image.new("RGB", (W, H), (232, 226, 214))
    d = ImageDraw.Draw(img)
    d.rectangle([16, 16, W - 16, H - 16], fill=PAPER, outline=EDGE, width=2)
    rnd = random.Random(7)
    for y in range(20, H - 20, 5):          # halftone speckle
        for x in range(20, W - 20, 5):
            if rnd.random() < .30:
                v = rnd.randint(222, 238)
                d.point((x + rnd.randint(0, 2), y + rnd.randint(0, 2)), fill=(v, v - 4, v - 12))
    return img
PAPER_BG = make_paper()

probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
def ctr(d, s, y, f, col=INK, cx=W // 2):
    d.text((cx - probe.textlength(s, font=f) / 2, y), s, font=f, fill=col)

def wrap(text, f, maxw):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if probe.textlength(t, font=f) <= maxw: cur = t
        else: lines.append(cur); cur = w
    if cur: lines.append(cur)
    return lines

def slam(img, text, size, color, cx, cy, prog, rot=0):
    """Headline hammers down: oversized + fading into place."""
    p = ease(clamp(prog)); sc = 1 + 1.35 * (1 - p)
    f = fa(max(6, int(size * sc)))
    tw, th = probe.textlength(text, font=f), size * sc * 1.25
    lay = Image.new("RGBA", (int(tw) + 60, int(th) + 60), (0, 0, 0, 0))
    ImageDraw.Draw(lay).text((30, 20), text, font=f, fill=color + (int(255 * p),))
    if rot: lay = lay.rotate(rot, resample=Image.BICUBIC, expand=True)
    img.paste(lay, (int(cx - lay.width / 2), int(cy - lay.height / 2)), lay)

def starburst(img, lines, cx, cy, r, prog):
    p = ease(clamp(prog))
    if p <= 0: return
    lay = Image.new("RGBA", (r * 4, r * 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    C = r * 2
    pts = []
    for i in range(28):
        a = i * math.pi / 14
        rad = r * (1.0 if i % 2 == 0 else 0.72)
        pts.append((C + rad * math.cos(a), C + rad * math.sin(a)))
    d.polygon(pts, fill=RED + (255,))
    for j, (s, sz) in enumerate(lines):
        ff = fa(sz)
        d.text((C - d.textlength(s, font=ff) / 2, C - 26 + j * 24), s, font=ff, fill=(255, 255, 255, 255))
    lay = lay.rotate(-14 + 22 * (1 - p), resample=Image.BICUBIC)
    lay = lay.resize((max(1, int(lay.width * (.35 + .65 * p))), max(1, int(lay.height * (.35 + .65 * p)))))
    img.paste(lay, (int(cx - lay.width / 2), int(cy - lay.height / 2)), lay)

# ── page content (all verbatim) ──────────────────────────────────────────────
HEAD = ["TRUDEAU IS", "LEGIT DATING", "MY COUSIN"]
DECK = "“…has been for like a year now”"
COL1 = ("OTTAWA — A lawyer in this city has quietly been seeing the former "
        "Prime Minister for roughly one year, according to a single source.")
COL2 = ("That source is Tyler. The Enquirer has not independently verified this "
        "claim, or frankly any claim. Tyler says it is legit.")
STRIPS = [
    ("MARKETS",  "“I got a little over 70,000 shares so”"),
    ("CAREER",   "“gotta write 11 exams at $500 a pop”"),
    ("LABOUR",   "“I’m getting paid $180k a year to do it”"),
]
ALSO = "ALSO INSIDE:  the yellow Camaro  ·  a company truck  ·  18% in the account"

frames, delays = [], []
def emit(img, ms):
    frames.append(img.convert("P", palette=Image.ADAPTIVE, colors=64)); delays.append(ms)

def page(t, stop=0.0):
    """t = assembly progress in 'beats'; stop = STOP PRESS overlay progress."""
    img = PAPER_BG.copy(); d = ImageDraw.Draw(img)

    # masthead
    if t > 0:
        p = ease(clamp(t))
        d.rectangle([30, 34, W - 30, 39], fill=RED)
        ctr(d, "THE QUINLAN ENQUIRER", 48, fs(31), INK)
        d.rectangle([30, 92, W - 30, 95], fill=RED)
        if t > .6:
            ctr(d, "FORT McMURRAY, ALTA.  ·  SOLE SOURCE: TYLER  ·  50¢", 102, fa(11), GREY)

    # kicker + headline
    if t > 1:
        ctr(d, "W O R L D   E X C L U S I V E", 128, fa(13), RED)
    for i, line in enumerate(HEAD):
        if t > 1.2 + i * .45:
            slam(img, line, 46, INK, W // 2, 175 + i * 50, (t - (1.2 + i * .45)) / .45)
    d = ImageDraw.Draw(img)

    # deck + body
    if t > 2.9:
        p = clamp((t - 2.9) / .5)
        ctr(d, DECK, 318, fs(19, False), (70, 66, 62) if p > .5 else GREY)
        d.line([46, 352, W - 46, 352], fill=INK, width=2)
    if t > 3.3:
        n = int(clamp((t - 3.3) / .8) * 12)
        for ci, col in enumerate((COL1, COL2)):
            x = 46 + ci * 206
            for j, l in enumerate(wrap(col, fa(11, False), 196)):
                if j + ci * 6 < n:
                    d.text((x, 364 + j * 16), l, font=fa(11, False), fill=(58, 54, 50))

    # starburst
    if t > 4.0:
        starburst(img, [("70,000", 18), ("SHARES", 15)], 514, 448, 56, (t - 4.0) / .6)
        d = ImageDraw.Draw(img)

    # sidebar strips
    for i, (tag, quote) in enumerate(STRIPS):
        if t > 4.4 + i * .35:
            p = ease(clamp((t - (4.4 + i * .35)) / .35))
            y = 500 + i * 62
            x0 = int(46 - 400 * (1 - p))
            d.rectangle([x0, y, x0 + 78, y + 46], fill=RED)
            d.text((x0 + 8, y + 16), tag, font=fa(11), fill=(255, 255, 255))
            d.rectangle([x0 + 78, y, x0 + 508, y + 46], fill=(236, 231, 220), outline=EDGE)
            for j, l in enumerate(wrap(quote, fs(13, False), 396)[:2]):
                d.text((x0 + 92, y + (14 if len(wrap(quote, fs(13, False), 396)) == 1 else 6) + j * 18),
                       l, font=fs(13, False), fill=INK)

    # also-inside strip
    if t > 5.6:
        d.rectangle([30, H - 92, W - 30, H - 46], fill=INK)
        for j, l in enumerate(wrap(ALSO, fa(11), 500)):
            ctr(d, l, H - 84 + j * 17, fa(11), (245, 241, 232))

    # STOP PRESS
    if stop > 0:
        p = ease(clamp(stop))
        band = Image.new("RGBA", (760, 150), (0, 0, 0, 0))
        bd = ImageDraw.Draw(band)
        bd.rectangle([0, 26, 760, 124], fill=RED + (245,))
        bd.rectangle([0, 26, 760, 32], fill=(255, 255, 255, 255))
        bd.rectangle([0, 118, 760, 124], fill=(255, 255, 255, 255))
        f1, f2 = fa(15), fa(31)
        bd.text((380 - bd.textlength("STOP PRESS", font=f1) / 2, 40), "STOP PRESS",
                font=f1, fill=(255, 220, 220, 255))
        bd.text((380 - bd.textlength("DUMPED FOR KATY PERRY", font=f2) / 2, 62),
                "DUMPED FOR KATY PERRY", font=f2, fill=(255, 255, 255, 255))
        band = band.rotate(9, resample=Image.BICUBIC, expand=True)
        yy = int(-200 + (H // 2 + 200) * p)
        img.paste(band, (int(W / 2 - band.width / 2), yy - band.height // 2), band)
    return img

# ── timeline ─────────────────────────────────────────────────────────────────
t = 0.0
while t < 6.3:                       # assembly
    emit(page(t), 55); t += 0.13
emit(page(6.3), 2400)                # hold the finished page
for i in range(9):                   # stop press slams in
    emit(page(6.3, (i + 1) / 9), 45)
emit(page(6.3, 1.0), 3800)           # final hold

raw = "/tmp/claude-1000/-home-tom-Documents-claude-CLIPWRIGHT-PLAN/3b825699-41ec-46e0-9dd6-60fabb208cc9/scratchpad/tyler_raw.gif"
out = "/home/tom/Documents/claude/CLIPWRIGHT_PLAN/tyler_01_quinlan_enquirer.gif"
frames[0].save(raw, save_all=True, append_images=frames[1:], duration=delays,
               loop=0, optimize=False, disposal=1)
subprocess.run(["gifsicle", "-O2", "--careful", "--colors", "72", raw, "-o", out], check=True)
print(out, len(frames), "frames")
