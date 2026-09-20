"""Shared plumbing for the clip pipelines (gifify, caption_loop, boomerang).

Every clip recipe is the same sequence with a different loop policy and an
optional caption overlay: probe the input, resolve and cap the trimmed
segment, plan the loop (running the perfect-loop finder when asked), then
either render one small MP4 (proxy mode) or walk the size-budget ladder to a
GIF that fits, squeeze it with gifsicle, and write an MP4 preview beside it.
``cook_clip`` is that sequence; the pipeline modules are one-call wrappers.

Nothing here assembles a filtergraph: every ffmpeg command comes from
``clipwright.ffmpeg``'s argv builders, and the only path handed to them
besides the input is a caption PNG that Pillow rendered into the workdir.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from clipwright import budget, ffmpeg, loopfind, recipe
from clipwright.budget import Candidate
from clipwright.ffmpeg import Probe, even
from clipwright.pipelines import CookContext, CookResult
from clipwright.recipe import RecipeError

DEFAULT_WIDTH = 480          # full-mode width for recipes without a width knob
PROXY_WIDTH = 240
PROXY_FPS = 12
SEGMENT_CAP_S = 15.0         # one cap for both modes, so the preview's loop is the export's loop
LOOP_MIN_S = 1.0             # shorter segments skip the loop finder
LOOP_SAMPLE_FPS = 10
LOOP_SAMPLE_WIDTH = 64
LOSSY_SQUEEZE = 30           # gifsicle --lossy, used only when -O3 alone is still over budget

LOOP_MODES = ("seamless", "boomerang", "crossfade", "none")

Overlay = Callable[[int, int], str | None]
"""(width, height) -> path of a caption PNG exactly ``width`` wide and no
taller than ``height`` (the frame it is composited on), or None for no overlay."""


def no_overlay(width: int, height: int) -> None:
    """The overlay factory for recipes without a caption."""
    return None


@dataclass(frozen=True)
class Segment:
    """The working range of the source, in seconds."""

    from_s: float
    to_s: float
    capped: bool


@dataclass(frozen=True)
class LoopPlan:
    """How the segment will loop and the (possibly nudged) range to render."""

    label: str                       # what report["loop"] says
    reverse: bool                    # render with reverse_append (boomerang)
    from_s: float
    to_s: float
    nudge_frames: int | None = None  # out-point shift in loop-finder frames (LOOP_SAMPLE_FPS)
    score: float | None = None       # seam score from loopfind.best_loop


def resolve_segment(inst: dict, probe: Probe) -> Segment:
    """``from``/``to`` from the instance (default 0 .. duration), capped at SEGMENT_CAP_S.

    ``to`` never exceeds the probed duration; ``capped`` says whether the cap
    shortened the range. The cap is the same in proxy and full mode so the
    loop finder sees identical footage for both and the preview shows the
    export's in/out points. Raises RecipeError when the range is empty,
    including when the clip's duration is unknown and no ``to`` was given.
    """
    start = recipe.parse_time(inst["from"]) if inst.get("from") is not None else 0.0
    end = recipe.parse_time(inst["to"]) if inst.get("to") is not None else probe.duration
    if probe.duration > 0:
        end = min(end, probe.duration)
    if end <= start:
        raise RecipeError(
            f"trim {start:.3f}..{end:.3f} s is empty (clip is {probe.duration:.3f} s long)")
    capped = end - start > SEGMENT_CAP_S
    if capped:
        end = start + SEGMENT_CAP_S
    return Segment(start, end, capped)


def plan_loop(loop: str, src: str, seg: Segment, *, log: list) -> LoopPlan:
    """Turn a loop mode into a render plan, running the loop finder for "seamless".

    "boomerang" always reverses; "none" renders straight; "crossfade" is not
    implemented yet, so it renders straight and says so in the label. For
    "seamless" on a segment of at least LOOP_MIN_S, the finder samples the
    range and moves ``from``/``to`` to the best-matching frame pair; when the
    seam scores too high it degrades to a boomerang, keeping the full segment
    (a boomerang has no seam to hide, so the nudge would only shorten it).
    """
    if loop == "boomerang":
        return LoopPlan("boomerang", True, seg.from_s, seg.to_s)
    if loop == "crossfade":
        return LoopPlan("none (crossfade not implemented)", False, seg.from_s, seg.to_s)
    if loop == "none":
        return LoopPlan("none", False, seg.from_s, seg.to_s)
    if loop != "seamless":
        raise RecipeError(f"loop must be one of {LOOP_MODES}, not {loop!r}")
    if seg.to_s - seg.from_s < LOOP_MIN_S:
        return LoopPlan("seamless (segment under 1 s, not analysed)", False, seg.from_s, seg.to_s)

    frames = loopfind.sample_frames(
        src, seg.from_s, seg.to_s, fps=LOOP_SAMPLE_FPS, width=LOOP_SAMPLE_WIDTH, log=log)
    in_idx, out_idx, score = loopfind.best_loop(frames)
    if loopfind.degrade(score) == "boomerang":
        return LoopPlan("boomerang (degraded)", True, seg.from_s, seg.to_s,
                        nudge_frames=0, score=score)
    # Play frames in_idx .. out_idx-1 and wrap: frame out_idx ~ frame in_idx is the seam.
    new_from = seg.from_s + in_idx / LOOP_SAMPLE_FPS
    new_to = seg.from_s + out_idx / LOOP_SAMPLE_FPS
    nudge = round((new_to - seg.to_s) * LOOP_SAMPLE_FPS)
    return LoopPlan("seamless", False, new_from, new_to, nudge_frames=nudge, score=score)


def cook_clip(
    inst: dict,
    ctx: CookContext,
    *,
    loop: str,
    overlay: Overlay = no_overlay,
    overlay_pos: str = "bottom",
) -> CookResult:
    """Render ``inst`` with the given loop policy; proxy or full per ``ctx.proxy``.

    ``overlay(width, height)`` is asked for a caption PNG at every width that
    gets rendered (each ladder rung may differ), so it should cache per width;
    ``height`` is the frame height at that width, the bound the band must
    respect so ``overlay`` never composites it partly off-frame.
    """
    src = inst.get("input")
    if not isinstance(src, str) or not src:
        raise RecipeError(f"{inst.get('recipe')!r} needs an input clip")
    try:
        limit = budget.budget_bytes(recipe.get(inst, "fits"))
    except ValueError as e:
        raise RecipeError(f"fits: {e}") from e   # before any ffmpeg work is spent
    probe = ffmpeg.probe(src)
    if probe.width < 1 or probe.height < 1:
        raise ffmpeg.FFmpegError(["ffprobe", src], f"no usable frame size: {probe.width}x{probe.height}")
    seg = resolve_segment(inst, probe)
    plan = plan_loop(loop, src, seg, log=ctx.log)

    frame = {"from_s": plan.from_s, "to_s": plan.to_s,
             "overlay_pos": overlay_pos, "reverse_append": plan.reverse}
    report: dict = {
        "proxy": ctx.proxy,
        "from": round(plan.from_s, 3),
        "to": round(plan.to_s, 3),
        "capped": seg.capped,
        "duration": round((plan.to_s - plan.from_s) * (2 if plan.reverse else 1), 3),
        "loop": plan.label,
        "budget": limit,
    }
    if plan.score is not None:
        report["loop_score"] = round(plan.score, 2)
        report["loop_nudge_frames"] = plan.nudge_frames
    if ctx.proxy:
        return _render_proxy(src, probe, ctx, frame, overlay, report)
    return _render_full(inst, src, probe, ctx, frame, overlay, limit, report)


def _render_proxy(src: str, probe: Probe, ctx: CookContext, frame: dict,
                  overlay: Overlay, report: dict) -> CookResult:
    """One small MP4 for previews: <= PROXY_WIDTH wide at PROXY_FPS, no GIF."""
    width = even(min(PROXY_WIDTH, probe.width))
    out = os.path.join(ctx.out_dir, ctx.stem + ".mp4")
    argv = ffmpeg.mp4_argv(src, out, fps=PROXY_FPS, width=width,
                           overlay_png=overlay(width, frame_height(probe, width)), **frame)
    ffmpeg.run(argv, log=ctx.log)
    report.update(bytes=os.path.getsize(out), fits=None, width=width,
                  fps=PROXY_FPS, colors=None, attempts=0)
    return CookResult(gif=None, mp4=out, report=report, argv_log=ctx.log)


def _render_full(inst: dict, src: str, probe: Probe, ctx: CookContext, frame: dict,
                 overlay: Overlay, limit: int | None, report: dict) -> CookResult:
    """Walk the ladder to a GIF under budget, squeeze it, and write the MP4 preview."""
    candidates = _candidates(inst, probe)

    def band(width: int) -> str | None:
        return overlay(width, frame_height(probe, width))

    def encode(cand: Candidate) -> str:
        path = os.path.join(
            ctx.workdir, f"{ctx.stem}-{cand.width}w-{cand.fps:g}f-{cand.colors}c.gif")
        argv = ffmpeg.gif_argv(src, path, fps=cand.fps, width=cand.width, colors=cand.colors,
                               overlay_png=band(cand.width), **frame)
        ffmpeg.run(argv, log=ctx.log)
        return path

    attempts: list[dict] = []
    cand, raw, _ = budget.solve(encode, candidates, limit, log=attempts, cost=budget.relative_size)

    gif = os.path.join(ctx.out_dir, ctx.stem + ".gif")
    size = budget.squeeze(raw, gif, log=ctx.log)
    lossy = None
    if limit is not None and size > limit:
        lossy = LOSSY_SQUEEZE
        size = budget.squeeze(raw, gif, lossy=lossy, log=ctx.log)

    mp4 = os.path.join(ctx.out_dir, ctx.stem + ".mp4")
    argv = ffmpeg.mp4_argv(src, mp4, fps=cand.fps, width=cand.width,
                           overlay_png=band(cand.width), **frame)
    ffmpeg.run(argv, log=ctx.log)

    report.update(bytes=size, fits=None if limit is None else size <= limit,
                  width=cand.width, fps=_tidy(cand.fps), colors=cand.colors,
                  attempts=len(attempts), lossy=lossy)
    return CookResult(gif=gif, mp4=mp4, report=report, argv_log=ctx.log)


def frame_height(probe: Probe, width: int) -> int:
    """The frame height ffmpeg's ``scale=<width>:-2`` yields for this source.

    ``-2`` keeps the aspect ratio and rounds to a multiple of two the way
    libavfilter does (the half-height rounded to nearest, half away from
    zero), so a caption band bounded by this never overhangs the frame.
    """
    if probe.width < 1 or probe.height < 1:
        raise ValueError(f"probe must have a frame size; got {probe.width}x{probe.height}")
    return max(2, 2 * int(width * probe.height / (2 * probe.width) + 0.5))


def _candidates(inst: dict, probe: Probe) -> list[Candidate]:
    """The knobs' exact ask first, then every ladder rung at or below it.

    The ladder alone would ignore the ``fps`` and ``colors`` knobs (its rungs
    are fixed), so attempt 1 is what the user asked for and the ladder only
    supplies the fallbacks when that does not fit the budget. The fallbacks
    are ordered by ``budget.relative_size`` (largest first) so that, once a
    miss has calibrated the estimate, ``solve`` lands on the rung that uses
    the most of the budget, whichever axis that means stepping down.
    """
    rungs = budget.ladder(probe, max_width=int(recipe.get(inst, "width", DEFAULT_WIDTH)))
    top = rungs[0]
    fps_knob = recipe.get(inst, "fps")
    colors_knob = recipe.get(inst, "colors")
    asked = Candidate(
        width=top.width,
        fps=min(float(fps_knob), probe.fps) if fps_knob is not None else top.fps,
        colors=int(colors_knob) if colors_knob is not None else top.colors,
    )
    below = [c for c in rungs
             if c != asked and c.width <= asked.width and c.fps <= asked.fps
             and c.colors <= asked.colors]
    below.sort(key=budget.relative_size, reverse=True)
    return [asked, *below]


def _tidy(fps: float) -> int | float:
    """15.0 -> 15 for the report; 29.97 stays a float."""
    return int(fps) if fps == int(fps) else round(fps, 3)
