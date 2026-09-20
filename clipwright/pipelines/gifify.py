"""gifify pipeline: any clip becomes a looping GIF that fits its platform.

The default recipe. The ``loop`` knob picks the loop policy (seamless via the
loop finder, boomerang, none; crossfade renders straight for now) and the
``width``/``fps``/``colors`` knobs set the first encode attempt; everything
else is the shared plumbing in ``common``.
"""
from __future__ import annotations

from clipwright import recipe
from clipwright.pipelines import CookContext, CookResult, common


def run(inst: dict, ctx: CookContext) -> CookResult:
    return common.cook_clip(inst, ctx, loop=recipe.get(inst, "loop", "seamless"))
