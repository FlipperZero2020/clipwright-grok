#!/usr/bin/env python3
"""No. 1 for Jeff Nemish — THE UNITY GAZETTE. Tabloid style. weir_tg, verbatim quotes.
Deliberately excludes: an "allegedly caused a ransomware/cyberattack" claim
against him — sourced only to Devin's ambiguous-tone banter in the chat, not
confirmed, and a serious criminal allegation against a real named person.
This is a hard exclude, not softened or alluded to. Also skipped: a vague
"increased risk of your gf finding out" aside from another participant —
unclear context, not worth the risk of implying something it may not mean.
"""
import os, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from tabloid_kit import Tabloid
from style_common import scratch_dir

t = Tabloid(
    masthead="THE UNITY GAZETTE",
    dateline="UNITY, SASKATCHEWAN  ·  P.ENG (APEGS)  ·  50¢",
    kicker="F I N A N C E   E X C L U S I V E",
    headline_lines=["EVERYTHING BUT", "AN ETF"],
    deck="“I just have a shitload of individual stocks. Hard to get rid of though but may try to buy more ETFs along the way.”",
    col1=("UNITY, SK — A local engineer has confirmed holding concentrated "
          "positions in Nvidia, AMD, TSM and CrowdStrike, roughly $5-6k USD apiece."),
    col2=("One of those bets, AMD, is already up over 100% in 9-10 months. "
          "He says he is ‘considering’ diversifying. No date has been set."),
    starburst_lines=[("AMD", 22), ("+100%", 17)],
    starburst_pos=(514, 448),
    strips=[
        ("LOVE LIFE",    "“I think she is haha. We started in like March”"),
        ("TRAVEL",       "“I'm going to Mexico Dec. 17-24. Just south of cancun”"),
        ("CREDENTIALS",  "“Have good standing as P eng with APEGS”"),
    ],
    also_inside="ALSO INSIDE:  Mazatlan, Nov 29–Dec 6  ·  works with 'Jefe' Devin  ·  lives in a town called Unity",
    stop_press_lines=("STOP PRESS", "STILL HASN'T BOUGHT THE ETF"),
)

S = scratch_dir()
print(t.save(S + "jeff_raw.gif", os.path.join(ROOT, "jeff_01_unity_gazette.gif")))
