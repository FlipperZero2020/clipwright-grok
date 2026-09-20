#!/usr/bin/env python3
"""No. 3 — ALWAYS LEAVES AGAIN. Ballad style, new subject: Devin (benchod_tg).
Every quote verbatim. Deliberately excludes: WiFi password cracking, the Amazon
review/refund scheme, and the sushi-restaurant stamp-card exploit — real
misconduct against real targets, not the harmless self-own the series is for.
"""
import math, os, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from ballad_kit import Ballad, GOLD
from style_common import scratch_dir


def suitcase(d, cx, cy, R, ang, color=GOLD):
    """A rolling suitcase — wheels spin, handle stays up. He's always leaving again."""
    s = R / 60.0
    P = lambda v: int(v * s)
    lw = max(2, P(4))
    top, bot = cy - P(46), cy + P(30)
    d.rounded_rectangle([cx - P(44), top, cx + P(44), bot], radius=P(10), outline=color, width=lw)
    d.line([cx - P(44), cy - P(2), cx + P(44), cy - P(2)], fill=color, width=max(1, P(2)))
    d.line([cx - P(14), top, cx - P(14), bot], fill=color, width=max(1, P(2)))
    d.line([cx + P(14), top, cx + P(14), bot], fill=color, width=max(1, P(2)))
    d.line([cx - P(16), top - P(24), cx - P(16), top], fill=color, width=lw)
    d.line([cx + P(16), top - P(24), cx + P(16), top], fill=color, width=lw)
    d.rounded_rectangle([cx - P(22), top - P(36), cx + P(22), top - P(20)], radius=P(6),
                        outline=color, width=lw)
    for wx in (cx - P(24), cx + P(24)):
        wy, wr = bot + P(10), P(11)
        d.ellipse([wx - wr, wy - wr, wx + wr, wy + wr], outline=color, width=max(1, P(2)))
        for i in range(5):
            a = ang + i * 2 * math.pi / 5
            d.line([wx, wy, wx + wr * .8 * math.cos(a), wy + wr * .8 * math.sin(a)],
                   fill=color, width=max(1, P(2)))
    for i, dx in enumerate((-P(58), -P(66), -P(74))):
        d.line([cx + dx, bot - P(6 + i * 6), cx + dx - P(14), bot - P(6 + i * 6)],
               fill=color, width=max(1, P(2)))


b = Ballad()

b.title(
    kicker="benchod_tg · NO. 3",
    headline="ALWAYS LEAVES AGAIN",
    tagline=["one hometown.  one hurricane claim.", "zero furniture that doesn't fold."],
    footer="Devin · Winnipeg → Houston → wherever's next",
    emblem=suitcase,
)

b.chat("benchod_tg", "3 members, 0 pieces of furniture with legs", [
    ("Devin", "Everytime i go back it is like i never left. Which is why i always leave again.", False),
    ("Devin", "Double dip claim rejected.", False),
    ("Devin", "Jets will be the one", False),
])

b.document(
    brand="STATE FARM",
    sub="CLAIM REVIEW · HURRICANE BERYL · HOTEL COSTS",
    lede="“More money.  Can probably double dip this one with insurance claim”",
    rows=[("FEMA CLAIM", "DENIED"),
          ("STATE FARM CLAIM", "DENIED")],
    quote="“Double dip claim rejected.”",
    verdict="double dip: rejected twice",
)

b.dossier("SUBJECT DOSSIER", "Devin · from The Peg, currently Houston Energy Corridor", [
    ("HOMETOWN",           "Winnipeg — “The Peg”",         True),
    ("HOCKEY TEAM",        "Winnipeg Jets",                False),
    ("FURNITURE OWNED",    "collapsible only",             True),
    ("REASON",             "moves often",                  False),
    ("DOUBLE-DIP CLAIMS",  "2 filed, 2 denied",             True),
    ("JOB-HUNT METHOD",    "n8n → resume → CEO, direct",    False),
    ("COMMITMENT MADE",    "n8n method, rest of his career", True),
    ("GALLONS MOVED",      "32 million in 5 days",          False),
])

b.counter("CLAIMS DENIED", "2", "everytime i go back it is like i never left", suitcase)

S = scratch_dir()
print(b.save(S + "ep03_raw.gif", os.path.join(ROOT, "devin_01_always_leaves_again.gif")))
