#!/usr/bin/env python3
"""No. 1 for Joseph — THE JOSEPH JOURNAL. Tabloid style. benchod_tg, verbatim quotes.
Deliberately excludes: Joseph's stated opinion on immigration/visa policy and
wages (political-opinion territory, not the harmless self-own the series is
for), and anything about Devin's own scam-adjacent behavior surfaced in the
same search results — not Joseph's, not this episode's business.
"""
import os, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from tabloid_kit import Tabloid
from style_common import scratch_dir

t = Tabloid(
    masthead="THE JOSEPH JOURNAL",
    dateline="REMOTE, CANADA  ·  EMPLOYER: PITCHBOOK  ·  50¢",
    kicker="S E C U R I T Y   E X C L U S I V E",
    headline_lines=["I INTERVIEWED A", "NORTH KOREAN", "SCAMMER"],
    deck="“Holy shit i interviewed a north korean scammer today”",
    col1=("PITCHBOOK — A senior technical screener has confirmed unknowingly "
          "interviewing an operative posing as a US-based engineer."),
    col2=("The screening pipeline uses Codility, which flags a candidate the "
          "moment they switch tabs mid-assessment and files a summary after."),
    starburst_lines=[("IMPOSTOR", 18), ("CONFIRMED", 13)],
    starburst_pos=(514, 448),
    strips=[
        ("HOBBY",  "“I get these emails every week and i call them to just waste their time”"),
        ("CAREER", "“the Fluencer award for spending 80 weeks a week wiring an ACOMP”"),
        ("HIRING", "“tells us live when a candidate goes onto another tab, then gives a summary report”"),
    ],
    also_inside="ALSO INSIDE:  TierZoo's first 2,000 subscribers  ·  20 shares of AMD  ·  studied under a LeCun student",
    stop_press_lines=("STOP PRESS", "ACED IT.  WASN'T EVEN LOOKING."),
)

S = scratch_dir()
print(t.save(S + "joseph_raw.gif", os.path.join(ROOT, "joseph_01_the_journal.gif")))
