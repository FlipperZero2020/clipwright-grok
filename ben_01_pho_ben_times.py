#!/usr/bin/env python3
"""No. 1 for Ben — THE PHO BEN TIMES. Tabloid style. benchod_tg, verbatim quotes.
Deliberately excludes: "Ben works for ICE" (real, named individual + a
politically live, safety-sensitive employer — pure downside, zero comedic
upside, excluded outright); a signal-jammer reference (operating one is
illegal, and attribution to Ben specifically wasn't even clear); and the
crude literal meaning of "Ben Chod" itself — it's the group's own inside
joke and the origin of the chat's name, but there is no need to spell out
the slur to make the "the bot outlives him" joke land.
"""
import os, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from tabloid_kit import Tabloid
from style_common import scratch_dir

t = Tabloid(
    masthead="THE PHO BEN TIMES",
    dateline="FLUENCE ANALYTICS, RET.  ·  FEB 2024  ·  50¢",
    kicker="L E G A C Y   E X C L U S I V E",
    headline_lines=["ACT LIKE BEN", "NEVER LEFT"],
    deck="“I lost the api key... you may just run until the account gets shut off”",
    col1=("FLUENCE — He left the company in February 2024, farewell lunch "
          "at Pho Ben, and by all accounts did not come back."),
    col2=("The chatbot built in his name did not get the memo. It runs on "
          "a lost API key, with no one able to shut it off."),
    starburst_lines=[("STILL", 22), ("RUNNING", 16)],
    starburst_pos=(514, 448),
    strips=[
        ("CAREER",   "“I am just the code monkey”"),
        ("FAREWELL", "“farewell Ben lunch at pho Ben right now, hurry up!”"),
        ("LEGAL",    "“don’t sign it until we actually get that document”"),
    ],
    also_inside="ALSO INSIDE:  a Japanese dog  ·  last seen moving west  ·  the bot outlives the API key",
    stop_press_lines=("STOP PRESS", "NEVER ACTUALLY LEFT"),
)

S = scratch_dir()
print(t.save(S + "ben_raw.gif", os.path.join(ROOT, "ben_01_pho_ben_times.gif")))
