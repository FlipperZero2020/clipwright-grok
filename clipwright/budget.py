"""Size-budget solver and platform presets.

A recipe says where its GIF has to fit (`fits = "telegram"`, `"6MB"`, `6.5`
or `"none"`); this module turns that into a byte budget, builds a best-first
ladder of encode candidates (width x fps x colours) from a probe of the
source, and walks the ladder until one encode lands under budget. A final
`squeeze` pass hands the winner to gifsicle for lossless (or `--lossy`)
re-optimisation.

It never assembles an ffmpeg command; the only external process it starts is
gifsicle, and that goes through `ffmpeg.run` like every other media call, so
it gets the same argv logging, timeout and `FFmpegError` reporting.
"""
from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from clipwright import ffmpeg
from clipwright.ffmpeg import even

PRESETS: dict[str, int] = {
    "telegram": 8_000_000,
    "discord": 10_000_000,
    "slack": 5_000_000,
    "whatsapp-sticker": 500_000,
    "shorts-9x16": 15_000_000,
}

BYTES_PER_MB = 1_000_000
MAX_MB = 2000                    # a numeric `fits` above this is a typo, not a budget
SQUEEZE_TIMEOUT_S = 300

_WIDTH_RUNGS = (400, 320, 240)
_FPS_RUNGS = (15, 12, 10, 8)     # 8 is the fps knob's floor, so fps=8 still has fallbacks
_COLOR_RUNGS = (128, 96, 64)
_MB_SUFFIX = re.compile(r"\s*mb?$")


class _ProbeLike(Protocol):
    width: int
    fps: float


@dataclass(frozen=True)
class Candidate:
    """One point on the encode ladder."""

    width: int
    fps: float
    colors: int


def budget_bytes(fits: str | int | float | None) -> int | None:
    """Resolve a `fits` value to a byte budget; None means unbounded.

    Accepts a preset name (case-insensitive), a number of megabytes as an
    int/float or a string ("6", "6MB", "6.5 mb", "6m"), and None / "none".
    Raises ValueError for anything else, including a non-finite or absurd
    (over MAX_MB) number.
    """
    if fits is None:
        return None
    if isinstance(fits, bool):
        raise ValueError("fits must be a preset name or a number of MB, not a bool")
    if isinstance(fits, (int, float)):
        try:
            return _mb_to_bytes(float(fits), fits)
        except OverflowError:
            raise ValueError("fits must be at most %d MB; got %r" % (MAX_MB, fits)) from None
    if not isinstance(fits, str):
        raise ValueError("fits must be a preset name, a number of MB, or None; got %r" % (fits,))

    key = fits.strip().lower()
    if key == "none":
        return None
    if key in PRESETS:
        return PRESETS[key]
    try:
        mb = float(_MB_SUFFIX.sub("", key))
    except ValueError:
        raise ValueError(
            "unknown fits preset %r (known: %s)" % (fits, ", ".join(PRESETS))
        ) from None
    return _mb_to_bytes(mb, fits)


def _mb_to_bytes(mb: float, original: object) -> int:
    if not math.isfinite(mb) or mb <= 0:
        raise ValueError("fits must be a positive number of MB; got %r" % (original,))
    if mb > MAX_MB:
        raise ValueError("fits must be at most %d MB; got %r" % (MAX_MB, original))
    return int(round(mb * BYTES_PER_MB))


def _dedupe(values: Iterable) -> list:
    seen: list = []
    for v in values:
        if v not in seen:
            seen.append(v)
    return seen


def ladder(probe: _ProbeLike, *, max_width: int = 480) -> list[Candidate]:
    """Best-first encode candidates for a source described by `probe`.

    Widths: the source width (evened) capped at `max_width`, then 400/320/240.
    Fps: the source rate capped at 20, then 15/12/10/8. Colours: 128/96/64.
    Fixed rungs wider or faster than the capped top rung are dropped, so the
    list is monotone and no candidate ever exceeds the source (or, in proxy
    mode, `max_width`). The cross product is ordered width-outermost, then
    fps, then colours; it is never truncated, so the narrowest rung is always
    reachable when `solve` needs it.
    """
    src_w = int(probe.width)
    src_fps = float(probe.fps)
    if src_w < 2 or src_fps <= 0:
        raise ValueError("probe must have width >= 2 and fps > 0; got width=%d fps=%s" % (src_w, src_fps))

    top_w = min(int(max_width), even(src_w))
    widths = _dedupe(w for w in (top_w, *_WIDTH_RUNGS) if w <= top_w)
    top_fps = min(src_fps, 20)
    fpss = _dedupe(f for f in (top_fps, *_FPS_RUNGS) if f <= top_fps)

    return [
        Candidate(width=w, fps=f, colors=c)
        for w in widths
        for f in fpss
        for c in _COLOR_RUNGS
    ]


def relative_size(cand: Candidate) -> float:
    """A candidate's GIF size relative to its neighbours (any unit, always > 0).

    Pixels per frame go with width squared (the height follows the aspect),
    frames with fps, and bytes per pixel roughly with the palette's bit depth.
    """
    return cand.width ** 2 * cand.fps * math.log2(cand.colors)


def solve(
    encode: Callable[[Candidate], str],
    candidates: Iterable[Candidate],
    budget: int | None,
    *,
    max_attempts: int = 4,
    log: list | None = None,
    cost: Callable[[Candidate], float] | None = None,
) -> tuple[Candidate, str, int]:
    """Walk `candidates` best-first, encoding until one fits `budget`.

    `encode(cand)` must return the path of the file it wrote. Returns
    `(candidate, path, size_bytes)` for the first encode that fits; with
    `budget=None` the first candidate wins outright. If nothing fits within
    `max_attempts` encodes, the smallest result seen is returned and the
    caller can tell by `size > budget`. Each attempt is appended to `log`
    (when given) as a dict with width/fps/colors/path/bytes/budget/fits.

    `cost(cand)` (optional; see `relative_size`) makes each miss steer the
    next attempt: the measured bytes are scaled by the cost ratio to predict
    every remaining candidate, and the walk jumps to the first one predicted
    to fit, or to the cheapest when none is. Without it the walk is plain
    best-first order.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    queue = list(candidates)
    if not queue:
        raise ValueError("solve() needs at least one candidate")
    best: tuple[Candidate, str, int] | None = None
    for _ in range(max_attempts):
        cand = queue.pop(0)
        path = encode(cand)
        size = os.path.getsize(path)
        fits = budget is None or size <= budget
        if log is not None:
            log.append({
                "width": cand.width,
                "fps": cand.fps,
                "colors": cand.colors,
                "path": path,
                "bytes": size,
                "budget": budget,
                "fits": fits,
            })
        if fits:
            return cand, path, size
        if best is None or size < best[2]:
            best = (cand, path, size)
        if not queue:
            break
        if cost is not None:
            queue = _predicted_to_fit(queue, cost, size / _positive(cost(cand)), budget)
    return best


def _predicted_to_fit(queue: list[Candidate], cost: Callable[[Candidate], float],
                      bytes_per_cost: float, budget: int) -> list[Candidate]:
    """The candidates a miss says can still fit, in order; the cheapest when none can."""
    likely = [c for c in queue if bytes_per_cost * _positive(cost(c)) <= budget]
    return likely or [min(queue, key=cost)]


def _positive(value: float) -> float:
    if not value > 0:
        raise ValueError("cost() must return a positive number; got %r" % (value,))
    return value


def squeeze(
    path: str,
    out: str,
    *,
    lossy: int | None = None,
    colors: int | None = None,
    log: list | None = None,
) -> int:
    """Re-optimise a GIF with `gifsicle -O3`; returns the output size in bytes.

    `lossy` adds `--lossy=N` (0..200), `colors` adds `--colors N` (2..256).
    The call goes through `ffmpeg.run` (argv logged to `log`, bounded by
    SQUEEZE_TIMEOUT_S), so a failing, missing or hung gifsicle raises
    FFmpegError carrying the argv and its stderr tail.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    if lossy is not None and not 0 <= int(lossy) <= 200:
        raise ValueError("lossy must be in 0..200; got %r" % (lossy,))
    argv = ffmpeg.gifsicle_argv(path, out, colors=colors, lossy=lossy)
    ffmpeg.run(argv, log=log, timeout=SQUEEZE_TIMEOUT_S)
    return os.path.getsize(out)
