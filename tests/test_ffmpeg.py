"""Tests for clipwright.ffmpeg: argv builders (pure) and real encodes on the synthetic clip."""
import os

import pytest
from PIL import Image, ImageSequence, ImageStat

from clipwright import ffmpeg as F

FPS = 15
WIDTH = 240  # 320x240 source scales to 240x180


def frame_count(path):
    return sum(1 for _ in ImageSequence.Iterator(Image.open(path)))


def band_mean(path, box):
    """Mean RGB over `box` of the first frame."""
    return ImageStat.Stat(Image.open(path).convert("RGB").crop(box)).mean


def assert_gif(path):
    assert os.path.isfile(path)
    with open(path, "rb") as fh:
        assert fh.read(6) == b"GIF89a"


@pytest.fixture(scope="module")
def plain_gif(test_clip, tmp_path_factory):
    out = str(tmp_path_factory.mktemp("gif") / "plain.gif")
    F.run(F.gif_argv(test_clip, out, fps=FPS, width=WIDTH, colors=128))
    return out


@pytest.fixture
def band_png(tmp_path):
    """An opaque white RGBA band the width of the scaled output."""
    path = str(tmp_path / "band.png")
    Image.new("RGBA", (WIDTH, 40), (255, 255, 255, 255)).save(path)
    return path


# --- run() -------------------------------------------------------------------

def test_run_returns_bytes_and_logs_argv():
    log = []
    proc = F.run(["ffmpeg", "-version"], log=log)
    assert proc.returncode == 0
    assert isinstance(proc.stdout, bytes) and proc.stdout.startswith(b"ffmpeg version")
    assert log == [["ffmpeg", "-version"]]


def test_run_raises_on_nonzero_exit_with_stderr_tail():
    log = []
    with pytest.raises(F.FFmpegError) as ei:
        F.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "/nonexistent/input.mp4", "-f", "null", "-"], log=log)
    err = ei.value
    assert err.argv[0] == "ffmpeg"
    assert err.returncode not in (None, 0)
    assert "nonexistent" in err.stderr
    assert "ffmpeg exited" in str(err) and "nonexistent" in str(err)
    assert len(log) == 1, "argv is logged even when the command fails"


def test_run_missing_binary_is_ffmpeg_error():
    with pytest.raises(F.FFmpegError, match="executable not found"):
        F.run(["clipwright-no-such-binary-xyz"])


def test_run_timeout_is_ffmpeg_error():
    log = []
    with pytest.raises(F.FFmpegError, match="timed out") as ei:
        F.run(["sleep", "5"], log=log, timeout=0.2)
    assert ei.value.argv == ["sleep", "5"] and ei.value.returncode is None
    assert log == [["sleep", "5"]], "argv is logged even when the command hangs"


def test_run_spawns_the_child_in_its_own_process_group():
    """A terminal's Ctrl-C goes to the foreground process group; the media child must
    not be in it, or the daemon's running render dies with 'exited 255' and the user
    is told their clip failed instead of that the daemon was shut down."""
    import sys
    proc = F.run([sys.executable, "-c", "import os; print(os.getpgrp())"])
    child_pgrp = int(proc.stdout.decode().strip())
    assert child_pgrp != os.getpgrp()


def test_stderr_tail_is_capped():
    long = "\n".join(f"line {i}" for i in range(200))
    err = F.FFmpegError(["ffmpeg"], long, 1)
    assert err.stderr.splitlines() == [f"line {i}" for i in range(160, 200)]


# --- probe() -----------------------------------------------------------------

def test_probe_fixture(test_clip):
    p = F.probe(test_clip)
    assert p.path == test_clip
    assert abs(p.duration - 3.0) < 0.15
    assert (p.width, p.height) == (320, 240)
    assert p.fps == 30.0
    assert 85 <= p.nb_frames <= 92
    assert p.vcodec == "h264"
    assert p.has_audio is True
    assert p.size_bytes == os.path.getsize(test_clip)


def test_probe_nonexistent_is_ffmpeg_error():
    with pytest.raises(F.FFmpegError, match="not a file"):
        F.probe("/nonexistent/clip.mp4")


def test_probe_audio_only_is_ffmpeg_error(tmp_path):
    wav = str(tmp_path / "tone.wav")
    F.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=0.2", wav])
    with pytest.raises(F.FFmpegError, match="no video stream"):
        F.probe(wav)


DASH_MPD = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT3S" minBufferTime="PT1S" profiles="urn:mpeg:dash:profile:isoff-on-demand:2011">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <Representation id="1" bandwidth="100000" codecs="avc1.42c01e" width="320" height="240">
        <BaseURL>{base_url}</BaseURL>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""


def test_probe_refuses_a_dash_manifest_dressed_as_mp4_before_ffprobe_runs(test_clip, tmp_path, monkeypatch):
    """ffprobe sniffs content, not extensions: a 10-line MPD saved as .mp4 probes as the
    clip its <BaseURL> names (fetching a URL while it does), and every later `-i` would
    render that file. A text head is refused before ffprobe is even started."""
    def never(*a, **k):
        raise AssertionError("ffprobe must not run on a text file")

    monkeypatch.setattr(F, "run", never)
    for name, base_url in (("local.mp4", "file://" + test_clip), ("remote.mp4", "http://127.0.0.1:9/clip.mp4")):
        evil = tmp_path / name
        evil.write_text(DASH_MPD.format(base_url=base_url), encoding="utf-8")
        with pytest.raises(F.FFmpegError, match="text file, not a media container"):
            F.probe(str(evil))
    bom = tmp_path / "bom.mp4"
    bom.write_bytes(b"\xef\xbb\xbf" + DASH_MPD.format(base_url="file://" + test_clip).encode("utf-8"))
    with pytest.raises(F.FFmpegError, match="text file"):
        F.probe(str(bom))


def test_probe_container_allowlist_refuses_dash_even_when_the_text_gate_is_off(test_clip, tmp_path, monkeypatch):
    """The second line of defence: whatever ffprobe sniffs, only CONTAINERS get past."""
    monkeypatch.setattr(F, "_looks_like_text", lambda path: False)
    evil = tmp_path / "upload.mp4"
    evil.write_text(DASH_MPD.format(base_url="file://" + test_clip), encoding="utf-8")
    with pytest.raises(F.FFmpegError, match="unsupported container 'dash'"):
        F.probe(str(evil))
    assert F.probe(test_clip).width == 320          # the real clip still passes


def test_looks_like_text(test_clip, tmp_path):
    assert F._looks_like_text(test_clip) is False
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    assert F._looks_like_text(str(empty)) is False   # ffprobe reports that one itself
    playlist = tmp_path / "hls.mp4"
    playlist.write_text("#EXTM3U\n#EXTINF:3,\nhttp://127.0.0.1:9/a.ts\n", encoding="utf-8")
    assert F._looks_like_text(str(playlist)) is True
    cut = tmp_path / "cut.mp4"
    head = ("<?xml?><!--  " + "é" * 40).encode("utf-8")          # 13 + 2n bytes: byte 64 splits an é
    assert head[F.TEXT_HEAD_BYTES - 1] == 0xC3
    cut.write_bytes(head)
    assert F._looks_like_text(str(cut)) is True      # a multi-byte character cut at the head's end
    gif = tmp_path / "x.gif"
    gif.write_bytes(b"GIF89a\x40\x01\xf0\x00\x00\x00" + bytes(64))
    assert F._looks_like_text(str(gif)) is False


def test_probe_accepts_every_listed_container(test_clip, tmp_path):
    made = {"mp4": test_clip}
    for ext in ("webm", "mkv", "gif", "avi"):
        out = str(tmp_path / f"clip.{ext}")
        extra = ["-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8"] if ext == "webm" else []
        F.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", test_clip, "-t", "0.3",
               "-an", *extra, out])
        made[ext] = out
    for ext, path in made.items():
        p = F.probe(path)
        assert p.width == 320 and p.height == 240 and p.rotation == 0, ext


def _rotated(src: str, dest: str, degrees: int) -> str:
    """`src` remuxed with a display matrix of `degrees` (no re-encode)."""
    try:
        F.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-display_rotation", str(degrees), "-i", src, "-c", "copy", "-an", dest])
    except F.FFmpegError:
        pytest.skip("this ffmpeg cannot write a display matrix (-display_rotation)")
    return dest


@pytest.mark.parametrize("degrees, size, rotation", [
    (90, (240, 320), 90), (-90, (240, 320), 270), (180, (320, 240), 180),
])
def test_probe_reports_the_displayed_size_of_a_rotated_clip(test_clip, tmp_path, degrees, size, rotation):
    """A phone's portrait clip is stored 320x240 with a 90 deg matrix and *displayed* 240x320,
    which is what ffmpeg autorotates every render to; the probe (and so the ladder) must
    size from the displayed frame or the export upscales a 240 px source to 320."""
    path = _rotated(test_clip, str(tmp_path / f"rot{degrees}.mp4"), degrees)
    p = F.probe(path)
    assert (p.width, p.height) == size
    assert p.rotation == rotation
    assert p.vcodec == "h264" and abs(p.duration - 3.0) < 0.15


def test_rotation_parser_reads_side_data_then_tags_and_snaps():
    assert F._rotation({}) == 0
    assert F._rotation({"side_data_list": [{"side_data_type": "Display Matrix", "rotation": 90}]}) == 90
    assert F._rotation({"side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}]}) == 270
    assert F._rotation({"side_data_list": [{"side_data_type": "Display Matrix", "rotation": -180}]}) == 180
    assert F._rotation({"side_data_list": [{"side_data_type": "Display Matrix", "rotation": 89.7}]}) == 90
    assert F._rotation({"side_data_list": [{"other": 1}], "tags": {"rotate": "270"}}) == 270
    assert F._rotation({"tags": {"rotate": "sideways"}}) == 0
    assert F._rotation({"side_data_list": [{"rotation": "nan"}]}) == 0


def test_parse_rate():
    assert F._parse_rate("30/1") == 30.0
    assert abs(F._parse_rate("30000/1001") - 29.97) < 0.01
    assert F._parse_rate("0/0") == 0.0
    assert F._parse_rate(None) == 0.0


# --- even() ------------------------------------------------------------------

def test_even():
    assert F.even(320) == 320
    assert F.even(321) == 320
    assert F.even(241.9) == 240
    assert F.even(1) == 2
    assert F.even(0) == 2


# --- argv builders (pure) ----------------------------------------------------

def test_gif_argv_shape():
    argv = F.gif_argv("in.mp4", "out.gif", fps=12, width=321, colors=64)
    assert argv[0] == "ffmpeg"
    assert argv[-1] == "out.gif"
    assert argv.count("-i") == 1
    for flag in ("-y", "-an", "-hide_banner"):
        assert flag in argv
    assert argv[argv.index("-loop") + 1] == "0"
    assert "-ss" not in argv and "-to" not in argv
    graph = argv[argv.index("-filter_complex") + 1]
    assert graph.startswith("[0:v]fps=12,scale=320:-2:flags=lanczos[v]")
    assert "split[p][q]" in graph
    assert "palettegen=max_colors=64:stats_mode=diff[pal]" in graph
    assert "paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle[out]" in graph
    assert "reverse" not in graph and "overlay" not in graph
    assert argv[argv.index("-map") + 1] == "[out]"


def test_gif_argv_trim_options_precede_input():
    argv = F.gif_argv("in.mp4", "out.gif", fps=10, width=200, colors=32, from_s=1.25, to_s=2.5)
    i_in = argv.index("-i")
    assert argv.index("-ss") < i_in and argv.index("-to") < i_in
    assert argv[argv.index("-ss") + 1] == "1.250"
    assert argv[argv.index("-to") + 1] == "2.500"
    with pytest.raises(ValueError):
        F.gif_argv("in.mp4", "out.gif", fps=10, width=200, colors=32, from_s=2.0, to_s=1.0)


def test_gif_argv_overlay_and_boomerang():
    argv = F.gif_argv("in.mp4", "out.gif", fps=10, width=200, colors=32,
                      overlay_png="cap.png", overlay_pos="top", reverse_append=True)
    assert argv.count("-i") == 2
    assert argv[argv.index("cap.png") - 1] == "-i"
    graph = argv[argv.index("-filter_complex") + 1]
    assert "[v][1:v]overlay=0:0[v]" in graph
    assert "[v]split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1:a=0[v]" in graph
    bottom = F.gif_argv("in.mp4", "out.gif", fps=10, width=200, colors=32, overlay_png="cap.png")
    assert "overlay=0:main_h-overlay_h" in bottom[bottom.index("-filter_complex") + 1]


def test_filtergraph_never_contains_the_overlay_path():
    """The caption PNG path is a separate -i input; only numbers and fixed tokens enter the graph."""
    weird = "/tmp/it's;a[weird]:path,name.png"
    argv = F.gif_argv("in.mp4", "out.gif", fps=10, width=200, colors=32, overlay_png=weird)
    graph = argv[argv.index("-filter_complex") + 1]
    assert weird not in graph and "path" not in graph
    assert weird in argv


@pytest.mark.parametrize("bad", [
    dict(colors=1), dict(colors=257), dict(dither="dots"), dict(overlay_pos="middle"), dict(fps=0),
])
def test_gif_argv_rejects_bad_values(bad):
    kw = dict(fps=10, width=200, colors=32)
    kw.update(bad)
    with pytest.raises(ValueError):
        F.gif_argv("in.mp4", "out.gif", **kw)


@pytest.mark.parametrize("times", [
    dict(from_s=float("nan")), dict(to_s=float("inf")), dict(from_s=-1.0),
    dict(from_s=float("nan"), to_s=1.0), dict(from_s=0.0, to_s=float("nan")),
    dict(from_s=float("-inf"), to_s=1.0),
])
def test_argv_builders_reject_non_finite_or_negative_times(times):
    """NaN slips past the to<=from guard, so `-ss nan` must be refused where the text is made."""
    with pytest.raises(ValueError, match="finite"):
        F.gif_argv("in.mp4", "out.gif", fps=10, width=200, colors=32, **times)
    with pytest.raises(ValueError, match="finite"):
        F.mp4_argv("in.mp4", "out.mp4", fps=10, width=200, **times)


def test_gif_argv_non_bayer_dither_has_no_bayer_scale():
    argv = F.gif_argv("in.mp4", "out.gif", fps=10, width=200, colors=32, dither="sierra2_4a")
    graph = argv[argv.index("-filter_complex") + 1]
    assert "paletteuse=dither=sierra2_4a:diff_mode=rectangle" in graph
    assert "bayer_scale" not in graph


def test_mp4_argv_shape():
    argv = F.mp4_argv("in.mp4", "out.mp4", fps=15, width=241, from_s=0.5, to_s=2.0)
    assert argv[0] == "ffmpeg" and argv[-1] == "out.mp4"
    assert argv.index("-ss") < argv.index("-i")
    graph = argv[argv.index("-filter_complex") + 1]
    assert graph == "[0:v]fps=15,scale=240:-2:flags=lanczos[v]"
    for pair in (("-c:v", "libx264"), ("-pix_fmt", "yuv420p"), ("-crf", "28"),
                 ("-preset", "veryfast"), ("-movflags", "+faststart"), ("-map", "[v]")):
        assert argv[argv.index(pair[0]) + 1] == pair[1]
    assert "-an" in argv and "-y" in argv
    assert "palettegen" not in graph and "-loop" not in argv


def test_gifsicle_argv():
    assert F.gifsicle_argv("a.gif", "b.gif") == ["gifsicle", "-O3", "-o", "b.gif", "a.gif"]
    assert F.gifsicle_argv("a.gif", "b.gif", colors=64, lossy=80, optimize=2) == [
        "gifsicle", "-O2", "--colors", "64", "--lossy=80", "-o", "b.gif", "a.gif"]
    assert "-O0" not in F.gifsicle_argv("a.gif", "b.gif", optimize=0)
    with pytest.raises(ValueError):
        F.gifsicle_argv("a.gif", "b.gif", optimize=4)
    with pytest.raises(ValueError):
        F.gifsicle_argv("a.gif", "b.gif", colors=300)


# --- real encodes ------------------------------------------------------------

def test_gif_encode_plain(plain_gif):
    assert_gif(plain_gif)
    assert abs(frame_count(plain_gif) - 3 * FPS) <= 1
    p = F.probe(plain_gif)
    assert p.width == WIDTH and p.height == 180
    assert abs(p.duration - 3.0) < 0.15


def test_gif_encode_trim_is_shorter(test_clip, tmp_out, plain_gif):
    out = os.path.join(tmp_out, "trim.gif")
    F.run(F.gif_argv(test_clip, out, fps=FPS, width=WIDTH, colors=128, from_s=1.0, to_s=2.0))
    assert_gif(out)
    assert abs(frame_count(out) - FPS) <= 1
    assert frame_count(out) < frame_count(plain_gif)
    assert F.probe(out).duration < F.probe(plain_gif).duration


def test_gif_encode_boomerang_doubles_frames(test_clip, tmp_out, plain_gif):
    out = os.path.join(tmp_out, "boom.gif")
    F.run(F.gif_argv(test_clip, out, fps=FPS, width=WIDTH, colors=128, reverse_append=True))
    assert_gif(out)
    plain = frame_count(plain_gif)
    assert frame_count(out) > plain
    assert abs(frame_count(out) - 2 * plain) <= 2


def test_gif_encode_overlay_bottom_and_top(test_clip, tmp_out, band_png, plain_gif):
    bottom = (0, 140, WIDTH, 180)
    top = (0, 0, WIDTH, 40)
    assert all(c < 200 for c in band_mean(plain_gif, bottom))
    assert all(c < 200 for c in band_mean(plain_gif, top))

    out_b = os.path.join(tmp_out, "ov_bottom.gif")
    F.run(F.gif_argv(test_clip, out_b, fps=FPS, width=WIDTH, colors=128, overlay_png=band_png))
    assert_gif(out_b)
    assert frame_count(out_b) == frame_count(plain_gif)
    assert all(c > 245 for c in band_mean(out_b, bottom))
    assert all(c < 200 for c in band_mean(out_b, top))

    out_t = os.path.join(tmp_out, "ov_top.gif")
    F.run(F.gif_argv(test_clip, out_t, fps=FPS, width=WIDTH, colors=128,
                     overlay_png=band_png, overlay_pos="top"))
    assert all(c > 245 for c in band_mean(out_t, top))
    assert all(c < 200 for c in band_mean(out_t, bottom))


def test_gif_encode_nonexistent_input(tmp_out):
    out = os.path.join(tmp_out, "never.gif")
    with pytest.raises(F.FFmpegError) as ei:
        F.run(F.gif_argv("/nonexistent/clip.mp4", out, fps=FPS, width=WIDTH, colors=64))
    assert ei.value.argv[0] == "ffmpeg"
    assert "nonexistent" in ei.value.stderr
    assert not os.path.exists(out)


def test_mp4_encode(test_clip, tmp_out, band_png):
    out = os.path.join(tmp_out, "preview.mp4")
    log = []
    F.run(F.mp4_argv(test_clip, out, fps=FPS, width=241, from_s=0.5, to_s=2.0,
                     overlay_png=band_png, reverse_append=True), log=log)
    assert len(log) == 1 and log[0][0] == "ffmpeg"
    p = F.probe(out)
    assert p.vcodec == "h264"
    assert (p.width, p.height) == (240, 180)
    assert p.fps == FPS
    assert p.has_audio is False
    assert abs(p.duration - 3.0) < 0.2  # 1.5 s trimmed, boomeranged


def test_gifsicle_squeeze(plain_gif, tmp_out):
    out = os.path.join(tmp_out, "squeezed.gif")
    F.run(F.gifsicle_argv(plain_gif, out, colors=32, lossy=80))
    assert_gif(out)
    assert frame_count(out) == frame_count(plain_gif)
    assert os.path.getsize(out) < os.path.getsize(plain_gif)


def test_run_maps_non_executable_binary_to_ffmpeg_error(tmp_path):
    """A binary that exists but cannot be executed is still a typed FFmpegError."""
    import stat
    from clipwright.ffmpeg import FFmpegError, run
    fake = tmp_path / "notexec"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(stat.S_IRUSR)  # readable, not executable
    with pytest.raises(FFmpegError) as ei:
        run([str(fake), "-version"])
    assert ei.value.argv[0] == str(fake)
    assert "cannot execute" in str(ei.value) or "executable" in str(ei.value)
