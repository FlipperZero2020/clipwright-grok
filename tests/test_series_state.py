"""tools/check_series.py and tools/gen_gif_index.py, exercised as importable
functions against the real ledger (which must be clean and must match the
committed GIF_INDEX.md) and against tampered copies of it (each kind of
inconsistency must be reported, and main() must exit non-zero listing all of
them)."""
import copy
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import check_series  # noqa: E402
import gen_gif_index  # noqa: E402


@pytest.fixture
def state():
    return check_series.load_state()


def _problems(state):
    return check_series.find_problems(state, ROOT)


def _has(problems, needle):
    return any(needle in p for p in problems)


# ── check_series: the real ledger ────────────────────────────────────────────

def test_real_ledger_has_no_problems(state):
    assert _problems(state) == []


def test_real_ledger_accounts_for_every_root_gif(state):
    listed = [e["file"] for e in state["done"]] + [e["file"] for e in state["one_offs"]]
    on_disk = {f for f in os.listdir(ROOT) if f.endswith(".gif")}
    assert set(listed) == on_disk
    assert len(listed) == len(set(listed))


def test_main_prints_one_line_summary_and_exits_zero(capsys):
    assert check_series.main([]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    assert out[0].startswith("series_state.json OK:")


# ── check_series: tampered copies ────────────────────────────────────────────

def test_missing_file_on_disk_is_reported(state):
    s = copy.deepcopy(state)
    s["done"].append({"subject": "Alex", "group": "benchod_tg", "style": "tabloid",
                      "file": "alex_05_ghost.gif", "topic": "never rendered"})
    assert _has(_problems(s), "done: alex_05_ghost.gif is missing on disk")


def test_missing_one_off_on_disk_is_reported(state):
    s = copy.deepcopy(state)
    s["one_offs"].append({"file": "vapour.gif", "style_base": "none", "topic": "n/a"})
    assert _has(_problems(s), "one_offs: vapour.gif is missing on disk")


def test_unlisted_root_gif_is_reported(state):
    s = copy.deepcopy(state)
    dropped = s["one_offs"].pop(0)["file"]
    assert _has(_problems(s), "%s is on disk but in neither done nor one_offs" % dropped)


def test_subject_in_both_pending_and_done_is_reported(state):
    s = copy.deepcopy(state)
    s["pending"].append({"subject": "Alex", "group": "benchod_tg", "topic": "again"})
    assert _has(_problems(s), "'Alex' is in both pending and done")


def test_unknown_style_is_reported(state):
    s = copy.deepcopy(state)
    s["done"][0]["style"] = "haiku"
    assert _has(_problems(s), "has style 'haiku', not one of")


def test_non_contiguous_personal_numbering_is_reported(state):
    s = copy.deepcopy(state)
    alex = [e for e in s["done"] if e["subject"] == "Alex"]
    alex[2]["file"] = "alex_05_model_at_capacity.gif"
    assert _has(_problems(s), "alex_05_model_at_capacity.gif is numbered 05 but is Alex's episode 03")


def test_file_not_starting_with_subject_is_reported(state):
    s = copy.deepcopy(state)
    s["done"][0]["file"] = "ben_01_pho_ben_times.gif"
    assert _has(_problems(s), "is filed under 'Justin' but doesn't start with 'justin_'")


def test_file_that_is_not_a_plain_root_gif_name_is_reported(state):
    s = copy.deepcopy(state)
    escaped = "../CLIPWRIGHT_PLAN/" + s["one_offs"][0]["file"]  # resolves to a real file, still wrong
    s["one_offs"].append({"file": "/etc/hostname", "style_base": "x", "topic": "x"})
    s["one_offs"].append({"file": escaped, "style_base": "x", "topic": "x"})
    s["done"].append({"subject": "Alex", "group": "benchod_tg", "style": "tabloid",
                      "file": "alex_05_notes.txt", "topic": "x"})
    problems = _problems(s)
    assert _has(problems, "one_offs: '/etc/hostname' is not a plain root-level .gif filename")
    assert _has(problems, "one_offs: %r is not a plain root-level .gif filename" % escaped)
    assert _has(problems, "done: 'alex_05_notes.txt' is not a plain root-level .gif filename")
    assert not _has(problems, "missing on disk")


@pytest.mark.parametrize("key", ["subject", "style", "file", "topic"])
def test_done_entry_missing_a_key_gen_gif_index_needs_is_reported(state, key):
    """The checker's OK must guarantee gen_gif_index can render the ledger, so
    every key it indexes has to be there, whether absent, null or blank."""
    for tamper in (lambda e: e.pop(key), lambda e: e.__setitem__(key, None),
                   lambda e: e.__setitem__(key, "   ")):
        s = copy.deepcopy(state)
        e = s["done"][-1]
        tamper(e)
        problems = _problems(s)
        label = e["file"] if key != "file" else "entry %r" % e["subject"]
        assert _has(problems, "done: %s is missing %r" % (label, key)), problems


@pytest.mark.parametrize("key", ["file", "style_base", "topic"])
def test_one_off_missing_a_key_gen_gif_index_needs_is_reported(state, key):
    s = copy.deepcopy(state)
    e = s["one_offs"][0]
    del e[key]
    label = e["file"] if key != "file" else "entry %r" % e["topic"]
    assert _has(_problems(s), "one_offs: %s is missing %r" % (label, key))


def test_checker_ok_means_gen_gif_index_renders(state):
    """Every tampered ledger gen_gif_index would crash on is caught by the checker."""
    for tamper in (lambda s: s["done"][-1].pop("topic"), lambda s: s["done"][-1].__setitem__("subject", None),
                   lambda s: s["one_offs"][0].pop("style_base"), lambda s: s["done"][0].__setitem__("file", None)):
        s = copy.deepcopy(state)
        tamper(s)
        with pytest.raises((KeyError, AttributeError, TypeError)):
            gen_gif_index.render_index(s)
        assert _problems(s)


def test_line_break_in_a_cell_string_is_reported(state):
    s = copy.deepcopy(state)
    s["done"][0]["topic"] = "line one\n| injected | row |"
    s["one_offs"][0]["style_base"] = "a\rb"
    problems = _problems(s)
    assert _has(problems, "done: %s has a line break in 'topic'" % s["done"][0]["file"])
    assert _has(problems, "one_offs: %s has a line break in 'style_base'" % s["one_offs"][0]["file"])


def test_null_file_does_not_crash_the_numbering_check(state):
    s = copy.deepcopy(state)
    s["done"][-1]["file"] = None
    s["done"][0]["subject"] = None
    problems = _problems(s)
    assert _has(problems, "is missing 'file'")
    assert _has(problems, "is missing 'subject'")
    assert not any("filed under None" in p for p in problems)


def test_subject_key_is_first_name_lowercased_and_filename_safe():
    assert check_series.subject_key("Tyler Quinlan") == "tyler"
    assert check_series.subject_key("Jean-Luc P.") == "jeanluc"
    assert check_series.subject_key("  alex ") == "alex"
    for empty in (None, "", "   ", "../", "!!!"):
        assert check_series.subject_key(empty) == ""


def test_main_lists_every_problem_and_exits_nonzero(state, tmp_path, capsys):
    s = copy.deepcopy(state)
    s["done"][0]["style"] = "haiku"
    s["pending"].append({"subject": "Alex", "group": "benchod_tg", "topic": "again"})
    path = tmp_path / "state.json"
    path.write_text(json.dumps(s))
    assert check_series.main(["--state", str(path)]) == 1
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.startswith("PROBLEM: ")]
    assert len(lines) == len(check_series.find_problems(s, ROOT)) == 2
    assert "2 problem(s)" in out


# ── gen_gif_index ────────────────────────────────────────────────────────────

def test_committed_index_is_current(state):
    with open(os.path.join(ROOT, "GIF_INDEX.md"), encoding="utf-8") as fh:
        assert fh.read() == gen_gif_index.render_index(state)


def test_counts_match_the_ledger(state):
    text = gen_gif_index.render_index(state)
    n_subjects = len({check_series.subject_key(e["subject"]) for e in state["done"]})
    assert "**%d series**" % n_subjects in text
    assert "**%d episodes** total" % len(state["done"]) in text
    assert "**%d one-off**" % len(state["one_offs"]) in text
    assert "**%d GIFs** total" % (len(state["done"]) + len(state["one_offs"])) in text


def test_null_group_renders_as_dash():
    s = {"done": [{"subject": "Michael", "group": None, "style": "tabloid",
                   "file": "michael_01_x.gif", "topic": "t"}], "one_offs": []}
    text = gen_gif_index.render_index(s)
    assert "| Michael | — | 01 | tabloid | [michael_01_x.gif](michael_01_x.gif) | t |" in text
    assert "- — subjects: Michael (1)" in text


def test_personal_numbers_follow_ledger_order_and_rows_sort_by_subject():
    s = {"done": [
        {"subject": "Justin", "group": "g", "style": "ballad", "file": "justin_ballad.gif", "topic": "a"},
        {"subject": "Alex", "group": "g", "style": "tabloid", "file": "alex_01_x.gif", "topic": "b"},
        {"subject": "Alex", "group": "g", "style": "ballad", "file": "alex_02_y.gif", "topic": "c"},
    ], "one_offs": []}
    rows = gen_gif_index.episode_rows(s["done"])
    assert [r.split(" | ")[2] for r in rows] == ["01", "02", "01"]
    assert [r.split(" | ")[0].strip("| ") for r in rows] == ["Alex", "Alex", "Justin"]


def test_link_escapes_the_label_and_percent_encodes_the_target():
    assert gen_gif_index.link("plain_01_x.gif") == "[plain_01_x.gif](plain_01_x.gif)"
    assert gen_gif_index.link("a b|c].gif") == "[a b\\|c\\].gif](a%20b%7Cc%5D.gif)"
    assert gen_gif_index.link("x).gif") == "[x).gif](x%29.gif)"


def test_notes_describe_the_global_style_rotation(state):
    text = gen_gif_index.render_index(state)
    assert "per subject: tabloid" not in text
    assert "Style rotation is global, not per subject" in text
    assert "most recent `done` entry" in text


def test_pipes_in_cells_are_escaped():
    s = {"done": [], "one_offs": [{"file": "x.gif", "style_base": "a|b", "topic": "c | d"}]}
    text = gen_gif_index.render_index(s)
    assert "| a\\|b | c \\| d |" in text


def test_line_breaks_in_cells_cannot_split_or_inject_a_row():
    s = {"done": [{"subject": "Alex", "group": "g", "style": "tabloid", "file": "alex_01_x.gif",
                   "topic": "line one\n| injected | row |"}],
         "one_offs": [{"file": "x.gif", "style_base": "a\r\nb", "topic": "tab\there"}]}
    text = gen_gif_index.render_index(s)
    assert "| Alex | g | 01 | tabloid | [alex_01_x.gif](alex_01_x.gif) | line one \\| injected \\| row \\| |" in text
    assert "| [x.gif](x.gif) | a b | tab here |" in text
    assert not any(line.startswith("\\|") for line in text.splitlines())


def test_main_writes_the_index(tmp_path, capsys):
    out = tmp_path / "INDEX.md"
    assert gen_gif_index.main(["--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == gen_gif_index.render_index(check_series.load_state())
    assert "wrote %s" % out in capsys.readouterr().out
