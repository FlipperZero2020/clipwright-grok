#!/usr/bin/env python3
"""No. 1 for Alex — THE RAYMOND REGISTER. Tabloid style. benchod_tg, verbatim quotes.
Deliberately excludes: the ayahuasca retreat invite (drug/health), Devin's
suggestion that Alex's bot swarm run fraud tasks (misconduct, and arguably
not even Alex's own act), and the $TRUMP meme coin (unrelated political noise).
"""
import sys
sys.path.insert(0, "/home/tom/Documents/claude/CLIPWRIGHT_PLAN")
from tabloid_kit import Tabloid

t = Tabloid(
    masthead="THE RAYMOND REGISTER",
    dateline="SUGAR LAND, TEX.  ·  BYLINE: @aRaymo  ·  50¢",
    kicker="T E C H   E X C L U S I V E",
    headline_lines=["I USED", "5 BILLION", "TOKENS"],
    deck="“…a token bill of about $2,500 a month, on top of his subscription plans”",
    col1=("SUGAR LAND — A local engineer has confirmed spending beyond every "
          "plan limit available, and does not appear to be stopping."),
    col2=("“I am expensing the tokens,” he said, of the arrangement that lets "
          "this continue. His employer was reached for comment."),
    starburst_lines=[("$2,500", 20), ("PER MONTH", 13)],
    starburst_pos=(514, 448),
    strips=[
        ("METROLOGY", "“But without it it’s less than one mm along the axis of the arm.”"),
        ("DOMESTIC",  "“Alex you forgot one child this morning”"),
        ("FITNESS",   "“Just wait till me and Justin start rollerblading together”"),
    ],
    also_inside="ALSO INSIDE:  roof damage, unrepaired  ·  the slop-stopper bot  ·  missed New Orleans",
    stop_press_lines=("STOP PRESS", "SKATES: STILL ZERO"),
)

S = "/tmp/claude-1000/-home-tom-Documents-claude-CLIPWRIGHT-PLAN/3b825699-41ec-46e0-9dd6-60fabb208cc9/scratchpad/"
print(t.save(S + "alex_raw.gif", "/home/tom/Documents/claude/CLIPWRIGHT_PLAN/alex_01_raymond_register.gif"))
