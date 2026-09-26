"""speed pipeline: play a trim faster, slower, or ramped, then GIF-fit it.

A thin sibling of gifify. There is no caption and no loop finder — the trim
plays straight (``loop`` reports ``none``; the GIF file still loops). ``mode
= fixed`` holds one playback rate for the whole trim. ``mode = ramp`` moves
linearly between 1× and ``rate``: ``slow_fast`` runs 1× → rate (the near end
stays at 1×, ``rate`` is the far end) and ``fast_slow`` runs rate → 1×.

The rate pair is numbers only. ``common.cook_clip`` forwards it to the argv
builders, which prepend a ``setpts`` expression and then take the usual GIF
budget / squeeze path.
"""
from __future__ import annotations

import math

from clipwright import recipe
from clipwright.pipelines import CookContext, CookResult, common
from clipwright.recipe import RecipeError

_MODES = ("fixed", "ramp")
_RAMPS = ("slow_fast", "fast_slow")


def _shown_rate(rate: float) -> int | float:
    """2.0 -> 2 so the report matches the knob's integer values."""
    return int(rate) if rate == int(rate) else rate


def _speed(inst: dict) -> tuple[str, int | float, str | None, tuple[float, float]]:
    """``(mode, rate, ramp or None, (start_rate, end_rate))``."""
    mode = recipe.get(inst, "mode", "fixed")
    if mode not in _MODES:
        raise RecipeError(f"mode must be one of {_MODES}, not {mode!r}")
    raw = recipe.get(inst, "rate", 2)
    try:
        rate = float(raw)
    except (TypeError, ValueError) as e:
        raise RecipeError(f"rate must be a positive number, not {raw!r}") from e
    if isinstance(raw, bool) or not math.isfinite(rate) or rate <= 0:
        raise RecipeError(f"rate must be a positive number, not {raw!r}")
    if mode == "fixed":
        return mode, _shown_rate(rate), None, (rate, rate)
    ramp = recipe.get(inst, "ramp", "slow_fast")
    if ramp == "slow_fast":
        return mode, _shown_rate(rate), ramp, (1.0, rate)
    if ramp == "fast_slow":
        return mode, _shown_rate(rate), ramp, (rate, 1.0)
    raise RecipeError(f"ramp must be one of {_RAMPS}, not {ramp!r}")


def run(inst: dict, ctx: CookContext) -> CookResult:
    mode, rate, ramp, ends = _speed(inst)
    result = common.cook_clip(inst, ctx, loop="none", speed=ends)
    result.report["mode"] = mode
    result.report["rate"] = rate
    result.report["ramp"] = ramp
    return result
