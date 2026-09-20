#!/usr/bin/env python3
"""profile_icon — GitHub-identicon-style avatar set for FlipperZero2020.

Same geometry GitHub uses for default avatars (420px, #F0F0F0 ground, 5x5
grid of 70px cells, 35px margin, left/right mirrored, one pastel HSL colour
per icon). The GIF opens on the identicon GitHub derives from the account id,
then card-flips cell by cell — centre ring outward, so the mirror symmetry
never breaks — through six glyphs for the project areas, and back:

    valve   gate-valve bowtie        (valve tagging / BOM / P&ID work)
    bubble  P&ID instrument bubble
    bot     Telegram bot face        (Ben_Chod_9000, CLIPWRIGHT front door)
    film    film strip               (GIF / video foundry)
    voice   sound bars               (clonin voice-clone TTS)
    chip    GPU die with pins        (the 4060 Ti box)

Outputs land in profile_icon/: avatar.gif (animated, for a profile README),
one PNG per glyph at 2x for the actual avatar slot, and a labelled sheet.
"""
import colorsys
import hashlib
import math
import os

from PIL import Image, ImageDraw

from style_common import GifRenderer, ease, load_font, scratch_dir

GITHUB_ID = "121824579"          # gh api user --jq .id — what GitHub hashes, not the login
BG = (240, 240, 240)
SIZE, CELL, MARGIN = 420, 70, 35
SS = 2                           # supersample, then LANCZOS down for clean flip edges

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profile_icon")

# Left three columns only; column 3 mirrors 1 and 4 mirrors 0, like the real thing.
GLYPHS = [
    ("valve",  ["X..", "XX.", "XXX", "XX.", "X.."], 212),
    ("bubble", [".XX", "X..", "X.X", "X..", ".XX"], 160),
    ("bot",    ["..X", "XXX", "X.X", "XXX", ".X."], 268),
    ("film",   ["X.X", "XXX", "X..", "XXX", "X.X"],  24),
    ("voice",  ["..X", "X.X", "X.X", "X.X", "..X"], 330),
    ("chip",   [".X.", "XXX", "XX.", "XXX", ".X."],  98),
]
SAT, LUM = 58, 60                # inside GitHub's 45-65 / 55-75 identicon bands

HOLD_MS, FLIP_FRAMES, FRAME_MS = 900, 16, 40


def hsl(h, s, l):
    r, g, b = colorsys.hls_to_rgb(h / 360, l / 100, s / 100)
    return tuple(int(round(v * 255)) for v in (r, g, b))


def mirror(rows):
    return [[1 if ch == "X" else 0 for ch in (r + r[1::-1])] for r in rows]


def identicon(seed):
    """GitHub's default-avatar recipe: md5, 15 even/odd nibbles for the cells,
    hue/sat/lum from the tail bytes."""
    h = hashlib.md5(seed.encode()).digest()
    nib = [n for b in h for n in (b >> 4, b & 15)]
    cells = [[0] * 5 for _ in range(5)]
    for i in range(15):
        x, y = i // 5, i % 5
        cells[y][x] = cells[y][4 - x] = int(nib[i] % 2 == 0)
    hue = (((h[12] & 0x0F) << 8) | h[13]) / 4095 * 360
    sat = 65 - h[14] / 255 * 20
    lum = 75 - h[15] / 255 * 20
    return cells, hsl(hue, sat, lum)


def draw_icon(cells, col, width=None, colors=None, scale=SS):
    """width[r][c] in 0..1 = horizontal card-flip amount (1 = full cell);
    colors[r][c] overrides `col` per cell mid-flip."""
    s = SIZE * scale
    img = Image.new("RGB", (s, s), BG)
    d = ImageDraw.Draw(img)
    cs, m = CELL * scale, MARGIN * scale
    for r in range(5):
        for c in range(5):
            w = width[r][c] if width else cells[r][c]
            if w <= 0:
                continue
            cx = m + c * cs + cs / 2
            x0, x1 = cx - cs * w / 2, cx + cs * w / 2
            if x1 - x0 < 1:
                continue
            y0 = m + r * cs
            d.rectangle([x0, y0, x1 - 1, y0 + cs - 1],
                        fill=(colors[r][c] if colors else col))
    if scale != 1:
        img = img.resize((SIZE, SIZE), Image.LANCZOS)
    return img


def flip_frame(a, ca, b, cb, t):
    """Between icon a and icon b at t in 0..1. Each cell flips about its own
    vertical axis; rings start 0.22 apart from the centre out."""
    width = [[0.0] * 5 for _ in range(5)]
    colors = [[ca] * 5 for _ in range(5)]
    for r in range(5):
        for c in range(5):
            ring = max(abs(r - 2), abs(c - 2))
            lt = min(1.0, max(0.0, (t - ring * 0.22) / 0.56))
            was, now = a[r][c], b[r][c]
            if was and now:
                # full flip: close on old colour, open on new
                if lt < 0.5:
                    width[r][c] = 1 - ease(lt * 2)
                else:
                    width[r][c] = ease((lt - 0.5) * 2); colors[r][c] = cb
            elif was:
                width[r][c] = 1 - ease(lt)
            elif now:
                width[r][c] = ease(lt); colors[r][c] = cb
    return width, colors


def main():
    os.makedirs(OUT, exist_ok=True)
    seed_cells, seed_col = identicon(GITHUB_ID)
    seq = [("seed", seed_cells, seed_col)] + [
        (name, mirror(rows), hsl(hue, SAT, LUM)) for name, rows, hue in GLYPHS
    ]

    # --- animated ---------------------------------------------------------
    g = GifRenderer()
    g.emit_colors = 64
    for i, (name, cells, col) in enumerate(seq):
        g._emit(draw_icon(cells, col), HOLD_MS)
        nname, ncells, ncol = seq[(i + 1) % len(seq)]
        for f in range(1, FLIP_FRAMES + 1):
            width, colors = flip_frame(cells, col, ncells, ncol, f / FLIP_FRAMES)
            g._emit(draw_icon(cells, col, width, colors), FRAME_MS)
    gif = g.save(scratch_dir() + "profile_icon_raw.gif",
                 os.path.join(OUT, "avatar.gif"), colors=48)

    # --- stills (2x, pixel-exact — no supersample needed) -----------------
    for name, cells, col in seq:
        draw_icon(cells, col, scale=1).resize((SIZE * 2, SIZE * 2), Image.NEAREST) \
            .save(os.path.join(OUT, f"avatar_{name}.png"))

    # --- contact sheet ----------------------------------------------------
    tile, pad = 210, 24
    f = load_font(22, bold=True, family="mono")
    sheet = Image.new("RGB", (len(seq) * (tile + pad) + pad, tile + pad * 2 + 40), BG)
    d = ImageDraw.Draw(sheet)
    for i, (name, cells, col) in enumerate(seq):
        x = pad + i * (tile + pad)
        sheet.paste(draw_icon(cells, col, scale=1).resize((tile, tile), Image.NEAREST), (x, pad))
        d.rectangle([x, pad, x + tile - 1, pad + tile - 1], outline=(200, 200, 200))
        d.text((x + tile / 2 - d.textlength(name, font=f) / 2, pad + tile + 12),
               name, font=f, fill=(60, 60, 60))
    sheet.save(os.path.join(OUT, "avatar_sheet.png"))
    print(gif, os.path.getsize(gif), "bytes,", len(g.frames), "frames")


if __name__ == "__main__":
    main()
