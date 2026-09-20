"""Render-smoke tests for the three episode kits: the smallest episode each
kit will accept, rendered to tmp_path through the real gifsicle squeeze, then
reopened to check dimensions and frame count. meme_kit's network fetch is
stubbed with a generated image so this stays offline. Also pins the tabloid
overflow warnings: silent for a fitting page, loud (and specific) otherwise."""
import io
import os
import sys
import warnings

import pytest
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import meme_kit  # noqa: E402
from ballad_kit import Ballad  # noqa: E402
from meme_kit import MemeReel  # noqa: E402
from tabloid_kit import Tabloid  # noqa: E402


def _gif(path):
    im = Image.open(path)
    return im.size, im.n_frames


def _emblem(d, cx, cy, R, ang):
    d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=(255, 209, 102), width=4)


def _tabloid(**over):
    kw = dict(
        masthead="THE TEST TIMES",
        dateline="SOMEWHERE  ·  BYLINE: PYTEST  ·  50¢",
        kicker="T E S T   E X C L U S I V E",
        headline_lines=["FIRST LINE", "SECOND LINE"],
        deck="A short deck line under the headline.",
        col1="Column one has a few words of body copy so the page looks lived in.",
        col2="Column two has a few more words of body copy for the same reason.",
        starburst_lines=[("42", 24), ("PCT", 12)],
        starburst_pos=(514, 448),
        strips=[("ONE", "first strip"), ("TWO", "second strip"), ("THREE", "third strip")],
        also_inside="ALSO INSIDE:  one  ·  two  ·  three",
        stop_press_lines=("STOP PRESS", "TESTED"),
    )
    kw.update(over)
    return Tabloid(**kw)


def test_tabloid_minimal_renders_600x780(tmp_path):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        final = _tabloid().save(str(tmp_path / "raw.gif"), str(tmp_path / "final.gif"))
    assert not [w for w in caught if "tabloid" in str(w.message)]
    size, n = _gif(final)
    assert size == (600, 780)
    assert n > 20


def test_tabloid_warns_naming_slot_and_dropped_text():
    t = _tabloid(
        strips=[("LONG", "alpha " * 30 + "OMEGA")],
        col2="beta " * 200 + "FINAL",
        also_inside="gamma " * 60 + "LAST",
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        t._page(t._page_end())
    msgs = [str(w.message) for w in caught if issubclass(w.category, UserWarning)]
    assert any("strip 'LONG'" in m and "OMEGA" in m for m in msgs)
    assert any(m.startswith("tabloid col2 ") and "FINAL" in m for m in msgs)
    assert any(m.startswith("tabloid also_inside ") and "LAST" in m for m in msgs)


def test_tabloid_col1_warns_past_the_nine_rows_above_the_first_strip():
    """col1's stagger cap is 12 rows, but the first quote strip is painted over
    the column 148px below its top, so rows 10-12 are never visible when there
    are strips: that must warn. Without strips the 12-row cap stands."""
    from tabloid_kit import _wrap, fa
    col1 = "beta " * 70 + "FINAL"
    assert len(_wrap(col1, fa(11, False), 196)) == 11
    for deck in ("A short deck line under the headline.",
                 "A much longer deck that wraps onto a second line under the headline, so the whole body moves down."):
        t = _tabloid(col1=col1, deck=deck)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            t._page(t._page_end())
        msgs = [str(w.message) for w in caught if issubclass(w.category, UserWarning)]
        assert any(m.startswith("tabloid col1 wraps to 11 lines but only 9 fit") and "FINAL" in m for m in msgs), deck
    t = _tabloid(col1=col1, strips=[])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        t._page(t._page_end())
    assert not [w for w in caught if "tabloid col1" in str(w.message)]


def test_tabloid_empty_starburst_draws_nothing_and_does_not_raise():
    from tabloid_kit import _starburst
    img = Image.new("RGB", (200, 200), (250, 250, 250))
    before = img.tobytes()
    _starburst(img, [], 100, 100, 56, 1.0)
    assert img.tobytes() == before
    t = _tabloid(starburst_lines=[])
    t._page(t._page_end())  # a whole page with no starburst still renders


def test_tabloid_warns_when_the_strip_stack_runs_into_the_footer():
    five = [("S%d" % i, "strip %d" % i) for i in range(5)]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _tabloid(strips=five)._page(0)
    msgs = [str(w.message) for w in caught if issubclass(w.category, UserWarning)]
    assert any(m.startswith("tabloid strips: 5 strips need ") and "fit above the footer" in m for m in msgs)


def test_tabloid_overflow_warning_prints_once_per_render():
    """Under Python's default filter a warning shows once per (message, location);
    it must be charged to one line inside _page, not to each of render()'s
    four _page call sites."""
    t = _tabloid(strips=[("LONG", "alpha " * 30 + "OMEGA")])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("default")
        t.render()
    assert len([w for w in caught if "strip 'LONG'" in str(w.message)]) == 1


def test_tabloid_page_is_fully_assembled_at_page_end():
    """Nothing moves after _page_end() (the frame loop's stop), and something
    was still to appear shortly before it — so the two timelines agree."""
    t = _tabloid(headline_lines=["ONE", "TWO", "THREE", "FOUR"], strips=[("A", "a"), ("B", "b")])
    end = t._page_end()
    assert t._page(end).tobytes() == t._page(end + 10).tobytes()
    assert t._page(end - .7).tobytes() != t._page(end).tobytes()


def test_ballad_minimal_renders_640x640(tmp_path):
    b = Ballad()
    b.title("kicker", "HEADLINE", ["tag one", "tag two"], "footer", _emblem)
    b.chat("header", "sub", [("Who", "a short message", False)])
    b.document("BRAND", "SUB", "lede text", [("ROW", "STAMP")], "quote", "verdict")
    b.dossier("DOSSIER", "sub", [("KEY", "value", True)])
    b.counter("LABEL", "1", "footer", _emblem)
    final = b.save(str(tmp_path / "raw.gif"), str(tmp_path / "final.gif"))
    size, n = _gif(final)
    assert size == (640, 640)
    assert n > 20


def _stub_meme():
    """A generated 400x300 RGB stand-in for a memegen render — a two-axis
    gradient rather than a flat colour, so a scaled copy never happens to be
    pixel-identical to its neighbour (Pillow merges identical frames on save)."""
    v = Image.linear_gradient("L").resize((400, 300))
    h = v.transpose(Image.Transpose.ROTATE_90).resize((400, 300))
    return Image.merge("RGB", (h, v, Image.new("L", (400, 300), 120)))


def test_meme_reel_renders_640x640_with_stubbed_fetch(tmp_path, monkeypatch):
    calls = []

    def fake_fetch(template, top, bottom=None):
        calls.append((template, top, bottom))
        return _stub_meme()

    monkeypatch.setattr(meme_kit, "fetch", fake_fetch)
    r = MemeReel("KICKER", footer="footer")
    r.card("drake", "top text", "bottom text", cap="a caption under the card")
    r.stinger("LINE ONE", "LINE TWO")
    assert len(r.frames) == 10 + 1 + 12 + 1
    # Each zoom is eased exactly once, so the card (and the stinger's headline)
    # is still visibly moving through the second half of its intro — double-
    # easing used to freeze the card from frame 5 and the headline from frame 5.
    # Only the last pair or two may coincide, where the ease-out has settled to
    # a sub-pixel size (card) or the same integer font size (stinger).
    intro = [f.tobytes() for f in r.frames[:10]]
    assert all(a != b for a, b in zip(intro[:8], intro[1:9]))
    zoom = [f.tobytes() for f in r.frames[11:23]]
    assert all(a != b for a, b in zip(zoom[:9], zoom[1:10]))
    logical = [f.tobytes() for f in r.frames]
    final = r.save(str(tmp_path / "raw.gif"), str(tmp_path / "final.gif"))
    assert calls == [("drake", "top text", "bottom text")]
    size, n = _gif(final)
    assert size == (640, 640)
    # Pillow merges only pixel-identical consecutive frames on save, so the
    # physical count is the distinct-run count; at defaults that is 20 (it was
    # 13 when both zooms were eased twice and their second halves collapsed).
    assert n == 1 + sum(a != b for a, b in zip(logical, logical[1:]))
    assert n >= 20


def test_meme_encode_order():
    """memegen's path escaping is order-sensitive: literal _ and - are doubled
    before spaces become _, then the punctuation shorthands, then percent-
    encoding for anything outside ASCII."""
    assert meme_kit._encode("a_b-c d? 100%") == "a__b--c_d~q_100~p"
    assert meme_kit._encode("#tag") == "~htag"
    assert meme_kit._encode("a/b") == "a~sb"
    assert meme_kit._encode("one\ntwo") == "one~ntwo"
    assert meme_kit._encode('say "hi"') == "say_''hi''"
    assert meme_kit._encode("it\u2019s") == "it%E2%80%99s"


def test_meme_fetch_builds_the_memegen_url(monkeypatch):
    seen = []

    class Resp:
        def __init__(self, data):
            self.data = data

        def read(self):
            return self.data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        buf = io.BytesIO()
        Image.new("RGB", (4, 4), (1, 2, 3)).save(buf, "PNG")
        return Resp(buf.getvalue())

    monkeypatch.setattr(meme_kit.urllib.request, "urlopen", fake_urlopen)
    img = meme_kit.fetch("drake", "top line?", "bottom_line")
    assert img.size == (4, 4)
    assert seen == ["https://api.memegen.link/images/drake/top_line~q/bottom__line.png"]
