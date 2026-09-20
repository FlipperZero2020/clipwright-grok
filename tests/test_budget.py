"""Tests for clipwright.budget: presets, the encode ladder, the solver, squeeze."""
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from PIL import Image

from clipwright import budget, ffmpeg
from clipwright.budget import Candidate, budget_bytes, ladder, relative_size, solve, squeeze
from clipwright.ffmpeg import FFmpegError


def _probe(width=1920, fps=30.0):
    return SimpleNamespace(width=width, fps=fps)


# --- budget_bytes ----------------------------------------------------------

@pytest.mark.parametrize("fits, expected", [
    ("telegram", 8_000_000),
    ("Telegram", 8_000_000),
    (" DISCORD ", 10_000_000),
    ("slack", 5_000_000),
    ("whatsapp-sticker", 500_000),
    ("shorts-9x16", 15_000_000),
    ("6", 6_000_000),
    ("6MB", 6_000_000),
    ("6.5mb", 6_500_000),
    ("0.5 MB", 500_000),
    ("6m", 6_000_000),
    ("6 M", 6_000_000),
    (6, 6_000_000),
    (6.5, 6_500_000),
    (None, None),
    ("none", None),
    ("NONE", None),
])
def test_budget_bytes_table(fits, expected):
    assert budget_bytes(fits) == expected


@pytest.mark.parametrize("bad", ["myspace", "", "6GB", "6mbm", "-3", 0, -1, True, [8]])
def test_budget_bytes_rejects_unknown(bad):
    with pytest.raises(ValueError):
        budget_bytes(bad)


@pytest.mark.parametrize("huge", [
    "1" + "0" * 400, int("1" + "0" * 400), float("inf"), "inf", "nan", float("nan"),
    1e300, budget.MAX_MB + 1, str(budget.MAX_MB + 1),
])
def test_budget_bytes_rejects_non_finite_and_absurd_numbers(huge):
    """A ValueError (never OverflowError), so the pipeline can turn it into a RecipeError."""
    with pytest.raises(ValueError):
        budget_bytes(huge)
    assert budget_bytes(budget.MAX_MB) == budget.MAX_MB * budget.BYTES_PER_MB


def test_presets_all_resolve():
    for name, size in budget.PRESETS.items():
        assert budget_bytes(name) == size


# --- ladder -----------------------------------------------------------------

def test_ladder_order_and_full_reach_for_hd_source():
    cands = ladder(_probe(1920, 30.0))
    assert len(cands) == 4 * 5 * 3
    assert cands[0] == Candidate(480, 20, 128)
    # width outermost, then fps, then colours
    assert [c.colors for c in cands[:3]] == [128, 96, 64]
    assert [c.fps for c in cands[:15:3]] == [20, 15, 12, 10, 8]
    assert [c.width for c in cands[::15]] == [480, 400, 320, 240]
    assert cands[-1] == Candidate(240, 8, 64)      # the narrowest rung is always reachable
    assert all(c.width <= 480 for c in cands)
    assert cands == sorted(cands, key=lambda c: (-c.width, -c.fps, -c.colors))


def test_ladder_never_exceeds_source_width():
    src_w = 300
    cands = ladder(_probe(src_w, 30.0))
    assert all(c.width <= src_w for c in cands)
    assert [c.width for c in cands[::15]] == [300, 240]
    # 320 and 400 rungs are dropped because they are wider than the source
    assert {c.width for c in cands} == {300, 240}


def test_ladder_dedupes_width_and_fps_rungs():
    # even(321) == 320 collides with the fixed 320 rung; fps 15 collides with
    # the capped source rate.
    cands = ladder(_probe(321, 15.0))
    assert {c.width for c in cands} == {320, 240}
    assert sorted({c.fps for c in cands}, reverse=True) == [15, 12, 10, 8]
    assert len(cands) == len(set(cands)) == 2 * 4 * 3


def test_ladder_respects_max_width_and_odd_source():
    cands = ladder(_probe(1279, 60.0), max_width=320)
    assert cands[0].width == 320
    # the fixed 400 rung is wider than the cap, so it is dropped
    assert [c.width for c in cands[::15]] == [320, 240]
    assert cands[0].fps == 20


def test_ladder_proxy_cap_is_monotone():
    cands = ladder(_probe(1920, 30.0), max_width=240)
    assert {c.width for c in cands} == {240}
    assert [c.width for c in cands] == sorted((c.width for c in cands), reverse=True)


def test_ladder_drops_fps_rungs_above_source_rate():
    cands = ladder(_probe(640, 12.0))
    assert {c.fps for c in cands} == {12, 10, 8}


def test_ladder_rejects_degenerate_probe():
    with pytest.raises(ValueError):
        ladder(_probe(1, 30.0))
    with pytest.raises(ValueError):
        ladder(_probe(640, 0))


# --- solve ------------------------------------------------------------------

def _fake_encoder(out_dir, calls):
    """Writes a file whose size is width*fps*colors // 100 bytes."""
    def encode(cand):
        calls.append(cand)
        path = os.path.join(out_dir, "%dx%gx%d.gif" % (cand.width, cand.fps, cand.colors))
        with open(path, "wb") as fh:
            fh.write(b"\0" * (int(cand.width * cand.fps * cand.colors) // 100))
        return path
    return encode


def test_solve_stops_at_first_fit(tmp_out):
    cands = ladder(_probe(1920, 30.0))
    calls, log = [], []
    # 480*20*128//100 = 12288, 480*20*96//100 = 9216, 480*20*64//100 = 6144
    cand, path, size = solve(_fake_encoder(tmp_out, calls), cands, 10_000, log=log)
    assert cand == Candidate(480, 20, 96)
    assert size == 9216 == os.path.getsize(path)
    assert calls == cands[:2]
    assert [a["fits"] for a in log] == [False, True]
    assert log[1] == {
        "width": 480, "fps": 20, "colors": 96, "path": path,
        "bytes": 9216, "budget": 10_000, "fits": True,
    }


def test_solve_unbounded_budget_returns_first(tmp_out):
    cands = ladder(_probe(1920, 30.0))
    calls = []
    cand, path, size = solve(_fake_encoder(tmp_out, calls), cands, None)
    assert cand == cands[0]
    assert calls == [cands[0]]
    assert size == os.path.getsize(path)


def test_solve_honours_max_attempts_and_returns_smallest(tmp_out):
    cands = ladder(_probe(1920, 30.0))
    calls, log = [], []
    cand, path, size = solve(_fake_encoder(tmp_out, calls), cands, 1, max_attempts=3, log=log)
    assert len(calls) == 3 == len(log)
    assert cand == cands[2]  # the smallest of the three tried
    assert size == 6144 > 1
    assert not any(a["fits"] for a in log)


def test_solve_default_max_attempts_is_four(tmp_out):
    cands = ladder(_probe(1920, 30.0))
    calls = []
    solve(_fake_encoder(tmp_out, calls), cands, 1)
    assert len(calls) == 4


def test_solve_requires_candidates(tmp_out):
    with pytest.raises(ValueError):
        solve(_fake_encoder(tmp_out, []), [], 1)


# --- solve with a cost estimate ----------------------------------------------

def test_relative_size_is_positive_and_monotone():
    a = Candidate(480, 15, 256)
    assert relative_size(a) == 480 ** 2 * 15 * 8
    assert relative_size(a) > relative_size(Candidate(400, 15, 256)) > relative_size(Candidate(320, 15, 256))
    assert relative_size(a) > relative_size(Candidate(480, 12, 256)) > relative_size(Candidate(480, 12, 128))
    assert all(relative_size(c) > 0 for c in ladder(_probe(1920, 30.0)))


def _model_encoder(out_dir, calls, *, bytes_per_cost, overhead=0):
    """Writes a file of `round(relative_size(cand) * bytes_per_cost) + overhead` bytes."""
    def encode(cand):
        calls.append(cand)
        path = os.path.join(out_dir, "%dx%gx%d.gif" % (cand.width, cand.fps, cand.colors))
        with open(path, "wb") as fh:
            fh.write(b"\0" * (round(relative_size(cand) * bytes_per_cost) + overhead))
        return path
    return encode


def _best_first(probe):
    """A 1920 px source's ladder in the pipeline's order: largest estimate first."""
    return sorted(ladder(probe), key=relative_size, reverse=True)


def test_solve_with_cost_jumps_straight_to_the_rung_that_fits(tmp_out):
    cands = _best_first(_probe(1920, 30.0))
    calls, log = [], []
    # sizes are the estimate to within half a byte: 480/20/128 -> 323, 400/10/64 -> 96,
    # and the next-larger rung 320/15/96 -> 101, so a 100-byte budget needs 400 px
    encode = _model_encoder(tmp_out, calls, bytes_per_cost=1e-5)
    cand, path, size = solve(encode, cands, 100, log=log, cost=relative_size)
    assert cand == Candidate(400, 10, 64) and size == 96 == os.path.getsize(path)
    assert calls == [cands[0], cand]                  # one miss, then the predicted rung
    assert cand == next(c for c in cands if relative_size(c) * 1e-5 <= 100)
    assert [a["fits"] for a in log] == [False, True]


def test_solve_with_cost_recalibrates_after_an_optimistic_estimate(tmp_out):
    cands = _best_first(_probe(1920, 30.0))
    calls = []
    # a fixed per-file overhead the model does not know about makes every
    # prediction optimistic; each miss tightens the ratio until one fits
    encode = _model_encoder(tmp_out, calls, bytes_per_cost=1e-6, overhead=6)
    budget_b = 14
    cand, path, size = solve(encode, cands, budget_b, cost=relative_size)
    tried = list(calls)
    assert size <= budget_b and 2 <= len(tried) <= 4 and cand == tried[-1]
    assert [relative_size(c) for c in tried] == sorted((relative_size(c) for c in tried), reverse=True)
    for earlier, later in zip(tried, tried[1:]):
        # each attempt was predicted to fit from the one before it, which missed
        measured = round(relative_size(earlier) * 1e-6) + 6
        assert measured > budget_b
        assert measured / relative_size(earlier) * relative_size(later) <= budget_b


def test_solve_with_cost_tries_the_cheapest_when_nothing_is_predicted_to_fit(tmp_out):
    cands = _best_first(_probe(1920, 30.0))
    calls, log = [], []
    cand, path, size = solve(_model_encoder(tmp_out, calls, bytes_per_cost=1), cands, 1,
                             log=log, cost=relative_size)
    assert calls == [cands[0], min(cands, key=relative_size)]
    assert cand == Candidate(240, 8, 64) and size == os.path.getsize(path) > 1
    assert len(log) == 2 and not any(a["fits"] for a in log)


def test_solve_with_cost_still_stops_at_the_first_fit(tmp_out):
    cands = _best_first(_probe(1920, 30.0))
    calls = []
    cand, _, _ = solve(_model_encoder(tmp_out, calls, bytes_per_cost=1e-9), cands, 10 ** 6,
                       cost=relative_size)
    assert cand == cands[0] and calls == [cands[0]]


def test_solve_rejects_a_non_positive_cost(tmp_out):
    cands = ladder(_probe(1920, 30.0))
    with pytest.raises(ValueError, match="positive"):
        solve(_fake_encoder(tmp_out, []), cands, 1, cost=lambda c: 0.0)


# --- squeeze ----------------------------------------------------------------

def _tiny_gif(path, frames=6, size=(48, 32)):
    imgs = []
    for i in range(frames):
        im = Image.new("RGB", size, (20 * i, 120, 255 - 30 * i))
        im.paste((255, 255, 0), (4 * i, 4, 4 * i + 12, 20))
        imgs.append(im)
    imgs[0].save(path, save_all=True, append_images=imgs[1:], duration=80, loop=0)
    return path


needs_gifsicle = pytest.mark.skipif(shutil.which("gifsicle") is None, reason="gifsicle not installed")


@needs_gifsicle
def test_squeeze_produces_gif(tmp_out):
    src = _tiny_gif(os.path.join(tmp_out, "in.gif"))
    out = os.path.join(tmp_out, "out.gif")
    log = []
    size = squeeze(src, out, log=log)
    assert os.path.isfile(out)
    assert size == os.path.getsize(out) > 0
    assert log == [["gifsicle", "-O3", "-o", out, src]]
    with Image.open(out) as im:
        assert im.format == "GIF"
        assert im.n_frames == 6


@needs_gifsicle
def test_squeeze_lossy_and_colors_flags(tmp_out):
    src = _tiny_gif(os.path.join(tmp_out, "in.gif"))
    out = os.path.join(tmp_out, "out.gif")
    log = []
    size = squeeze(src, out, lossy=80, colors=16, log=log)
    assert size == os.path.getsize(out) > 0
    assert log == [["gifsicle", "-O3", "--colors", "16", "--lossy=80", "-o", out, src]]


def test_squeeze_validates_inputs(tmp_out):
    with pytest.raises(FileNotFoundError):
        squeeze(os.path.join(tmp_out, "missing.gif"), os.path.join(tmp_out, "o.gif"))
    src = _tiny_gif(os.path.join(tmp_out, "in.gif"))
    with pytest.raises(ValueError):
        squeeze(src, os.path.join(tmp_out, "o.gif"), lossy=999)
    with pytest.raises(ValueError):
        squeeze(src, os.path.join(tmp_out, "o.gif"), colors=1)


def _capture_subprocess(monkeypatch, outcome):
    """Replace the real subprocess.run under ffmpeg.run; returns the kwargs it was given."""
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = list(argv), kw
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)
    return seen


def test_squeeze_failure_is_an_ffmpeg_error_with_the_argv(tmp_out, monkeypatch):
    src = _tiny_gif(os.path.join(tmp_out, "in.gif"))
    out = os.path.join(tmp_out, "out.gif")
    failed = subprocess.CompletedProcess(["gifsicle"], 1, stdout=b"", stderr=b"warning\ngifsicle: bad frame\n")
    seen = _capture_subprocess(monkeypatch, failed)
    log = []
    with pytest.raises(FFmpegError) as info:
        squeeze(src, out, lossy=30, log=log)
    assert isinstance(info.value, RuntimeError)
    assert info.value.argv == ["gifsicle", "-O3", "--lossy=30", "-o", out, src] == log[0]
    assert info.value.returncode == 1 and "bad frame" in info.value.stderr
    assert seen["kw"]["timeout"] == budget.SQUEEZE_TIMEOUT_S
    assert seen["kw"]["stdin"] is subprocess.DEVNULL


def test_squeeze_timeout_raises_instead_of_hanging(tmp_out, monkeypatch):
    src = _tiny_gif(os.path.join(tmp_out, "in.gif"))
    out = os.path.join(tmp_out, "out.gif")
    _capture_subprocess(monkeypatch, subprocess.TimeoutExpired(["gifsicle"], budget.SQUEEZE_TIMEOUT_S))
    with pytest.raises(FFmpegError, match="timed out"):
        squeeze(src, out)


def test_squeeze_never_shells_out_by_itself():
    """gifsicle goes through ffmpeg.run: budget has no subprocess of its own."""
    assert not hasattr(budget, "subprocess")
    assert 0 < budget.SQUEEZE_TIMEOUT_S <= 600
