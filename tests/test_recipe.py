"""Tests for clipwright.recipe: cookbook loading, validation, time parsing,
button semantics (apply_knob) and the TOML writer round trip. Pure; no ffmpeg."""
import copy
import math
import tomllib

import pytest

from clipwright import budget, recipe
from clipwright.recipe import Knob, RecipeDef, RecipeError

EXPECTED = {
    "gifify": ("gifify", True),
    "caption-loop": ("caption_loop", True),
    "boomerang": ("boomerang", True),
    "ken-burns": ("ken_burns", True),
    "typecard": ("typecard", False),
    "speed": ("speed", True),
}


@pytest.fixture(scope="module")
def book():
    return recipe.load_cookbook()


@pytest.fixture(scope="module")
def caption(book):
    return book["caption-loop"]


@pytest.fixture(scope="module")
def gifify(book):
    return book["gifify"]


def knob_index(defn, key):
    return next(i for i, k in enumerate(defn.knobs) if k.key == key)


# ---------------------------------------------------------------- cookbook

def test_cookbook_loads_all_shipped_defs(book):
    assert {n: (d.pipeline, d.needs_input) for n, d in book.items()} == EXPECTED
    for name, defn in book.items():
        assert defn.name == name
        assert defn.blurb and defn.emoji
        assert defn.knobs


def test_every_knob_is_well_typed(book):
    for defn in book.values():
        for knob in defn.knobs:
            assert knob.type in recipe.KNOB_TYPES
            assert knob.key and knob.label
            if knob.type == "enum":
                assert knob.values and knob.default in knob.values
                if knob.labels is not None:
                    assert len(knob.labels) == len(knob.values)
            elif knob.type == "step":
                assert knob.min < knob.max and knob.step > 0
                assert knob.min <= knob.default <= knob.max
            elif knob.type == "text":
                assert knob.max_len > 0 and isinstance(knob.default, str)
            else:
                assert knob.default is None and knob.step_s > 0


def test_cookbook_knob_specifics(book):
    g = book["gifify"]
    fps = g.knobs[knob_index(g, "fps")]
    assert (fps.min, fps.max, fps.step, fps.default) == (8, 30, 2, 15)
    colors = g.knobs[knob_index(g, "colors")]
    assert colors.values == [64, 128, 256] and colors.labels
    c = book["caption-loop"]
    text = c.knobs[knob_index(c, "caption.text")]
    assert (text.max_len, text.default) == (60, "")
    assert c.knobs[knob_index(c, "caption.pos")].values == ["top", "bottom"]
    assert c.knobs[knob_index(c, "trim")].step_s == 0.1
    assert [k.key for k in book["boomerang"].knobs] == ["fps", "width", "fits", "trim"]
    speed = book["speed"]
    assert [k.key for k in speed.knobs] == ["mode", "rate", "ramp", "fps", "width", "colors", "fits", "trim"]
    assert speed.knobs[knob_index(speed, "mode")].values == ["fixed", "ramp"]
    assert speed.knobs[knob_index(speed, "mode")].default == "fixed"
    rate = speed.knobs[knob_index(speed, "rate")]
    assert rate.values == [0.5, 0.75, 1.5, 2, 3, 4] and rate.default == 2
    assert rate.labels == ["0.5×", "0.75×", "1.5×", "2×", "3×", "4×"]
    ramp = speed.knobs[knob_index(speed, "ramp")]
    assert ramp.values == ["slow_fast", "fast_slow"] and ramp.default == "slow_fast"
    assert speed.knobs[knob_index(speed, "fps")].default == g.knobs[knob_index(g, "fps")].default
    assert speed.knobs[knob_index(speed, "trim")].step_s == 0.1


def test_load_cookbook_rejects_bad_definitions(tmp_path):
    (tmp_path / "bad.toml").write_text(
        'name = "bad"\npipeline = "gifify"\n[[knob]]\nkey = "x"\nlabel = "X"\n'
        'type = "enum"\nvalues = ["a"]\ndefault = "zzz"\n', encoding="utf-8")
    with pytest.raises(RecipeError, match="default"):
        recipe.load_cookbook(str(tmp_path))
    (tmp_path / "bad.toml").write_text('name = "bad"\npipeline = = 1\n', encoding="utf-8")
    with pytest.raises(RecipeError):
        recipe.load_cookbook(str(tmp_path))
    with pytest.raises(RecipeError):
        recipe.load_cookbook(str(tmp_path / "missing"))


# ---------------------------------------------------------------- defaults / validate

def test_defaults_validate_clean(book):
    for defn in book.values():
        inst = recipe.defaults(defn)
        assert inst["recipe"] == defn.name
        assert recipe.validate(inst, defn) == []


def test_validate_catches_bad_enum(caption):
    inst = recipe.defaults(caption)
    recipe.set_(inst, "caption.color", "#123456")
    assert any("caption.color" in p for p in recipe.validate(inst, caption))


def test_validate_catches_out_of_range_step(caption):
    inst = recipe.defaults(caption)
    recipe.set_(inst, "caption.size", 8)
    assert any("caption.size" in p for p in recipe.validate(inst, caption))
    recipe.set_(inst, "caption.size", "64")
    assert any("caption.size" in p for p in recipe.validate(inst, caption))


def test_validate_catches_over_long_text(caption):
    inst = recipe.defaults(caption)
    recipe.set_(inst, "caption.text", "x" * 61)
    assert any("caption.text" in p for p in recipe.validate(inst, caption))
    recipe.set_(inst, "caption.text", "x" * 60)
    assert recipe.validate(inst, caption) == []


def test_validate_catches_from_not_before_to(gifify):
    inst = recipe.defaults(gifify)
    inst["from"], inst["to"] = "0:04.0", "0:04.0"
    assert any("from" in p for p in recipe.validate(inst, gifify))
    inst["from"], inst["to"] = "0:04.5", 4.0
    assert any("from" in p for p in recipe.validate(inst, gifify))
    inst["from"], inst["to"] = "0:02.0", "abc"
    assert any("to" in p for p in recipe.validate(inst, gifify))
    inst["from"], inst["to"] = "0:02.0", "0:04.5"
    assert recipe.validate(inst, gifify) == []


def test_validate_recipe_name_and_scalars(gifify):
    inst = recipe.defaults(gifify)
    inst["recipe"] = "caption-loop"
    assert any("recipe" in p for p in recipe.validate(inst, gifify))
    inst = recipe.defaults(gifify)
    inst["seed"], inst["input"] = "six", 3
    problems = recipe.validate(inst, gifify)
    assert any("seed" in p for p in problems) and any("input" in p for p in problems)
    assert recipe.validate("nope", gifify)


def test_speed_defaults_validate_and_bad_enums_do_not(book):
    defn = book["speed"]
    inst = recipe.defaults(defn)
    assert recipe.validate(inst, defn) == []
    assert inst["mode"] == "fixed" and inst["rate"] == 2 and inst["ramp"] == "slow_fast"
    inst["rate"] = 1.25
    assert any("rate" in p for p in recipe.validate(inst, defn))
    inst = recipe.defaults(defn)
    inst["mode"] = "zoom"
    assert any("mode" in p for p in recipe.validate(inst, defn))
    inst = recipe.defaults(defn)
    inst["ramp"] = "sideways"
    assert any("ramp" in p for p in recipe.validate(inst, defn))
    inst = recipe.defaults(defn)
    inst["mode"] = "ramp"
    inst["rate"] = 0.5
    inst["ramp"] = "fast_slow"
    assert recipe.validate(inst, defn) == []


def test_validate_enum_is_type_strict(gifify):
    inst = recipe.defaults(gifify)
    inst["colors"] = "128"
    assert any("colors" in p for p in recipe.validate(inst, gifify))


def test_validate_fits_accepts_every_preset_none_and_mb(gifify):
    inst = recipe.defaults(gifify)
    for fits in (*budget.PRESETS, "none", "NONE", "TELEGRAM", " discord ",
                 "6", "6MB", "2.5 mb", 6, 2.5):
        inst["fits"] = fits
        assert recipe.validate(inst, gifify) == [], fits
    for fits in ("mars", "0", -1, "6GB", "", True, float("inf"), float("nan")):
        inst["fits"] = fits
        assert any("fits" in p for p in recipe.validate(inst, gifify)), fits


def test_validate_fits_agrees_with_budget_bytes(book):
    """validate() defers to the one parser: what the cook can resolve is legal, nothing else is."""
    for defn in book.values():
        inst = recipe.defaults(defn)
        for fits in ("whatsapp-sticker", "shorts-9x16", "6m", "6M", "6 MB", "0.5", "1e3",
                     "-6", "6mbps", 1e3, "none"):
            inst["fits"] = fits
            try:
                budget.budget_bytes(fits)
                legal = True
            except ValueError:
                legal = False
            assert (recipe.validate(inst, defn) == []) is legal, (defn.name, fits)


def test_missing_knobs_are_not_errors(caption):
    assert recipe.validate({"recipe": "caption-loop"}, caption) == []


# ---------------------------------------------------------------- get / set_

def test_get_and_set_nest_dotted_keys():
    inst = {"recipe": "caption-loop"}
    recipe.set_(inst, "caption.text", "hi")
    assert inst == {"recipe": "caption-loop", "caption": {"text": "hi"}}
    assert recipe.get(inst, "caption.text") == "hi"
    assert recipe.get(inst, "caption.size") is None
    assert recipe.get(inst, "caption.size", 64) == 64
    assert recipe.get(inst, "recipe.x", "dflt") == "dflt"
    with pytest.raises(RecipeError):
        recipe.set_(inst, "recipe.x", 1)


# ---------------------------------------------------------------- time

@pytest.mark.parametrize("raw, want", [
    ("0:02.5", 2.5), ("2.5", 2.5), (2.5, 2.5), (2, 2.0), ("1:02:03.5", 3723.5),
    ("0:00.0", 0.0), (" 12 ", 12.0), ("1:05", 65.0),
])
def test_parse_time_accepts(raw, want):
    assert recipe.parse_time(raw) == pytest.approx(want)


@pytest.mark.parametrize("raw", ["abc", "-1", -1, -0.5, "", "1:2:3:4", "1.5:00", None,
                                 True, "0:0x", float("nan"), float("inf")])
def test_parse_time_rejects(raw):
    with pytest.raises(RecipeError):
        recipe.parse_time(raw)


def test_fmt_time():
    assert recipe.fmt_time(2.5) == "0:02.5"
    assert recipe.fmt_time(0) == "0:00.0"
    assert recipe.fmt_time(65) == "1:05.0"
    assert recipe.fmt_time(2.25) == "0:02.25"
    assert recipe.fmt_time(3723.5) == "1:02:03.5"
    assert recipe.fmt_time(59.96) == "0:59.96"
    with pytest.raises(RecipeError):
        recipe.fmt_time(-1)


def test_fmt_parse_round_trip():
    for tenths in range(0, 1300, 7):
        secs = tenths / 10
        assert recipe.parse_time(recipe.fmt_time(secs)) == pytest.approx(secs)


# ---------------------------------------------------------------- apply_knob

def test_apply_enum_returns_new_dict(caption):
    inst = recipe.defaults(caption)
    before = copy.deepcopy(inst)
    out = recipe.apply_knob(inst, caption, knob_index(caption, "caption.color"), 1)
    assert out is not inst and out["caption"] is not inst["caption"]
    assert recipe.get(out, "caption.color") == "#ffdd00"
    assert inst == before
    with pytest.raises(RecipeError):
        recipe.apply_knob(inst, caption, knob_index(caption, "caption.color"), 9)


def test_apply_step_increments_decrements_and_clamps(caption):
    idx = knob_index(caption, "caption.size")
    inst = recipe.defaults(caption)
    before = copy.deepcopy(inst)
    up = recipe.apply_knob(inst, caption, idx, 1)
    down = recipe.apply_knob(inst, caption, idx, 0)
    assert recipe.get(up, "caption.size") == 72
    assert recipe.get(down, "caption.size") == 56
    assert inst == before
    recipe.set_(inst, "caption.size", 160)
    assert recipe.get(recipe.apply_knob(inst, caption, idx, 1), "caption.size") == 160
    recipe.set_(inst, "caption.size", 24)
    assert recipe.get(recipe.apply_knob(inst, caption, idx, 0), "caption.size") == 24
    with pytest.raises(RecipeError):
        recipe.apply_knob(inst, caption, idx, 2)


def test_apply_step_starts_from_default_when_absent(gifify):
    out = recipe.apply_knob({"recipe": "gifify"}, gifify, knob_index(gifify, "fps"), 1)
    assert out["fps"] == 17


def test_apply_range_moves_in_and_out(gifify):
    idx = knob_index(gifify, "trim")
    inst = recipe.defaults(gifify)
    inst["from"], inst["to"] = "0:01.0", "0:02.0"
    before = copy.deepcopy(inst)
    assert recipe.apply_knob(inst, gifify, idx, 0)["from"] == "0:00.9"
    assert recipe.apply_knob(inst, gifify, idx, 1)["from"] == "0:01.1"
    assert recipe.apply_knob(inst, gifify, idx, 2)["to"] == "0:01.9"
    assert recipe.apply_knob(inst, gifify, idx, 3)["to"] == "0:02.1"
    assert inst == before
    with pytest.raises(RecipeError):
        recipe.apply_knob(inst, gifify, idx, 4)


def test_apply_range_clamps_at_zero_and_keeps_in_before_out(gifify):
    idx = knob_index(gifify, "trim")
    inst = recipe.defaults(gifify)
    inst["to"] = "0:00.3"
    out = recipe.apply_knob(inst, gifify, idx, 0)
    assert out["from"] == "0:00.0"
    out = recipe.apply_knob(out, gifify, idx, 0)
    assert out["from"] == "0:00.0"
    for _ in range(10):
        out = recipe.apply_knob(out, gifify, idx, 1)
        assert recipe.parse_time(out["from"]) < recipe.parse_time(out["to"])
    assert out["from"] == "0:00.2"
    for _ in range(10):
        out = recipe.apply_knob(out, gifify, idx, 2)
        assert recipe.parse_time(out["from"]) < recipe.parse_time(out["to"])
    assert out["to"] == "0:00.3"
    assert recipe.validate(out, gifify) == []


def test_apply_range_out_needs_known_to(gifify):
    idx = knob_index(gifify, "trim")
    inst = recipe.defaults(gifify)
    assert recipe.apply_knob(inst, gifify, idx, 1)["from"] == "0:00.1"
    for press in (2, 3):
        with pytest.raises(RecipeError, match="to"):
            recipe.apply_knob(inst, gifify, idx, press)


def test_apply_text_is_not_a_button(caption):
    inst = recipe.defaults(caption)
    before = copy.deepcopy(inst)
    with pytest.raises(RecipeError):
        recipe.apply_knob(inst, caption, knob_index(caption, "caption.text"), 0)
    with pytest.raises(RecipeError):
        recipe.apply_knob(inst, caption, 99, 0)
    assert inst == before


# ---------------------------------------------------------------- TOML writer

def test_dumps_round_trips_through_tomllib():
    inst = {
        "recipe": "caption-loop",
        "input": "clips/paper airplane.mp4",
        "from": "0:02.0",
        "to": 4.5,
        "loop": "seamless",
        "seed": 6,
        "proxy": False,
        "ratio": 1.0,
        "tags": ["a", "b c", 3, 2.5, True],
        "empty": [],
        "caption": {
            "text": 'she said "über" \\ ünïcödé — 🎞️\tтест\nnew line \x01',
            "size": 64,
            "color": "#ffdd00",
            "sharp": True,
        },
    }
    text = recipe.dumps_toml(inst)
    assert tomllib.loads(text) == inst
    assert recipe.loads_instance(text) == inst


def test_dumps_writes_scalars_before_tables_and_lowercase_bools():
    text = recipe.dumps_toml({"recipe": "x", "caption": {"text": "hi"}, "loop": "none",
                              "flag": True, "f": 2.0, "none_dropped": None})
    assert text == 'recipe = "x"\nloop = "none"\nflag = true\nf = 2.0\n\n[caption]\ntext = "hi"\n'


def test_dumps_quotes_odd_keys_and_rejects_deep_nesting():
    text = recipe.dumps_toml({"recipe": "x", "odd key": 1, "t": {"a.b": 2}})
    assert tomllib.loads(text) == {"recipe": "x", "odd key": 1, "t": {"a.b": 2}}
    with pytest.raises(RecipeError):
        recipe.dumps_toml({"a": {"b": {"c": 1}}})
    with pytest.raises(RecipeError):
        recipe.dumps_toml({"a": [[1]]})
    with pytest.raises(RecipeError):
        recipe.dumps_toml({"a": object()})


def test_dumps_floats_via_repr():
    for f in (1e-05, 1e100, -0.0, 0.1, float("inf"), 3.0):
        got = tomllib.loads(recipe.dumps_toml({"f": f}))["f"]
        assert got == f and isinstance(got, float)
    nan = tomllib.loads(recipe.dumps_toml({"f": float("nan")}))["f"]
    assert math.isnan(nan)


def test_dump_and_load_instance_file(tmp_path, caption):
    inst = recipe.defaults(caption)
    recipe.set_(inst, "caption.text", 'quote " and 🪃')
    inst["from"], inst["to"] = "0:01.0", "0:02.5"
    path = str(tmp_path / "x.recipe.toml")
    recipe.dump_instance(inst, path)
    assert recipe.load_instance(path) == inst
    assert not (tmp_path / "x.recipe.toml.tmp").exists()
    assert recipe.validate(recipe.load_instance(path), caption) == []


def test_loads_instance_rejects_bad_input():
    with pytest.raises(RecipeError):
        recipe.loads_instance("loop = ")
    with pytest.raises(RecipeError, match="recipe"):
        recipe.loads_instance('loop = "seamless"\n')


def test_load_instance_rejects_bad_file(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_bytes(b'recipe = "x"\n\xff\xfe')
    with pytest.raises(RecipeError):
        recipe.load_instance(str(p))


def test_dataclasses_match_contract():
    knob = Knob(key="k", label="K", type="enum", values=["a"], default="a")
    assert knob.step_s == 0.1 and knob.max_len is None
    defn = RecipeDef(name="n", blurb="b", emoji="e", pipeline="p", needs_input=False, knobs=[knob])
    assert recipe.defaults(defn) == {"recipe": "n", "k": "a"}
    assert recipe.validate(recipe.defaults(defn), defn) == []
