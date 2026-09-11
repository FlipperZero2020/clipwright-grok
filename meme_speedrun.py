#!/usr/bin/env python3
"""Standalone one-off — NOT a CLIPWRIGHT_PLAN coworker episode, not added to
series_state.json. A follow-up to meme_kit's debut (alex_04_dad_house_blender.gif,
the "stereotypical gifts" one): instead of two real quotes on two templates,
this is every classic stock meme format meme_kit can find, rapid-fired one
after another. No real coworker, no verbatim-quote requirement (that rule is
specific to the roast series) — the joke is the format itself: packing as
many overused, "stereotypical" meme templates as will fit into one gif,
each one roasting itself for being a cliché. Captions are original writing,
not quotes attributed to anyone.

Every template below is capped at 2 lines (memegen's `lines` field) so it
fits meme_kit's card(template, top, bottom) two-slot signature cleanly —
3-line templates like Distracted Boyfriend or Epic Handshake are skipped
rather than rendered with a missing third line.
"""
import sys
sys.path.insert(0, "/home/tom/Documents/claude/CLIPWRIGHT_PLAN")
from meme_kit import MemeReel

# (template id, top, bottom, cap quip) — all lines<=2 templates per api.memegen.link/templates
CARDS = [
    ("drake", "an original joke", "drake format, again",
     "the least original meme on earth"),
    ("doge", "much variety", "very same template",
     "wow. such recycling."),
    ("yodawg", "yo dawg i heard you like stereotypes",
     "so we put a stereotype in your stereotype", "xzibit, still typecast"),
    ("success", "used this meme in 2012", "using it again right now",
     "success kid, forever 4 years old"),
    ("fine", "my creative range", "totally fine",
     "the dog knows"),
    ("fry", "not sure if this is funny", "or just extremely recycled",
     "futurama fry, squinting at content"),
    ("grumpycat", "another rapid meme reel", "no.",
     "grumpy cat, unimpressed since 2012"),
    ("mordor", "one does not simply", "avoid every meme cliche",
     "boromir, also typecast"),
    ("philosoraptor", "what if every meme format", "is just the same five jokes",
     "the raptor has a point"),
    ("spiderman", "meme #1", "meme #1, recolored",
     "two spider-men, zero new ideas"),
    ("stonks", "packing 20 memes into 1 gif", "stonks",
     "line only goes up"),
    ("woman-cat", "me, explaining the bit", "the reader, unimpressed",
     "the cat remains unconvinced"),
    ("disastergirl", "this whole gif", "a controlled disaster",
     "she's smiling for a reason"),
    ("persian", "you", "shall not skip this gif",
     "the guardian cat allows no exits"),
    ("boat", "i should", "just make an original joke instead",
     "the cat never does buy the boat"),
    ("bongo", "typing", "the same joke format, again",
     "bongo cat, still drumming the bit"),
    ("cmm", "every meme format is a stereotype", "",
     "change my mind (you can't)"),
]

r = MemeReel(kicker="MEME KIT · STEREOTYPE SPEEDRUN",
             footer="zero original jokes were harmed in this rendering")

n = len(CARDS)
for i, (template, top, bottom, quip) in enumerate(CARDS, 1):
    r.card(template, top, bottom, cap="#%d/%d — %s" % (i, n, quip), hold=1900, intro_frames=4)

r.stinger("CLICHES DEPLOYED", str(n), hold=3000)

S = "/tmp/claude-1000/-home-tom-Documents-claude-CLIPWRIGHT-PLAN/e3426d34-9bc4-4901-8a1a-c4d77e5b7f97/scratchpad/"
print(r.save(S + "speedrun_raw.gif", "/home/tom/Documents/claude/CLIPWRIGHT_PLAN/stereotype_speedrun.gif", colors=64))
