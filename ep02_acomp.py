#!/usr/bin/env python3
"""No. 2 — EXCEPT AN ACOMP. Every quote verbatim from benchod_tg."""
import math, sys
sys.path.insert(0, "/home/tom/Documents/claude/CLIPWRIGHT_PLAN")
from ballad_kit import Ballad, GOLD


def acomp(d, cx, cy, R, ang, color=GOLD):
    """An ACOMP skid: tank, column, gauge, and a turning valve wheel.
    (It only runs on Ignition because it vaguely resembles a plant.)"""
    s = R / 60.0
    P = lambda v: int(v * s)
    lw = max(2, P(4))
    # skid base
    d.rounded_rectangle([cx - P(78), cy + P(46), cx + P(78), cy + P(58)], radius=P(4), fill=color)
    # tank
    d.rounded_rectangle([cx - P(56), cy - P(30), cx - P(8), cy + P(46)], radius=P(12),
                        outline=color, width=lw)
    # column
    d.rounded_rectangle([cx + P(8), cy - P(52), cx + P(38), cy + P(46)], radius=P(6),
                        outline=color, width=lw)
    # cross pipe
    d.line([cx - P(8), cy - P(10), cx + P(8), cy - P(10)], fill=color, width=lw)
    # riser + valve wheel (the animated part)
    vx, vy, vr = cx + P(23), cy - P(78), P(17)
    d.line([vx, cy - P(52), vx, vy + vr], fill=color, width=max(2, P(3)))
    d.ellipse([vx - vr, vy - vr, vx + vr, vy + vr], outline=color, width=max(2, P(3)))
    for i in range(6):
        a = ang + i * math.pi / 3
        d.line([vx, vy, vx + vr * .95 * math.cos(a), vy + vr * .95 * math.sin(a)],
               fill=color, width=max(1, P(2)))
    # pressure gauge, needle drifting
    gx, gy, gr = cx - P(32), cy - P(6), P(10)
    d.ellipse([gx - gr, gy - gr, gx + gr, gy + gr], outline=color, width=max(1, P(2)))
    d.line([gx, gy, gx + gr * .7 * math.cos(ang * .6 - 1.2), gy + gr * .7 * math.sin(ang * .6 - 1.2)],
           fill=color, width=max(1, P(2)))


b = Ballad()

b.title(
    kicker="@topo_chino · NO. 2",
    headline="EXCEPT AN ACOMP",
    tagline=["three years.  one product.", "zero trips to Singapore."],
    footer="benchod_tg · Fluence Analytics → Yokogawa",
    emblem=acomp,
)

b.chat("benchod_tg", "3 members, 0 reliably manufacturable ACOMPs", [
    ("Justin", "We are stopping the industrial ACOMP project", False),
    ("Alex",   "We are a finely tuned manufacturing machine.  We can make everything, except an ACOMP.", True),
    ("Justin", "That’s why I didn’t get to go to Singapore", False),
])

b.document(
    brand="YOKOGAWA",
    sub="PRESS RELEASE · 2023-02-02 · FLUENCE ANALYTICS",
    lede="“…ACOMP system is estimated to deliver US$1.5 million in value per year”",
    rows=[("INDUSTRIAL ACOMP", "STOPPED"),
          ("SINGAPORE TRIP", "TOM")],
    quote="“We are stopping the industrial ACOMP project”",
    verdict="singapore: reassigned",
)

b.dossier("ACOMP · FIELD REPORT", "Justin · Stafford, TX · the man you ask about ACOMP", [
    ("VALUE PER YEAR",       "US$1.5M  (per press release)", True),
    ("ACOMPS BUILT",         "more than two, apparently",    False),
    ("DOME REGULATORS",      "2  — Tom B said only buy 2",   True),
    ("INDUSTRIAL PROJECT",   "stopped 2023-04-13",           False),
    ("AUTOMATION SOFTWARE",  "Ignition (resembles a plant)", False),
    ("WHO TO ASK",           "“justin could tell you”",      True),
    ("MANUFACTURABLE",       "not reliably, as of Aug 2024", False),
    ("TRIPS TO SINGAPORE",   "Tom got it",                   True),
])

b.counter("TRIPS TO SINGAPORE", "0",
          "the ACOMP vaguely resembles a plant", acomp)

S = "/tmp/claude-1000/-home-tom-Documents-claude-CLIPWRIGHT-PLAN/3b825699-41ec-46e0-9dd6-60fabb208cc9/scratchpad/"
print(b.save(S + "ep02_raw.gif", "/home/tom/Documents/claude/CLIPWRIGHT_PLAN/justin_02_except_an_acomp.gif"))
