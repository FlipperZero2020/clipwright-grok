#!/usr/bin/env python3
"""Standalone one-off — NOT a CLIPWRIGHT_PLAN coworker episode, not added to
series_state.json. Dark political satire built from a poster the user pasted
in chat: "DRILL, BABY, DRILL! Announcing the historic NABEP U.S. Gov't
Venezuela Deal" (nabep.net) — ICE-supplied labor pitched as solving both the
migration crisis and oil production, "two birds, one stone." Targets the
policy/PR framing, not any named individual or ethnic/national group.
Reuses ballad_kit's 5-scene structure for visual consistency with the
project's house style.
"""
import sys, math
sys.path.insert(0, "/home/tom/Documents/claude/CLIPWRIGHT_PLAN")
from ballad_kit import Ballad, GOLD

W = H = 640


def pumpjack(d, cx, cy, R, ang, color=GOLD):
    """Vector pumpjack: Samson-post legs + a walking beam that rocks."""
    base_y = cy + R * 0.55
    apex = (cx, cy - R * 1.35)
    d.line([cx - R * 0.55, base_y, apex[0], apex[1]], fill=color, width=5)
    d.line([cx + R * 0.55, base_y, apex[0], apex[1]], fill=color, width=5)
    d.line([cx - R * 0.55, base_y, cx + R * 0.55, base_y], fill=color, width=5)

    tilt = math.sin(ang) * 0.32
    beam_len = R * 1.35
    x1 = apex[0] - beam_len * math.cos(tilt)
    y1 = apex[1] - beam_len * math.sin(tilt) - 8
    x2 = apex[0] + beam_len * math.cos(tilt)
    y2 = apex[1] + beam_len * math.sin(tilt) - 8
    d.line([x1, y1, x2, y2], fill=color, width=6)
    d.ellipse([apex[0] - 6, apex[1] - 6, apex[0] + 6, apex[1] + 6], fill=color)

    d.line([x1, y1, x1, y1 + R * 0.85], fill=color, width=4)
    d.arc([x1 - 20, y1 - 12, x1 + 20, y1 + 20], start=200, end=340, fill=color, width=6)
    d.ellipse([x2 - 13, y2 - 9, x2 + 13, y2 + 9], fill=color)

    d.rectangle([cx - 7, base_y - 4, cx + 7, base_y + 12], fill=color)


b = Ballad()

b.title(
    kicker="OIL DESK · U.S.–VENEZUELA",
    headline="DRILL, BABY, DRILL",
    tagline=["one historic deal.", "one two-bird stone."],
    footer="satire, based on real news · nabep.net",
    emblem=pumpjack,
)

b.chat(
    header="NABEP WIRE",
    sub="3 dispatches · nabep.net/articles",
    msgs=[
        ("NABEP", "“…historic deal to develop Venezuela's oil sector.”", False),
        ("STAFFING DESK", "“ICE is supplying the employees, the article says.”", True),
        ("PR", "“Two birds, one stone!”", False),
    ],
)

b.document(
    brand="U.S.–NABEP MEMORANDUM",
    sub="OIL SECTOR DEVELOPMENT · VENEZUELA",
    lede="Interagency staffing memo, migration/oil crosswalk desk.",
    rows=[("MIGRATION CRISIS", "REROUTED"), ("OIL PRODUCTION", "ONLINE")],
    quote="“ICE is supplying the employees, the article says.”",
    verdict="TWO BIRDS, ONE STONE.",
)

b.dossier(
    heading="DEAL DOSSIER",
    sub="NABEP · Venezuela oil sector · 2026",
    rows=[
        ("PARTIES", "NABEP x U.S. Government", False),
        ("COMMODITY", "Venezuela's oil sector", False),
        ("LABOR SOURCE", "ICE-supplied workers", True),
        ("SLOGAN", "“Drill, baby, drill”", False),
        ("PR FRAMING", "“Two birds, one stone”", True),
        ("GENRE", "satire, based on real news", False),
        ("SOURCE", "nabep.net/articles/...", False),
    ],
)

b.counter(
    label="BIRDS, STONED",
    value="2",
    footer="efficiency achieved. nobody asked how.",
    emblem=pumpjack,
)

S = "/tmp/claude-1000/-home-tom-Documents-claude-CLIPWRIGHT-PLAN/16291870-5951-4c4b-b20c-a3abb5ae73d1/scratchpad/"
print(b.save(S + "nabep_raw.gif", "/home/tom/Documents/claude/CLIPWRIGHT_PLAN/nabep_drill_baby_drill.gif"))
