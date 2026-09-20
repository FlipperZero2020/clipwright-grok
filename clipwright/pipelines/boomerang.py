"""boomerang pipeline: play the trimmed clip forward, then backward, forever.

The loop policy is fixed (``reverse_append`` on every render), so the recipe
has no ``loop`` knob; the output is twice the segment's duration and needs no
loop finder because a palindrome has no seam.
"""
from __future__ import annotations

from clipwright.pipelines import CookContext, CookResult, common


def run(inst: dict, ctx: CookContext) -> CookResult:
    return common.cook_clip(inst, ctx, loop="boomerang")
