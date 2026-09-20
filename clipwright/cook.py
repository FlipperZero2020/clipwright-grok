"""Recipe instance -> validated pipeline run -> outputs plus their sidecar.

``cook`` is the engine's front door: it checks the instance against its
cookbook definition, confirms the input exists, gives the pipeline a private
temp workdir (always removed afterwards), names the outputs from a stem the
engine chooses (``stem_for`` never uses the input filename), and writes the
``<stem>.recipe.toml`` sidecar beside the outputs so any export can be
reopened later. ``cli_command`` prints the one-line CLI that reproduces an
instance, which the daemon shows under every preview.

The validation here is the boundary the pipelines trust: besides the knob
checks in ``recipe.validate``, an instance may carry only the instance fields
(``recipe``/``input``/``from``/``to``/``seed``) and the knobs its recipe
declares, so a hand-edited sidecar cannot smuggle a setting no keyboard or
``--set`` could have produced. Knobs the instance leaves out are filled with
the cookbook's defaults here (``with_defaults``), once, so the render, the
stem, the sidecar and the CLI line all see the same complete instance and a
sidecar that omits ``fits`` still cooks to Telegram's budget. A caller whose
input path should stay private (the daemon's per-user upload directory)
passes ``public_input`` and that is what the sidecar and the CLI line say
instead.
"""
from __future__ import annotations

import copy
import hashlib
import os
import re
import shlex
import shutil
import tempfile

from clipwright import pipelines, recipe
from clipwright.pipelines import CookContext, CookResult
from clipwright.recipe import RecipeDef, RecipeError

SIDECAR_SUFFIX = ".recipe.toml"
SLUG_MAX = 24
HASH_LEN = 6
INSTANCE_KEYS = frozenset({"recipe", "input", "from", "to", "seed"})

_STEM_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_NOT_SLUG = re.compile(r"[^a-z0-9]+")


def cook(
    inst: dict,
    *,
    out_dir: str,
    stem: str | None = None,
    proxy: bool = False,
    cookbook: dict[str, RecipeDef] | None = None,
    public_input: str | None = None,
) -> CookResult:
    """Validate ``inst``, run its pipeline into ``out_dir``, write the sidecar.

    Raises RecipeError for an unknown recipe, an invalid instance (including
    one carrying keys the recipe does not declare), or a missing input. The
    returned report carries ``sidecar`` (its path) and ``cli`` (the
    reproducing command) on top of the pipeline's own keys. ``public_input``,
    when given, replaces ``inst["input"]`` in the sidecar and the CLI line
    (the render itself still reads the real path). ``inst`` is never
    modified: knobs it leaves unset are filled on a copy (``with_defaults``).
    """
    defn = _definition(inst, cookbook)
    problems = recipe.validate(inst, defn) + undeclared_keys(inst, defn)
    if problems:
        raise RecipeError(f"{defn.name}: " + "; ".join(problems))
    inst = with_defaults(inst, defn)
    if defn.needs_input:
        src = inst.get("input")
        if not src:
            raise RecipeError(f"{defn.name} needs an input clip")
        if not os.path.isfile(src):
            raise RecipeError(f"input not found: {src}")
    stem = _check_stem(stem) if stem is not None else stem_for(inst)
    try:
        pipeline = pipelines.get(defn.pipeline)
    except KeyError as e:
        raise RecipeError(f"{defn.name}: {e.args[0]}") from e

    os.makedirs(out_dir, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="clipwright-")
    try:
        ctx = CookContext(workdir=workdir, out_dir=out_dir, stem=stem, proxy=proxy)
        result = pipeline(inst, ctx)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    shown = inst if public_input is None else {**inst, "input": public_input}
    sidecar = os.path.join(out_dir, stem + SIDECAR_SUFFIX)
    recipe.dump_instance(shown, sidecar)
    result.report["sidecar"] = sidecar
    result.report["cli"] = cli_command(shown, cookbook={defn.name: defn})
    return result


def with_defaults(inst: dict, defn: RecipeDef) -> dict:
    """A deep copy of ``inst`` with every knob it leaves unset at the recipe's default.

    ``recipe.validate`` lets an absent knob through, and a pipeline reading
    one would fall back on its own (the ladder's top rung for ``fps`` and
    ``colors``, no size budget at all for ``fits``) instead of the cookbook's
    default. Filling here, at the engine boundary, makes ``fits`` absent mean
    ``"telegram"`` and keeps a trimmed sidecar hashing to the same stem as
    the full one. Range knobs have no default and edit ``from``/``to``, so
    they are left alone.
    """
    out = copy.deepcopy(inst)
    for knob in defn.knobs:
        if knob.type != "range" and recipe.get(out, knob.key) is None:
            recipe.set_(out, knob.key, knob.default)
    return out


def undeclared_keys(inst: dict, defn: RecipeDef) -> list[str]:
    """Problems for every key of ``inst`` that is neither an instance field nor a knob.

    Tables are walked one level (``caption.style``), matching the dotted knob
    keys; a range knob edits ``from``/``to`` and declares no key of its own.
    """
    declared = INSTANCE_KEYS | {k.key for k in defn.knobs if k.type != "range"}
    keys: list[str] = []
    for key, value in inst.items():
        if isinstance(value, dict):
            keys += [f"{key}.{sub}" for sub in value]
        else:
            keys.append(str(key))
    return [f"{k}: not a knob of {defn.name}" for k in keys if k not in declared]


def stem_for(inst: dict) -> str:
    """``<recipe>-<slug>-<6 hex>``: filesystem-safe, deterministic, never the input filename.

    The slug comes from ``caption.text`` when there is one, else ``seed<n>``
    when a seed is set; with neither it is omitted. The hex is the first six
    characters of the SHA-1 of the canonical (key-sorted) TOML of the whole
    instance, so any knob change gives a new stem.
    """
    text = recipe.get(inst, "caption.text")
    slug = _slugify(text) if isinstance(text, str) else ""
    if not slug and inst.get("seed") is not None:
        slug = f"seed{inst['seed']}"
    digest = hashlib.sha1(recipe.dumps_toml(_canonical(inst)).encode("utf-8")).hexdigest()
    parts = [str(inst.get("recipe", "recipe")), slug, digest[:HASH_LEN]]
    return "-".join(p for p in parts if p)


def cli_command(inst: dict, *, cookbook: dict[str, RecipeDef] | None = None) -> str:
    """The ``clipwright cook ...`` line that reproduces ``inst``.

    Trim points go through ``--from``/``--to``, a seed through ``--seed``,
    and every knob whose value differs from the recipe's default through
    ``--set key=value``.
    """
    defn = _definition(inst, cookbook)
    argv = ["clipwright", "cook", defn.name]
    if inst.get("input"):
        argv.append(str(inst["input"]))
    for key in ("from", "to"):
        if inst.get(key) is not None:
            argv += [f"--{key}", recipe.fmt_time(inst[key])]
    if inst.get("seed") is not None:
        argv += ["--seed", str(inst["seed"])]
    base = recipe.defaults(defn)
    for knob in defn.knobs:
        if knob.type == "range":
            continue
        value = recipe.get(inst, knob.key)
        if value is not None and not _same(value, recipe.get(base, knob.key)):
            argv += ["--set", f"{knob.key}={_cli_value(value)}"]
    return shlex.join(argv)


def _definition(inst: dict, cookbook: dict[str, RecipeDef] | None) -> RecipeDef:
    name = inst.get("recipe") if isinstance(inst, dict) else None
    if not isinstance(name, str) or not name:
        raise RecipeError("instance has no 'recipe' name")
    book = cookbook if cookbook is not None else recipe.load_cookbook()
    if name not in book:
        raise RecipeError(f"unknown recipe {name!r} (known: {', '.join(sorted(book))})")
    return book[name]


def _check_stem(stem: str) -> str:
    if not _STEM_OK.fullmatch(stem):
        raise RecipeError(f"stem must match {_STEM_OK.pattern}, not {stem!r}")
    return stem


def _slugify(text: str) -> str:
    slug = _NOT_SLUG.sub("-", text.lower()).strip("-")
    return slug[:SLUG_MAX].rstrip("-")


def _canonical(d: dict) -> dict:
    """Key-sorted copy (one level of tables), so equal instances hash equally."""
    return {k: (dict(sorted(v.items())) if isinstance(v, dict) else v)
            for k, v in sorted(d.items())}


def _same(a: object, b: object) -> bool:
    """Equality that never lets ``True`` match ``1``."""
    return isinstance(a, bool) is isinstance(b, bool) and a == b


def _cli_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
