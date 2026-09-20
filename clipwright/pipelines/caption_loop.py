"""caption_loop pipeline: trim, loop, and burn a Pillow-rendered caption.

This is where user text becomes pixels, and it happens in Pillow only: the
caption band is rendered as a PNG in the workdir at exactly the output width
and handed to ffmpeg as a second input for ``overlay``, so the text never
touches a filtergraph. Every ladder rung may be a different width, so the
overlay factory renders (and caches) one PNG per width it is asked for.

The ``caption.size`` knob is defined against a 480 px frame; the font scales
with the actual output width so the 240 px proxy preview shows the layout the
export will have. The band is also bounded by the frame height at that width
(``render_caption``'s ``max_height``: shrink, then ellipsize), because
``overlay`` anchors it to the frame's bottom (or top) edge and would push a
taller band's first (or last) lines off-frame. Every value read here is a
knob of the recipe (the style is ``caption``'s default), so
``cook.cli_command`` reproduces the render exactly.
"""
from __future__ import annotations

import os

from clipwright import caption, recipe
from clipwright.pipelines import CookContext, CookResult, common

MIN_FONT_PX = 8


def run(inst: dict, ctx: CookContext) -> CookResult:
    return common.cook_clip(
        inst, ctx,
        loop=recipe.get(inst, "loop", "seamless"),
        overlay=caption_overlay(inst, ctx),
        overlay_pos=recipe.get(inst, "caption.pos", "bottom"),
    )


def caption_overlay(inst: dict, ctx: CookContext) -> common.Overlay:
    """An overlay factory for ``inst``'s caption knobs; ``no_overlay`` when the text is blank."""
    text = str(recipe.get(inst, "caption.text", ""))
    if not caption.sanitize_text(text):
        return common.no_overlay
    size = recipe.get(inst, "caption.size", 64)
    color = recipe.get(inst, "caption.color", "#ffffff")
    cache: dict[int, str] = {}

    def overlay(width: int, height: int) -> str:
        if width not in cache:
            path = os.path.join(ctx.workdir, f"caption-{width}.png")
            px = max(MIN_FONT_PX, round(size * width / common.DEFAULT_WIDTH))
            cache[width] = caption.write_caption_overlay(
                text, width, path, size=px, color=color, max_height=height)
        return cache[width]

    return overlay
