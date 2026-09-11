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
`style_order` below), always the one after `last_style_used`.

`gif_prefix` is missing its slug on purpose — pick a slug that fits the
episode's masthead/title and append it yourself, e.g. gif_prefix + "memory_leak.gif".

`global_episode_number` is the plain int behind `next_script_name` — use it
for any "NO. X" numbering inside the episode itself (see SKILL.md).

`warning`, when not null, means queue mode found the subject already in
`done` despite also being in `pending` — the ledger is inconsistent. Stop
and surface it instead of picking a side.
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


def next_global_episode_number(root):
    nums = []
    for f in glob.glob(os.path.join(root, "ep*_*.py")):
        m = re.match(r"ep(\d+)_", os.path.basename(f))
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def personal_episode_number(done, subject):
    first = subject.split()[0].lower()
    count = sum(1 for e in done if e.get("subject", "").split()[0].lower() == first)
    return count + 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", default=None, help="Ad hoc mode: subject's name. Omit for queue mode.")
    args = ap.parse_args()

    root = find_root(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "series_state.json")) as f:
        state = json.load(f)

    done = state.get("done", [])
    pending = state.get("pending", [])
    last_style = done[-1]["style"] if done else None
    style_order = ["tabloid", "ballad", "meme"]
    kit_modules = {"tabloid": "tabloid_kit", "ballad": "ballad_kit", "meme": "meme_kit"}
    if last_style in style_order:
        recommended_style = style_order[(style_order.index(last_style) + 1) % len(style_order)]
    else:
        recommended_style = "tabloid"

    mode, group, topic_hint = "ad_hoc", None, None
    subject = args.subject
    if subject is None:
        if not pending:
            print(json.dumps({"error": "pending queue is empty", "mode": "queue"}, indent=2))
            return
        mode = "queue"
        entry = pending[0]
        subject, group, topic_hint = entry.get("subject"), entry.get("group"), entry.get("topic")

    firstname = subject.split()[0]
    pnum = personal_episode_number(done, subject)
    gnum = next_global_episode_number(root)

    warning = None
    if mode == "queue" and pnum > 1:
        warning = (
            "%r is already in `done` %d time(s), but also still showed up in `pending`. "
            "That means the queue and the done list have drifted out of sync somehow "
            "(a stale/uncommitted edit, a manual mistake, two branches). Stop and tell "
            "the user what you found instead of guessing which one is right." % (subject, pnum - 1)
        )

    out = {
        "mode": mode,
        "subject": subject,
        "group": group,
        "topic_hint": topic_hint,
        "global_episode_number": gnum,
        "next_script_name": "ep%02d_%s.py" % (gnum, firstname.lower()),
        "personal_episode_number": "%02d" % pnum,
        "gif_prefix": "%s_%02d_" % (firstname.lower(), pnum),
        "last_style_used": last_style,
        "recommended_style": recommended_style,
        "kit_module": kit_modules[recommended_style],
        "project_root": root,
        "warning": warning,
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
