"""Knob spec -> Telegram InlineKeyboardMarkup rows. The generic renderer.

The bot never hand-writes a keyboard for a specific recipe. A RecipeDef
declares its knobs and this module turns them into rows of
``{"text", "callback_data"}`` dicts. Callbacks carry indices and a 6-char
session token only (``callback_data`` is capped at 64 bytes by Telegram);
the session token resolves to the live recipe in SQLite. No user text ever
round-trips through a button. Pure functions, no I/O.

Callback grammar::

    c/<session6>/<knob_idx>/<value_idx>   knob press (enum, step, range)
    t/<session6>/<knob_idx>               text knob -> daemon answers with force_reply
    a/<session6>/<action>                 undo | cli | export | grid
    r/<session6>/<recipe>                 switch the session to another cookbook recipe
    n/<session6>                          no-op label button

When ``build_keyboard`` is given the cookbook it puts a recipe row first:
one button per recipe, the session's own marked with ``•``. Recipe names
travel by name (``[A-Za-z0-9_-]``, as the cookbook files spell them), so a
button survives a cookbook reorder; the daemon checks the name is still
in its cookbook before acting on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from clipwright.recipe import Knob, RecipeDef, fmt_time, get

ACTIONS: list[str] = ["undo", "cli", "export", "grid"]
MAX_CB_BYTES = 64
ENUM_PER_ROW = 4
RECIPES_PER_ROW = 4

_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{6}$")
_RECIPE_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_ACTION_BUTTONS = [("↩ Undo", "undo"), ("⌘ Show CLI", "cli"), ("⬇ Export", "export"), ("🎲 Grid", "grid")]


@dataclass(frozen=True)
class Callback:
    """A decoded callback_data. ``kind`` is one of c | t | a | r | n."""

    kind: str
    session: str
    knob_idx: int | None = None
    value_idx: int | None = None
    action: str | None = None
    recipe: str | None = None


def _check_session(session6: str) -> str:
    if not isinstance(session6, str) or not _SESSION_RE.match(session6):
        raise ValueError(f"bad session token: {session6!r}")
    return session6


def _check_recipe(name: object) -> str:
    if not isinstance(name, str) or not _RECIPE_RE.match(name):
        raise ValueError(f"bad recipe name for a callback: {name!r}")
    return name


def _check_index(name: str, idx: object) -> int:
    if isinstance(idx, bool) or not isinstance(idx, int) or idx < 0:
        raise ValueError(f"{name} must be a non-negative int, got {idx!r}")
    return idx


def _parse_index(name: str, text: str) -> int:
    if not text.isdigit():
        raise ValueError(f"{name} must be a non-negative int, got {text!r}")
    return int(text)


def _sealed(data: str) -> str:
    assert len(data.encode("utf-8")) <= MAX_CB_BYTES, f"callback_data over {MAX_CB_BYTES} bytes: {data!r}"
    return data


def encode_cb(session6: str, knob_idx: int, value_idx: int) -> str:
    """``c/<s>/<k>/<v>`` — a knob press carrying indices only."""
    return _sealed(f"c/{_check_session(session6)}/{_check_index('knob_idx', knob_idx)}/{_check_index('value_idx', value_idx)}")


def encode_text_cb(session6: str, knob_idx: int) -> str:
    """``t/<s>/<k>`` — ask the daemon to force_reply-prompt for a text knob."""
    return _sealed(f"t/{_check_session(session6)}/{_check_index('knob_idx', knob_idx)}")


def encode_action_cb(session6: str, action: str) -> str:
    """``a/<s>/<action>`` for one of ACTIONS."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}; expected one of {ACTIONS}")
    return _sealed(f"a/{_check_session(session6)}/{action}")


def encode_recipe_cb(session6: str, name: str) -> str:
    """``r/<s>/<recipe>`` — switch the session to the named cookbook recipe."""
    return _sealed(f"r/{_check_session(session6)}/{_check_recipe(name)}")


def encode_noop_cb(session6: str) -> str:
    """``n/<s>`` — a label button the daemon acknowledges and ignores."""
    return _sealed(f"n/{_check_session(session6)}")


def decode_cb(data: str) -> Callback:
    """Parse any callback_data produced by this module; ValueError on anything else."""
    if not isinstance(data, str) or len(data.encode("utf-8")) > MAX_CB_BYTES:
        raise ValueError(f"bad callback_data: {data!r}")
    parts = data.split("/")
    kind = parts[0]
    if kind == "c" and len(parts) == 4:
        return Callback("c", _check_session(parts[1]), knob_idx=_parse_index("knob_idx", parts[2]), value_idx=_parse_index("value_idx", parts[3]))
    if kind == "t" and len(parts) == 3:
        return Callback("t", _check_session(parts[1]), knob_idx=_parse_index("knob_idx", parts[2]))
    if kind == "a" and len(parts) == 3:
        session = _check_session(parts[1])
        if parts[2] not in ACTIONS:
            raise ValueError(f"unknown action {parts[2]!r}; expected one of {ACTIONS}")
        return Callback("a", session, action=parts[2])
    if kind == "r" and len(parts) == 3:
        return Callback("r", _check_session(parts[1]), recipe=_check_recipe(parts[2]))
    if kind == "n" and len(parts) == 2:
        return Callback("n", _check_session(parts[1]))
    raise ValueError(f"bad callback_data: {data!r}")


def _button(text: str, callback_data: str) -> dict[str, str]:
    return {"text": text, "callback_data": callback_data}


def _enum_rows(knob: Knob, inst: dict, knob_idx: int, session6: str) -> list[list[dict[str, str]]]:
    values = list(knob.values or [])
    labels = list(knob.labels or [])
    current = get(inst, knob.key, knob.default)
    buttons = []
    for value_idx, value in enumerate(values):
        label = labels[value_idx] if value_idx < len(labels) else str(value)
        text = f"• {label}" if value == current else label
        buttons.append(_button(text, encode_cb(session6, knob_idx, value_idx)))
    return [buttons[i : i + ENUM_PER_ROW] for i in range(0, len(buttons), ENUM_PER_ROW)]


def _step_row(knob: Knob, inst: dict, knob_idx: int, session6: str) -> list[dict[str, str]]:
    current = get(inst, knob.key, knob.default)
    return [
        _button("−", encode_cb(session6, knob_idx, 0)),
        _button(f"{knob.label} {current}", encode_noop_cb(session6)),
        _button("+", encode_cb(session6, knob_idx, 1)),
    ]


def _range_rows(inst: dict, knob_idx: int, session6: str) -> list[list[dict[str, str]]]:
    def shown(key: str) -> str:
        raw = inst.get(key)
        return fmt_time(0.0 if raw in (None, "") else raw)

    return [
        [
            _button("◀ in", encode_cb(session6, knob_idx, 0)),
            _button(f"in {shown('from')}", encode_noop_cb(session6)),
            _button("in ▶", encode_cb(session6, knob_idx, 1)),
        ],
        [
            _button("◀ out", encode_cb(session6, knob_idx, 2)),
            _button(f"out {shown('to')}", encode_noop_cb(session6)),
            _button("out ▶", encode_cb(session6, knob_idx, 3)),
        ],
    ]


def _recipe_rows(cookbook: dict[str, RecipeDef], current: object,
                 session6: str) -> list[list[dict[str, str]]]:
    buttons = []
    for name, defn in cookbook.items():
        label = f"{defn.emoji} {name}".strip()
        text = f"• {label}" if name == current else label
        buttons.append(_button(text, encode_recipe_cb(session6, name)))
    return [buttons[i : i + RECIPES_PER_ROW] for i in range(0, len(buttons), RECIPES_PER_ROW)]


def _text_row(knob: Knob, knob_idx: int, session6: str) -> list[dict[str, str]]:
    return [_button(f"✎ {knob.label}", encode_text_cb(session6, knob_idx))]


def _actions_row(session6: str) -> list[dict[str, str]]:
    return [_button(text, encode_action_cb(session6, action)) for text, action in _ACTION_BUTTONS]


def build_keyboard(defn: RecipeDef, inst: dict, session6: str,
                   cookbook: dict[str, RecipeDef] | None = None) -> list[list[dict[str, str]]]:
    """Rows of inline buttons for every knob in ``defn``, then the actions row.

    ``knob_idx`` is the knob's position in ``defn.knobs`` so ``apply_knob`` can
    resolve a press without any user text in the callback. With ``cookbook``
    given, a recipe-switch row (or rows, 4 per row) comes first.
    """
    _check_session(session6)
    rows: list[list[dict[str, str]]] = []
    if cookbook:
        rows.extend(_recipe_rows(cookbook, inst.get("recipe"), session6))
    for knob_idx, knob in enumerate(defn.knobs):
        if knob.type == "enum":
            rows.extend(_enum_rows(knob, inst, knob_idx, session6))
        elif knob.type == "step":
            rows.append(_step_row(knob, inst, knob_idx, session6))
        elif knob.type == "range":
            rows.extend(_range_rows(inst, knob_idx, session6))
        elif knob.type == "text":
            rows.append(_text_row(knob, knob_idx, session6))
        else:
            raise ValueError(f"unknown knob type {knob.type!r} on {knob.key!r}")
    rows.append(_actions_row(session6))
    return rows
