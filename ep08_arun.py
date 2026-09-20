#!/usr/bin/env python3
"""No. 8 — AT LEAST 6-7. Ballad style, subject: Arun (weir_tg). Verbatim quotes.
Deliberately excludes: his pending drug test result (health/employment
sensitive); his own joking remark that he and Devin "found a US wife" to
get US status — self-directed humor, but immigration/residency status is
exactly the "immigration risk" category the rules call out, so it's out
regardless of tone. Also declines to name his daughter, even though she is
named in the graph — extending the same discretion used for other members'
minor children throughout this series.
"""
import math, os, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from ballad_kit import Ballad, GOLD
from style_common import scratch_dir


def factory_stacks(d, cx, cy, R, ang, color=GOLD):
    """A little chemical plant — stacks with drifting smoke. He's ringed by them."""
    s = R / 60.0
    P = lambda v: int(v * s)
    lw = max(2, P(3))
    d.rectangle([cx - P(48), cy + P(18), cx + P(48), cy + P(34)], outline=color, width=lw)
    heights = (34, 52, 40)
    xs = (-P(30), P(0), P(30))
    for i, (hx, hh) in enumerate(zip(xs, heights)):
        top = cy + P(18) - P(hh)
        d.rectangle([cx + hx - P(7), top, cx + hx + P(7), cy + P(18)], outline=color, width=max(1, P(2)))
        phase = ang + i * 1.9
        for j in range(3):
            yy = top - P(6) - j * P(14)
            wob = P(7) * math.sin(phase + j * 1.2)
            r = max(1, P(3) - j)
            d.ellipse([cx + hx + wob - r, yy - r, cx + hx + wob + r, yy + r],
                     outline=color, width=max(1, P(1)))
    for tx in (-P(58), P(58)):
        d.line([cx + tx, cy + P(34), cx + tx, cy + P(50)], fill=color, width=max(1, P(2)))


b = Ballad()

b.title(
    kicker="weir_tg · NO. 3",
    headline="AT LEAST 6-7",
    tagline=["chemical plants visible on the way", "to his own chemical plant."],
    footer="Arun · Oxychem · Pasadena, TX (via Canada)",
    emblem=factory_stacks,
)

b.chat("weir_tg", "3 founding members, 1 skyline made entirely of plants", [
    ("Arun", "I can see at least 6-7 when heading towards mine", False),
    ("Arun", "Sell your home before you make a permanent move. Otherwise a big hassle with cra", False),
    ("Arun", "Good, don’t miss the snow at all", False),
])

b.document(
    brand="CANADA REVENUE AGENCY",
    sub="PERMANENT DEPARTURE · ADVISORY (UNOFFICIAL)",
    lede="“Sell your home before you make a permanent move. Otherwise a big hassle with cra”",
    rows=[("HOME OWNERSHIP", "SELL FIRST"),
          ("THE SNOW", "NOT MISSED")],
    quote="“Good, don’t miss the snow at all”",
    verdict="no hassle. no snow. no regrets.",
)

b.dossier("SUBJECT DOSSIER", "Arun · Oxychem · Pasadena, TX", [
    ("EMPLOYER",       "Oxychem, formerly OQ Chemicals",     False),
    ("COMMUTE VIEW",   "“at least 6-7” chemical plants",      True),
    ("SPECIALTY",      "predictive → prescriptive maintenance", False),
    ("RELOCATED FROM", "Canada, via a brief look at N. Carolina", True),
    ("WHY HOUSTON",    "bigger community fit — “we decided to come to Houston lol”", False),
    ("CRA ADVICE",     "sell the house before you leave",      True),
    ("2026 GOAL",       "learn SQL",                           False),
    ("FAMILY",         "three kids",                           False),
])

b.counter("CHEMICAL PLANTS VISIBLE ON COMMUTE", "6-7",
          "still doesn't miss the snow", factory_stacks)

S = scratch_dir()
print(b.save(S + "ep08_raw.gif", os.path.join(ROOT, "arun_01_at_least_6_7.gif")))
