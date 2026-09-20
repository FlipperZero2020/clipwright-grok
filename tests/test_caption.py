import os

import pytest
from PIL import Image, ImageDraw, ImageFont

from clipwright import caption

FILL = "#ffdd00"
FILL_RGBA = (255, 221, 0, 255)


def alpha_bbox(band):
    return band.getchannel("A").getbbox()


def pixel_set(im):
    raw = im.tobytes()
    return {tuple(raw[i:i + 4]) for i in range(0, len(raw), 4)}


def test_band_is_rgba_of_requested_width():
    band = caption.render_caption("hello world", 320, color=FILL)
    assert band.mode == "RGBA"
    assert band.width == 320
    assert band.height > 1


@pytest.mark.parametrize("text", ["", "   ", "\n\t", "\x00\x07\x1f"])
def test_empty_text_yields_1px_transparent_band(text):
    band = caption.render_caption(text, 320)
    assert band.mode == "RGBA"
    assert band.size == (320, 1)
    assert band.getpixel((0, 0)) == (0, 0, 0, 0)


def test_long_text_wraps_and_height_grows():
    one_line = caption.render_caption("hi", 320)
    wrapped = caption.render_caption(
        "the quick brown fox jumps over the lazy dog again and again", 320)
    assert wrapped.height > one_line.height
    # band height = lines*line_h + 2*pad, and no word here forces a shrink
    line_h = one_line.height - 2 * 12
    assert (wrapped.height - 2 * 12) % line_h == 0
    assert (wrapped.height - 2 * 12) // line_h >= 2


def test_pad_adds_to_band_height_only():
    narrow = caption.render_caption("hi", 320, pad=12)
    wide = caption.render_caption("hi", 320, pad=20)
    assert wide.height == narrow.height + 16
    assert wide.width == narrow.width == 320


def test_fill_and_stroke_pixels_present_on_transparent_ground():
    band = caption.render_caption("STROKE", 320, color=FILL)
    pixels = pixel_set(band)
    assert FILL_RGBA in pixels
    assert (0, 0, 0, 255) in pixels
    w, h = band.size
    for corner in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        assert band.getpixel(corner)[3] == 0


def test_write_caption_overlay_roundtrips_as_rgba_png(tmp_out):
    path = os.path.join(tmp_out, "cap.png")
    assert caption.write_caption_overlay("hello", 240, path, color=FILL) == path
    with Image.open(path) as im:
        assert im.format == "PNG"
        assert im.mode == "RGBA"
        assert im.width == 240
        assert FILL_RGBA in pixel_set(im)


@pytest.mark.parametrize("bad", ["ffdd00", "#fff", "#ggdd00", "yellow", "#ffdd00ff", "", None])
def test_bad_colour_raises_value_error(bad):
    with pytest.raises(ValueError):
        caption.render_caption("x", 320, color=bad)


def test_unknown_style_raises_value_error():
    with pytest.raises(ValueError):
        caption.render_caption("x", 320, style="comic")


@pytest.mark.parametrize("kw", [{"width": 0}, {"size": 0}, {"pad": -1}])
def test_bad_geometry_raises_value_error(kw):
    args = {"width": 320, "size": 64, "pad": 12, **kw}
    with pytest.raises(ValueError):
        caption.render_caption("x", args["width"], size=args["size"], pad=args["pad"])


def test_impact_outline_uppercases_and_plain_keeps_case():
    assert (caption.render_caption("abc", 320).tobytes()
            == caption.render_caption("ABC", 320).tobytes())
    assert (caption.render_caption("abc", 320, style="plain").tobytes()
            != caption.render_caption("ABC", 320, style="plain").tobytes())


def test_long_word_shrinks_font_and_never_touches_edges():
    reference = caption.render_caption("x", 600, size=96)
    band = caption.render_caption("supercalifragilisticexpialidocious", 600, size=96)
    assert band.width == 600
    assert band.height < reference.height          # a smaller size was chosen
    x0, _, x1, _ = alpha_bbox(band)
    assert x0 > 0 and x1 < band.width


def test_word_too_wide_at_floor_size_is_broken_by_character():
    band = caption.render_caption("supercalifragilisticexpialidocious", 120, size=64)
    assert band.width == 120
    x0, _, x1, _ = alpha_bbox(band)
    assert x0 > 0 and x1 < band.width
    floor_line = caption.render_caption("x", 120, size=caption.MIN_SIZE)
    assert (band.height - 24) // (floor_line.height - 24) >= 2


def test_tiny_width_still_terminates():
    band = caption.render_caption("hello", 10)
    assert band.width == 10
    assert band.height > 1


def test_sanitize_text():
    assert caption.sanitize_text("  hello \n\t world \x00\x07!  ") == "hello world !"
    assert caption.sanitize_text("tab\tsep\r\nlines") == "tab sep lines"
    assert len(caption.sanitize_text("a" * 500)) == caption.MAX_CHARS


def test_explicit_font_path_is_used_and_missing_one_raises(tmp_path):
    band = caption.render_caption("x", 200, font_path=caption.pick_font())
    assert band.mode == "RGBA"
    with pytest.raises(FileNotFoundError):
        caption.render_caption("x", 200, font_path=str(tmp_path / "nope.ttf"))


def test_pick_font_returns_existing_files():
    for bold in (True, False):
        path = caption.pick_font(bold=bold)
        assert os.path.isfile(path)
        assert path.endswith(".ttf")


def test_pick_font_search_order_and_error(tmp_path, monkeypatch):
    monkeypatch.setattr(caption, "FONT_ROOT", str(tmp_path))
    with pytest.raises(RuntimeError) as err:
        caption.pick_font()
    message = str(err.value)
    assert "Impact.ttf" in message
    assert "LiberationSans-Bold.ttf" in message
    assert "DejaVuSans-Bold.ttf" in message

    dejavu = tmp_path / "truetype" / "dejavu" / "DejaVuSans-Bold.ttf"
    dejavu.parent.mkdir(parents=True)
    dejavu.write_bytes(b"")
    assert caption.pick_font() == str(dejavu)

    anton = tmp_path / "opentype" / "anton" / "Anton-Regular.ttf"
    anton.parent.mkdir(parents=True)
    anton.write_bytes(b"")
    assert caption.pick_font() == str(anton)      # glob hit outranks the fallbacks

    impact = tmp_path / "truetype" / "msttcorefonts" / "Impact.ttf"
    impact.parent.mkdir(parents=True)
    impact.write_bytes(b"")
    assert caption.pick_font() == str(impact)

    with pytest.raises(RuntimeError):
        caption.pick_font(bold=False)             # regular weights are searched separately


# --- max_height ---------------------------------------------------------------

SIXTY = "the quarterly roadmap is a shared hallucination we all agree"


def _measure_at(size):
    font = ImageFont.truetype(caption.pick_font(), size)
    return caption._measurer(ImageDraw.Draw(Image.new("RGBA", (1, 1))), font)


def test_max_height_bounds_the_band_by_shrinking_first():
    assert len(SIXTY) == 60                        # the caption knob's limit
    unbounded = caption.render_caption(SIXTY, 480, size=64)
    assert unbounded.height > 135                  # taller than half a 480x270 frame: the clipped case
    bounded = caption.render_caption(SIXTY, 480, size=64, max_height=135)
    assert bounded.width == 480 and bounded.height <= 135
    # exactly the unbounded render at the largest ladder size whose lines fit: no text lost
    fitting = next(s for s in caption._size_ladder(64)
                   if caption.render_caption(SIXTY, 480, size=s).height <= 135)
    assert fitting > caption.MIN_SIZE
    assert bounded.tobytes() == caption.render_caption(SIXTY, 480, size=fitting).tobytes()


def test_max_height_cuts_lines_with_an_ellipsis_at_the_floor_size():
    one_line = caption.render_caption("x", 480, size=caption.MIN_SIZE).height
    band = caption.render_caption(SIXTY, 480, size=64, max_height=one_line)
    assert band.height == one_line
    measure = _measure_at(caption.MIN_SIZE)
    lines, _ = caption._wrap(SIXTY.upper().split(), measure, 480 - 24)
    assert len(lines) >= 2                         # so something had to be cut
    first = caption._ellipsize(lines[0], measure, 480 - 24)
    assert first.endswith(caption.ELLIPSIS) and measure(first) <= 480 - 24
    assert band.tobytes() == caption.render_caption(first, 480, size=caption.MIN_SIZE).tobytes()
    x0, _, x1, _ = alpha_bbox(band)
    assert x0 > 0 and x1 < band.width


def test_ellipsize_only_trims_what_does_not_fit():
    measure = _measure_at(32)
    line = "HELLO WORLD"
    roomy = caption._ellipsize(line, measure, 10_000)
    assert roomy == line + caption.ELLIPSIS
    limit = int(measure("HELLO W"))
    tight = caption._ellipsize(line, measure, limit)
    assert tight.endswith(caption.ELLIPSIS) and len(tight) < len(roomy)
    assert measure(tight) <= limit
    assert line.startswith(tight[:-1].rstrip())


def test_max_height_none_or_generous_changes_nothing():
    plain = caption.render_caption("hello world", 320)
    assert caption.render_caption("hello world", 320, max_height=None).tobytes() == plain.tobytes()
    assert caption.render_caption("hello world", 320, max_height=10_000).tobytes() == plain.tobytes()


def test_max_height_keeps_one_line_even_when_the_bound_is_tiny():
    floor = caption.render_caption("hello", 320, size=caption.MIN_SIZE)
    band = caption.render_caption("hello", 320, size=64, max_height=1)
    assert band.height == floor.height             # never an empty band, never below MIN_SIZE
    assert band.tobytes() == floor.tobytes()


def test_bad_max_height_raises_value_error():
    with pytest.raises(ValueError):
        caption.render_caption("x", 320, max_height=0)


def test_write_caption_overlay_passes_max_height_through(tmp_out):
    path = os.path.join(tmp_out, "cap.png")
    caption.write_caption_overlay(SIXTY, 480, path, size=64, max_height=135)
    with Image.open(path) as im:
        assert im.width == 480 and 1 < im.height <= 135


# --- emoji ----------------------------------------------------------------------

def test_sanitize_text_drops_emoji_and_keeps_latin1_symbols():
    assert caption.sanitize_text("émoji 🙂 ok") == "émoji ok"
    assert caption.sanitize_text("☕ coffee ✅ done 👍🏽 team 👨\u200d👩\u200d👧 go 🇺🇸") == "coffee done team go"
    assert caption.sanitize_text("1\ufe0f\u20e3 first, 90° © ok") == "1 first, 90° © ok"
    assert caption.sanitize_text("🙂🙂🙂") == ""


def test_emoji_never_reach_the_band():
    assert (caption.render_caption("hi 🙂", 320).tobytes()
            == caption.render_caption("hi", 320).tobytes())
    assert caption.render_caption("🙂", 320).size == (320, 1)
