#!/usr/bin/env python3
"""check_series — lint series_state.json against the GIFs actually on disk.

The ledger is the source of truth for the rotation, and GIF_INDEX.md is
generated from it, so it has to agree with reality. This prints every
problem it can find and exits 1 if there were any, or prints a one-line
summary and exits 0. Checks:

  * every `done` entry has a non-empty, single-line string `subject`, `style`,
    `file` and `topic`, and every `one_offs` entry a `file`, `style_base` and
    `topic` — exactly the keys gen_gif_index.py indexes, so a ledger this
    signs off can always be rendered into GIF_INDEX.md
  * every `done` / `one_offs` file is a plain root-level *.gif name (no
    directory part) and exists at the repo root
  * every root *.gif is listed in exactly one of `done` / `one_offs`
  * no subject is in both `pending` and `done` (plan_episode.py would warn)
  * every `done` style is a key of `styles`
  * every `done` file starts with its subject's first name, and the personal
    episode numbers in filenames (`<first>_NN_`) match the entry's position
    among that subject's `done` entries — i.e. they're contiguous and in
    ledger order, which is exactly how plan_episode.py hands out the next one

Usage: python3 tools/check_series.py [--state PATH] [--root DIR]
"""
import argparse
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "series_state.json")


def load_state(path: str = STATE_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def subject_key(subject: str | None) -> str:
    """First name, lowercased and stripped to [a-z0-9] — the filename-safe identity
    plan_episode.py numbers episodes by and prefixes their files with. "" for a
    missing or blank subject, or one with no usable characters at all."""
    first = (subject or "").split()[:1]
    return re.sub(r"[^a-z0-9]+", "", first[0].lower()) if first else ""


REQUIRED_KEYS = {"done": ("subject", "style", "file", "topic"),
                 "one_offs": ("file", "style_base", "topic")}


def _label(e: dict) -> str:
    """How a problem names an entry: its file when that is usable, else its subject/topic."""
    f = e.get("file")
    return f if isinstance(f, str) and f.strip() else "entry %r" % (e.get("subject") or e.get("topic"))


def _shape_problems(kind: str, entries: list[dict]) -> list[str]:
    """Every key gen_gif_index.py reads off a `kind` entry must be there, a
    non-blank string, and on one line (a line break would split its table row)."""
    problems = []
    for e in entries:
        for key in REQUIRED_KEYS[kind]:
            v = e.get(key)
            if not isinstance(v, str) or not v.strip():
                problems.append("%s: %s is missing %r" % (kind, _label(e), key))
            elif "\n" in v or "\r" in v:
                problems.append("%s: %s has a line break in %r" % (kind, _label(e), key))
    return problems


def personal_numbers(done: list[dict]) -> list[int]:
    """The 1-based personal episode number of every `done` entry, by position
    among that subject's entries, aligned with `done`."""
    seen: dict[str, int] = {}
    out = []
    for e in done:
        k = subject_key(e.get("subject"))
        seen[k] = seen.get(k, 0) + 1
        out.append(seen[k])
    return out


def _numbering_problems(done: list[dict]) -> list[str]:
    problems = []
    for e, expected in zip(done, personal_numbers(done)):
        subject, f = e.get("subject"), e.get("file")
        if not isinstance(f, str) or not isinstance(subject, str):
            continue  # already reported by _shape_problems
        k = subject_key(subject)
        if not f.startswith(k + "_"):
            problems.append("done: %s is filed under %r but doesn't start with %r" % (f, subject, k + "_"))
            continue
        m = re.match(r"%s_(\d{2})_" % re.escape(k), f)
        if m and int(m.group(1)) != expected:
            problems.append("done: %s is numbered %s but is %s's episode %02d in ledger order "
                            "(personal numbers must be contiguous)" % (f, m.group(1), subject, expected))
    return problems


def find_problems(state: dict, root: str = ROOT) -> list[str]:
    """Every inconsistency between the ledger and the repo, as one message each."""
    problems = []
    done = state.get("done", [])
    pending = state.get("pending", [])
    one_offs = state.get("one_offs", [])
    styles = state.get("styles", {})

    listed: list[str] = []
    for kind, entries in (("done", done), ("one_offs", one_offs)):
        problems.extend(_shape_problems(kind, entries))
        for e in entries:
            f = e.get("file")
            if not isinstance(f, str) or not f:
                continue  # already reported by _shape_problems
            if f != os.path.basename(f) or not f.endswith(".gif"):
                problems.append("%s: %r is not a plain root-level .gif filename" % (kind, f))
                continue
            listed.append(f)
            if not os.path.isfile(os.path.join(root, f)):
                problems.append("%s: %s is missing on disk" % (kind, f))
    for f in sorted({f for f in listed if listed.count(f) > 1}):
        problems.append("%s is listed more than once across done/one_offs" % f)

    for path in sorted(glob.glob(os.path.join(root, "*.gif"))):
        f = os.path.basename(path)
        if f not in listed:
            problems.append("%s is on disk but in neither done nor one_offs" % f)

    done_keys = {subject_key(e.get("subject")) for e in done}
    for e in pending:
        if subject_key(e.get("subject")) in done_keys:
            problems.append("%r is in both pending and done" % e.get("subject"))

    for e in done:
        if isinstance(e.get("style"), str) and e["style"] not in styles:
            problems.append("done: %s has style %r, not one of %s"
                            % (_label(e), e["style"], sorted(styles)))

    problems.extend(_numbering_problems(done))
    return problems


def summary(state: dict, root: str = ROOT) -> str:
    n_gifs = len(glob.glob(os.path.join(root, "*.gif")))
    return "series_state.json OK: %d done, %d one_offs, %d pending, %d gifs on disk" % (
        len(state.get("done", [])), len(state.get("one_offs", [])),
        len(state.get("pending", [])), n_gifs)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state", default=STATE_PATH)
    ap.add_argument("--root", default=ROOT, help="directory holding the *.gif files")
    args = ap.parse_args(argv)
    state = load_state(args.state)
    problems = find_problems(state, args.root)
    if problems:
        for p in problems:
            print("PROBLEM: " + p)
        print("%d problem(s) in %s" % (len(problems), args.state))
        return 1
    print(summary(state, args.root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
