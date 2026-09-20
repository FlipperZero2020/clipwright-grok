import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import ImageDraw, Image

from style_common import ease, load_font, wrap, center_text, scratch_dir, GifRenderer


def test_ease_endpoints():
    assert ease(0) == 0
    assert ease(1) == 1


def test_ease_monotonic():
    xs = [i / 20 for i in range(21)]
    ys = [ease(x) for x in xs]
    assert all(b >= a for a, b in zip(ys, ys[1:]))


def test_wrap_splits_long_text_into_multiple_lines():
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    f = load_font(20)
    lines = wrap(probe, "this is a fairly long sentence that should wrap", f, maxw=80)
    assert len(lines) > 1
    assert " ".join(lines) == "this is a fairly long sentence that should wrap"


def test_wrap_keeps_short_text_on_one_line():
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    f = load_font(20)
    lines = wrap(probe, "hi", f, maxw=200)
    assert lines == ["hi"]


def test_wrap_overwide_first_word_has_no_blank_line():
    """A word wider than maxw gets its own line; it must never be preceded by an
    empty line (which would eat one of tabloid's two strip lines and open a gap
    at the top of a ballad bubble)."""
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    assert wrap(probe, "averyveryverylongword tail", load_font(20), maxw=20) == ["averyveryverylongword", "tail"]
    lines = wrap(probe, "https://api.memegen.link/templates/ is long", load_font(11), maxw=60)
    assert lines[0] == "https://api.memegen.link/templates/"
    assert "" not in lines
    # an overwide word in the middle flushes the line before it, still without blanks
    lines = wrap(probe, "a b averyveryverylongword c", load_font(20), maxw=40)
    assert lines == ["a b", "averyveryverylongword", "c"]


def test_load_font_families():
    for family in ("sans", "serif", "mono"):
        f = load_font(16, family=family)
        assert f.size == 16


def test_center_text_centers_around_cx():
    img = Image.new("RGB", (200, 30))
    d = ImageDraw.Draw(img)
    center_text(d, "HH", 0, load_font(14), (255, 255, 255), cx=100)
    x0, _, x1, _ = img.getbbox()  # the ink actually drawn
    assert abs((x0 + x1) / 2 - 100) <= 1.5
    assert x1 - x0 > 5  # and something was drawn at all


def test_scratch_dir_is_private_and_stable_within_a_run():
    d = scratch_dir()
    assert d.endswith("/")
    assert os.path.isdir(d)
    path = d.rstrip("/")
    assert os.path.basename(path).startswith("clipwright_render_")
    assert os.path.dirname(path) == tempfile.gettempdir()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700  # owner-only, never a shared /tmp name
    assert scratch_dir() == d  # one dir per run, so S + "x_raw.gif" call sites agree
    probe_file = os.path.join(d, ".write_probe")
    with open(probe_file, "w") as fh:
        fh.write("ok")
    os.remove(probe_file)


def test_gif_renderer_emit_and_save_drops_the_raw_intermediate(tmp_path):
    class Tiny(GifRenderer):
        pass

    r = Tiny()
    for _ in range(2):
        img = Image.new("RGB", (10, 10), (255, 0, 0))
        r._emit(img, 100)
    raw = str(tmp_path / "raw.gif")
    final = str(tmp_path / "final.gif")
    assert r.save(raw, final) == final
    assert os.path.exists(final)
    assert not os.path.exists(raw)
