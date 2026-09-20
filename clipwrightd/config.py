"""clipwrightd.config — the daemon's settings, read from one 0600 env file.

The file is ``$CLIPWRIGHT_HOME/bot.env`` (default ``~/.clipwright/bot.env``):
plain ``KEY=VALUE`` lines, ``#`` comment lines, blank lines, an optional
``export`` prefix, and one pair of surrounding quotes stripped from a value.
No interpolation, no inline comments — a bot token is the kind of value you
do not want parsed cleverly.

Required keys: ``CLIPWRIGHT_BOT_TOKEN`` and ``CLIPWRIGHT_OWNER_ID``.
Optional: ``CLIPWRIGHT_FRIEND_IDS`` (comma list of Telegram user ids),
``CLIPWRIGHT_HOME`` (state dir; the process environment wins over the file),
and the numeric limits listed in ``_OPTIONAL`` — upload size, clip length and
resolution, the daily export quota, the queue depth, the per-user disk
budget and how many days an idle session is kept before its files go.

The token lives here and nowhere else, so the file mode is checked: anything
other than 0600 is logged as a warning (never an error — a mis-chmodded file
should still start the bot, loudly).

Every numeric limit must be a finite, positive number (the integer ones at
least 1): a zero queue depth or a ``nan`` duration cap would otherwise crash
the daemon later or quietly switch a gate off. A ``ConfigError`` names the
key and the shape expected, never the value: whatever was pasted into
``CLIPWRIGHT_OWNER_ID`` by mistake may well be the token.

The state dir (``CLIPWRIGHT_HOME`` from the environment or the file) is
``expanduser``'d and made absolute, so ``~/.clipwright`` means the home
directory and a relative path does not move with the daemon's cwd.
"""

from __future__ import annotations

import logging
import math
import os
import stat
from dataclasses import dataclass

log = logging.getLogger("clipwrightd.config")

ENV_FILE = "bot.env"
REQUIRED = ("CLIPWRIGHT_BOT_TOKEN", "CLIPWRIGHT_OWNER_ID")
OPTIONAL_LIST = "CLIPWRIGHT_FRIEND_IDS"
EXPECTED_MODE = 0o600

# env key -> (Config field, parser)
_OPTIONAL: dict[str, tuple[str, type]] = {
    "CLIPWRIGHT_MAX_UPLOAD_BYTES": ("max_upload_bytes", int),
    "CLIPWRIGHT_MAX_DURATION_S": ("max_duration_s", float),
    "CLIPWRIGHT_MAX_DIM": ("max_dim", int),
    "CLIPWRIGHT_PER_DAY_QUOTA": ("per_day_quota", int),
    "CLIPWRIGHT_QUEUE_DEPTH": ("queue_depth", int),
    "CLIPWRIGHT_MAX_USER_BYTES": ("max_user_bytes", int),
    "CLIPWRIGHT_RETENTION_DAYS": ("retention_days", float),
}


class ConfigError(ValueError):
    """The env file is missing, unreadable, missing a required key, or has a bad value."""


@dataclass
class Config:
    token: str
    owner_id: int
    friend_ids: set[int]
    home: str
    max_upload_bytes: int = 20_000_000
    max_duration_s: float = 60.0
    max_dim: int = 1920
    per_day_quota: int = 200
    queue_depth: int = 8
    max_user_bytes: int = 1_000_000_000     # uploads + renders on disk, per user
    retention_days: float = 14.0            # idle sessions (and their un-exported uploads) expire after this

    @property
    def allowed(self) -> set[int]:
        """Every user id the daemon will talk to: the owner plus friends."""
        return {self.owner_id} | self.friend_ids


def resolve_home(path: str) -> str:
    """A state-dir setting as the daemon will use it: ``~`` expanded, made absolute."""
    return os.path.abspath(os.path.expanduser(path))


def home_dir() -> str:
    """The state directory: ``$CLIPWRIGHT_HOME`` or ``~/.clipwright``."""
    return resolve_home(os.environ.get("CLIPWRIGHT_HOME")
                        or os.path.join(os.path.expanduser("~"), ".clipwright"))


def default_path() -> str:
    return os.path.join(home_dir(), ENV_FILE)


def parse_env(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines. Later keys override earlier ones."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _parse_ids(text: str, key: str) -> set[int]:
    ids: set[int] = set()
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ids.add(int(item))
        except ValueError:
            raise ConfigError(f"{key}: expected a comma list of integer user ids") from None
    return ids


def _parse_number(values: dict[str, str], key: str, kind: type) -> int | float | None:
    """A positive, finite ``kind`` (ints at least 1), or None when the key is unset."""
    raw = values.get(key)
    if raw is None or raw == "":
        return None
    shape = "a finite number above zero" if kind is float else "an integer of at least 1"
    try:
        number = kind(raw)
    except ValueError:
        raise ConfigError(f"{key}: expected {shape}") from None
    if number <= 0 or (isinstance(number, float) and not math.isfinite(number)):
        raise ConfigError(f"{key}: expected {shape}")
    return number


def check_mode(path: str) -> int:
    """Return the file's permission bits, warning when they are not 0600."""
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode != EXPECTED_MODE:
        log.warning("%s has mode %04o, expected %04o — run: chmod 600 %s",
                    path, mode, EXPECTED_MODE, path)
    return mode


def load_config(path: str | None = None) -> Config:
    """Read the env file and return a ``Config``; raise ``ConfigError`` if unusable."""
    path = path or default_path()
    required = ", ".join(REQUIRED)
    if not os.path.isfile(path):
        raise ConfigError(
            f"config file not found: {path} — create it (mode 0600) with "
            f"{required} (and optionally {OPTIONAL_LIST}, a comma list)")
    try:
        check_mode(path)
        with open(path, encoding="utf-8") as fh:
            values = parse_env(fh.read())
    except (OSError, UnicodeDecodeError) as err:
        raise ConfigError(f"{path}: cannot read it ({err.__class__.__name__}: "
                          f"{getattr(err, 'strerror', None) or 'not UTF-8 text'})") from None

    missing = [k for k in REQUIRED if not values.get(k)]
    if missing:
        raise ConfigError(
            f"{path}: missing required key(s) {', '.join(missing)} — "
            f"required: {required}; optional: {OPTIONAL_LIST}")

    owner_key = "CLIPWRIGHT_OWNER_ID"
    try:
        owner_id = int(values[owner_key])
    except ValueError:
        raise ConfigError(f"{owner_key}: expected an integer user id") from None

    home = resolve_home(os.environ.get("CLIPWRIGHT_HOME")
                        or values.get("CLIPWRIGHT_HOME")
                        or os.path.dirname(os.path.abspath(path)))

    extra: dict[str, int | float] = {}
    for key, (field_name, kind) in _OPTIONAL.items():
        number = _parse_number(values, key, kind)
        if number is not None:
            extra[field_name] = number

    return Config(
        token=values["CLIPWRIGHT_BOT_TOKEN"],
        owner_id=owner_id,
        friend_ids=_parse_ids(values.get(OPTIONAL_LIST, ""), OPTIONAL_LIST),
        home=home,
        **extra,
    )
