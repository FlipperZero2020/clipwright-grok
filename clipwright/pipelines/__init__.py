"""Pipeline registry: one fixed ffmpeg/Pillow pipeline per recipe type.

Each pipeline module exposes `run(inst, ctx) -> CookResult`. Recipes name a
pipeline by key in PIPELINES (see cookbook/*.toml `pipeline = ...`).
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


_NAMES = ("gifify", "caption_loop", "boomerang")


def get(name):
    if name not in _NAMES:
        raise KeyError("unknown pipeline %r (known: %s)" % (name, ", ".join(_NAMES)))
    return import_module("clipwright.pipelines." + name).run


PIPELINES = {n: (lambda inst, ctx, _n=n: get(_n)(inst, ctx)) for n in _NAMES}
