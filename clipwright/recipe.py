"""Recipe definitions and instances.

A recipe *definition* (``cookbook/*.toml``) names a pipeline and declares the
knobs a user can turn; a recipe *instance* is the plain nested dict a session
edits and that every export writes beside the output as ``<stem>.recipe.toml``.

This module owns the cookbook loader, the TOML writer for the subset we use,
dotted-key access (``caption.text``), validation, time parsing, and the
button-press semantics (``apply_knob``) that the daemon's keyboards compile
to. It is pure: no ffmpeg, no network, and no I/O beyond reading cookbook
files and reading/writing instance files. Its one engine import is ``budget``,
so a ``fits`` value is legal here exactly when ``budget.budget_bytes`` can
resolve it.
"""
from __future__ import annotations

import copy
import math
import os
import re
import tomllib
from dataclasses import dataclass

from clipwright import budget

KNOB_TYPES = ("enum", "step", "text", "range")

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_TIME_PART = re.compile(r"^\d+$")
_TIME_LAST = re.compile(r"^(?:\d+(?:\.\d*)?|\.\d+)$")
_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t",
            "\n": "\\n", "\f": "\\f", "\r": "\\r"}


class RecipeError(ValueError):
    """A definition, instance, or edit the cookbook cannot accept."""


@dataclass
class Knob:
    key: str
    label: str
    type: str
    default: object = None
    values: list | None = None
    labels: list | None = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    max_len: int | None = None
    step_s: float = 0.1


@dataclass
class RecipeDef:
    name: str
    blurb: str
    emoji: str
    pipeline: str
    needs_input: bool
    knobs: list[Knob]


# --------------------------------------------------------------------------
# Small predicates
# --------------------------------------------------------------------------

def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_scalar(v: object) -> bool:
    return isinstance(v, (bool, int, float, str))


def _in_values(value: object, values: list) -> bool:
    """Membership that never lets ``True`` match ``1`` or ``"128"`` match 128."""
    return any(isinstance(value, bool) is isinstance(x, bool) and value == x
               for x in values)


def _is_budget(value: object) -> bool:
    """Whatever ``budget.budget_bytes`` resolves: any preset, "none", or a number of MB."""
    try:
        budget.budget_bytes(value)
    except ValueError:
        return False
    return True


# Enum knobs whose instance value may also be free-form. The instance format
# documents `fits` as "preset name or a number of MB"; the cookbook enum only
# lists the presets that get buttons, and `budget_bytes` is the one parser
# that says what else is legal, so validation never disagrees with the cook.
_FREEFORM_ENUM = {"fits": _is_budget}


# --------------------------------------------------------------------------
# TOML reading
# --------------------------------------------------------------------------

def _parse_toml(text: str, src: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise RecipeError(f"{src}: {e}") from e


def _read_toml(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except UnicodeDecodeError as e:
        raise RecipeError(f"{path}: not UTF-8 text ({e})") from e
    return _parse_toml(text, path)


def _require_recipe(inst: dict, src: str) -> dict:
    if not isinstance(inst.get("recipe"), str) or not inst["recipe"]:
        raise RecipeError(f"{src}: missing a 'recipe = \"...\"' line")
    return inst


def load_instance(path: str) -> dict:
    """Read a recipe instance file; raises RecipeError on bad TOML."""
    return _require_recipe(_read_toml(path), path)


def loads_instance(text: str) -> dict:
    """Parse a recipe instance from TOML text; raises RecipeError on bad TOML."""
    return _require_recipe(_parse_toml(text, "recipe"), "recipe")


# --------------------------------------------------------------------------
# Cookbook
# --------------------------------------------------------------------------

def cookbook_dir() -> str:
    """The bundled cookbook directory (``clipwright/cookbook/``)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookbook")


def load_cookbook(dir: str | None = None) -> dict[str, RecipeDef]:
    """Load every ``*.toml`` definition in ``dir``, keyed by recipe name."""
    root = dir or cookbook_dir()
    try:
        names = sorted(n for n in os.listdir(root) if n.endswith(".toml"))
    except OSError as e:
        raise RecipeError(f"cannot read cookbook {root}: {e}") from e
    book: dict[str, RecipeDef] = {}
    for name in names:
        path = os.path.join(root, name)
        defn = _parse_def(_read_toml(path), path)
        if defn.name in book:
            raise RecipeError(f"{path}: duplicate recipe name {defn.name!r}")
        book[defn.name] = defn
    return book


def _parse_def(doc: dict, src: str) -> RecipeDef:
    name, pipeline = doc.get("name"), doc.get("pipeline")
    if not isinstance(name, str) or not name:
        raise RecipeError(f"{src}: 'name' must be a non-empty string")
    if not isinstance(pipeline, str) or not pipeline:
        raise RecipeError(f"{src}: 'pipeline' must be a non-empty string")
    needs_input = doc.get("needs_input", True)
    if not isinstance(needs_input, bool):
        raise RecipeError(f"{src}: 'needs_input' must be true or false")
    raw_knobs = doc.get("knob", [])
    if not isinstance(raw_knobs, list):
        raise RecipeError(f"{src}: 'knob' must be an array of tables ([[knob]])")
    knobs = [_parse_knob(raw, f"{src} knob[{i}]") for i, raw in enumerate(raw_knobs)]
    keys = [k.key for k in knobs]
    if len(set(keys)) != len(keys):
        raise RecipeError(f"{src}: duplicate knob keys")
    return RecipeDef(name=name, blurb=str(doc.get("blurb", "")),
                     emoji=str(doc.get("emoji", "")), pipeline=pipeline,
                     needs_input=needs_input, knobs=knobs)


def _parse_knob(raw: object, where: str) -> Knob:
    if not isinstance(raw, dict):
        raise RecipeError(f"{where}: must be a table")
    key, label, ktype = raw.get("key"), raw.get("label"), raw.get("type")
    if not isinstance(key, str) or not key:
        raise RecipeError(f"{where}: 'key' must be a non-empty string")
    if not isinstance(label, str) or not label:
        raise RecipeError(f"{where}: 'label' must be a non-empty string")
    if ktype not in KNOB_TYPES:
        raise RecipeError(f"{where}: type must be one of {KNOB_TYPES}, not {ktype!r}")
    knob = Knob(key=key, label=label, type=ktype, default=raw.get("default"))

    if ktype == "enum":
        values, labels = raw.get("values"), raw.get("labels")
        if not isinstance(values, list) or not values or not all(_is_scalar(v) for v in values):
            raise RecipeError(f"{where}: enum needs a non-empty list of scalar 'values'")
        if labels is not None and (not isinstance(labels, list) or len(labels) != len(values)
                                   or not all(isinstance(x, str) for x in labels)):
            raise RecipeError(f"{where}: 'labels' must be one string per value")
        knob.values = list(values)
        knob.labels = list(labels) if labels is not None else None
    elif ktype == "step":
        for name in ("min", "max", "step"):
            if not _is_number(raw.get(name)):
                raise RecipeError(f"{where}: step knob needs a numeric '{name}'")
        knob.min, knob.max, knob.step = raw["min"], raw["max"], raw["step"]
        if not knob.min < knob.max or knob.step <= 0:
            raise RecipeError(f"{where}: need min < max and step > 0")
    elif ktype == "text":
        max_len = raw.get("max")
        if not isinstance(max_len, int) or isinstance(max_len, bool) or max_len <= 0:
            raise RecipeError(f"{where}: text knob needs a positive integer 'max'")
        knob.max_len = max_len
    else:  # range: edits the instance's from/to, so it carries no default
        step_s = raw.get("step_s", 0.1)
        if not _is_number(step_s) or step_s <= 0:
            raise RecipeError(f"{where}: 'step_s' must be a positive number")
        if "default" in raw:
            raise RecipeError(f"{where}: range knobs take no default")
        knob.step_s = float(step_s)
        return knob

    if knob.default is None:
        raise RecipeError(f"{where}: {ktype} knob needs a 'default'")
    problem = _check_value(knob, knob.default)
    if problem:
        raise RecipeError(f"{where}: default {problem}")
    return knob


# --------------------------------------------------------------------------
# Dotted access, defaults, validation
# --------------------------------------------------------------------------

def get(d: dict, dotted: str, default=None):
    """``get(inst, "caption.text")`` -> ``inst["caption"]["text"]`` or default."""
    node = d
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_(d: dict, dotted: str, value) -> None:
    """``set_(inst, "caption.text", v)``, creating intermediate tables."""
    *path, last = dotted.split(".")
    node = d
    for part in path:
        child = node.get(part)
        if child is None:
            child = node[part] = {}
        elif not isinstance(child, dict):
            raise RecipeError(f"{dotted}: {part!r} is not a table")
        node = child
    node[last] = value


def defaults(defn: RecipeDef) -> dict:
    """An instance of ``defn`` with every knob at its default."""
    inst: dict = {"recipe": defn.name}
    for knob in defn.knobs:
        if knob.type != "range":
            set_(inst, knob.key, knob.default)
    return inst


def _check_value(knob: Knob, value: object) -> str | None:
    """Why ``value`` is unacceptable for ``knob``, or None when it is fine."""
    if knob.type == "enum":
        if _in_values(value, knob.values):
            return None
        if _FREEFORM_ENUM.get(knob.key, lambda _v: False)(value):
            return None
        return f"{value!r} is not one of {knob.values!r}"
    if knob.type == "step":
        if not _is_number(value):
            return f"{value!r} is not a number"
        if not knob.min <= value <= knob.max:
            return f"{value!r} is outside {knob.min}..{knob.max}"
        return None
    if knob.type == "text":
        if not isinstance(value, str):
            return f"{value!r} is not a string"
        if len(value) > knob.max_len:
            return f"{len(value)} characters is over the {knob.max_len} limit"
    return None


def _check_trim(inst: dict) -> list[str]:
    times: dict[str, float] = {}
    problems: list[str] = []
    for key in ("from", "to"):
        if inst.get(key) is not None:
            try:
                times[key] = parse_time(inst[key])
            except RecipeError as e:
                problems.append(f"{key}: {e}")
    if "to" in times and times.get("from", 0.0) >= times["to"]:
        problems.append("from must be earlier than to")
    return problems


def validate(inst: dict, defn: RecipeDef) -> list[str]:
    """Problems with ``inst`` as an instance of ``defn``; ``[]`` when valid.

    Knobs absent from the instance are not problems: pipelines read them with
    the knob's default. Whether ``input`` exists on disk is the cook's job.
    """
    if not isinstance(inst, dict):
        return ["instance must be a table"]
    problems: list[str] = []
    if inst.get("recipe") != defn.name:
        problems.append(f"recipe is {inst.get('recipe')!r}, expected {defn.name!r}")
    if inst.get("input") is not None and not isinstance(inst["input"], str):
        problems.append("input must be a path string")
    if inst.get("seed") is not None and not (isinstance(inst["seed"], int)
                                             and not isinstance(inst["seed"], bool)):
        problems.append("seed must be an integer")
    for knob in defn.knobs:
        if knob.type == "range":
            continue
        value = get(inst, knob.key)
        if value is None:
            continue
        problem = _check_value(knob, value)
        if problem:
            problems.append(f"{knob.key}: {problem}")
    problems.extend(_check_trim(inst))
    return problems


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------

def parse_time(s) -> float:
    """Seconds from ``"0:02.5"``, ``"1:02:03.5"``, ``"2.5"`` or a number."""
    if isinstance(s, bool) or not isinstance(s, (int, float, str)):
        raise RecipeError(f"not a time: {s!r}")
    if isinstance(s, str):
        parts = s.strip().split(":")
        if (not 1 <= len(parts) <= 3 or not all(_TIME_PART.match(p) for p in parts[:-1])
                or not _TIME_LAST.match(parts[-1])):
            raise RecipeError(f"not a time: {s!r} (want seconds, M:SS.s or H:MM:SS.s)")
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + float(part)
    else:
        seconds = float(s)
    if not math.isfinite(seconds) or seconds < 0:
        raise RecipeError(f"time must be finite and non-negative: {s!r}")
    return seconds


def fmt_time(seconds) -> str:
    """``2.5`` -> ``"0:02.5"``; hours appear only when needed (``"1:02:03.5"``)."""
    total_ms = round(parse_time(seconds) * 1000)
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs = f"{rest / 1000:06.3f}".rstrip("0")
    if secs.endswith("."):
        secs += "0"
    return f"{hours}:{minutes:02d}:{secs}" if hours else f"{minutes}:{secs}"


# --------------------------------------------------------------------------
# Button presses
# --------------------------------------------------------------------------

def apply_knob(inst: dict, defn: RecipeDef, knob_idx: int, value_idx: int) -> dict:
    """Apply one keyboard press and return a NEW instance (``inst`` is untouched).

    enum: ``value_idx`` indexes ``values``. step: 0 = decrement, 1 = increment,
    clamped to min/max. range: 0..3 = in −, in +, out −, out + by ``step_s``
    seconds, keeping 0 <= in < out. text: not a button; use ``set_``.
    """
    if not 0 <= knob_idx < len(defn.knobs):
        raise RecipeError(f"{defn.name} has no knob #{knob_idx}")
    knob = defn.knobs[knob_idx]
    out = copy.deepcopy(inst)
    if knob.type == "enum":
        if not 0 <= value_idx < len(knob.values):
            raise RecipeError(f"{knob.key}: no value #{value_idx}")
        set_(out, knob.key, knob.values[value_idx])
    elif knob.type == "step":
        if value_idx not in (0, 1):
            raise RecipeError(f"{knob.key}: step takes 0 (−) or 1 (+), not {value_idx}")
        current = get(inst, knob.key, knob.default)
        if not _is_number(current):
            raise RecipeError(f"{knob.key}: current value {current!r} is not a number")
        moved = current - knob.step if value_idx == 0 else current + knob.step
        set_(out, knob.key, min(knob.max, max(knob.min, moved)))
    elif knob.type == "range":
        _nudge_trim(out, knob, value_idx)
    else:
        raise RecipeError(f"{knob.key}: text knobs are edited with set_(), not buttons")
    return out


def _nudge_trim(inst: dict, knob: Knob, value_idx: int) -> None:
    if value_idx not in (0, 1, 2, 3):
        raise RecipeError(f"{knob.key}: range takes 0..3 (in −, in +, out −, out +)")
    step = knob.step_s
    start = parse_time(inst["from"]) if inst.get("from") is not None else 0.0
    end = parse_time(inst["to"]) if inst.get("to") is not None else None
    if value_idx == 0:
        inst["from"] = fmt_time(max(0.0, start - step))
    elif value_idx == 1:
        moved = start + step if end is None else min(start + step, end - step)
        inst["from"] = fmt_time(max(start, moved))
    elif end is None:
        raise RecipeError("out-point unknown: set 'to' (e.g. the clip duration) first")
    elif value_idx == 2:
        inst["to"] = fmt_time(min(end, max(end - step, start + step)))
    else:
        inst["to"] = fmt_time(end + step)


# --------------------------------------------------------------------------
# TOML writing (the subset we use)
# --------------------------------------------------------------------------

def _quote(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _key(k: object) -> str:
    if not isinstance(k, str) or not k:
        raise RecipeError(f"TOML keys must be non-empty strings, not {k!r}")
    return k if _BARE_KEY.match(k) else _quote(k)


def _value(v: object, where: str) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return _quote(v)
    if isinstance(v, list):
        if not all(_is_scalar(x) for x in v):
            raise RecipeError(f"{where}: lists may only hold scalars")
        return "[" + ", ".join(_value(x, where) for x in v) + "]"
    raise RecipeError(f"{where}: cannot write a {type(v).__name__} to TOML")


def dumps_toml(d: dict) -> str:
    """Write a dict as TOML: str/int/float/bool/list-of-scalars, one-level tables.

    Top-level scalars come first, then each dict value as a ``[table]``
    section. ``None`` values are omitted (TOML has no null; an absent key is
    how the instance format says "not set").
    """
    lines: list[str] = []
    tables: list[tuple[str, dict]] = []
    for key, value in d.items():
        if value is None:
            continue
        if isinstance(value, dict):
            tables.append((key, value))
        else:
            lines.append(f"{_key(key)} = {_value(value, key)}")
    for name, table in tables:
        if lines:
            lines.append("")
        lines.append(f"[{_key(name)}]")
        for key, value in table.items():
            if value is None:
                continue
            if isinstance(value, dict):
                raise RecipeError(f"{name}.{key}: tables nested deeper than one level are not supported")
            lines.append(f"{_key(key)} = {_value(value, f'{name}.{key}')}")
    return "\n".join(lines) + "\n"


def dump_instance(d: dict, path: str) -> None:
    """Write ``d`` to ``path`` as TOML (atomically: temp file + rename)."""
    text = dumps_toml(d)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)
