"""The gif-episode skill's scripts/plan_episode.py, imported as a module: queue
and ad hoc modes, the empty-queue and unusable-subject error objects, the
pending/done drift warning, the style rotation, filename-safe names, and the
load-bearing one — the personal number it hands out is exactly what
tools/check_series.py will later verify, for every subject in the real ledger."""
import copy
import importlib.util
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, ".claude", "skills", "gif-episode", "scripts", "plan_episode.py")
TOOLS = os.path.join(ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import check_series  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("plan_episode", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


plan_episode = _load()


@pytest.fixture
def state():
    return check_series.load_state()


@pytest.fixture
def root(tmp_path):
    """A stand-in project root holding ep01..ep03, so the next global number is 4."""
    for n in (1, 2, 3):
        (tmp_path / ("ep%02d_x.py" % n)).write_text("")
    return str(tmp_path)


def _done_count(state, subject):
    """How many `done` entries the real ledger already has for `subject` — the
    expectations below are derived from this, not pinned to today's ledger, so
    shipping the next episode per SKILL.md can't turn the suite red."""
    key = check_series.subject_key(subject)
    return sum(check_series.subject_key(e.get("subject")) == key for e in state["done"])


def test_ad_hoc_on_the_real_ledger(state):
    n_alex = _done_count(state, "Alex")
    gnum = plan_episode.next_global_episode_number(ROOT)
    assert n_alex >= 1 and gnum >= 2  # the real ledger, not an empty one
    out = plan_episode.plan(state, ROOT, subject="Alex")
    assert out["mode"] == "ad_hoc"
    assert (out["group"], out["topic_hint"], out["warning"]) == (None, None, None)
    assert out["gif_prefix"] == "alex_%02d_" % (n_alex + 1)
    assert out["personal_episode_number"] == "%02d" % (n_alex + 1)
    assert out["next_script_name"] == "ep%02d_alex.py" % gnum
    assert out["global_episode_number"] == gnum
    assert out["last_style_used"] == state["done"][-1]["style"]
    assert out["recommended_style"] == plan_episode.recommended_style(state["done"][-1]["style"])
    assert out["kit_module"] == out["recommended_style"] + "_kit"
    assert out["project_root"] == ROOT


def test_queue_mode_takes_pending_zero(state, root):
    s = copy.deepcopy(state)
    s["pending"] = [{"subject": "Zed Example", "group": "weir_tg", "topic": "the thing"},
                    {"subject": "Nobody", "group": None, "topic": None}]
    out = plan_episode.plan(s, root)
    assert out["mode"] == "queue"
    assert (out["subject"], out["group"], out["topic_hint"]) == ("Zed Example", "weir_tg", "the thing")
    assert out["gif_prefix"] == "zed_01_"
    assert out["next_script_name"] == "ep04_zed.py"
    assert out["warning"] is None


def test_subject_in_both_lists_sets_warning(state, root):
    s = copy.deepcopy(state)
    s["pending"] = [{"subject": "Alex", "group": "benchod_tg", "topic": "again"}]
    n_alex = _done_count(state, "Alex")
    out = plan_episode.plan(s, root)
    assert "already in `done` %d time(s)" % n_alex in out["warning"]
    assert out["gif_prefix"] == "alex_%02d_" % (n_alex + 1)


def test_ad_hoc_never_warns_about_a_repeat_subject(state, root):
    assert plan_episode.plan(state, root, subject="Alex")["warning"] is None


def test_empty_queue_is_an_error_object(state, root):
    s = copy.deepcopy(state)
    s["pending"] = []
    assert plan_episode.plan(s, root) == {"error": "pending queue is empty", "mode": "queue"}


@pytest.mark.parametrize("subject", ["", "   ", "../", "!!!"])
def test_unusable_subject_is_an_error_object(state, root, subject):
    out = plan_episode.plan(state, root, subject=subject)
    assert set(out) == {"error", "mode"}
    assert out["mode"] == "ad_hoc"
    assert "no filename-safe characters" in out["error"]


def test_null_subject_in_pending_is_an_error_not_a_traceback(state, root):
    s = copy.deepcopy(state)
    s["pending"] = [{"subject": None, "group": None, "topic": None}]
    s["done"].append({"subject": None, "group": None, "style": "meme", "file": "x.gif", "topic": "t"})
    out = plan_episode.plan(s, root)
    assert set(out) == {"error", "mode"}
    assert out["mode"] == "queue"


def test_names_are_filename_safe(state, root):
    out = plan_episode.plan(state, root, subject="../x Y")
    assert out["next_script_name"] == "ep04_x.py"
    assert out["gif_prefix"] == "x_01_"
    out = plan_episode.plan(state, root, subject="Jean-Luc Picard")
    assert out["gif_prefix"] == "jeanluc_01_"
    assert out["next_script_name"] == "ep04_jeanluc.py"


@pytest.mark.parametrize("last, expected", [
    ("tabloid", "ballad"), ("ballad", "meme"), ("meme", "tabloid"), (None, "tabloid"), ("haiku", "tabloid"),
])
def test_style_rotates_off_the_last_done_entry_whoever_its_subject_is(root, last, expected):
    done = [] if last is None else [{"subject": "Someone", "style": last, "file": "someone_01_x.gif"}]
    out = plan_episode.plan({"done": done, "pending": []}, root, subject="Zed")
    assert out["last_style_used"] == last
    assert out["recommended_style"] == expected
    assert out["kit_module"] == expected + "_kit"


def test_first_episode_in_an_empty_root_is_ep01(tmp_path):
    out = plan_episode.plan({"done": [], "pending": []}, str(tmp_path), subject="Zed")
    assert out["global_episode_number"] == 1
    assert out["next_script_name"] == "ep01_zed.py"


def test_personal_numbers_agree_with_check_series(state):
    done = state["done"]
    numbers = check_series.personal_numbers(done)
    for subject in sorted({e["subject"] for e in done}):
        key = check_series.subject_key(subject)
        so_far = max(n for e, n in zip(done, numbers) if check_series.subject_key(e["subject"]) == key)
        out = plan_episode.plan(state, ROOT, subject=subject)
        assert out["personal_episode_number"] == "%02d" % (so_far + 1), subject
        assert out["gif_prefix"] == "%s_%02d_" % (key, so_far + 1), subject


def test_main_prints_the_plan_as_json(state, capsys):
    assert plan_episode.main(["--subject", "Alex"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["subject"] == "Alex"
    assert out["gif_prefix"] == "alex_%02d_" % (_done_count(state, "Alex") + 1)
    assert out["project_root"] == ROOT


def test_main_reports_the_empty_queue_as_json(capsys):
    assert plan_episode.main([]) == 0
    assert json.loads(capsys.readouterr().out) == {"error": "pending queue is empty", "mode": "queue"}
