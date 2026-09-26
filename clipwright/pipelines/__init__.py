"""Pipeline registry: one fixed ffmpeg/Pillow pipeline per recipe type.

Each pipeline module exposes `run(inst, ctx) -> CookResult`. Recipes name a
pipeline by key in PIPELINES (see cookbook/*.toml `pipeline = ...`).
Clip recipes (gifify, caption_loop, boomerang, speed) accept video *or* a still
(common.materialize_still holds a JPEG/PNG as a short clip). ken_burns
zoom-pans a still; typecard draws frames from text with no input. speed is
gifify's frame chain plus a numeric setpts (fixed rate or a linear ramp).
"""
from dataclasses import dataclass, field
from importlib import import_module


@dataclass
class CookContext:
    workdir: str
    out_dir: str
    stem: str
    proxy: bool = False
    log: list = field(default_factory=list)


@dataclass
class CookResult:
    gif: str | None
    mp4: str | None
    report: dict
    argv_log: list


_NAMES = ("gifify", "caption_loop", "boomerang", "ken_burns", "typecard", "speed")


def get(name):
    if name not in _NAMES:
        raise KeyError("unknown pipeline %r (known: %s)" % (name, ", ".join(_NAMES)))
    return import_module("clipwright.pipelines." + name).run


PIPELINES = {n: (lambda inst, ctx, _n=n: get(_n)(inst, ctx)) for n in _NAMES}
