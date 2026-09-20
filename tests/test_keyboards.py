"""Pure tests for clipwrightd.keyboards: callback grammar and knob -> rows."""

import pytest

from clipwright.recipe import Knob, RecipeDef
from clipwrightd import keyboards as kb

S = "Ab3_-z"  # a valid 6-char token


def _defn() -> RecipeDef:
    """One knob of each type, in a fixed order so knob_idx is known."""
    return RecipeDef(
        name="synthetic",
        blurb="one of each",
        emoji="🧪",
        pipeline="gifify",
        needs_input=True,
        knobs=[
            Knob(key="caption.text", label="Text", type="text", default="", max_len=60),
            Knob(key="caption.size", label="Size", type="step", default=64, min=24, max=160, step=8),
            Knob(
                key="caption.color", label="Colour", type="enum", default="#ffffff",
                values=["#ffffff", "#ffdd00", "#ff4444", "#44ff88", "#4488ff"],
                labels=["White", "Yellow", "Red", "Green", "Blue"],
            ),
            Knob(key="trim", label="Trim", type="range"),
        ],
    )


def _texts(row):
    return [b["text"] for b in row]


def _all_buttons(rows):
    return [b for row in rows for b in row]


# --- encode / decode -------------------------------------------------------

def test_knob_roundtrip():
    data = kb.encode_cb(S, 2, 3)
    assert data == f"c/{S}/2/3"
    cb = kb.decode_cb(data)
    assert (cb.kind, cb.session, cb.knob_idx, cb.value_idx, cb.action) == ("c", S, 2, 3, None)


def test_text_roundtrip():
    data = kb.encode_text_cb(S, 0)
    assert data == f"t/{S}/0"
    cb = kb.decode_cb(data)
    assert (cb.kind, cb.session, cb.knob_idx, cb.value_idx, cb.action) == ("t", S, 0, None, None)


@pytest.mark.parametrize("action", kb.ACTIONS)
def test_action_roundtrip(action):
    data = kb.encode_action_cb(S, action)
    assert data == f"a/{S}/{action}"
    cb = kb.decode_cb(data)
    assert (cb.kind, cb.session, cb.action, cb.knob_idx) == ("a", S, action, None)


def test_noop_roundtrip():
    data = kb.encode_noop_cb(S)
    assert data == f"n/{S}"
    cb = kb.decode_cb(data)
    assert (cb.kind, cb.session, cb.knob_idx, cb.value_idx, cb.action) == ("n", S, None, None, None)


def test_recipe_roundtrip():
    data = kb.encode_recipe_cb(S, "caption-loop")
    assert data == f"r/{S}/caption-loop"
    cb = kb.decode_cb(data)
    assert (cb.kind, cb.session, cb.recipe, cb.knob_idx, cb.action) == ("r", S, "caption-loop", None, None)


@pytest.mark.parametrize("bad", ["", "with space", "a/b", "x" * 41, "émoji", None, 3])
def test_invalid_recipe_name_rejected_on_encode(bad):
    with pytest.raises(ValueError):
        kb.encode_recipe_cb(S, bad)


def test_actions_list_is_fixed():
    assert kb.ACTIONS == ["undo", "cli", "export", "grid"]


@pytest.mark.parametrize("bad", ["abcde", "abcdefg", "abc/de", "ab.cde", "", "abcd e", "abcdé0"])
def test_invalid_session_token_rejected_on_encode(bad):
    with pytest.raises(ValueError):
        kb.encode_cb(bad, 0, 0)
    with pytest.raises(ValueError):
        kb.encode_noop_cb(bad)
    with pytest.raises(ValueError):
        kb.encode_action_cb(bad, "undo")


@pytest.mark.parametrize("bad", [-1, 1.5, "1", None, True])
def test_invalid_index_rejected_on_encode(bad):
    with pytest.raises(ValueError):
        kb.encode_cb(S, bad, 0)
    with pytest.raises(ValueError):
        kb.encode_cb(S, 0, bad)
    with pytest.raises(ValueError):
        kb.encode_text_cb(S, bad)


@pytest.mark.parametrize("bad", ["reroll", "UNDO", "", "cli/extra"])
def test_invalid_action_rejected_on_encode(bad):
    with pytest.raises(ValueError):
        kb.encode_action_cb(S, bad)


@pytest.mark.parametrize("bad", [
    "",
    "x/abcdef",
    f"c/{S}",                 # too few parts
    f"c/{S}/1",               # too few parts
    f"c/{S}/1/2/3",           # too many parts
    f"c/{S}/-1/0",            # negative index
    f"c/{S}/a/0",             # non-int index
    f"c/{S}/1.0/0",           # float index
    "c/abcde/0/0",            # 5-char token
    "c/abcdefg/0/0",          # 7-char token
    "c/ab.def/0/0",           # bad char in token
    f"t/{S}",                 # missing knob idx
    f"t/{S}/x",
    f"a/{S}/reroll",          # unknown action
    f"a/{S}",
    f"n/{S}/0",               # noop takes no indices
    "n/abc",
    f"r/{S}",                 # recipe switch needs a name
    f"r/{S}/",
    f"r/{S}/two/parts",
    "c/" + S + "/" + "9" * 70 + "/0",   # over 64 bytes
])
def test_decode_rejects_garbage(bad):
    with pytest.raises(ValueError):
        kb.decode_cb(bad)


def test_decode_rejects_non_string():
    with pytest.raises(ValueError):
        kb.decode_cb(None)  # type: ignore[arg-type]


def test_64_byte_cap_holds_with_three_digit_indices():
    data = kb.encode_cb(S, 999, 999)
    assert len(data.encode("utf-8")) <= 64
    assert kb.decode_cb(data).knob_idx == 999
    assert len(kb.encode_text_cb(S, 999).encode("utf-8")) <= 64
    for action in kb.ACTIONS:
        assert len(kb.encode_action_cb(S, action).encode("utf-8")) <= 64


# --- build_keyboard --------------------------------------------------------

def test_keyboard_shape_one_of_each():
    defn = _defn()
    inst = {"recipe": "synthetic", "from": "0:02.0", "to": 4.5,
            "caption": {"text": "hi", "size": 72, "color": "#ff4444"}}
    rows = kb.build_keyboard(defn, inst, S)

    # text (1 row) + step (1) + enum of 5 wrapped at 4 (2) + range (2) + actions (1)
    assert len(rows) == 7

    text_row = rows[0]
    assert _texts(text_row) == ["✎ Text"]
    assert text_row[0]["callback_data"] == f"t/{S}/0"

    step_row = rows[1]
    assert _texts(step_row) == ["−", "Size 72", "+"]
    assert [b["callback_data"] for b in step_row] == [f"c/{S}/1/0", f"n/{S}", f"c/{S}/1/1"]

    enum_rows = rows[2:4]
    assert [len(r) for r in enum_rows] == [4, 1]
    assert _texts(enum_rows[0]) == ["White", "Yellow", "• Red", "Green"]
    assert _texts(enum_rows[1]) == ["Blue"]
    assert [b["callback_data"] for b in enum_rows[0]] == [f"c/{S}/2/{i}" for i in range(4)]
    assert enum_rows[1][0]["callback_data"] == f"c/{S}/2/4"

    in_row, out_row = rows[4], rows[5]
    assert _texts(in_row) == ["◀ in", "in 0:02.0", "in ▶"]
    assert _texts(out_row) == ["◀ out", "out 0:04.5", "out ▶"]
    assert [b["callback_data"] for b in in_row] == [f"c/{S}/3/0", f"n/{S}", f"c/{S}/3/1"]
    assert [b["callback_data"] for b in out_row] == [f"c/{S}/3/2", f"n/{S}", f"c/{S}/3/3"]

    actions = rows[6]
    assert _texts(actions) == ["↩ Undo", "⌘ Show CLI", "⬇ Export", "🎲 Grid"]
    assert [b["callback_data"] for b in actions] == [f"a/{S}/{a}" for a in kb.ACTIONS]


def test_cookbook_puts_a_recipe_row_first_marking_the_current_one():
    defn = _defn()
    book = {
        "synthetic": defn,
        "other": RecipeDef("other", "", "🧩", "gifify", True, knobs=[]),
        "plain": RecipeDef("plain", "", "", "gifify", True, knobs=[]),
    }
    rows = kb.build_keyboard(defn, {"recipe": "synthetic"}, S, cookbook=book)
    assert len(rows) == 8 and rows[1:] == kb.build_keyboard(defn, {"recipe": "synthetic"}, S)
    assert _texts(rows[0]) == ["• 🧪 synthetic", "🧩 other", "plain"]
    assert [b["callback_data"] for b in rows[0]] == [f"r/{S}/synthetic", f"r/{S}/other", f"r/{S}/plain"]
    for b in rows[0]:
        assert kb.decode_cb(b["callback_data"]).kind == "r"

    five = {f"r{i}": RecipeDef(f"r{i}", "", "", "gifify", True, knobs=[]) for i in range(5)}
    wrapped = kb.build_keyboard(defn, {"recipe": "r4"}, S, cookbook=five)
    assert [len(r) for r in wrapped[:2]] == [4, 1] and _texts(wrapped[1]) == ["• r4"]
    assert kb.build_keyboard(defn, {}, S, cookbook={}) == kb.build_keyboard(defn, {}, S)


def test_current_enum_value_is_marked_exactly_once():
    defn = _defn()
    inst = {"caption": {"color": "#ffdd00"}}
    rows = kb.build_keyboard(defn, inst, S)
    enum_buttons = [b for b in _all_buttons(rows) if b["callback_data"].startswith(f"c/{S}/2/")]
    marked = [b["text"] for b in enum_buttons if b["text"].startswith("• ")]
    assert marked == ["• Yellow"]


def test_enum_falls_back_to_knob_default_and_str_value():
    defn = RecipeDef("t", "", "", "gifify", True, knobs=[
        Knob(key="loop", label="Loop", type="enum", default="seamless",
             values=["seamless", "boomerang", "crossfade", "none"]),
    ])
    rows = kb.build_keyboard(defn, {}, S)
    assert _texts(rows[0]) == ["• seamless", "boomerang", "crossfade", "none"]


def test_range_shows_zero_when_from_to_absent():
    defn = _defn()
    rows = kb.build_keyboard(defn, {}, S)
    assert _texts(rows[4]) == ["◀ in", "in 0:00.0", "in ▶"]
    assert _texts(rows[5]) == ["◀ out", "out 0:00.0", "out ▶"]


def test_step_uses_default_when_instance_lacks_value():
    defn = _defn()
    rows = kb.build_keyboard(defn, {}, S)
    assert _texts(rows[1]) == ["−", "Size 64", "+"]


def test_every_button_has_only_text_and_callback_data_under_64_bytes():
    defn = _defn()
    inst = {"caption": {"text": "x" * 60, "size": 160, "color": "#4488ff"}, "from": 0, "to": 59.9}
    rows = kb.build_keyboard(defn, inst, S)
    assert rows and all(isinstance(r, list) and r for r in rows)
    for b in _all_buttons(rows):
        assert set(b) == {"text", "callback_data"}
        assert b["text"]
        assert 1 <= len(b["callback_data"].encode("utf-8")) <= 64
        kb.decode_cb(b["callback_data"])  # every produced callback decodes


def test_build_keyboard_is_pure():
    defn = _defn()
    inst = {"caption": {"color": "#ffffff"}}
    before = repr(inst)
    a = kb.build_keyboard(defn, inst, S)
    b = kb.build_keyboard(defn, inst, S)
    assert a == b
    assert repr(inst) == before


def test_build_keyboard_rejects_bad_session_and_unknown_knob_type():
    defn = _defn()
    with pytest.raises(ValueError):
        kb.build_keyboard(defn, {}, "short")
    weird = RecipeDef("t", "", "", "gifify", True, knobs=[Knob(key="k", label="K", type="dial")])
    with pytest.raises(ValueError):
        kb.build_keyboard(weird, {}, S)


def test_real_cookbook_keyboards_stay_under_cap():
    """Every shipped recipe must render with every callback_data <= 64 bytes."""
    from clipwright.recipe import defaults, load_cookbook

    try:
        book = load_cookbook()
    except Exception:  # cookbook dir may be empty while other agents write it
        pytest.skip("no cookbook available")
    if not book:
        pytest.skip("cookbook is empty")
    for defn in book.values():
        rows = kb.build_keyboard(defn, defaults(defn), S)
        assert rows[-1][0]["callback_data"] == f"a/{S}/undo"
        for b in _all_buttons(rows):
            assert len(b["callback_data"].encode("utf-8")) <= 64


def test_enum_short_labels_fall_back_per_value_without_dropping_buttons():
    defn = RecipeDef("t", "", "", "gifify", True, knobs=[
        Knob(key="pos", label="Pos", type="enum", default="top",
             values=["top", "bottom", "center"], labels=["Top", "Bottom"]),
    ])
    rows = kb.build_keyboard(defn, {}, S)
    assert _texts(rows[0]) == ["• Top", "Bottom", "center"]
