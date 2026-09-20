"""``clipwright doctor``: preflight the machine before the first render.

Every environment assumption the engine makes is a ``Check`` here: the
media binaries and what ffmpeg was built with, the palette filters the GIF
encoder relies on, Pillow and tomllib, a caption font, the fonts directory,
the state directory the daemon will write to (resolved exactly the way
``clipwrightd`` resolves it, ``--home`` included), the daemon's ``bot.env``,
and that the cookbook loads. ``required=False`` marks the nice-to-haves
(libass, Impact, Noto Color Emoji, ``bot.env`` itself) whose absence only
changes looks or only matters to the daemon, never whether a render works.

Binaries are queried with argv lists through ``ffmpeg.run``; a missing or
non-executable binary is a failed check, never a traceback. The engine
modules that need Pillow (``caption``) or tomllib (``recipe``) are imported
inside their checks, not here, so this module loads on the very machine it
is meant to diagnose and a missing dependency is a failed check too.
"""
from __future__ import annotations

import argparse
import glob
import importlib.metadata
import os
import re
import stat
import sys
from dataclasses import dataclass

from clipwright import ffmpeg
from clipwright.ffmpeg import FFmpegError

PALETTE_FILTERS = ("palettegen", "paletteuse")
LIBS = (("libx264", True), ("libass", False))
EMOJI_FONT_GLOB = os.path.join("**", "NotoColorEmoji*.ttf")
HOME_HELP = ("the daemon's state directory, as clipwrightd --home would set it "
             "(default: $CLIPWRIGHT_HOME, then a CLIPWRIGHT_HOME line in ~/.clipwright/bot.env, "
             "then ~/.clipwright)")

_VERSION_LINE = re.compile(r"^(?:ffmpeg|ffprobe) version (\S+)")
_GIFSICLE_LINE = re.compile(r"Gifsicle (\S+)", re.IGNORECASE)


@dataclass
class Check:
    name: str
    ok: bool
    required: bool
    detail: str


def env_file(home: str | None = None) -> str:
    """The ``bot.env`` the daemon would read: under ``home`` when given, else the default home."""
    from clipwrightd import config  # lazy: the engine has no hard dependency on the daemon
    return os.path.join(os.path.abspath(home) if home else config.home_dir(), config.ENV_FILE)


def state_dir(home: str | None = None) -> str:
    """The state directory the daemon will use, with clipwrightd's precedence.

    ``home`` (``--home DIR``) wins outright. Otherwise ``$CLIPWRIGHT_HOME``,
    then a ``CLIPWRIGHT_HOME=`` line in the default ``bot.env``, then
    ``~/.clipwright`` — the same three steps as ``clipwrightd.config``.

    Only that one line of ``bot.env`` is consulted, nothing from the file is
    ever printed, and a file the daemon could not read either (missing,
    unreadable, not UTF-8) just means the default home.
    """
    from clipwrightd import config
    if home:
        return os.path.abspath(home)
    default = config.home_dir()
    if os.environ.get("CLIPWRIGHT_HOME"):
        return default
    try:
        with open(os.path.join(default, config.ENV_FILE), encoding="utf-8") as fh:
            override = config.parse_env(fh.read()).get("CLIPWRIGHT_HOME")
    except (OSError, ValueError):     # ValueError: UnicodeDecodeError
        override = None
    return config.resolve_home(override) if override else default


# --------------------------------------------------------------------------
# Individual checks
# --------------------------------------------------------------------------

def _tool_output(argv: list[str]) -> tuple[str | None, str]:
    """stdout of a version query, or (None, why) when the tool is missing, fails or cannot exec.

    ``ffmpeg.run`` types a missing binary, a non-zero exit and a timeout as
    ``FFmpegError``; any other ``OSError`` from exec (a non-executable file
    or a directory named ``ffmpeg`` on PATH, ENOEXEC) is caught here so the
    report still prints.
    """
    try:
        proc = ffmpeg.run(argv, timeout=30)
    except FFmpegError as err:
        return None, err.stderr.splitlines()[0] if err.stderr else str(err)
    except OSError as err:
        return None, str(err)
    return proc.stdout.decode("utf-8", "replace"), ""


def _first_match(text: str, pattern: re.Pattern) -> str | None:
    m = pattern.search(text)
    return m.group(1) if m else None


def check_binary(name: str, argv: list[str], pattern: re.Pattern) -> tuple[Check, str]:
    """Presence + version of one binary; returns the check and its full output."""
    out, why = _tool_output(argv)
    if out is None:
        return Check(name, False, True, f"not runnable ({why})"), ""
    version = _first_match(out, pattern) or "version unknown"
    return Check(name, True, True, version), out


def check_ffmpeg_libs(version_output: str) -> list[Check]:
    """``--enable-<lib>`` in ffmpeg's ``configuration:`` line, one check per LIBS entry."""
    conf = ""
    for line in version_output.splitlines():
        if line.startswith("configuration:"):
            conf = line
            break
    checks = []
    for lib, required in LIBS:
        if not version_output:
            checks.append(Check(lib, False, required, "ffmpeg not found"))
            continue
        present = f"--enable-{lib}" in conf.split()
        why = "built in" if present else f"not in ffmpeg's configuration ({'needed for the MP4 preview' if required else 'optional'})"
        checks.append(Check(lib, present, required, why))
    return checks


def check_palette_filters() -> Check:
    out, why = _tool_output(["ffmpeg", "-hide_banner", "-filters"])
    if out is None:
        return Check("palette filters", False, True, f"ffmpeg -filters failed ({why})")
    names = {line.split()[1] for line in out.splitlines() if len(line.split()) > 2}
    missing = [f for f in PALETTE_FILTERS if f not in names]
    if missing:
        return Check("palette filters", False, True, f"missing: {', '.join(missing)}")
    return Check("palette filters", True, True, ", ".join(PALETTE_FILTERS))


def check_pillow() -> Check:
    try:
        import PIL
    except ImportError as err:
        return Check("Pillow", False, True, f"not importable ({err})")
    version = getattr(PIL, "__version__", None)
    if version is None:
        try:
            version = importlib.metadata.version("Pillow")
        except importlib.metadata.PackageNotFoundError:
            version = "version unknown"
    return Check("Pillow", True, True, version)


def check_tomllib() -> Check:
    try:
        import tomllib  # noqa: F401
    except ImportError:
        return Check("tomllib", False, True, f"needs Python 3.11+, this is {sys.version.split()[0]}")
    return Check("tomllib", True, True, f"stdlib, Python {sys.version.split()[0]}")


def _caption_module():
    """``clipwright.caption``, or None when it cannot import (no Pillow)."""
    try:
        from clipwright import caption
    except ImportError:
        return None
    return caption


def check_caption_font() -> Check:
    caption = _caption_module()
    if caption is None:
        return Check("caption font", False, True, "cannot look: Pillow is not importable")
    try:
        path = caption.pick_font(bold=True)
    except RuntimeError as err:
        return Check("caption font", False, True, str(err))
    if "impact" in os.path.basename(path).lower():
        return Check("caption font", True, True, path)
    return Check("caption font", True, True, f"{path} (Impact absent; captions use this fallback)")


def check_fonts_dir() -> Check:
    caption = _caption_module()
    if caption is None:
        return Check("fonts dir", False, True, "cannot look: Pillow is not importable")
    root = caption.FONT_ROOT
    if not os.path.isdir(root):
        return Check("fonts dir", False, True, f"{root} is not a directory")
    if not os.access(root, os.R_OK | os.X_OK):
        return Check("fonts dir", False, True, f"{root} is not readable")
    return Check("fonts dir", True, True, f"{root} readable")


def check_emoji_font() -> Check:
    """Noto Color Emoji under FONT_ROOT — optional until the emoji recipes land."""
    caption = _caption_module()
    if caption is None:
        return Check("emoji font", False, False, "cannot look: Pillow is not importable")
    hits = sorted(glob.glob(os.path.join(caption.FONT_ROOT, EMOJI_FONT_GLOB), recursive=True))
    if hits:
        return Check("emoji font", True, False, hits[0])
    return Check("emoji font", False, False,
                 f"no NotoColorEmoji*.ttf under {caption.FONT_ROOT} (needed only by the "
                 "milestone-4 emoji recipes)")


def check_state_dir(home: str | None = None) -> Check:
    """The state dir is writable, or its nearest existing ancestor is (so it can be created)."""
    home = state_dir(home)
    if os.path.isdir(home):
        ok = os.access(home, os.W_OK | os.X_OK)
        return Check("state dir", ok, True, f"{home} {'writable' if ok else 'not writable'}")
    if os.path.exists(home):
        return Check("state dir", False, True, f"{home} exists but is not a directory")
    ancestor = os.path.dirname(os.path.abspath(home))
    while not os.path.isdir(ancestor):
        parent = os.path.dirname(ancestor)
        if parent == ancestor:
            break
        ancestor = parent
    ok = os.access(ancestor, os.W_OK | os.X_OK)
    return Check("state dir", ok, True,
                 f"{home} will be created" if ok else f"cannot create {home}: {ancestor} not writable")


def check_bot_env(home: str | None = None) -> Check:
    """``bot.env`` exists with mode 0600 — optional, since only the daemon needs it.

    Reports the path and mode only; nothing from the file is ever printed
    (``state_dir`` consults its ``CLIPWRIGHT_HOME`` line and nothing else).
    """
    from clipwrightd import config
    path = env_file(home)
    if not os.path.isfile(path):
        return Check("bot.env", False, False, f"{path} absent (the daemon needs it; the CLI does not)")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode != config.EXPECTED_MODE:
        return Check("bot.env", False, False,
                     f"{path} has mode {mode:04o}, expected {config.EXPECTED_MODE:04o}: chmod 600 {path}")
    return Check("bot.env", True, False, f"{path} mode {mode:04o}")


def check_cookbook() -> Check:
    try:
        from clipwright import recipe
    except ImportError as err:
        return Check("cookbook", False, True, f"cannot load: {err}")
    try:
        book = recipe.load_cookbook()
    except (recipe.RecipeError, OSError) as err:
        return Check("cookbook", False, True, str(err))
    if not book:
        return Check("cookbook", False, True, f"no recipes in {recipe.cookbook_dir()}")
    n = len(book)
    return Check("cookbook", True, True, f"{n} recipe{'s' if n != 1 else ''}: {', '.join(sorted(book))}")


# --------------------------------------------------------------------------
# Runner and report
# --------------------------------------------------------------------------

def run_checks(home: str | None = None) -> list[Check]:
    """Every check, in report order; ``home`` is the daemon's ``--home``. Never raises."""
    ffmpeg_check, ffmpeg_out = check_binary("ffmpeg", ["ffmpeg", "-version"], _VERSION_LINE)
    ffprobe_check, _ = check_binary("ffprobe", ["ffprobe", "-version"], _VERSION_LINE)
    gifsicle_check, _ = check_binary("gifsicle", ["gifsicle", "--version"], _GIFSICLE_LINE)
    return [
        ffmpeg_check,
        ffprobe_check,
        gifsicle_check,
        *check_ffmpeg_libs(ffmpeg_out),
        check_palette_filters(),
        check_pillow(),
        check_tomllib(),
        check_caption_font(),
        check_fonts_dir(),
        check_emoji_font(),
        check_state_dir(home),
        check_bot_env(home),
        check_cookbook(),
    ]


def format_report(checks: list[Check]) -> str:
    """A ✓/✗ table plus a one-line verdict."""
    width = max(len(c.name) for c in checks)
    lines = []
    for c in checks:
        mark = "✓" if c.ok else "✗"
        tag = "" if c.required else "  (optional)"
        lines.append(f"{mark} {c.name:<{width}}  {c.detail}{tag}")
    failed = [c.name for c in checks if c.required and not c.ok]
    if failed:
        lines.append(f"{len(failed)} required check{'s' if len(failed) != 1 else ''} failed: {', '.join(failed)}")
    else:
        lines.append("all required checks passed")
    return "\n".join(lines)


def report(home: str | None = None) -> int:
    """Print the report; 1 when any required check failed, else 0."""
    checks = run_checks(home)
    print(format_report(checks))
    return 1 if any(c.required and not c.ok for c in checks) else 0


def main(argv: list[str] | None = None) -> int:
    """``python3 -m clipwright.doctor [--home DIR]``; exit 1 when a required check failed."""
    parser = argparse.ArgumentParser(prog="clipwright doctor",
                                     description="preflight the machine before the first render")
    parser.add_argument("--home", metavar="DIR", help=HOME_HELP)
    args = parser.parse_args(argv)
    return report(args.home)


if __name__ == "__main__":
    raise SystemExit(main())
