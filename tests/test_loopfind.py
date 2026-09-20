"""Tests for clipwright.loopfind: frame scoring, loop-point search, degrade, frame sampling."""
import random
import subprocess

import pytest
from PIL import Image, ImageDraw

from clipwright import ffmpeg, loopfind
from clipwright.ffmpeg import FFmpegError, Probe
from clipwright.loopfind import best_loop, degrade, frame_diff, sample_frames

W, H = 64, 48
SQUARE = 12


def _square_frame(x: int, y: int) -> Image.Image:
    img = Image.new("L", (W, H), 0)
    ImageDraw.Draw(img).rectangle([x, y, x + SQUARE - 1, y + SQUARE - 1], fill=255)
    return img


def _noise_frame(seed: int) -> Image.Image:
    return Image.frombytes("L", (W, H), random.Random(seed).randbytes(W * H))


def _square_loop_frames() -> list[Image.Image]:
    """24 frames: a square walks a closed 20-step path (frame 20 == frame 0), then parks elsewhere."""
    path = (
        [(8 + 4 * t, 8) for t in range(5)]
        + [(28, 8 + 4 * t) for t in range(5)]
        + [(28 - 4 * t, 28) for t in range(5)]
        + [(8, 28 - 4 * t) for t in range(5)]
        + [(8, 8)]
        + [(48, 30), (50, 32), (52, 34)]
    )
    assert len(path) == 24 and path[20] == path[0] and path.count(path[0]) == 2
    return [_square_frame(x, y) for x, y in path]


# --- frame_diff -------------------------------------------------------------

def test_frame_diff_identical_is_zero():
    a = _noise_frame(1)
    assert frame_diff(a, a.copy()) == 0.0


def test_frame_diff_black_vs_white_is_255():
    assert frame_diff(Image.new("L", (W, H), 0), Image.new("L", (W, H), 255)) == 255.0


def test_frame_diff_converts_non_grey_modes():
    rgb = Image.new("RGB", (W, H), (255, 255, 255))
    grey = Image.new("L", (W, H), 0)
    assert frame_diff(rgb, grey) == 255.0
    assert frame_diff(grey, rgb.convert("RGBA")) == 255.0


def test_frame_diff_rejects_size_mismatch():
    with pytest.raises(ValueError):
        frame_diff(Image.new("L", (W, H)), Image.new("L", (W, H + 2)))


# --- best_loop --------------------------------------------------------------

def test_best_loop_finds_the_returning_square():
    frames = _square_loop_frames()
    in_idx, out_idx, score = best_loop(frames, window=10)
    assert (in_idx, out_idx) == (0, 20)
    assert score == 0.0
    assert out_idx < len(frames) - 1  # the out-point was nudged inward, not left at the end
    assert degrade(score) == "seamless"


def test_best_loop_noise_scores_high_and_degrades_to_boomerang():
    frames = [_noise_frame(k) for k in range(24)]
    in_idx, out_idx, score = best_loop(frames, window=10)
    assert 0 <= in_idx < 10 and 14 <= out_idx < 24
    assert score > 60.0  # two independent uniform noise frames differ by ~85 on average
    assert degrade(score) == "boomerang"


@pytest.mark.parametrize("n", [0, 1, 2, 3])
def test_best_loop_too_few_frames(n):
    frames = [_noise_frame(k) for k in range(n)]
    assert best_loop(frames) == (0, n - 1, 255.0)


def test_best_loop_four_frames_has_exactly_one_candidate():
    frames = [_noise_frame(k) for k in range(4)]
    in_idx, out_idx, score = best_loop(frames)
    assert (in_idx, out_idx) == (0, 3)
    assert score == frame_diff(frames[0], frames[3])


def test_best_loop_ignores_pairs_closer_than_half_the_clip():
    # frames[1] == frames[5] is a perfect match but 5 <= 1 + 12 // 2, so it is not a loop.
    frames = [_noise_frame(k) for k in range(12)]
    frames[5] = frames[1].copy()
    in_idx, out_idx, score = best_loop(frames, window=10)
    assert out_idx > in_idx + 6
    assert score > 30.0


def test_best_loop_tie_prefers_longest_span_then_earliest_in():
    frames = [Image.new("L", (W, H), 7) for _ in range(30)]
    assert best_loop(frames, window=10) == (0, 29, 0.0)


def test_best_loop_window_limits_the_search():
    frames = _square_loop_frames()
    # window=3: in-points 0..2, out-points 21..23 only; the frame-20 match is out of reach.
    in_idx, out_idx, score = best_loop(frames, window=3)
    assert in_idx <= 2 and out_idx >= 21
    assert score > 0.0


def test_best_loop_rejects_bad_window():
    with pytest.raises(ValueError):
        best_loop(_square_loop_frames(), window=0)


# --- degrade ----------------------------------------------------------------

@pytest.mark.parametrize("score, expected", [
    (0.0, "seamless"),
    (18.0, "seamless"),
    (18.01, "boomerang"),
    (255.0, "boomerang"),
])
def test_degrade_default_threshold(score, expected):
    assert degrade(score) == expected


def test_degrade_custom_threshold():
    assert degrade(25.0, threshold=30.0) == "seamless"
    assert degrade(25.0, threshold=20.0) == "boomerang"


# --- sample_frames (real ffmpeg) ---------------------------------------------

def test_sample_frames_real_clip(test_clip):
    log = []
    frames = sample_frames(test_clip, 0.5, 2.5, fps=10, width=64, log=log)
    assert abs(len(frames) - 20) <= 2
    assert all(f.mode == "L" and f.size == (64, 48) for f in frames)
    assert frame_diff(frames[0], frames[-1]) > 0.0  # testsrc2 moves
    assert len(log) == 1 and log[0][0] == "ffmpeg"


# --- sample_frames (mocked ffmpeg) -------------------------------------------

def _fake_probe(width, height):
    def probe(path):
        return Probe(path=path, duration=3.0, width=width, height=height, fps=30.0,
                     nb_frames=90, vcodec="h264", has_audio=False, size_bytes=1)
    return probe


def _fake_run(payload: bytes, calls: list):
    def run(argv, *, log=None, timeout=600):
        calls.append(list(argv))
        if log is not None:
            log.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr=b"")
    return run


def test_sample_frames_argv_and_frame_split(monkeypatch):
    calls, log = [], []
    payload = b"".join(bytes([k * 10]) * (64 * 48) for k in range(3))
    monkeypatch.setattr(loopfind.ffmpeg, "probe", _fake_probe(320, 240))
    monkeypatch.setattr(loopfind.ffmpeg, "run", _fake_run(payload, calls))

    frames = sample_frames("/clips/in.mp4", 0.5, 2.5, fps=10, width=64, log=log)

    assert [f.getpixel((0, 0)) for f in frames] == [0, 10, 20]
    assert all(f.mode == "L" and f.size == (64, 48) for f in frames)
    (argv,) = calls
    assert log == [argv]
    assert argv[0] == "ffmpeg" and argv[-1] == "pipe:1"
    i_at = argv.index("-i")
    assert argv[i_at + 1] == "/clips/in.mp4"
    for opt, val in (("-ss", "0.500"), ("-to", "2.500")):
        at = argv.index(opt)
        assert at < i_at and argv[at + 1] == val
    assert argv.index("-noautorotate") < i_at
    assert argv[argv.index("-vf") + 1] == "fps=10,scale=64:48,format=gray"
    assert argv[argv.index("-f") + 1] == "rawvideo"
    assert argv[argv.index("-pix_fmt") + 1] == "gray"


def test_sample_frames_fits_the_box_to_the_stored_frame_of_a_rotated_clip(monkeypatch):
    """`ffmpeg.probe` reports a phone's portrait clip as 240x320 (displayed), but this
    sampler passes -noautorotate and so decodes the stored 320x240 frames: the box
    must follow the stored frame or the rawvideo stream no longer splits into whole
    frames of the size the filter was asked for."""
    calls = []

    def probe(path):
        return Probe(path=path, duration=3.0, width=240, height=320, fps=30.0,
                     nb_frames=90, vcodec="h264", has_audio=False, size_bytes=1, rotation=90)

    monkeypatch.setattr(loopfind.ffmpeg, "probe", probe)
    monkeypatch.setattr(loopfind.ffmpeg, "run", _fake_run(bytes(64 * 48 * 2), calls))
    frames = sample_frames("/clips/portrait.mp4", 0.0, 1.0, fps=10, width=64)
    assert len(frames) == 2 and frames[0].size == (64, 48)
    assert calls[0][calls[0].index("-vf") + 1] == "fps=10,scale=64:48,format=gray"
    assert "-noautorotate" in calls[0]


def test_sample_frames_keeps_aspect_for_portrait_source(monkeypatch):
    """A portrait source is `width` tall, not `width` wide: 1080x1920 -> 36x64."""
    calls = []
    monkeypatch.setattr(loopfind.ffmpeg, "probe", _fake_probe(1080, 1920))
    monkeypatch.setattr(loopfind.ffmpeg, "run", _fake_run(bytes(36 * 64 * 2), calls))
    frames = sample_frames("/clips/tall.mp4", 0.0, 1.0, fps=12.5, width=64)
    assert len(frames) == 2 and frames[0].size == (36, 64)
    assert calls[0][calls[0].index("-vf") + 1] == "fps=12.5,scale=36:64,format=gray"


@pytest.mark.parametrize("src_w, src_h, want", [
    (2, 1920, (2, 64)),        # a legal 1920 px-long upload: 128 bytes a frame, not 3.9 MB
    (1920, 2, (64, 2)),
    (1, 1080, (2, 64)),        # never below 2 px on a side
    (64, 64, (64, 64)),
    (63, 64, (62, 64)),        # sides are even
])
def test_sample_frames_bounds_the_sample_to_a_width_box(monkeypatch, src_w, src_h, want):
    calls = []
    w, h = want
    monkeypatch.setattr(loopfind.ffmpeg, "probe", _fake_probe(src_w, src_h))
    monkeypatch.setattr(loopfind.ffmpeg, "run", _fake_run(bytes(w * h * 3), calls))
    frames = sample_frames("/clips/odd.mp4", 0.0, 0.3, fps=10, width=64)
    assert len(frames) == 3 and frames[0].size == want
    assert max(want) == 64 and w * h <= 64 * 64
    assert calls[0][calls[0].index("-vf") + 1] == f"fps=10,scale={w}:{h},format=gray"


def test_number_and_even_helpers_come_from_ffmpeg():
    """One vetted formatter builds every filter token; loopfind keeps no copy."""
    assert loopfind._num is ffmpeg._num and loopfind.even is ffmpeg.even
    assert loopfind._num(12.5) == "12.5" and loopfind._num(10.0) == "10"


@pytest.mark.parametrize("payload", [b"", bytes(64 * 48 * 2 + 1)])
def test_sample_frames_rejects_partial_or_empty_output(monkeypatch, payload):
    monkeypatch.setattr(loopfind.ffmpeg, "probe", _fake_probe(320, 240))
    monkeypatch.setattr(loopfind.ffmpeg, "run", _fake_run(payload, []))
    with pytest.raises(FFmpegError):
        sample_frames("/clips/in.mp4", 0.0, 1.0)


def test_sample_frames_rejects_source_without_dimensions(monkeypatch):
    monkeypatch.setattr(loopfind.ffmpeg, "probe", _fake_probe(0, 0))
    monkeypatch.setattr(loopfind.ffmpeg, "run", _fake_run(b"", []))
    with pytest.raises(FFmpegError):
        sample_frames("/clips/in.mp4", 0.0, 1.0)


@pytest.mark.parametrize("kwargs", [
    dict(from_s=2.0, to_s=1.0),
    dict(from_s=1.0, to_s=1.0),
    dict(from_s=-0.5, to_s=1.0),
    dict(from_s=0.0, to_s=float("inf")),
    dict(from_s=0.0, to_s=1.0, fps=0),
    dict(from_s=0.0, to_s=1.0, fps=float("nan")),
    dict(from_s=0.0, to_s=1.0, width=1),
])
def test_sample_frames_validates_before_touching_ffmpeg(monkeypatch, kwargs):
    def boom(*a, **k):
        raise AssertionError("ffmpeg must not be called")
    monkeypatch.setattr(loopfind.ffmpeg, "probe", boom)
    monkeypatch.setattr(loopfind.ffmpeg, "run", boom)
    with pytest.raises(ValueError):
        sample_frames("/clips/in.mp4", **kwargs)


def test_ffmpeg_module_untouched_after_monkeypatch():
    assert ffmpeg.run.__module__ == "clipwright.ffmpeg"
    assert ffmpeg.probe.__module__ == "clipwright.ffmpeg"
