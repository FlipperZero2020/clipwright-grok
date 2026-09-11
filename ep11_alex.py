#!/usr/bin/env python3
"""No. 3 for Alex — MODEL AT CAPACITY. Ballad style, benchod_tg. Quotes are
verbatim from a live chat thread the user shared directly (not yet ingested
as a graphiti episode), cross-checked against Alex's existing graph entity
for consistent supporting facts (Onshape API integration, Grok bot swarm,
Opus opinion, Fable usage, $2,500/mo bill, $8,000 August spend). Kameron's
"straight jarvis shit" is a real quote-reply to a separate, verified Alex
line ("This is awesome lol") — not to the fusion360/onshape message it might
look adjacent to in the raw screenshot. Kameron is a previously-featured,
named group member (kameron_01_friendship_ended.gif), so his line is
included as setup per the skill's chat-scene guidance. One line in the raw
screenshot ("You use semantic for your knowledge graphs?") was the chat
owner's own outgoing message, not Alex's — left out rather than misattributed.
Nothing to exclude otherwise: no confidential data, health, immigration, or
misconduct angle here.
"""
import math, sys
sys.path.insert(0, "/home/tom/Documents/claude/CLIPWRIGHT_PLAN")
from ballad_kit import Ballad, GOLD


def gear_chip(d, cx, cy, R, ang, color=GOLD):
    """A CAD gear turning next to a little AI chip — his two arguing halves."""
    s = R / 60.0
    P = lambda v: int(v * s)
    lw = max(2, P(4))
    teeth, outer, inner = 10, P(46), P(34)
    pts = []
    for i in range(teeth * 2):
        a = ang * 0.05 + i * math.pi / teeth
        r = outer if i % 2 == 0 else inner
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    d.polygon(pts, outline=color, width=lw)
    d.ellipse([cx - P(14), cy - P(14), cx + P(14), cy + P(14)], outline=color, width=lw)
    chx, chy = cx + P(74), cy + P(8)
    d.rounded_rectangle([chx - P(16), chy - P(16), chx + P(16), chy + P(16)],
                        radius=P(3), outline=color, width=max(1, P(2)))
    for k in range(3):
        yy = chy - P(10) + k * P(10)
        d.line([chx - P(26), yy, chx - P(16), yy], fill=color, width=max(1, P(2)))
        d.line([chx + P(16), yy, chx + P(26), yy], fill=color, width=max(1, P(2)))


b = Ballad()

b.title(
    kicker="benchod_tg · NO. 11",
    headline="MODEL AT CAPACITY",
    tagline=["mid-argument about which CAD tool wins,", "his own AI usage hits a ceiling first."],
    footer="Alex · benchod_tg · Onshape × McMaster-Carr, live in prod",
    emblem=gear_chip,
)

b.chat("benchod_tg", "9 members, Etude 80 playing in the background", [
    ("Alex", "I have liked fusion 360 personally but I think as time goes on, "
             "on shape will prove to be the best choice because it's already "
             "web native and can be integrated with easily. It will win out "
             "probably due to ai", False),
    ("Alex", "I'm starting to get “model at capacity” messages", False),
    ("Alex", "This is awesome lol", False),
    ("Kameron", "straight jarvis shit", False),
])

b.document(
    brand="CAD DEPARTMENT",
    sub="TOOL COMPARISON LOG · ALEX",
    lede="“It will win out probably due to ai”",
    rows=[("CAD VERDICT", "ONSHAPE"),
          ("REASON GIVEN", "“AI”")],
    quote="“This is awesome lol”",
    verdict="Kameron's read: “straight jarvis shit”",
)

b.dossier("SUBJECT DOSSIER", "Alex · benchod_tg · engineer", [
    ("AI STACK",         "Fable + Grok bot swarm",                    True),
    ("BENCHED MODEL",    "Opus — “poor”",                    False),
    ("MONTHLY SPEND",    "$2,500/mo (Aug: $8,000)",                    True),
    ("SIDE PROJECT",     "Onshape × McMaster-Carr app",           False),
    ("CAD OPINION",      "Onshape > Fusion 360, “due to ai”", True),
    ("CAPACITY STATUS",  "hitting “model at capacity” messages", False),
    ("MEMORY SETUP",     "still hasn't said “graphs?”",       True),
    ("PEER REVIEW",      "Kameron: “straight jarvis shit”",   False),
])

b.counter("AUGUST AI SPEND", "$8,000",
          "Kameron's read: “straight jarvis shit”", gear_chip)

S = "/tmp/claude-1000/-home-tom-Documents-claude-CLIPWRIGHT-PLAN/f54982ce-b35a-454f-8dd8-5ca438a79103/scratchpad/"
print(b.save(S + "ep11_raw.gif", "/home/tom/Documents/claude/CLIPWRIGHT_PLAN/alex_03_model_at_capacity.gif"))
