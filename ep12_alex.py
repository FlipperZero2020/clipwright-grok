#!/usr/bin/env python3
"""No. 4 for Alex — meme style, benchod_tg. Built around one verified Alex
quote from a live chat thread the user shared directly: "I gave it my dad's
house plans and it created a full 3d walkthrough using blender." The rest of
that screenshot (the Qwen/browser-use/"no guardrails" paragraph and "Can it
play doom?") turned out to be the chat owner's own messages, not Alex's, per
their styling in the source screenshot (unlabeled, right-aligned, same bubble
color as his other outgoing texts) — deliberately excluded rather than
misattributed. The opening card's supporting texture (Fable, the "slop
stopper" bot) comes from Alex's existing graph entity, not this thread.
Nothing else to exclude: no confidential data, health, immigration, or
misconduct angle in the one quote actually used here.
"""
import os, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from meme_kit import MemeReel
from style_common import scratch_dir

r = MemeReel(kicker="benchod_tg · NO. 12", footer="Alex · benchod_tg · dad's house, now fully rendered")

r.card("yodawg", "yo alex we heard you like ai tools",
       "so we put ai tools in your ai tools",
       cap="uses Fable as his coding tool, keeps a “slop stopper” bot on the payroll")

r.card("success", "gave the ai his dad's house plans",
       "got a full 3d blender walkthrough back",
       cap="“I gave it my dad's house plans and it created a full 3d walkthrough using blender”",
       hold=2800)

r.stinger("STILL WAITING ON", "THE DOOM PORT")

S = scratch_dir()
print(r.save(S + "ep12_raw.gif", os.path.join(ROOT, "alex_04_dad_house_blender.gif")))
