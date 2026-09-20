"""The ``clipwright`` command line: cook, remix, recipes, probe, doctor.

This is the engine's human front door and the complete product on its own
(the Telegram daemon is a client of the same ``cook``). It builds a recipe
instance from flags, hands it to ``cook.cook``, and prints what came out:
one line per output (only the GIF carries the size-budget verdict, straight
from the pipeline's report), what the loop finder decided and whether the
segment was capped, then the ``Re-run anytime:`` line that reproduces it.
Nothing here touches ffmpeg directly; every error the engine raises
(``RecipeError``, ``FFmpegError``, and the plain ``ValueError``/
``RuntimeError``/``OSError`` a bad sidecar value or a failing binary can
surface) becomes one message on stderr and exit status 2.

Flags map onto the instance in two layers: ``--set key=value`` is the
general mechanism (any knob of the recipe, or a top-level ``from``/``to``/
``seed``), and ``--text``/``--color``/``--size``/``--pos``/``--loop``/
``--fits`` are sugar for knobs (``caption.*``, ``loop``, ``fits``) that fail
loudly on a recipe that does not declare them. ``--set`` is applied last, so
it is the final word.

The engine proper (``recipe``, ``cook``, the pipelines) needs Pillow and
tomllib; ``doctor`` is the command that reports when they are missing, so
those modules are imported by the functions that use them and this module
loads without them.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import TYPE_CHECKING

from clipwright import budget, doctor, ffmpeg

if TYPE_CHECKING:
    from clipwright.recipe import Knob, RecipeDef

DEFAULT_OUT = "clips"
TOP_LEVEL_KEYS = ("from", "to", "seed")          # instance fields, settable on every recipe
CAPTION_FLAGS = {"text": "caption.text", "color": "caption.color",
                 "size": "caption.size", "pos": "caption.pos"}
KNOB_FLAGS = {**CAPTION_FLAGS, "loop": "loop", "fits": "fits"}   # sugar: only where the recipe declares the knob
TOP_FLAGS = {"from_": "from", "to": "to", "seed": "seed"}
RERUN_HEADER = "Re-run anytime:"

_BARE_HEX = re.compile(r"[0-9a-fA-F]{6}")


# --------------------------------------------------------------------------
# Value handling
# --------------------------------------------------------------------------

def coerce(text: str) -> object:
    """``"20"`` -> 20, ``"1.5"`` -> 1.5, ``"true"`` -> True, else the string.

    Anything starting with ``#`` stays a string, so colours like ``#ff4444``
    survive. Only plain decimal forms coerce: ``"1e3"`` and ``"0x10"`` stay
    strings.
    """
    if text.startswith("#"):
        return text
    low = text.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    if re.fullmatch(r"[+-]?\d+", low):
        return int(low)
    if re.fullmatch(r"[+-]?(?:\d+\.\d*|\.\d+|\d+)", low):
        return float(low)
    return text


def parse_set(item: str) -> tuple[str, str]:
    """Split one ``--set key=value`` argument at its first ``=``."""
    from clipwright.recipe import RecipeError
    key, sep, value = item.partition("=")
    key = key.strip()
    if not sep or not key:
        raise RecipeError(f"--set wants key=value, not {item!r}")
    return key, value


def _color(value: str) -> str:
    """``ffdd00``, ``FFDD00`` or ``#FFDD00`` -> ``#ffdd00``: the palette values carry the ``#`` in lowercase."""
    value = value.lower()
    return f"#{value}" if _BARE_HEX.fullmatch(value) else value


def _knob_map(defn: RecipeDef) -> dict[str, Knob]:
    return {k.key: k for k in defn.knobs}


def apply_set(inst: dict, defn: RecipeDef, items: list[str]) -> None:
    """Apply ``--set`` items to ``inst`` in order, coercing values per knob type.

    A text knob keeps the raw string (``caption.text=2024`` is the caption
    "2024"); every other value goes through ``coerce``. Keys must be a knob
    of the recipe or one of TOP_LEVEL_KEYS, so a typo never lands silently
    in the sidecar (``loop`` and ``fits`` are knobs: a recipe without one,
    like boomerang, refuses them here rather than at cook time).
    """
    from clipwright import recipe
    from clipwright.recipe import RecipeError
    knobs = _knob_map(defn)
    for item in items:
        key, raw = parse_set(item)
        knob = knobs.get(key)
        if knob is None and key not in TOP_LEVEL_KEYS:
            raise RecipeError(
                f"{defn.name} has no knob {key!r} (knobs: {', '.join(knobs)}; "
                f"also settable: {', '.join(TOP_LEVEL_KEYS)})")
        value: object = raw if knob is not None and knob.type == "text" else coerce(raw)
        recipe.set_(inst, key, value)


def apply_flags(inst: dict, defn: RecipeDef, args: argparse.Namespace) -> None:
    """Apply the convenience flags; a knob flag on a recipe without that knob is an error.

    ``--text``/``--color``/``--size``/``--pos`` need the ``caption.*`` knobs
    and ``--loop``/``--fits`` the ``loop``/``fits`` knobs (boomerang declares
    no ``loop``: its motion is the loop). ``--from``/``--to``/``--seed`` are
    instance fields every recipe has.
    """
    from clipwright import recipe
    from clipwright.recipe import RecipeError
    knobs = _knob_map(defn)
    for attr, key in KNOB_FLAGS.items():
        value = getattr(args, attr)
        if value is None:
            continue
        if key not in knobs:
            hint = " (try a recipe with a caption, e.g. caption-loop)" if attr in CAPTION_FLAGS else ""
            raise RecipeError(f"{defn.name} has no {key} knob, so --{attr} does not apply{hint}")
        if attr == "color":
            value = _color(value)
        elif attr == "fits":
            value = coerce(value)
        recipe.set_(inst, key, value)
    for attr, key in TOP_FLAGS.items():
        value = getattr(args, attr)
        if value is not None:
            inst[key] = value


# --------------------------------------------------------------------------
# Listings
# --------------------------------------------------------------------------

def _knob_line(knob: Knob) -> str:
    if knob.type == "enum":
        spec = " | ".join(str(v) for v in knob.values)
    elif knob.type == "step":
        spec = f"{knob.min}..{knob.max} by {knob.step}"
    elif knob.type == "text":
        spec = f"up to {knob.max_len} chars"
    else:
        return f"    {knob.key:<14} range  nudges --from/--to by {knob.step_s:g} s"
    default = f'"{knob.default}"' if isinstance(knob.default, str) else str(knob.default)
    return f"    {knob.key:<14} {knob.type:<6} {spec}  (default {default})"


def format_recipes(book: dict[str, RecipeDef]) -> str:
    """One ``emoji name — blurb`` line per recipe, its knobs indented beneath."""
    lines: list[str] = []
    for name in sorted(book):
        defn = book[name]
        head = f"{defn.emoji} {defn.name}".strip()
        lines.append(f"{head} — {defn.blurb}" if defn.blurb else head)
        lines.extend(_knob_line(k) for k in defn.knobs)
    return "\n".join(lines)


def format_probe(info: ffmpeg.Probe) -> str:
    """The Probe fields, one per line, with width and height as ``WxH``."""
    rows = [
        ("path", info.path),
        ("size", f"{info.width}x{info.height}"),
        ("duration", f"{info.duration:.3f} s"),
        ("fps", f"{info.fps:g}"),
        ("frames", str(info.nb_frames)),
        ("vcodec", info.vcodec or "?"),
        ("audio", "yes" if info.has_audio else "no"),
        ("bytes", f"{info.size_bytes:,}"),
    ]
    return "\n".join(f"{k + ':':<10}{v}" for k, v in rows)


def _fits_label(inst: dict) -> str:
    from clipwright import recipe
    fits = recipe.get(inst, "fits")
    if isinstance(fits, (int, float)) and not isinstance(fits, bool):
        return f"{fits:g} MB"
    return str(fits)


def format_status(report: dict) -> list[str]:
    """What the pipeline decided that the user did not ask for verbatim.

    Always says how the clip loops (``report["loop"]`` is the honest label:
    ``seamless``, ``boomerang (degraded)``, ``none (crossfade not
    implemented)`` ...) with the seam score and any out-point nudge, and
    says so when the segment was cut to ``common.SEGMENT_CAP_S``.
    """
    from clipwright.pipelines import common
    lines: list[str] = []
    loop = report.get("loop")
    if loop is not None:
        extras = []
        score = report.get("loop_score")
        if score is not None:
            extras.append(f"seam {score:g}")
        nudge = report.get("loop_nudge_frames")
        if nudge:
            extras.append(f"out-point moved {nudge / common.LOOP_SAMPLE_FPS:+.1f} s")
        lines.append(", ".join([f"loop: {loop}", *extras]))
    if report.get("capped"):
        cap = common.SEGMENT_CAP_S
        lines.append(f"trimmed to {cap:g} s (renders cap at {cap:g} s; --from/--to picks the segment)")
    return lines


def format_result(inst: dict, result) -> str:
    """One line per output, the status lines, then the re-run command.

    Only the GIF gets a ``fits`` mark, and only from ``report["fits"]``: the
    MP4 is the preview and a proxy run never ran the budget search.
    """
    report = result.report
    lines: list[str] = []
    for path in (result.gif, result.mp4):
        if path is None:
            continue
        line = f"{path}  {os.path.getsize(path):,} bytes"
        if path == result.gif and report.get("fits") is not None:
            line += f"  fits {_fits_label(inst)} {'✓' if report['fits'] else '✗'}"
        lines.append(line)
    sidecar = report["sidecar"]
    lines.append(f"{sidecar}  {os.path.getsize(sidecar):,} bytes")
    lines += format_status(report)
    lines += [RERUN_HEADER, "  " + report["cli"]]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def _cook_and_print(inst: dict, book: dict[str, RecipeDef], out: str, proxy: bool) -> int:
    from clipwright import cook as cook_mod
    result = cook_mod.cook(inst, out_dir=out, proxy=proxy, cookbook=book)
    print(format_result(inst, result))
    return 0


def cmd_cook(args: argparse.Namespace) -> int:
    from clipwright import recipe
    from clipwright.recipe import RecipeError
    book = recipe.load_cookbook()
    defn = book.get(args.recipe)
    if defn is None:
        print(f"clipwright: unknown recipe {args.recipe!r}. The cookbook:\n{format_recipes(book)}",
              file=sys.stderr)
        return 2
    inst = recipe.defaults(defn)
    if args.input is not None:
        inst["input"] = args.input
    elif defn.needs_input:
        raise RecipeError(f"{defn.name} needs an input clip: clipwright cook {defn.name} <input>")
    apply_flags(inst, defn, args)
    apply_set(inst, defn, args.set)
    return _cook_and_print(inst, book, args.out, args.proxy)


def cmd_remix(args: argparse.Namespace) -> int:
    from clipwright import recipe
    from clipwright.recipe import RecipeError
    book = recipe.load_cookbook()
    inst = recipe.load_instance(args.sidecar)
    defn = book.get(inst["recipe"])
    if defn is None:
        raise RecipeError(f"{args.sidecar} names unknown recipe {inst['recipe']!r} "
                          f"(known: {', '.join(sorted(book))})")
    src = inst.get("input")
    if isinstance(src, str) and not os.path.isabs(src) and not os.path.isfile(src):
        beside = os.path.join(os.path.dirname(os.path.abspath(args.sidecar)), src)
        if os.path.isfile(beside):
            inst["input"] = beside
    # a sidecar may omit knobs; the cook fills them the same way, and the
    # `fits` label printed beside the GIF has to name what was enforced
    from clipwright.cook import with_defaults
    inst = with_defaults(inst, defn)
    apply_flags(inst, defn, args)
    apply_set(inst, defn, args.set)
    return _cook_and_print(inst, book, args.out, args.proxy)


def cmd_recipes(args: argparse.Namespace) -> int:
    from clipwright import recipe
    print(format_recipes(recipe.load_cookbook()))
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    print(format_probe(ffmpeg.probe(args.input)))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    return doctor.report(args.home)


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

def _add_knob_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--text", help="caption text (caption.text)")
    p.add_argument("--color", help="caption colour: one of the recipe's palette values, "
                                   "as #rrggbb or bare rrggbb, any case (caption.color)")
    p.add_argument("--size", type=int, help="caption font size (caption.size)")
    p.add_argument("--pos", choices=("top", "bottom"), help="caption position (caption.pos)")
    p.add_argument("--from", dest="from_", metavar="T", help="in-point: seconds or M:SS.s")
    p.add_argument("--to", metavar="T", help="out-point: seconds or M:SS.s")
    p.add_argument("--loop", choices=("seamless", "boomerang", "crossfade", "none"),
                   help="loop mode, for recipes with a loop knob (loop)")
    p.add_argument("--fits", metavar="F",
                   help=f"size budget: {', '.join(budget.PRESETS)}, none, or a number of MB (fits)")
    p.add_argument("--seed", type=int)
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="set any knob (repeatable); applied after the flags above")
    p.add_argument("--out", default=DEFAULT_OUT, help=f"output directory (default ./{DEFAULT_OUT})")
    p.add_argument("--proxy", action="store_true", help="small MP4 preview only, no GIF")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clipwright",
        description="A $0-per-render GIF/video foundry: ffmpeg + Pillow, no LLM.")
    sub = parser.add_subparsers(dest="command", required=True)

    cook_p = sub.add_parser("cook", help="render a recipe into a GIF + MP4 + sidecar")
    cook_p.add_argument("recipe")
    cook_p.add_argument("input", nargs="?", help="source clip (omit for generative recipes)")
    _add_knob_flags(cook_p)
    cook_p.set_defaults(func=cmd_cook)

    remix_p = sub.add_parser("remix", help="re-cook a <stem>.recipe.toml sidecar with changes")
    remix_p.add_argument("sidecar", metavar="file.recipe.toml")
    _add_knob_flags(remix_p)
    remix_p.set_defaults(func=cmd_remix)

    sub.add_parser("recipes", help="list the cookbook and every knob").set_defaults(func=cmd_recipes)

    probe_p = sub.add_parser("probe", help="show what ffprobe sees in a clip")
    probe_p.add_argument("input")
    probe_p.set_defaults(func=cmd_probe)

    doctor_p = sub.add_parser("doctor", help="check ffmpeg, gifsicle, fonts, the state dir and the cookbook")
    doctor_p.add_argument("--home", metavar="DIR", help=doctor.HOME_HELP)
    doctor_p.set_defaults(func=cmd_doctor)
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand; engine errors become one stderr line and status 2.

    ``RecipeError`` is a ``ValueError`` and ``FFmpegError`` a ``RuntimeError``,
    so the tuple also covers the bare forms: an integer too long for
    ``int()``, a gifsicle exit, or a font-less machine. A sidecar the
    validator accepts but the render cannot use (``from = "5"`` on a 3 s
    clip) is a ``RecipeError`` from the pipeline and lands here the same way.
    An ``ImportError`` (Pillow or tomllib missing) is one line too, naming
    ``doctor`` as the command that can say what is wrong.
    """
    args = parse_args(argv)
    try:
        return args.func(args)
    except ImportError as err:      # no Pillow or tomllib: `doctor` is the command that still runs
        print(f"clipwright: {err} (run: clipwright doctor)", file=sys.stderr)
        return 2
    except (ValueError, RuntimeError, OSError) as err:
        print(f"clipwright: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
