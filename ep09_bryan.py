#!/usr/bin/env python3
"""No. 9 — 8 YEARS OF PROCRASTINATION. Ballad style, subject: Bryan Powell
"Big_D_B" (weir_tg). Verbatim quotes. Last subject in the current pending
queue. Note: a search hit surfaced "Billy died of cancer" — Billy is a
different, unrelated former Weir employee who has nothing to do with Bryan;
noted and set aside, not used. The great-grandparents'-diary detail is kept
warm rather than played for a laugh — it's a genuinely nice thing about him,
not a self-own.
"""
import math, os, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from ballad_kit import Ballad, GOLD
from style_common import scratch_dir


def camper(d, cx, cy, R, ang, color=GOLD):
    """A camper with a solar panel and a charge level that keeps climbing."""
    s = R / 60.0
    P = lambda v: int(v * s)
    lw = max(2, P(4))
    d.rounded_rectangle([cx - P(52), cy - P(6), cx + P(40), cy + P(30)], radius=P(8),
                        outline=color, width=lw)
    d.line([cx + P(40), cy + P(4), cx + P(58), cy + P(14)], fill=color, width=lw)
    d.line([cx + P(58), cy + P(14), cx + P(58), cy + P(30)], fill=color, width=lw)
    d.line([cx + P(40), cy + P(30), cx + P(58), cy + P(30)], fill=color, width=lw)
    for wx in (cx - P(30), cx + P(14)):
        r = P(9)
        d.ellipse([wx - r, cy + P(30) - r * 0 + P(0), wx + r, cy + P(30) + r * 2 - r], outline=color, width=max(1, P(2)))
    d.ellipse([cx - P(30) - P(9), cy + P(30) - P(0), cx - P(30) + P(9), cy + P(30) + P(18)], outline=color, width=max(1, P(2)))
    d.ellipse([cx + P(14) - P(9), cy + P(30) - P(0), cx + P(14) + P(9), cy + P(30) + P(18)], outline=color, width=max(1, P(2)))
    px0, py0 = cx - P(46), cy - P(28)
    d.polygon([(px0, py0), (px0 + P(58), py0 - P(6)), (px0 + P(58), py0 + P(4)), (px0, py0 + P(10))],
             outline=color, width=max(1, P(2)))
    for k in range(1, 4):
        xx = px0 + P(58) * k / 4
        d.line([xx, py0 - P(6) * (1 - k / 4), xx, py0 + P(10) - P(6) * (k / 4)], fill=color, width=1)
    level = (math.sin(ang * 0.6) + 1) / 2
    bw, bh = P(14), P(24)
    bx, by = cx - P(80), cy - P(4)
    d.rectangle([bx, by - bh, bx + bw, by], outline=color, width=max(1, P(2)))
    d.rectangle([bx + P(4), by - P(4), bx + bw - P(4), by - P(2)], fill=color)
    fh = int(bh * level)
    if fh > 0:
        d.rectangle([bx + 1, by - fh, bx + bw - 1, by - 1], fill=color)


b = Ballad()

b.title(
    kicker="weir_tg · NO. 4",
    headline="8 YEARS",
    tagline=["of procrastination.", "one P.Eng license, one swapped Hawaii trip."],
    footer="Bryan Powell · “Big_D_B” · Syncrude, Mildred Lake/Aurora",
    emblem=camper,
)

b.chat("weir_tg", "3 founding members, ~3,000 unread messages", [
    ("Bryan", "Got the P.Eng last summer, Jeff. It only took me about 8 years of procrastination.", False),
    ("Bryan", "We were going to go this week but ended up in Hawaii instead", False),
    ("Bryan", "Just tolerating work to take vacations and spend time with the little guy, hahaha.", False),
])

b.document(
    brand="GROUP CHAT",
    sub="RE-ENTRY LOG · BRYAN POWELL",
    lede="“Jees, the chat has been quiet.”",
    rows=[("MESSAGES MISSED", "~3,000"),
          ("CALGARY TRIP", "REROUTED")],
    quote="“We were going to go this week but ended up in Hawaii instead”",
    verdict="still checks Telegram, eventually",
)

b.dossier("SUBJECT DOSSIER", "Bryan Powell · “Big_D_B” · Syncrude", [
    ("LICENSE",         "P.Eng, summer 2023",                   True),
    ("STATION",         "Mildred Lake Utilities → Aurora, Dec 2024", False),
    ("PRIORITIES",      "vacations & “the little guy” over work", True),
    ("VACATION SWAP",   "Calgary → Hawaii, Feb 2025",            False),
    ("CAMPER SETUP",    "2x 200Ah lithium, 2x 30A inverters",   True),
    ("FAMILY ARCHIVE",  "40+ photos, great-grandparents' diary", False),
    ("ARCHIVE PROJECT", "transcribing it with AI",               True),
    ("REFERRAL GIVEN",  "helped Tyler apply at Syncrude",        False),
])

b.counter("MESSAGES MISSED BEFORE HE NOTICED", "~3,000",
          "the chat was quiet. now it isn't.", camper)

S = scratch_dir()
print(b.save(S + "ep09_raw.gif", os.path.join(ROOT, "bryan_01_8_years.gif")))
