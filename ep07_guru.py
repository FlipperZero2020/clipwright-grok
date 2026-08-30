#!/usr/bin/env python3
"""No. 7 — JUST ME I THINK. Ballad style, subject: Guru (weir_tg). Verbatim quotes.
Deliberately excludes: a workplace "stolen equipment" incident that the graph
itself already redacted on the user's own prior instruction (named a specific
item, dollar amount, and a third party's court case) — already protected,
left untouched; his use of hawala transfers and Bitcoin-to-Russia payments
among friends (sanctions-evasion-adjacent, real legal risk, not a self-own);
his opinion on US/Canada immigration trends (political-opinion territory);
and his friends' account of the Iran conflict (war-adjacent, not comedic).
"""
import math, sys
sys.path.insert(0, "/home/tom/Documents/claude/CLIPWRIGHT_PLAN")
from ballad_kit import Ballad, GOLD


def lone_globe(d, cx, cy, R, ang, color=GOLD):
    """A globe with one glowing pin, alone on the far side of the meridians."""
    s = R / 60.0
    P = lambda v: int(v * s)
    lw = max(2, P(3))
    r = P(42)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=lw)
    for k in (-0.55, 0.0, 0.55):
        rx = max(2, int(r * math.sqrt(max(0.01, 1 - k * k))))
        d.ellipse([cx - rx, cy + int(k * r) - P(1), cx + rx, cy + int(k * r) + P(1)],
                 outline=color, width=max(1, P(1)))
    for k in (-0.7, -0.35, 0.0, 0.35, 0.7):
        d.arc([cx - r, cy - r, cx + r, cy + r], -90, 90, fill=color, width=max(1, P(1)))
    pulse = (math.sin(ang * 1.6) + 1) / 2
    px, py = cx + int(r * 0.6), cy - int(r * 0.35)
    pr = P(4) + int(P(3) * pulse)
    d.ellipse([px - pr, py - pr, px + pr, py + pr], fill=color)
    for i in range(5):
        a = ang * 0.4 + i * 2 * math.pi / 5
        d.point((px + int(P(9) * math.cos(a)), py + int(P(9) * math.sin(a))), fill=color)


b = Ballad()

b.title(
    kicker="weir_tg · NO. 2",
    headline="JUST ME I THINK",
    tagline=["one man.  one timezone.", "one 'freedom day' that lasted a week."],
    footer="Guru · India · founding member, chat handle: g",
    emblem=lone_globe,
)

b.chat("weir_tg", "3 founding members, 1 timezone that isn't North America", [
    ("Guru", "Just me i think", False),
    ("Guru", "Freedom day?? telegram was blocked in India for the past week.", False),
    ("Guru", "Hey I am brown skinned in the usa.. what can I say I get preferential treatment.", False),
])

b.document(
    brand="NETWORK STATUS",
    sub="TELEGRAM · INDIA · WEEK-LONG OUTAGE, 2026",
    lede="“Freedom day?? telegram was blocked in India for the past week.”",
    rows=[("TELEGRAM ACCESS", "BLOCKED"),
          ("TIMEZONE BACKUP", "NONE")],
    quote="“Just me i think”",
    verdict="one man, one timezone, zero backup",
)

b.dossier("SUBJECT DOSSIER", "Guru · India · one of three founding members", [
    ("CHAT HANDLE",       "“g” — confirmed to be Guru",     True),
    ("ROLE",              "founding member (of three)",       False),
    ("TIMEZONE",          "only non-North-American in the group", True),
    ("FAMILY",            "wife and kid — “impossible, Devin”", False),
    ("CRYPTO",            "“i have been taking etherium”",   True),
    ("LOCAL FIXER",       "arranges Mumbai deliveries on request", False),
    ("AIRPORT TREATMENT", "self-described as “preferential”", True),
    ("INTERNET FREEDOM",  "revoked for one week, 2026",         False),
])

b.counter("DAYS TELEGRAM WAS DOWN", "7",
          "still just him, still the only timezone", lone_globe)

S = "/tmp/claude-1000/-home-tom-Documents-claude-CLIPWRIGHT-PLAN/3b825699-41ec-46e0-9dd6-60fabb208cc9/scratchpad/"
print(b.save(S + "ep07_raw.gif", "/home/tom/Documents/claude/CLIPWRIGHT_PLAN/guru_01_just_me_i_think.gif"))
