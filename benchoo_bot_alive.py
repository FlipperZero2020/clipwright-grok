#!/usr/bin/env python3
"""Standalone one-off — NOT a CLIPWRIGHT_PLAN coworker episode, not added to
series_state.json. Animates the pasted composite photo (BENCHOO_9000_BOT, the
green muscle-bot with "Heartbeat.md Eaten" on its chest screen, next to a
coworker) into a smooth, large looping GIF: a mesh-warp "wiggle" gives the
whole figure a living, breathing sway (more motion up top at the arms/head,
less at the feet) plus a slow weight-shift bob, and the chest monitor gets a
brightness flicker so the screen reads as live and glitching. No new pixels
are drawn — this is pure photo animation of the source frame.
"""
import math
import os
import subprocess

from PIL import Image, ImageEnhance
from style_common import scratch_dir

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("CLIPWRIGHT_BENCHOO_SRC", os.path.join(ROOT, "assets", "Eaten_Ben_Chod.jpg"))
SCRATCH = scratch_dir()
OUT_RAW = SCRATCH + "benchoo_alive_raw.gif"
OUT_FINAL = os.path.join(ROOT, "benchoo_9000_bot_alive.gif")

SIZE = 960          # large square canvas
PAD = 56            # room for the weight-shift bob before we crop back
GX, GY = 12, 16     # warp grid — finer vertically for arm/torso motion
FRAMES = 30
DELAY_MS = 38

W = H = SIZE
CW, CH = W + PAD * 2, H + PAD * 2


def require_src(script):
    """Fail before any frame is built if the composite photo isn't on disk."""
    if not os.path.isfile(SRC):
        raise SystemExit(
            "%s: source photo not found at %s\n"
            "Set CLIPWRIGHT_BENCHOO_SRC to the BENCHOO_9000_BOT composite photo "
            "(it originally lived at %s) or put it at the default path above. "
            "The photo is deliberately not part of the repo."
            % (script, SRC, os.path.expanduser("~/Downloads/Eaten_Ben_Chod.jpg")))

def smoothstep(a, b, x):
    t = max(0.0, min(1.0, (x - a) / (b - a)))
    return t * t * (3 - 2 * t)


def amp_for_y(vy):
    """Motion weight by height: full sway up top, tapering near the feet."""
    return 1.0 - smoothstep(0.55, 1.0, vy) * 0.82


def amp_for_x(vx):
    """Keep the coworker and the wall/floor mostly still — only the bot sways.

    Also fades to ~0 at the far edges so the border pixels stay put, which
    keeps the GIF encoder from re-touching flat background every frame.
    """
    left_edge = smoothstep(0.0, 0.05, vx)
    right_fall = 1.0 - smoothstep(0.58, 0.80, vx) * 0.85
    right_edge = 1.0 - smoothstep(0.95, 1.0, vx)
    return left_edge * right_fall * right_edge


def grid_points(gx, gy, w, h):
    xs = [round(i * w / gx) for i in range(gx + 1)]
    ys = [round(j * h / gy) for j in range(gy + 1)]
    return xs, ys


def warp(im, xs, ys, t):
    phase = 2 * math.pi * t
    pts = {}
    for j, y in enumerate(ys):
        vy = y / H
        amp_y = amp_for_y(vy)
        for i, x in enumerate(xs):
            vx = x / W
            amp = amp_y * amp_for_x(vx)
            dx = amp * 11 * math.sin(phase + vy * 3.1 + vx * 1.6)
            dy = amp * 8 * math.sin(phase * 1.3 + vx * 2.4 + 1.0)
            pts[(i, j)] = (x + dx, y + dy)

    mesh = []
    for j in range(len(ys) - 1):
        for i in range(len(xs) - 1):
            box = (xs[i], ys[j], xs[i + 1], ys[j + 1])
            ul, ll, lr, ur = pts[(i, j)], pts[(i, j + 1)], pts[(i + 1, j + 1)], pts[(i + 1, j)]
            mesh.append((box, (*ul, *ll, *lr, *ur)))
    return im.transform((W, H), Image.MESH, mesh, resample=Image.BILINEAR)


def flicker_screen(im, box, t):
    """Pulse the chest monitor's brightness so it reads as a live, glitchy screen."""
    x0, y0, x1, y1 = box
    crop = im.crop(box)
    factor = 1.0 + 0.22 * math.sin(2 * math.pi * (4 * t)) + 0.06 * math.sin(2 * math.pi * (11 * t))
    crop = ImageEnhance.Brightness(crop).enhance(max(0.6, factor))
    im.paste(crop, (x0, y0))
    return im


def main():
    require_src('benchoo_bot_alive.py')
    src = Image.open(SRC).convert("RGB").resize((W, H), Image.LANCZOS)
    bg = tuple(int(c) for c in src.crop((4, 4, 36, 36)).resize((1, 1)).getpixel((0, 0)))
    xs, ys = grid_points(GX, GY, W, H)

    scale = W / 2048
    screen_box = (round(819 * scale), round(552 * scale), round(1351 * scale), round(901 * scale))

    rgb_frames, delays = [], []
    for f in range(FRAMES):
        t = f / FRAMES
        frame = warp(src, xs, ys, t)
        frame = flicker_screen(frame, screen_box, t)

        bob_y = round(4 * math.sin(2 * math.pi * t + 0.6))
        bob_x = round(2 * math.sin(2 * math.pi * t * 1.0))
        canvas = Image.new("RGB", (CW, CH), bg)
        canvas.paste(frame, (PAD + bob_x, PAD + bob_y))
        canvas = canvas.crop((PAD, PAD, PAD + W, PAD + H))

        rgb_frames.append(canvas)
        delays.append(DELAY_MS)

    # one shared, stable palette across every frame — lets gifsicle diff
    # frames pixel-for-pixel instead of re-encoding the whole photo each time
    # build the shared palette from the pristine, unwarped source — richer
    # and truer to the original greens than picking any one animated frame
    shared_pal = src.quantize(colors=180, method=Image.MEDIANCUT)
    frames = [f.quantize(palette=shared_pal, dither=Image.FLOYDSTEINBERG) for f in rgb_frames]

    frames[0].save(
        OUT_RAW, save_all=True, append_images=frames[1:],
        duration=delays, loop=0, optimize=False, disposal=1,
    )
    subprocess.run(
        ["gifsicle", "-O3", "--careful", "--lossy=80", "--colors", "180", OUT_RAW, "-o", OUT_FINAL],
        check=True,
    )
    print(OUT_FINAL)


if __name__ == "__main__":
    main()
