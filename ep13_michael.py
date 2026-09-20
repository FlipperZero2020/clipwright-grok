#!/usr/bin/env python3
"""No. 1 for Michael — THE WHISTLEBLOWER. Tabloid style. One-off: Michael
isn't a benchod_tg/weir_tg subject, this is a Microsoft Teams DM screenshot
the user pasted directly, not a graphiti-ingested episode. Quotes are
verbatim from that screenshot. The unnamed knock-out-round opponent stays
unnamed per house rules (quote drags in a third party the source itself
doesn't name). Nothing else to exclude — no health/finance/immigration/
misconduct content, and the whole thing is Michael's own words about his own
hobby.
"""
import os, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from tabloid_kit import Tabloid
from style_common import scratch_dir

t = Tabloid(
    masthead="THE WHISTLEBLOWER",
    dateline="OPEN-PLAN OFFICE  ·  BYLINE: A COWORKER  ·  50¢",
    kicker="W H I S T L I N G   E X C L U S I V E",
    headline_lines=["BEAT THE ROBIN,", "LOST TO THE 'STACHE"],
    deck="“You have to out whistle an actual robin, it's pretty tough”",
    col1=("Local whistler cleared the qualifying round the hard way — a "
          "straight head-to-head against an actual robin, a result he "
          "still calls, in his own words, 'pretty tough.'"),
    col2=("The run ended in the knock-out rounds against a competitor with "
          "a handlebar mustache, who'd reportedly twist the stache to hit "
          "his high notes. The crowd, by all accounts, loved it."),
    starburst_lines=[("2030", 24), ("ALL GAS", 12), ("NO BRAKES", 12)],
    starburst_pos=(514, 448),
    strips=[
        ("QUALIFYING",     "“You have to out whistle an actual robin, it's pretty tough”"),
        ("KNOCK-OUT ROUND", "“got beat out by a guy with a handlebar mustache in the knock-out rounds”"),
        ("TRAINING",       "“I started training for 2030 in 2020 man. All gas, no brakes.”"),
    ],
    also_inside=("ALSO INSIDE:  didn't make the round of 8  ·  chant confirmed: “U-S-A, U-S-A”  ·  "
                 "open floor plan requests earplugs"),
    stop_press_lines=("STOP PRESS", "ALL GAS, NO BRAKES"),
)

S = scratch_dir()
print(t.save(S + "michael_01_raw.gif", os.path.join(ROOT, "michael_01_whistleblower.gif")))
