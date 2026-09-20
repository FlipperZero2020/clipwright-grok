#!/usr/bin/env python3
"""Standalone one-off — NOT a CLIPWRIGHT_PLAN coworker episode, not added to
series_state.json. Animates the pasted composite photo (BENCHOO_9000_BOT, the
green muscle-bot with "Heartbeat.md Eaten" on its chest screen, next to a
coworker) into a gag GIF: the flexed fist stretches out and clamps near her
hands, then the multi-lens eye charges up and fires a beam at her face (she
blinks as it lands), with a screen-shake kick and a flash on impact, before
everything springs back to the idle pose so the loop is seamless.

v2 fixes vs the first pass:
  - The chest monitor is re-pasted from the pristine, un-warped source every
    frame (only brightness is animated on it), so its edges stay crisp
    instead of rippling with the body's mesh warp.
  - The "reach" is no longer a radial mesh-warp pull toward a point (that
    produced a swirly/portal-like compression artifact where the source
    texture converged). It's now a soft-edged sprite of the fist that flies
    from its resting spot to the grab point and back, with the vacated
    resting spot patched over by a cloned strip of forearm above it so
    there's never two fists on screen at once.
  - She blinks (a skin-toned eyelid patch closing/opening over one frame)
    once early and once right as the beam lands.
"""
import math
import os
import subprocess

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
from style_common import scratch_dir

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("CLIPWRIGHT_BENCHOO_SRC", os.path.join(ROOT, "assets", "Eaten_Ben_Chod.jpg"))
SCRATCH = scratch_dir()
OUT_RAW = SCRATCH + "benchoo_grab_laser_raw.gif"
OUT_FINAL = os.path.join(ROOT, "benchoo_grab_laser.gif")

SIZE = 800
PAD = 44
GX, GY = 16, 20
FRAMES = 60
DELAY_MS = 35

W = H = SIZE
CW, CH = W + PAD * 2, H + PAD * 2
SC = W / 2048  # source photo is 2048x2048; every reference point below is
                # given in source-photo pixels and scaled by SC.

EYE       = (1190 * SC, 315 * SC)   # centre lens of the eye cluster
FIST      = (670 * SC, 1440 * SC)   # knuckle mass of the flexed fist
GRAB      = (1290 * SC, 995 * SC)   # her clasped hands — where the fist lands (clear of the screen)
ZAP       = (1393 * SC, 589 * SC)   # her visible eye — where the beam lands
WOMAN_EYE = ZAP
SCREEN_BOX = (round(819 * SC), round(552 * SC), round(1351 * SC), round(901 * SC))

FIST_R = 95 * SC   # half-size of the flying fist sprite
FIST_BOX = (round(FIST[0] - 100 * SC), round(FIST[1] - 65 * SC),
            round(FIST[0] + 100 * SC), round(FIST[1] + 65 * SC))
_fbw, _fbh = FIST_BOX[2] - FIST_BOX[0], FIST_BOX[3] - FIST_BOX[1]
HEAL_SRC_BOX = (FIST_BOX[0], FIST_BOX[1] - _fbh, FIST_BOX[2], FIST_BOX[1])


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
    if a == b:
        return 1.0 if x >= a else 0.0
    t = max(0.0, min(1.0, (x - a) / (b - a)))
    return t * t * (3 - 2 * t)


def window(t, up0, up1, down0, down1):
    """0 -> 1 ramp between up0/up1, holds at 1, then 1 -> 0 between down0/down1."""
    return smoothstep(up0, up1, t) - smoothstep(down0, down1, t)


def tri(t, center, half):
    return max(0.0, 1.0 - abs(t - center) / half)


def reach_env(t):
    return window(t, 0.10, 0.20, 0.58, 0.70)


def laser_env(t):
    return window(t, 0.24, 0.33, 0.56, 0.66)


def flash_env(t):
    return max(0.0, 1.0 - abs(t - 0.335) / 0.011)


def shake_env(t):
    return laser_env(t)


def blink_env(t):
    return max(tri(t, 0.055, 0.02), tri(t, 0.335, 0.022))


def fist_pos(t):
    r = reach_env(t)
    fx, fy = FIST
    gx_, gy_ = GRAB
    return (fx + (gx_ - fx) * r, fy + (gy_ - fy) * r)


def grid_points(gx, gy, w, h):
    xs = [round(i * w / gx) for i in range(gx + 1)]
    ys = [round(j * h / gy) for j in range(gy + 1)]
    return xs, ys


def screen_suppress(x, y):
    """0 right at/inside the chest-screen box (so the warp can't wrinkle its
    edges against the rigid re-pasted screen), ramping to 1 a bit outside."""
    x0, y0, x1, y1 = SCREEN_BOX
    pad = 46
    dx = x0 - x if x < x0 else (x - x1 if x > x1 else 0)
    dy = y0 - y if y < y0 else (y - y1 if y > y1 else 0)
    return smoothstep(0, pad, math.hypot(dx, dy))


def warp(im, xs, ys, t):
    """Gentle idle breathing sway only — no targeted pulls, so nothing else
    (the screen, the fist) needs to fight this warp for its shape."""
    phase = 2 * math.pi * t
    pts = {}
    for j, y in enumerate(ys):
        vy = y / H
        for i, x in enumerate(xs):
            vx = x / W
            amp_y = 1.0 - smoothstep(0.55, 1.0, vy) * 0.82
            amp_x = smoothstep(0.0, 0.05, vx) * (1.0 - smoothstep(0.95, 1.0, vx))
            amp = amp_y * amp_x * screen_suppress(x, y)
            dx = amp * 6 * math.sin(phase + vy * 3.1 + vx * 1.6)
            dy = amp * 4 * math.sin(phase * 1.3 + vx * 2.4 + 1.0)
            pts[(i, j)] = (x + dx, y + dy)

    mesh = []
    for j in range(len(ys) - 1):
        for i in range(len(xs) - 1):
            box = (xs[i], ys[j], xs[i + 1], ys[j + 1])
            ul, ll, lr, ur = pts[(i, j)], pts[(i, j + 1)], pts[(i + 1, j + 1)], pts[(i + 1, j)]
            mesh.append((box, (*ul, *ll, *lr, *ur)))
    return im.transform((W, H), Image.MESH, mesh, resample=Image.BILINEAR)


def make_fist_sprite(src):
    cx, cy = FIST
    box = (round(cx - FIST_R), round(cy - FIST_R), round(cx + FIST_R), round(cy + FIST_R))
    crop = src.crop(box).convert("RGBA")
    w, h = crop.size
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).ellipse([2, 2, w - 3, h - 3], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(w * 0.10))
    crop.putalpha(mask)
    return crop


def make_heal_patch(src):
    w, h = _fbw, _fbh
    patch = src.crop(HEAL_SRC_BOX).resize((w, h), Image.LANCZOS)
    mask = Image.new("L", (w, h), 255)
    fade = max(1, int(h * 0.4))
    md = ImageDraw.Draw(mask)
    for i in range(fade):
        md.line([(0, i), (w, i)], fill=int(255 * i / fade))
    mask = mask.filter(ImageFilter.GaussianBlur(3))
    return patch, mask


def heal_fist_spot(frame, heal_patch, heal_mask, t):
    env = reach_env(t)
    if env <= 0.02:
        return frame
    base = frame.crop(FIST_BOX)
    scaled_mask = heal_mask.point(lambda p: int(p * env))
    blended = Image.composite(heal_patch, base, scaled_mask)
    frame.paste(blended, (FIST_BOX[0], FIST_BOX[1]))
    return frame


def repaste_screen(frame, screen_src, t):
    x0, y0, x1, y1 = SCREEN_BOX
    glitch = 1.0 + 0.7 * laser_env(t) * math.sin(2 * math.pi * 23 * t)
    factor = 1.0 + 0.22 * math.sin(2 * math.pi * 4 * t) + 0.06 * math.sin(2 * math.pi * 11 * t) + glitch * 0.15
    patch = ImageEnhance.Brightness(screen_src).enhance(max(0.5, factor))
    frame.paste(patch, (x0, y0))
    return frame


def swoosh(layer_draw, t):
    """A few translucent motion-streak arcs trailing the fist during the swing-in."""
    local = smoothstep(0.10, 0.20, t) * (1.0 - smoothstep(0.20, 0.26, t))
    if local <= 0.01:
        return
    fx, fy = FIST
    tx, ty = GRAB
    mx, my = (fx + tx) / 2, (fy + ty) / 2 - 40 * SC
    for k in range(3):
        off = (k - 1) * 26 * SC
        pts = [
            (fx + off * 0.3, fy + off),
            (mx + off * 0.6, my + off * 0.5),
            (tx + off * 0.2, ty + off * 0.3),
        ]
        alpha = int(150 * local * (1 - abs(k - 1) * 0.35))
        layer_draw.line(pts, fill=(235, 255, 235, alpha), width=max(2, int(5 * SC)), joint="curve")


def grip_shadow(layer_draw, t):
    hold = window(t, 0.18, 0.24, 0.58, 0.66)
    if hold <= 0.01:
        return
    tx, ty = GRAB
    r = 58 * SC
    layer_draw.ellipse(
        [tx - r, ty - r * 0.8, tx + r, ty + r * 0.8],
        fill=(20, 40, 15, int(90 * hold)),
    )


def laser_beam(layer_draw, t):
    env = laser_env(t)
    if env <= 0.01:
        return
    x0, y0 = EYE
    x1, y1 = ZAP
    jig = 5 * SC * math.sin(2 * math.pi * t * 41)
    x1, y1 = x1 + jig, y1 + jig * 0.5
    glow = (255, 40, 30)
    core = (255, 250, 210)
    for width, alpha in ((30, 45), (18, 85), (10, 140), (5, 210)):
        layer_draw.line([x0, y0, x1, y1], fill=(*glow, int(alpha * env)), width=max(1, round(width * SC * 2)))
    layer_draw.line([x0, y0, x1, y1], fill=(*core, int(255 * env)), width=max(1, round(4 * SC * 2)))

    r = 34 * SC * env
    layer_draw.ellipse([x0 - r, y0 - r, x0 + r, y0 + r], fill=(*core, int(210 * env)))
    r2 = 26 * SC * env
    spark = (255, 244, 200)
    layer_draw.ellipse([x1 - r2, y1 - r2, x1 + r2, y1 + r2], fill=(*spark, int(220 * env)))
    for k in range(6):
        ang = k / 6 * 2 * math.pi + t * 9
        sx, sy = x1 + math.cos(ang) * r2 * 1.6, y1 + math.sin(ang) * r2 * 1.6
        layer_draw.line([x1, y1, sx, sy], fill=(*spark, int(150 * env)), width=max(1, round(2 * SC)))


def effects_layer(t):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    swoosh(d, t)
    grip_shadow(d, t)
    laser_beam(d, t)
    return layer.filter(ImageFilter.GaussianBlur(1.4))


def blink_layer(skin_color, t):
    env = blink_env(t)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if env <= 0.02:
        return layer
    ex, ey = WOMAN_EYE
    half_w = 13
    half_h = 1 + 7 * env
    d = ImageDraw.Draw(layer)
    d.ellipse([ex - half_w, ey - half_h, ex + half_w, ey + half_h],
              fill=(*skin_color, int(245 * min(1.0, env * 1.4))))
    return layer.filter(ImageFilter.GaussianBlur(0.5))


def main():
    require_src('benchoo_grab_laser.py')
    src = Image.open(SRC).convert("RGB").resize((W, H), Image.LANCZOS)
    bg = tuple(int(c) for c in src.crop((4, 4, 36, 36)).resize((1, 1)).getpixel((0, 0)))
    xs, ys = grid_points(GX, GY, W, H)

    screen_src = src.crop(SCREEN_BOX)
    heal_patch, heal_mask = make_heal_patch(src)
    fist_sprite = make_fist_sprite(src)
    skin_color = src.getpixel((round(WOMAN_EYE[0] + 21), round(WOMAN_EYE[1] + 27)))

    rgb_frames, delays = [], []
    for f in range(FRAMES):
        t = f / FRAMES
        frame = warp(src, xs, ys, t)
        frame = heal_fist_spot(frame, heal_patch, heal_mask, t)
        frame = frame.convert("RGBA")

        renv = reach_env(t)
        if renv > 0.02:
            px, py = fist_pos(t)
            frame.alpha_composite(fist_sprite, (round(px - FIST_R), round(py - FIST_R)))

        frame = repaste_screen(frame.convert("RGB"), screen_src, t).convert("RGBA")
        frame = Image.alpha_composite(frame, effects_layer(t))
        frame = Image.alpha_composite(frame, blink_layer(skin_color, t))
        frame = frame.convert("RGB")

        fl = flash_env(t)
        if fl > 0.01:
            white = Image.new("RGB", frame.size, (255, 235, 210))
            frame = Image.blend(frame, white, min(0.55, fl))

        bob_y = round(4 * math.sin(2 * math.pi * t + 0.6))
        bob_x = round(2 * math.sin(2 * math.pi * t))
        shk = shake_env(t)
        if shk > 0.01:
            bob_x += round(shk * 7 * math.sin(2 * math.pi * t * 53))
            bob_y += round(shk * 7 * math.sin(2 * math.pi * t * 61 + 1.3))

        canvas = Image.new("RGB", (CW, CH), bg)
        canvas.paste(frame, (PAD + bob_x, PAD + bob_y))
        canvas = canvas.crop((PAD, PAD, PAD + W, PAD + H))

        rgb_frames.append(canvas)
        delays.append(DELAY_MS)

    # Build the shared palette from several representative frames (idle,
    # mid-grab, full-beam, release) rather than one frame — a single frame
    # picked near the ignition flash would skew the whole palette pale/white.
    sample_idxs = sorted(set([
        0, round(FRAMES * 0.055), round(FRAMES * 0.14), round(FRAMES * 0.28),
        round(FRAMES * 0.335), round(FRAMES * 0.45), round(FRAMES * 0.62), round(FRAMES * 0.85),
    ]))
    strip = Image.new("RGB", (W * len(sample_idxs), H))
    for k, si in enumerate(sample_idxs):
        strip.paste(rgb_frames[si], (k * W, 0))
    shared_pal = strip.quantize(colors=256, method=Image.MEDIANCUT)
    frames = [f.quantize(palette=shared_pal, dither=Image.NONE) for f in rgb_frames]

    frames[0].save(
        OUT_RAW, save_all=True, append_images=frames[1:],
        duration=delays, loop=0, optimize=False, disposal=1,
    )
    subprocess.run(
        ["gifsicle", "-O3", "--careful", "--lossy=60", "--colors", "256", OUT_RAW, "-o", OUT_FINAL],
        check=True,
    )
    print(OUT_FINAL)


if __name__ == "__main__":
    main()
