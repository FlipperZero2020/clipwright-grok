#!/usr/bin/env python3
"""No. 2 for Alex — THE MEMORY LEAK. Tabloid style. benchod_tg. Quotes are
verbatim from a live chat thread the user shared directly (not yet ingested
as a graphiti episode) — cross-checked against Alex's existing graph entity
for consistent supporting facts (Sugar Land TX, Mac usage, the slop-stopper
bot, the $2,500/mo token bill). Nothing to exclude here: the exchange is the
whole joke, no named third party appears, and none of it touches health,
immigration, or misconduct.
"""
import sys
sys.path.insert(0, "/home/tom/Documents/claude/CLIPWRIGHT_PLAN")
from tabloid_kit import Tabloid

t = Tabloid(
    masthead="THE MEMORY LEAK",
    dateline="SUGAR LAND, TEX.  ·  BYLINE: @aRaymo  ·  50¢",
    kicker="M E M O R Y   E X C L U S I V E",
    headline_lines=["MY APP HAS", "“ONES”"],
    deck="“My app has ones for memory like that”",
    col1=("BENCHOD_TG — A local engineer's memory-system comparison stalled "
          "on one word, moments after a colleague laid out the group's "
          "actual MCP setup in full."),
    col2=("The correction that followed, “My app has ones for—”, was never "
          "finished. The floor's best guess, never confirmed by the source: "
          "“graphs?”"),
    starburst_lines=[("80%", 22), ("ONE TOOL", 13)],
    starburst_pos=(514, 448),
    strips=[
        ("MCP TOOLS",   "“really just use the one 80% of the time search_memory_facts”"),
        ("DATA DUMP",   "“pst email files, gmails, TG chats, not all my work stuff yet”"),
        ("BEST GUESS",  "“graphs?”"),
    ],
    also_inside="ALSO INSIDE:  still “mostly” a Mac guy  ·  slop-stopper bot unretired  ·  token bill: no comment",
    stop_press_lines=("STOP PRESS", "STILL HASN'T SAID GRAPHS"),
)

S = "/tmp/claude-1000/-home-tom-Documents-claude-CLIPWRIGHT-PLAN/94c4d75b-0862-434a-86bb-fbfb9d44adfb/scratchpad/"
print(t.save(S + "alex_02_raw.gif", "/home/tom/Documents/claude/CLIPWRIGHT_PLAN/alex_02_memory_leak.gif"))
