#!/usr/bin/env python3
"""Compute the next episode's identifiers so Claude never has to eyeball a
file listing and guess a number. Read-only: this script writes nothing.

Usage:
    python3 plan_episode.py                  # queue mode: reads pending[0] from series_state.json
    python3 plan_episode.py --subject "Alex"  # ad hoc mode: subject supplied explicitly

Prints one JSON object to stdout, e.g.:
    {
      "mode": "ad_hoc",
      "subject": "Alex",
      "group": null,
      "topic_hint": null,
      "global_episode_number": 11,
      "next_script_name": "ep11_alex.py",
      "personal_episode_number": "03",
      "gif_prefix": "alex_03_",
      "last_style_used": "tabloid",
      "recommended_style": "ballad",
      "kit_module": "ballad_kit",
      "project_root": "/home/tom/Documents/claude/CLIPWRIGHT_PLAN",
      "warning": null
    }

`recommended_style` cycles tabloid -> ballad -> meme -> tabloid -> ... (see
STYLE_ORDER below), always the one after `last_style_used`.

`gif_prefix` is missing its slug on purpose — pick a slug that fits the
episode's masthead/title and append it yourself, e.g. gif_prefix + "memory_leak.gif".

`global_episode_number` is the plain int behind `next_script_name` — use it
for any "NO. X" numbering inside the episode itself (see SKILL.md).

`warning`, when not null, means queue mode found the subject already in
`done` despite also being in `pending` — the ledger is inconsistent. Stop
and surface it instead of picking a side.

An object with `error` (and `mode`) instead of the fields above means there
was nothing to plan: the queue is empty, or the subject is blank / has no
filename-safe characters. Surface it the same way.

The subject's identity (first name, lowercased, [a-z0-9] only — so it is safe
to build a filename from) and its personal numbering come from
tools/check_series.py, so the planner hands out exactly the numbers the
checker later verifies.
"""
import argparse
import glob
import json
import os
import re
import sys


def find_root(start):
    d = os.path.abspath(start)
    while True:
        if os.path.exists(os.path.join(d, "series_state.json")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            sys.exit("could not find series_state.json above " + start)
        d = parent


ROOT = find_root(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from check_series import personal_numbers, subject_key  # noqa: E402

STYLE_ORDER = ["tabloid", "ballad", "meme"]
KIT_MODULES = {"tabloid": "tabloid_kit", "ballad": "ballad_kit", "meme": "meme_kit"}


def next_global_episode_number(root):
    nums = []
    for f in glob.glob(os.path.join(root, "ep*_*.py")):
        m = re.match(r"ep(\d+)_", os.path.basename(f))
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def recommended_style(last_style):
    """The style after `last_style` in STYLE_ORDER; tabloid when there is no usable last one."""
    if last_style in STYLE_ORDER:
        return STYLE_ORDER[(STYLE_ORDER.index(last_style) + 1) % len(STYLE_ORDER)]
    return "tabloid"


def plan(state, root, subject=None):
    """The next episode's identifiers (see the module docstring) for the ledger
    `state` and project `root`: ad hoc mode when `subject` is given, queue mode
    (pending[0]) otherwise. Returns {"error": ..., "mode": ...} when there is
    nothing to plan instead of raising."""
    done = state.get("done", [])
    pending = state.get("pending", [])
    last_style = done[-1].get("style") if done else None

    mode, group, topic_hint = "ad_hoc", None, None
    if subject is None:
        if not pending:
            return {"error": "pending queue is empty", "mode": "queue"}
        mode = "queue"
        entry = pending[0]
        subject, group, topic_hint = entry.get("subject"), entry.get("group"), entry.get("topic")

    key = subject_key(subject)
    if not key:
        return {"error": "subject %r is empty or has no filename-safe characters" % (subject,),
                "mode": mode}
    pnum = personal_numbers(done + [{"subject": subject}])[-1]
    gnum = next_global_episode_number(root)

    warning = None
    if mode == "queue" and pnum > 1:
        warning = (
            "%r is already in `done` %d time(s), but also still showed up in `pending`. "
            "That means the queue and the done list have drifted out of sync somehow "
            "(a stale/uncommitted edit, a manual mistake, two branches). Stop and tell "
            "the user what you found instead of guessing which one is right." % (subject, pnum - 1)
        )

    style = recommended_style(last_style)
    return {
        "mode": mode,
        "subject": subject,
        "group": group,
        "topic_hint": topic_hint,
        "global_episode_number": gnum,
        "next_script_name": "ep%02d_%s.py" % (gnum, key),
        "personal_episode_number": "%02d" % pnum,
        "gif_prefix": "%s_%02d_" % (key, pnum),
        "last_style_used": last_style,
        "recommended_style": style,
        "kit_module": KIT_MODULES[style],
        "project_root": root,
        "warning": warning,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--subject", default=None, help="Ad hoc mode: subject's name. Omit for queue mode.")
    args = ap.parse_args(argv)
    with open(os.path.join(ROOT, "series_state.json"), encoding="utf-8") as fh:
        state = json.load(fh)
    print(json.dumps(plan(state, ROOT, args.subject), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
