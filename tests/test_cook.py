"""Tests for clipwright.cook and the clip pipelines (gifify, caption_loop, boomerang).

Renders are shared through module-scoped fixtures so each pipeline cooks once;
the whole file stays well under the 40 s budget.
"""
import os
import shlex
import re
import subprocess

import pytest
from PIL import Image, ImageChops, ImageStat

from clipwright import budget, cli, cook as cook_mod, ffmpeg, recipe
from clipwright.budget import Candidate
from clipwright.cook import cli_command, cook, stem_for, undeclared_keys, with_defaults
from clipwright.ffmpeg import Probe
from clipwright.pipelines import CookContext, caption_loop, common
from clipwright.recipe import RecipeError

REPORT_KEYS = {"bytes", "budget", "fits", "width", "fps", "colors", "duration",
               "loop", "attempts", "from", "to", "capped", "sidecar", "cli", "lossy"}
STEM_RE = re.compile(r"^[a-z0-9-]+-[0-9a-f]{6}$")


def _inst(book, name, clip, **knobs):
    inst = recipe.defaults(book[name])
    inst["input"] = clip
    for key, value in knobs.items():
        recipe.set_(inst, key, value)
    return inst


def _fake_probe(duration: float, width: int = 320, height: int = 240) -> Probe:
    return Probe(path="fake.mp4", duration=duration, width=width, height=height, fps=30.0,
                 nb_frames=int(duration * 30), vcodec="h264", has_audio=False, size_bytes=0)


def _band(path: str, *, top: bool) -> Image.Image:
    """The top or bottom quarter of a GIF's first frame, greyscale."""
    im = Image.open(path).convert("L")
    w, h = im.size
    return im.crop((0, 0, w, h // 4) if top else (0, h - h // 4, w, h))


def _coarse_hist(im: Image.Image, bins: int = 16) -> list[float]:
    hist = im.histogram()
    total = sum(hist)
    step = 256 // bins
    return [sum(hist[i:i + step]) / total for i in range(0, 256, step)]


def _l1(a: list[float], b: list[float]) -> float:
    return sum(abs(x - y) for x, y in zip(a, b))


def _mean_abs_diff(a: Image.Image, b: Image.Image) -> float:
    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]


# --- shared renders ---------------------------------------------------------

@pytest.fixture(scope="module")
def book():
    return recipe.load_cookbook()


@pytest.fixture(scope="module")
def out_root(tmp_path_factory):
    return tmp_path_factory.mktemp("cooked")


@pytest.fixture(scope="module")
def full_gifify(book, test_clip, out_root):
    inst = _inst(book, "gifify", test_clip)
    return inst, cook(inst, out_dir=str(out_root / "gifify"), cookbook=book)


@pytest.fixture(scope="module")
def plain_none(book, test_clip, out_root):
    inst = _inst(book, "gifify", test_clip, loop="none", colors=128)
    return inst, cook(inst, out_dir=str(out_root / "none"), cookbook=book)


@pytest.fixture(scope="module")
def boom(book, test_clip, out_root):
    inst = _inst(book, "boomerang", test_clip)
    return inst, cook(inst, out_dir=str(out_root / "boom"), cookbook=book)


@pytest.fixture(scope="module")
def captioned(book, test_clip, out_root):
    inst = _inst(book, "caption-loop", test_clip, loop="none", **{"caption.text": "hello world"})
    return inst, cook(inst, out_dir=str(out_root / "caption"), cookbook=book)


@pytest.fixture(scope="module")
def proxy(book, test_clip, out_root):
    inst = _inst(book, "gifify", test_clip)
    return inst, cook(inst, out_dir=str(out_root / "proxy"), proxy=True, cookbook=book)


@pytest.fixture(scope="module")
def long_clip(tmp_path_factory):
    """A 12 s clip: longer than the old 10 s proxy cap, shorter than the 15 s cap."""
    path = str(tmp_path_factory.mktemp("long") / "long.mp4")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=12:duration=12",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", path],
        check=True,
    )
    return path


@pytest.fixture(scope="module")
def over_budget(book, test_clip, out_root):
    """gifify against a 10 000-byte budget nothing can meet: the lossy-squeeze path."""
    inst = _inst(book, "gifify", test_clip, loop="none", fits=0.01)
    return inst, cook(inst, out_dir=str(out_root / "over"), cookbook=book)


# --- full gifify ------------------------------------------------------------

def test_full_gifify_writes_gif_mp4_and_sidecar(full_gifify):
    inst, result = full_gifify
    assert os.path.isfile(result.gif) and result.gif.endswith(".gif")
    assert os.path.isfile(result.mp4) and result.mp4.endswith(".mp4")
    with open(result.gif, "rb") as fh:
        assert fh.read(6) == b"GIF89a"
    sidecar = result.report["sidecar"]
    assert os.path.isfile(sidecar)
    assert sidecar == os.path.splitext(result.gif)[0] + ".recipe.toml"
    assert os.path.dirname(result.gif) == os.path.dirname(result.mp4) == os.path.dirname(sidecar)


def test_full_gifify_report(full_gifify):
    inst, result = full_gifify
    report = result.report
    assert REPORT_KEYS <= set(report)
    assert report["fits"] is True
    assert report["budget"] == 8_000_000
    assert report["bytes"] == os.path.getsize(result.gif) <= report["budget"]
    assert report["width"] == 320          # the source is narrower than the 480 knob
    assert report["fps"] == 15 and report["colors"] == 256   # the knobs' exact ask, attempt 1
    assert report["attempts"] == 1
    assert report["capped"] is False and report["proxy"] is False
    assert report["from"] == 0.0 and 0 < report["to"] <= 3.0
    assert report["duration"] == pytest.approx(report["to"] - report["from"], abs=1e-3)


def test_seamless_runs_the_loop_finder(full_gifify):
    inst, result = full_gifify
    report = result.report
    assert report["loop"] in ("seamless", "boomerang (degraded)")
    assert isinstance(report["loop_score"], float) and report["loop_score"] >= 0
    assert isinstance(report["loop_nudge_frames"], int) and report["loop_nudge_frames"] <= 0
    if report["loop"] == "seamless":
        assert report["to"] == pytest.approx(3.0 + report["loop_nudge_frames"] / common.LOOP_SAMPLE_FPS, abs=0.01)
    # the finder's rawvideo sample is the first logged command
    assert result.argv_log[0][0] == "ffmpeg" and "rawvideo" in result.argv_log[0]


def test_argv_log_is_lists_of_strings_with_no_shell(full_gifify):
    inst, result = full_gifify
    assert result.argv_log and result.argv_log is not None
    for argv in result.argv_log:
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        assert argv[0] in ("ffmpeg", "gifsicle")
    tools = [argv[0] for argv in result.argv_log]
    assert tools.count("gifsicle") == 1
    assert tools.count("ffmpeg") >= 3   # loop sample, gif encode, mp4 preview
    assert result.report["lossy"] is None


# --- over budget: smallest encode, -O3, then --lossy --------------------------

def test_over_budget_returns_the_smallest_encode_lossy_squeezed(over_budget):
    inst, result = over_budget
    report = result.report
    assert report["budget"] == 10_000
    assert report["fits"] is False
    assert report["lossy"] == common.LOSSY_SQUEEZE
    assert report["bytes"] == os.path.getsize(result.gif) > report["budget"]
    # attempt 1 is the knobs' ask; when its bytes say no rung can fit, the
    # solver tries the cheapest rung once and stops instead of burning attempts
    assert report["attempts"] == 2
    assert (report["width"], report["fps"], report["colors"]) == (240, 8, 64)
    squeezes = [argv for argv in result.argv_log if argv[0] == "gifsicle"]
    assert len(squeezes) == 2
    assert "--lossy=30" not in squeezes[0] and f"--lossy={common.LOSSY_SQUEEZE}" in squeezes[1]
    assert all(argv[-1].endswith("-240w-8f-64c.gif") for argv in squeezes)
    with open(result.gif, "rb") as fh:
        assert fh.read(6) == b"GIF89a"
    assert "fits 0.01 MB ✗" in cli.format_result(inst, result)


# --- absent knobs cook with the cookbook defaults ---------------------------

def test_with_defaults_fills_only_what_the_instance_leaves_unset(book):
    inst = {"recipe": "gifify", "input": "x.mp4", "loop": "none", "from": "0:01.0"}
    filled = with_defaults(inst, book["gifify"])
    assert filled == {"recipe": "gifify", "input": "x.mp4", "loop": "none", "from": "0:01.0",
                      "fps": 15, "width": 480, "colors": 256, "fits": "telegram"}
    assert inst == {"recipe": "gifify", "input": "x.mp4", "loop": "none", "from": "0:01.0"}
    assert "trim" not in filled and "to" not in filled           # range knobs carry no default
    cap = with_defaults({"recipe": "caption-loop", "caption": {"text": "hi"}}, book["caption-loop"])
    assert cap["caption"] == {"text": "hi", "size": 64, "color": "#ffffff", "pos": "bottom"}
    full = _inst(book, "caption-loop", "x.mp4", **{"caption.size": 72})
    assert with_defaults(full, book["caption-loop"]) == full and with_defaults(full, book["caption-loop"]) is not full


def test_absent_knobs_cook_with_the_cookbook_defaults(book, test_clip, tmp_out):
    """A hand-trimmed sidecar (no fits/fps/colors) validates, so it must cook exactly like
    the full one: Telegram's budget enforced and reported, 15 fps / 256 colours, the same
    stem, and a sidecar that spells the defaults out."""
    inst = {"recipe": "gifify", "input": test_clip, "loop": "none"}
    assert recipe.validate(inst, book["gifify"]) == []
    result = cook(inst, out_dir=tmp_out, cookbook=book)
    report = result.report
    assert (report["fps"], report["colors"]) == (15, 256)
    assert report["budget"] == 8_000_000 and report["fits"] is True
    assert inst == {"recipe": "gifify", "input": test_clip, "loop": "none"}   # caller's dict untouched
    loaded = recipe.load_instance(report["sidecar"])
    assert loaded == _inst(book, "gifify", test_clip, loop="none")
    assert os.path.basename(result.gif) == stem_for(_inst(book, "gifify", test_clip, loop="none")) + ".gif"
    assert report["cli"] == cli_command(_inst(book, "gifify", test_clip, loop="none"), cookbook=book)


# --- caption_loop -----------------------------------------------------------

def test_frame_height_matches_ffmpeg_scale_minus_two():
    """`scale=W:-2` rounds the half-height to nearest (half away from zero) and doubles it;
    these values were checked against ffmpeg's actual output for each source."""
    cases = [((1280, 720), 480, 270), ((320, 240), 240, 180), ((240, 320), 320, 426),
             ((320, 180), 240, 136), ((500, 169), 480, 162), ((1080, 1920), 240, 426),
             ((2, 1920), 2, 1920), ((1920, 2), 240, 2), ((701, 300), 240, 102)]
    for (w, h), out_w, expected in cases:
        assert common.frame_height(_fake_probe(3.0, w, h), out_w) == expected, (w, h, out_w)
    with pytest.raises(ValueError):
        common.frame_height(_fake_probe(3.0, 0, 240), 240)


def test_cook_clip_hands_the_overlay_factory_the_frame_height(test_clip, tmp_path):
    asked = []

    def spy(width, height):
        asked.append((width, height))
        return None

    ctx = CookContext(workdir=str(tmp_path), out_dir=str(tmp_path / "out"), stem="spy", proxy=True)
    os.makedirs(ctx.out_dir)
    common.cook_clip({"recipe": "gifify", "input": test_clip}, ctx, loop="none", overlay=spy)
    assert asked == [(240, 180)]                                     # 320x240 proxied at 240 wide


def test_caption_band_never_taller_than_the_frame(book, test_clip, tmp_path):
    """The cookbook-legal defaults (60 chars at size 64) wrap to six lines: 504 px tall on a
    480x270 frame, so `overlay=0:main_h-overlay_h` would start the band 234 px above the
    frame and the first three lines would never be seen. The factory bounds the band by
    the frame height it is given; the top of the text then lands inside the frame."""
    sixty = "The quick brown fox jumps over the lazy dog again and again!"
    inst = _inst(book, "caption-loop", test_clip, **{"caption.text": sixty})
    ctx = CookContext(workdir=str(tmp_path), out_dir=str(tmp_path), stem="band")
    overlay = caption_loop.caption_overlay(inst, ctx)
    unbounded = caption_loop.caption.render_caption(sixty, 480, size=64)
    assert unbounded.height > 270                                    # the bound has to bite
    for width, frame_h in ((480, 270), (240, 136), (320, 240)):
        with Image.open(overlay(width, frame_h)) as band:
            assert band.width == width and 1 < band.height <= frame_h, (width, frame_h)
    assert overlay(480, 270) == overlay(480, 270)                    # cached per width
    huge = _inst(book, "caption-loop", test_clip, **{"caption.text": sixty, "caption.size": 160})
    with Image.open(caption_loop.caption_overlay(huge, ctx)(320, 240)) as band:
        assert band.height <= 240


# --- caption_loop (shared renders) ------------------------------------------

def test_caption_changes_only_the_bottom_band(plain_none, captioned):
    plain, cap = plain_none[1], captioned[1]
    assert os.path.isfile(cap.gif)
    bottom_l1 = _l1(_coarse_hist(_band(plain.gif, top=False)), _coarse_hist(_band(cap.gif, top=False)))
    top_l1 = _l1(_coarse_hist(_band(plain.gif, top=True)), _coarse_hist(_band(cap.gif, top=True)))
    assert bottom_l1 > 0.2
    assert bottom_l1 > 5 * top_l1
    assert _mean_abs_diff(_band(plain.gif, top=False), _band(cap.gif, top=False)) > 10
    assert _mean_abs_diff(_band(plain.gif, top=True), _band(cap.gif, top=True)) < 5


def test_caption_text_never_enters_an_argv(captioned):
    inst, result = captioned
    for argv in result.argv_log:
        assert not any("hello world" in a or "HELLO WORLD" in a for a in argv)
        if "-filter_complex" in argv:
            assert "overlay=0:main_h-overlay_h" in argv[argv.index("-filter_complex") + 1]
    # the overlay PNG lived in the temp workdir, which is gone after the cook
    pngs = [a for argv in result.argv_log for a in argv if a.endswith(".png")]
    assert pngs and all(os.path.basename(p).startswith("caption-") for p in pngs)
    assert not any(os.path.exists(p) for p in pngs)


def test_blank_caption_renders_without_an_overlay(book, test_clip, tmp_out):
    inst = _inst(book, "caption-loop", test_clip, loop="none")
    result = cook(inst, out_dir=tmp_out, proxy=True, cookbook=book)
    assert not any(a.endswith(".png") for argv in result.argv_log for a in argv)
    assert stem_for(inst).startswith("caption-loop-")


# --- boomerang --------------------------------------------------------------

def test_boomerang_has_twice_the_frames(plain_none, boom):
    plain, back = plain_none[1], boom[1]
    assert plain.report["fps"] == back.report["fps"] == 15
    n_plain = Image.open(plain.gif).n_frames
    n_boom = Image.open(back.gif).n_frames
    assert abs(n_boom - 2 * n_plain) <= 2
    assert back.report["loop"] == "boomerang"
    assert back.report["duration"] == pytest.approx(2 * plain.report["duration"], abs=1e-3)


# --- proxy mode -------------------------------------------------------------

def test_proxy_returns_a_small_mp4_only(proxy):
    inst, result = proxy
    assert result.gif is None
    assert os.path.isfile(result.mp4)
    assert not any(n.endswith(".gif") for n in os.listdir(os.path.dirname(result.mp4)))
    report = result.report
    assert report["proxy"] is True and report["width"] <= 240
    assert report["fps"] == 12 and report["colors"] is None and report["fits"] is None
    assert report["attempts"] == 0 and report["bytes"] == os.path.getsize(result.mp4)
    info = ffmpeg.probe(result.mp4)
    assert info.width <= 240 and info.fps == pytest.approx(12, abs=0.01)
    assert os.path.isfile(report["sidecar"])


def test_crossfade_renders_straight_and_says_so(book, test_clip, tmp_out):
    inst = _inst(book, "gifify", test_clip, loop="crossfade")
    result = cook(inst, out_dir=tmp_out, proxy=True, cookbook=book)
    assert result.report["loop"] == "none (crossfade not implemented)"
    assert "loop_score" not in result.report
    assert result.report["to"] == 3.0


# --- cook() validation ------------------------------------------------------

def test_cook_rejects_missing_input(book, tmp_out):
    inst = _inst(book, "gifify", os.path.join(tmp_out, "nope.mp4"))
    with pytest.raises(RecipeError, match="not found"):
        cook(inst, out_dir=tmp_out, cookbook=book)
    inst["input"] = None
    with pytest.raises(RecipeError, match="needs an input"):
        cook(inst, out_dir=tmp_out, cookbook=book)


def test_cook_rejects_invalid_knob_and_unknown_recipe(book, test_clip, tmp_out):
    inst = _inst(book, "gifify", test_clip, fps=7)
    with pytest.raises(RecipeError, match="fps"):
        cook(inst, out_dir=tmp_out, cookbook=book)
    with pytest.raises(RecipeError, match="unknown recipe"):
        cook({"recipe": "nope", "input": test_clip}, out_dir=tmp_out, cookbook=book)
    assert not os.listdir(tmp_out)


@pytest.mark.parametrize("name, key, value", [
    ("caption-loop", "caption.style", "bogus"),   # rendered by an old build, never a knob
    ("caption-loop", "width", float("inf")),      # no width knob: would overflow int()
    ("caption-loop", "fps", 0),                   # no fps knob: would reach ffmpeg's fps check
    ("gifify", "caption.text", "hi"),             # a knob, but of another recipe
    ("gifify", "sparkle", 1),
    ("boomerang", "loop", "none"),                # boomerang's loop is fixed, not a knob
])
def test_cook_rejects_keys_the_recipe_does_not_declare(book, test_clip, tmp_out, name, key, value):
    inst = _inst(book, name, test_clip, **{key: value})
    assert recipe.validate(inst, book[name]) == []      # the knob checks alone let it through
    assert undeclared_keys(inst, book[name]) == [f"{key}: not a knob of {name}"]
    with pytest.raises(RecipeError, match=re.escape(key)):
        cook(inst, out_dir=tmp_out, cookbook=book)
    assert not os.listdir(tmp_out)


def test_undeclared_keys_accepts_every_instance_field_and_knob(book, test_clip):
    for name, defn in book.items():
        inst = _inst(book, name, test_clip)
        inst["from"], inst["to"], inst["seed"] = "0:00.5", 2.5, 6
        assert undeclared_keys(inst, defn) == []
    assert undeclared_keys({"recipe": "gifify", "trim": 1}, book["gifify"]) == ["trim: not a knob of gifify"]


def test_bad_fits_fails_before_any_ffmpeg_work(book, test_clip, tmp_out, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("ffmpeg must not run for an unusable fits")

    monkeypatch.setattr(common.ffmpeg, "probe", boom)
    monkeypatch.setattr(common.ffmpeg, "run", boom)
    for fits in (float("inf"), "1" + "0" * 400, budget.MAX_MB + 1):
        inst = _inst(book, "gifify", test_clip, fits=fits)
        # validate defers to budget.budget_bytes, so it flags the value first;
        # common.cook_clip's own budget_bytes guard is the second line of defence.
        assert any("fits" in p for p in recipe.validate(inst, book["gifify"]))
        with pytest.raises(RecipeError, match="fits"):
            cook(inst, out_dir=tmp_out, cookbook=book)
    assert not os.listdir(tmp_out)


def test_cook_removes_its_workdir(book, test_clip, tmp_out, monkeypatch):
    made = []
    real = cook_mod.tempfile.mkdtemp

    def spy(**kw):
        path = real(**kw)
        made.append(path)
        return path

    monkeypatch.setattr(cook_mod.tempfile, "mkdtemp", spy)
    cook(_inst(book, "gifify", test_clip, loop="none"), out_dir=tmp_out, proxy=True, cookbook=book)
    assert len(made) == 1 and "clipwright-" in os.path.basename(made[0])
    assert not os.path.exists(made[0])


def test_custom_stem_is_used_and_checked(book, test_clip, tmp_out):
    inst = _inst(book, "gifify", test_clip, loop="none")
    result = cook(inst, out_dir=tmp_out, stem="mine", proxy=True, cookbook=book)
    assert os.path.basename(result.mp4) == "mine.mp4"
    assert os.path.basename(result.report["sidecar"]) == "mine.recipe.toml"
    with pytest.raises(RecipeError):
        cook(inst, out_dir=tmp_out, stem="../evil", proxy=True, cookbook=book)


# --- sidecar and CLI --------------------------------------------------------

def test_sidecar_round_trips(captioned, full_gifify):
    for inst, result in (captioned, full_gifify):
        loaded = recipe.load_instance(result.report["sidecar"])
        assert loaded == inst
        assert stem_for(loaded) == stem_for(inst)


def test_public_input_keeps_the_upload_path_out_of_the_sidecar_and_cli(book, test_clip, tmp_path):
    """A daemon-style ``<home>/uploads/<telegram id>/<token>.mp4`` never leaves the machine."""
    home = tmp_path / "home" / ".clipwright"
    upload = home / "uploads" / "123456789" / "abc123.mp4"
    upload.parent.mkdir(parents=True)
    upload.write_bytes(open(test_clip, "rb").read())
    inst = _inst(book, "gifify", str(upload), loop="none")
    out = str(home / "renders" / "123456789" / "tok")
    result = cook(inst, out_dir=out, proxy=True, cookbook=book, public_input=upload.name)
    assert inst["input"] == str(upload)                        # the caller's instance is untouched
    loaded = recipe.load_instance(result.report["sidecar"])
    assert loaded["input"] == "abc123.mp4" and os.sep not in loaded["input"]
    assert {k: v for k, v in loaded.items() if k != "input"} == {k: v for k, v in inst.items() if k != "input"}
    assert shlex.split(result.report["cli"])[3] == "abc123.mp4"
    with open(result.report["sidecar"], encoding="utf-8") as fh:
        text = fh.read()
    assert "123456789" not in text and "uploads" not in text and str(tmp_path) not in text
    assert "123456789" not in result.report["cli"] and str(tmp_path) not in result.report["cli"]
    # without the override the real path is written, as the CLI relies on
    plain = cook(inst, out_dir=out, stem="plain", proxy=True, cookbook=book)
    assert recipe.load_instance(plain.report["sidecar"])["input"] == str(upload)


def test_cli_command_reproduces_the_instance(book, test_clip, captioned):
    inst = _inst(book, "gifify", test_clip, fps=20)
    inst["from"], inst["to"], inst["seed"] = "0:00.5", 2.5, 6
    line = cli_command(inst, cookbook=book)
    assert line.startswith("clipwright cook gifify ")
    argv = shlex.split(line)
    assert argv[3] == test_clip
    assert argv[argv.index("--from") + 1] == "0:00.5"
    assert argv[argv.index("--to") + 1] == "0:02.5"
    assert argv[argv.index("--seed") + 1] == "6"
    assert "--set" in argv and argv[argv.index("--set") + 1] == "fps=20"
    assert argv.count("--set") == 1            # only the changed knob

    cap_inst, cap_result = captioned
    cap_line = cap_result.report["cli"]
    assert cap_line == cli_command(cap_inst, cookbook=book)
    cap_argv = shlex.split(cap_line)
    sets = [cap_argv[i + 1] for i, a in enumerate(cap_argv) if a == "--set"]
    assert sets == ["caption.text=hello world", "loop=none"]

    assert cli_command(_inst(book, "gifify", test_clip), cookbook=book) == shlex.join(
        ["clipwright", "cook", "gifify", test_clip])


# --- stem_for ---------------------------------------------------------------

def test_stem_for_slug_hash_and_never_the_input_name(book, tmp_path):
    secret = str(tmp_path / "SECRET_customer_clip.mp4")
    inst = _inst(book, "caption-loop", secret, **{"caption.text": "Hello, World! It's me — again & again"})
    stem = stem_for(inst)
    assert STEM_RE.match(stem)
    # "hello-world-it-s-me-again" is 25 chars; the slug caps at 24 and never ends in "-"
    assert stem.startswith("caption-loop-hello-world-it-s-me-agai-")
    assert "secret" not in stem.lower() and "customer" not in stem
    slug = stem[len("caption-loop-"):-7]
    assert len(slug) == 24 and not slug.endswith("-")
    assert stem_for(dict(inst)) == stem                       # deterministic
    assert stem_for(_inst(book, "caption-loop", secret, **{"caption.text": "Hello, World! It's me — again & again", "caption.size": 72})) != stem

    seeded = _inst(book, "gifify", secret)
    seeded["seed"] = 6
    assert stem_for(seeded).startswith("gifify-seed6-")
    bare = stem_for(_inst(book, "gifify", secret))
    assert re.fullmatch(r"gifify-[0-9a-f]{6}", bare)
    reordered = {"input": secret, **{k: v for k, v in _inst(book, "gifify", secret).items() if k != "input"}}
    assert stem_for(reordered) == bare                        # key order does not matter


# --- common: segment cap and loop plan (no ffmpeg) --------------------------

def test_resolve_segment_defaults_and_caps():
    thirty = _fake_probe(30.0)
    seg = common.resolve_segment({}, thirty)
    assert (seg.from_s, seg.to_s, seg.capped) == (0.0, common.SEGMENT_CAP_S, True)
    seg = common.resolve_segment({"from": "0:02.0"}, thirty)
    assert (seg.from_s, seg.to_s, seg.capped) == (2.0, 17.0, True)
    seg = common.resolve_segment({"from": 1, "to": "0:04.5"}, thirty)
    assert (seg.from_s, seg.to_s, seg.capped) == (1.0, 4.5, False)
    seg = common.resolve_segment({"to": 99}, _fake_probe(3.0))
    assert (seg.from_s, seg.to_s, seg.capped) == (0.0, 3.0, False)   # clamped to the clip
    seg = common.resolve_segment({}, _fake_probe(12.0))
    assert (seg.from_s, seg.to_s, seg.capped) == (0.0, 12.0, False)  # no shorter proxy cap
    with pytest.raises(RecipeError, match="empty"):
        common.resolve_segment({"from": 5}, _fake_probe(3.0))


def test_proxy_and_export_plan_the_same_loop(book, long_clip, tmp_out):
    """What each tap previews is what export produces: same footage, same seam."""
    inst = _inst(book, "gifify", long_clip)
    preview = cook(inst, out_dir=os.path.join(tmp_out, "p"), proxy=True, cookbook=book).report
    export = cook(inst, out_dir=os.path.join(tmp_out, "f"), proxy=False, cookbook=book).report
    assert preview["to"] > 10.0                                # the old proxy cap would have cut here
    for key in ("from", "to", "duration", "loop", "loop_score", "loop_nudge_frames", "capped"):
        assert preview[key] == export[key], key
    assert preview["capped"] is False


def test_candidates_reach_every_axis_and_give_fps_8_fallbacks(book):
    hd = _fake_probe(15.0, width=1920, height=1080)
    cands = common._candidates(_inst(book, "gifify", "x.mp4"), hd)
    assert cands[0] == Candidate(480, 15, 256)                 # the knobs' exact ask
    assert {c.width for c in cands} == {480, 400, 320, 240}
    assert {c.fps for c in cands} == {15, 12, 10, 8}
    assert all(c.width <= 480 and c.fps <= 15 and c.colors <= 256 for c in cands)
    sizes = [budget.relative_size(c) for c in cands[1:]]
    assert sizes == sorted(sizes, reverse=True)                # fallbacks: largest estimate first
    assert cands[-1] == Candidate(240, 8, 64)
    assert len(cands) == len(set(cands))

    slow = common._candidates(_inst(book, "gifify", "x.mp4", fps=8), hd)
    assert slow[0] == Candidate(480, 8, 256) and len(slow) > 1
    assert all(c.fps == 8 for c in slow) and any(c.width == 240 for c in slow)


def test_plan_loop_static_modes():
    seg = common.Segment(1.0, 3.0, False)
    assert common.plan_loop("boomerang", "x", seg, log=[]) == common.LoopPlan("boomerang", True, 1.0, 3.0)
    assert common.plan_loop("none", "x", seg, log=[]) == common.LoopPlan("none", False, 1.0, 3.0)
    assert common.plan_loop("crossfade", "x", seg, log=[]).label == "none (crossfade not implemented)"
    short = common.plan_loop("seamless", "x", common.Segment(0.0, 0.5, False), log=[])
    assert short.reverse is False and short.score is None and short.label.startswith("seamless")
    with pytest.raises(RecipeError, match="loop"):
        common.plan_loop("wobble", "x", seg, log=[])
