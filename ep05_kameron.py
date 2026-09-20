#!/usr/bin/env python3
"""No. 5 — FRIENDSHIP ENDED WITH ACOMP. Ballad style, subject: Kameron (benchod_tg).
Every quote verbatim. The whole episode turns on one double meaning: "ARGEN" is
both his new work project AND his own codename for vaping in the server room.
"""
import math, os, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from ballad_kit import Ballad, GOLD
from style_common import scratch_dir


def vape_pen(d, cx, cy, R, ang, color=GOLD):
    """A vape pen with rising, drifting vapor — the habit he's 3 weeks off."""
    s = R / 60.0
    P = lambda v: int(v * s)
    lw = max(2, P(4))
    d.rounded_rectangle([cx - P(12), cy - P(10), cx + P(12), cy + P(58)], radius=P(6),
                        outline=color, width=lw)
    d.rounded_rectangle([cx - P(7), cy - P(24), cx + P(7), cy - P(10)], radius=P(3),
                        outline=color, width=max(1, P(2)))
    d.ellipse([cx - P(4), cy + P(4), cx + P(4), cy + P(12)], fill=color)
    for i, dx in enumerate((-P(10), P(0), P(10))):
        phase = ang + i * 2.1
        for j in range(3):
            yy = cy - P(30) - j * P(16)
            wob = P(8) * math.sin(phase + j * 1.3)
            r = max(1, P(3) - j)
            a = max(20, 190 - j * 60)
            d.ellipse([cx + dx + wob - r, yy - r, cx + dx + wob + r, yy + r],
                     outline=color, width=max(1, P(1)))


b = Ballad()

b.title(
    kicker="benchod_tg · NO. 5",
    headline="FRIENDSHIP ENDED",
    tagline=["with ACOMP.  now ARGEN is my friend.", "(ARGEN is also the server room.)"],
    footer="Kameron · Houston-area office · Fluence/Yokogawa",
    emblem=vape_pen,
)

b.chat("benchod_tg", "3 members, 1 project with a double meaning", [
    ("Kameron", "friendship ended with acomp. now argen is my friend", False),
    ("Kameron", "ive moved argen operations into the server room so it much easier now.", False),
    ("Kameron", "25$/week on disposables so 100$/month was me.", False),
])

b.document(
    brand="ARGEN OPS",
    sub="INTERNAL STATUS · PROJECT TRANSITION, FEB 2023",
    lede="“ive moved argen operations into the server room so it much easier now.”",
    rows=[("ACOMP FRIENDSHIP", "ENDED"),
          ("ARGEN OPERATIONS", "SERVER ROOM")],
    quote="“friendship ended with acomp. now argen is my friend”",
    verdict="not once caught (allegedly)",
)

b.dossier("SUBJECT DOSSIER", "Kameron Billingsley · Houston-area office", [
    ("VEHICLE",          "Mazda 3 — lift kit: aspirational", False),
    ("VAPE SPEND",       "$100/month, at its peak",           True),
    ("QUIT STREAK",      "3 weeks, no disposable",             True),
    ("HOME AIR QUALITY", "DIY ozone gen., ex-car transformer", False),
    ("CHINA EXCHANGE RATE", "8 RMB = 1 bowl of noodles",       False),
    ("CAREER PIVOT",     "ACOMP → ARGEN, Feb 2023",            True),
    ("STREAMING",        "pays for Prime, pirates anyway",     False),
])

b.counter("DISPOSABLES BOUGHT, LAST 3 WEEKS", "0",
          "argen is my friend now", vape_pen)

S = scratch_dir()
print(b.save(S + "ep05_raw.gif", os.path.join(ROOT, "kameron_01_friendship_ended.gif")))
