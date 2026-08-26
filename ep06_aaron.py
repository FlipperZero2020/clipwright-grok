#!/usr/bin/env python3
"""No. 6 — CAME HERE FOR THE HUMIDITY. Ballad style, subject: Aaron Bidart (weir_tg).
Every quote verbatim, first-person only. Deliberately excludes: the "mental
breakdown" claim about him circulating in the chat (health disclosure about a
real person, sourced only to a third party's gossip, not confirmed by Aaron
himself) — and with it, the "skis in Japan" detail, since in the graph the two
are inseparable (same source quote). Also excludes the Suncor/Alberta-company
contracting detail — not clearly wrongdoing, but not clearly not, either.
"""
import math, sys
sys.path.insert(0, "/home/tom/Documents/claude/CLIPWRIGHT_PLAN")
from ballad_kit import Ballad, GOLD


def circuit_heart(d, cx, cy, R, ang, color=GOLD):
    """A heart traced in circuit-board lines — he builds the dating apps himself."""
    s = R / 60.0
    P = lambda v: int(v * s)
    lw = max(2, P(4))
    pts = []
    for i in range(65):
        t = i / 64 * 2 * math.pi
        x = 16 * math.sin(t) ** 3
        y = -(13 * math.cos(t) - 5 * math.cos(2*t) - 2 * math.cos(3*t) - math.cos(4*t))
        pts.append((cx + P(x * 2.6), cy + P(y * 2.6)))
    d.line(pts + [pts[0]], fill=color, width=lw, joint="curve")
    for i in range(4):
        a = ang + i * 1.6
        r0, r1 = P(6), P(30 + 8 * math.sin(ang * 0.7 + i))
        x0, y0 = cx + r0 * math.cos(a), cy + r0 * math.sin(a) * 0.9
        x1, y1 = cx + r1 * math.cos(a), cy + r1 * math.sin(a) * 0.9
        d.line([x0, y0, x1, y1], fill=color, width=max(1, P(2)))
        d.ellipse([x1 - P(3), y1 - P(3), x1 + P(3), y1 + P(3)], fill=color)
    pulse = (math.sin(ang * 1.3) + 1) / 2
    r = P(4) + int(P(3) * pulse)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)


b = Ballad()

b.title(
    kicker="weir_tg · NO. 1",
    headline="THE HUMIDITY",
    tagline=["“I came here for the humidity”", "builds AI dating apps in San Francisco"],
    footer="Aaron Bidart · Weir · Mechademy clients",
    emblem=circuit_heart,
)

b.chat("weir_tg", "one guy, two coding subscriptions, zero client uploads", [
    ("Aaron", "I came here for the humidity", False),
    ("Aaron", "Running models locally (or hosted in a VM) is seeming more relevant as I work with clients who don’t want all their data uploaded to anthropic servers", False),
    ("Aaron", "lots is claude usage that wont show up here too", False),
])

b.document(
    brand="MONTHLY STATEMENT",
    sub="AI TOOLING · PERSONAL ACCOUNT",
    lede="“Claude code $100 per month plan plus about $100 a month in cursor seems to work at the moment.”",
    rows=[("CURSOR BILLING", "TOO FAST"),
          ("CLIENT DATA POLICY", "AVOIDED")],
    quote="“I stopped using cursor because I run up the Bill too quickly.”",
    verdict="personal claude usage: untracked",
)

b.dossier("SUBJECT DOSSIER", "Aaron Bidart · Weir / Mechademy · San Francisco", [
    ("RELOCATED TO",        "Houston — “for the humidity”",     True),
    ("PRODUCT",             "AI dating apps, San Francisco",     False),
    ("CLAUDE CODE PLAN",    "“Max”, $100/month",                 True),
    ("CURSOR",              "~$100/month, separate",             False),
    ("CURSOR STATUS",       "demoted — ran the bill up too fast", True),
    ("CLIENT DATA POLICY",  "no uploads to Anthropic's servers",  False),
    ("PERSONAL USAGE",      "“wont show up” in the Cursor stats", True),
    ("AGENTS RUNNING",      "multiple, same codebase at once",    False),
])

b.counter("MONTHLY AI SPEND", "$200",
          "clients avoid Anthropic's servers. he doesn't.", circuit_heart)

S = "/tmp/claude-1000/-home-tom-Documents-claude-CLIPWRIGHT-PLAN/3b825699-41ec-46e0-9dd6-60fabb208cc9/scratchpad/"
print(b.save(S + "ep06_raw.gif", "/home/tom/Documents/claude/CLIPWRIGHT_PLAN/aaron_01_the_humidity.gif"))
