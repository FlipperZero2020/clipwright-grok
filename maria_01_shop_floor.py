#!/usr/bin/env python3
"""No. 1 for Maria — THE SHOP FLOOR BULLETIN. Tabloid style. benchod_tg, verbatim quotes.
Deliberately excludes: a graph fact stating "Devin is married to Maria,"
corroborated by "Devin submitted his (marriage-based) US green card ...
application... 'I submitted the papers today Maria.'" This is real,
personal, marriage-based immigration status about real named people —
exactly the "immigration risk" category these rules call out, and not even
Maria's own material to begin with. Also excludes: her hospital visit/anemia
(health), her years-as-an-immigrant framing (immigration-adjacent), and the
group's running jokes about her Russian background/Megafon detail — the
distinction from e.g. Guru's self-directed airport joke is that this one
isn't hers; it's the group's joke about her national origin, which is a
different and less defensible category.
"""
import sys
sys.path.insert(0, "/home/tom/Documents/claude/CLIPWRIGHT_PLAN")
from tabloid_kit import Tabloid

t = Tabloid(
    masthead="THE SHOP FLOOR BULLETIN",
    dateline="FLUENCE ANALYTICS  ·  JUNIOR ENGINEER  ·  50¢",
    kicker="P L A N T   F L O O R   E X C L U S I V E",
    headline_lines=["BEN COME", "BACK!!!"],
    deck="“Man we have Maria and Tom both head deep in the ACOMP, Ben come back!!!”",
    col1=("FLUENCE — Two engineers report being 'head deep' in the ACOMP "
          "following one departure, with no relief scheduled."),
    col2=("Maria has since taken on a second, smaller gatekeeping role: "
          "deciding who gets into the group chat named for the man who left."),
    starburst_lines=[("HEAD", 20), ("DEEP", 20)],
    starburst_pos=(514, 448),
    strips=[
        ("SHOP FLOOR", "“And it's literally just me and Kameron in the back”"),
        ("SKILLSET",   "“cad skills for the next 2 months”"),
        ("AUTO",       "“Hyundai replacing my engine for free”"),
    ],
    also_inside="ALSO INSIDE:  studying scrum at school  ·  Montrose storm survivor  ·  “I love being a junior engineer”",
    stop_press_lines=("STOP PRESS", "BEN STILL HASN'T COME BACK"),
)

S = "/tmp/claude-1000/-home-tom-Documents-claude-CLIPWRIGHT-PLAN/3b825699-41ec-46e0-9dd6-60fabb208cc9/scratchpad/"
print(t.save(S + "maria_raw.gif", "/home/tom/Documents/claude/CLIPWRIGHT_PLAN/maria_01_shop_floor_bulletin.gif"))
