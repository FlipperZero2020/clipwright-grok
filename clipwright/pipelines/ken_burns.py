"""ken_burns pipeline: slow zoom over a still (or the first frame of a clip).

A photo has no duration, so this pipeline first writes a Ken Burns H.264
clip (zoompan expressions are numbers and fixed tokens only) and then hands
that clip to the shared cook_clip plumbing with ``loop = "none"`` — the
motion *is* the loop. An optional caption is the same Pillow overlay
caption-loop uses.
"""
from __future__ import annotations

import os

from clipwright import ffmpeg, recipe
from clipwright.pipelines import CookContext, CookResult, caption_loop, common
from clipwright.recipe import RecipeError

DEFAULT_DURATION = 3.0
DEFAULT_ZOOM = 1.4
DEFAULT_FPS = 15


def run(inst: dict, ctx: CookContext) -> CookResult:
    src = inst.get("input")
    if not isinstance(src, str) or not src:
        raise RecipeError(f"{inst.get('recipe')!r} needs an input still or clip")
    probe = ffmpeg.probe(src)
    if probe.width < 1 or probe.height < 1:
        raise ffmpeg.FFmpegError(["ffprobe", src], f"no usable frame size: {probe.width}x{probe.height}")
    still = src
    frame = probe
    if not ffmpeg.is_still(probe):
        still = os.path.join(ctx.workdir, "frame.png")
        ffmpeg.run(ffmpeg.first_frame_argv(src, still), log=ctx.log)
        frame = ffmpeg.probe(still)
    duration = float(recipe.get(inst, "duration", DEFAULT_DURATION) or DEFAULT_DURATION)
    duration = min(max(0.5, duration), common.SEGMENT_CAP_S)
    zoom = float(recipe.get(inst, "zoom", DEFAULT_ZOOM) or DEFAULT_ZOOM)
    width = ffmpeg.even(int(recipe.get(inst, "width", common.DEFAULT_WIDTH) or common.DEFAULT_WIDTH))
    height = common.frame_height(frame, width)
    fps = float(recipe.get(inst, "fps", DEFAULT_FPS) or DEFAULT_FPS)
    clip = os.path.join(ctx.workdir, "kenburns.mp4")
    ffmpeg.run(
        ffmpeg.kenburns_argv(still, clip, width=width, height=height, fps=fps,
                             duration_s=duration, zoom=zoom),
        log=ctx.log,
    )
    clip_inst = {**inst, "input": clip,
                 "from": recipe.fmt_time(0.0), "to": recipe.fmt_time(duration)}
    return common.cook_clip(
        clip_inst, ctx, loop="none",
        overlay=caption_loop.caption_overlay(inst, ctx),
        overlay_pos=recipe.get(inst, "caption.pos", "bottom"),
    )
