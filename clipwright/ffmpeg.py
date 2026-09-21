"""Thin, safe wrappers around ffmpeg / ffprobe / gifsicle.

Role in the engine: this is the only module that talks to the media
binaries. Everything else builds an argv list here and hands it to `run`.

Rules enforced here:

- Every call is an argv list run with `subprocess.run`; `shell=True` never
  appears. `run` captures stderr and turns a non-zero exit into `FFmpegError`
  carrying the argv and the tail of stderr, so a failed render is debuggable
  and reproducible from the exception alone.
- `run` returns stdout as *bytes* (not text): callers such as
  `loopfind.sample_frames` pipe rawvideo through it, and `probe` decodes the
  JSON itself.
- Filtergraph strings are assembled ONLY from numbers and fixed tokens. No
  caller-supplied text ever enters a filter: captions arrive as a PNG path
  (a separate `-i` input) and are composited with `overlay`.
- `probe` validates the input path with `os.path.isfile` before use and is
  the gate every pipeline goes through; the `*_argv` builders are pure. It
  also refuses any container outside CONTAINERS: ffprobe sniffs content, not
  extensions, and a DASH manifest saved as `.mp4` would otherwise make every
  later `-i` render whatever local file or URL the manifest names.
- Media binaries run in their own process group, so a Ctrl-C at the terminal
  interrupts the daemon (which lets the running render finish) and not the
  ffmpeg it is waiting on.

GIF encoding uses the palettegen/paletteuse "two-pass" technique in ONE
process via `split` — no palette temp file. `-loop 0` always.
"""
from __future__ import annotations

import codecs
import json
import math
import os
import subprocess
from dataclasses import dataclass
from fractions import Fraction

STDERR_TAIL_LINES = 40

DITHERS = frozenset({"bayer", "floyd_steinberg", "sierra2", "sierra2_4a", "none"})
OVERLAY_POSITIONS = {"bottom": "overlay=0:main_h-overlay_h", "top": "overlay=0:0"}

# ffprobe `format_name` values `probe` accepts: self-contained demuxers only.
# Playlist-style formats (dash, hls, concat, ...) fetch what they reference.
VIDEO_CONTAINERS = frozenset({
    "mov,mp4,m4a,3gp,3g2,mj2",   # .mp4 / .mov / .m4v (what phones and Telegram send)
    "matroska,webm",             # .webm / .mkv
    "gif",
    "avi",
    "mpegts",
    "mpeg",
})
# Still-image demuxers. A JPEG/PNG/WebP is a one-frame "video" as far as
# ffmpeg is concerned; Ken Burns / still-hold turn it into a clip.
IMAGE_CONTAINERS = frozenset({
    "jpeg_pipe", "png_pipe", "webp_pipe", "bmp_pipe", "ppm_pipe", "tiff_pipe",
    "image2", "mjpeg",
})
CONTAINERS = VIDEO_CONTAINERS | IMAGE_CONTAINERS

# Every accepted container opens with binary (an atom size, an EBML id, a
# GIF screen descriptor, a RIFF size, a sync byte); every referencing format
# ffmpeg knows (DASH XML, HLS/ffconcat playlists, SDP) opens with text. A
# text head is refused BEFORE ffprobe runs, because ffprobe's sniff already
# follows a manifest's URL — the allowlist above alone would leave one GET.
TEXT_HEAD_BYTES = 64
_TEXT_CONTROLS = frozenset("\t\n\r")


class FFmpegError(RuntimeError):
    """A media binary failed. Carries the argv and the tail of its stderr."""

    def __init__(self, argv: list[str], stderr: str, returncode: int | None = None):
        self.argv = list(argv)
        self.stderr = _tail(stderr)
        self.returncode = returncode
        tool = self.argv[0] if self.argv else "?"
        head = f"{tool} failed" if returncode is None else f"{tool} exited {returncode}"
        super().__init__(f"{head}: {' '.join(self.argv)}\n{self.stderr}".rstrip())


def _tail(text: str, lines: int = STDERR_TAIL_LINES) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def run(argv: list[str], *, log: list | None = None, timeout: float = 600) -> subprocess.CompletedProcess:
    """Run one media command. Appends argv to `log` (if given) before running.

    stdout and stderr are captured as bytes; a non-zero exit, a missing
    binary, or a timeout all raise FFmpegError. The child gets its own
    process group (``process_group=0``): a terminal's Ctrl-C then reaches
    only this process, which still kills the child itself on a timeout or
    when the interrupt lands in the thread waiting on it.
    """
    argv = [str(a) for a in argv]
    if log is not None:
        log.append(list(argv))
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            process_group=0,
        )
    except FileNotFoundError as e:
        raise FFmpegError(argv, f"{argv[0]}: executable not found") from e
    except OSError as e:  # PermissionError etc.: still a typed error carrying argv
        raise FFmpegError(argv, f"{argv[0]}: cannot execute ({e.strerror or e})") from e
    except subprocess.TimeoutExpired as e:
        partial = (e.stderr or b"").decode("utf-8", "replace")
        raise FFmpegError(argv, f"timed out after {timeout}s\n{partial}") from e
    if proc.returncode != 0:
        raise FFmpegError(argv, proc.stderr.decode("utf-8", "replace"), proc.returncode)
    return proc


@dataclass
class Probe:
    """What ffprobe saw. ``width``/``height`` are the *displayed* size: when
    the stream carries a display rotation (a phone's portrait recording is
    stored landscape with a 90° matrix) they are swapped to match the frames
    ffmpeg autorotates on every render; ``rotation`` (0/90/180/270) says so."""

    path: str
    duration: float
    width: int
    height: int
    fps: float
    nb_frames: int
    vcodec: str
    has_audio: bool
    size_bytes: int
    rotation: int = 0
    still: bool = False          # True for a one-frame image (jpeg/png/webp, …)


def _parse_rate(text: str | None) -> float:
    """'30000/1001' -> 29.97; '0/0', '', None -> 0.0."""
    if not text:
        return 0.0
    try:
        return float(Fraction(text))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _parse_float(text, default: float = 0.0) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _rotation(video: dict) -> int:
    """The stream's display rotation snapped to 0/90/180/270.

    Read from the Display Matrix side data (``rotation``, which ffprobe
    reports as e.g. ``-90`` for a 270° matrix) or, from older muxers, the
    ``rotate`` tag; anything unreadable counts as 0.
    """
    raw = None
    for side in video.get("side_data_list") or []:
        if isinstance(side, dict) and side.get("rotation") is not None:
            raw = side["rotation"]
            break
    if raw is None:
        raw = (video.get("tags") or {}).get("rotate")
    degrees = _parse_float(raw)
    if not math.isfinite(degrees):
        return 0
    return int(round(degrees / 90)) * 90 % 360


def _looks_like_text(path: str) -> bool:
    """True when the file's first TEXT_HEAD_BYTES are text (UTF-8, no control
    characters beyond tab/newline), which no accepted container starts with."""
    with open(path, "rb") as fh:
        head = fh.read(TEXT_HEAD_BYTES)
    if not head:
        return False
    try:  # an incremental decoder tolerates a multi-byte character cut at the end
        text = codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
    except UnicodeDecodeError:
        return False
    return all(ch >= " " or ch in _TEXT_CONTROLS for ch in text)


def is_still(probe: Probe) -> bool:
    """True when ``probe`` describes a still image, not a clip with duration."""
    return bool(probe.still) or (probe.nb_frames <= 1 and probe.duration <= 0)


def probe(path: str) -> Probe:
    """ffprobe the file. Raises FFmpegError if it is missing, is a text file
    (a manifest or playlist), has no video stream, or is not one of the
    CONTAINERS this engine renders from. Still images (jpeg/png/webp) are
    accepted: ``still`` is True and ``duration`` is 0."""
    argv = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path]
    if not os.path.isfile(path):
        raise FFmpegError(argv, f"input is not a file: {path}")
    if _looks_like_text(path):
        raise FFmpegError(argv, f"input is a text file, not a media container: {path}")
    proc = run(argv)
    try:
        info = json.loads(proc.stdout)
    except ValueError as e:
        raise FFmpegError(argv, f"ffprobe produced no JSON: {e}") from e

    streams = info.get("streams") or []
    video = None
    for s in streams:
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic"):
            video = s
            break
    if video is None:
        raise FFmpegError(argv, f"no video stream in {path}")

    fmt = info.get("format") or {}
    container = str(fmt.get("format_name") or "")
    if container not in CONTAINERS:
        raise FFmpegError(argv, f"unsupported container {container!r} in {path} "
                                f"(want one of {sorted(CONTAINERS)})")
    fps = _parse_rate(video.get("r_frame_rate")) or _parse_rate(video.get("avg_frame_rate"))
    duration = _parse_float(fmt.get("duration")) or _parse_float(video.get("duration"))
    try:
        nb_frames = int(video.get("nb_frames"))
    except (TypeError, ValueError):
        nb_frames = int(round(duration * fps))
    image = container in IMAGE_CONTAINERS
    still = image or (nb_frames <= 1 and duration <= 0)
    if still:
        if duration <= 0:
            duration = 0.0
        if nb_frames <= 0:
            nb_frames = 1
        if fps <= 0:
            fps = 25.0
    size_bytes = int(_parse_float(fmt.get("size"), 0)) or os.path.getsize(path)
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    rotation = _rotation(video)
    if rotation in (90, 270):
        width, height = height, width
    return Probe(
        path=path,
        duration=duration,
        width=width,
        height=height,
        fps=fps,
        nb_frames=nb_frames,
        vcodec=str(video.get("codec_name") or ""),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        size_bytes=size_bytes,
        rotation=rotation,
        still=still,
    )


def even(n) -> int:
    """Largest even integer <= n, never below 2 (H.264 and scale=W:-2 need even sizes)."""
    return max(2, int(n) // 2 * 2)


def _num(x) -> str:
    """Render a number for a filter string: 15 -> '15', 12.5 -> '12.5', 2.0 -> '2'.

    Shared with `loopfind.sample_frames`, the one other place a number enters
    a filter string, so there is a single rule for what that text looks like.
    """
    f = float(x)
    if not math.isfinite(f):
        raise ValueError(f"not a finite number: {x!r}")
    if f == int(f):
        return str(int(f))
    return f"{f:.3f}".rstrip("0").rstrip(".")


def _seconds(x) -> str:
    """`-ss`/`-to` text: a finite, non-negative time with three decimals."""
    f = float(x)
    if not math.isfinite(f) or f < 0:
        raise ValueError(f"time must be finite and non-negative: {x!r}")
    return f"{f:.3f}"


def _trim_options(from_s, to_s) -> list[str]:
    """`-ss`/`-to` as INPUT options (placed before `-i`), or [] when untrimmed."""
    if from_s is not None and to_s is not None and float(to_s) <= float(from_s):
        raise ValueError(f"to_s ({to_s}) must be greater than from_s ({from_s})")
    opts: list[str] = []
    if from_s is not None:
        opts += ["-ss", _seconds(from_s)]
    if to_s is not None:
        opts += ["-to", _seconds(to_s)]
    return opts


def _frame_chain(*, fps, width, overlay_png, overlay_pos, reverse_append) -> tuple[list[str], str]:
    """The shared front of a filtergraph: fps, scale, optional overlay, optional boomerang.

    Returns (extra input args, graph text ending in the pad `[v]`).
    """
    if overlay_pos not in OVERLAY_POSITIONS:
        raise ValueError(f"overlay_pos must be one of {sorted(OVERLAY_POSITIONS)}, not {overlay_pos!r}")
    if float(fps) <= 0:
        raise ValueError(f"fps must be positive, not {fps!r}")
    inputs: list[str] = []
    parts = [f"[0:v]fps={_num(fps)},scale={even(width)}:-2:flags=lanczos[v]"]
    if overlay_png is not None:
        inputs += ["-i", str(overlay_png)]
        parts.append(f"[v][1:v]{OVERLAY_POSITIONS[overlay_pos]}[v]")
    if reverse_append:
        parts.append("[v]split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1:a=0[v]")
    return inputs, ";".join(parts)


def gif_argv(
    src: str,
    out: str,
    *,
    fps,
    width,
    colors: int,
    from_s=None,
    to_s=None,
    overlay_png: str | None = None,
    overlay_pos: str = "bottom",
    reverse_append: bool = False,
    dither: str = "bayer",
) -> list[str]:
    """One ffmpeg command that trims, scales, overlays, boomerangs and palette-encodes a GIF."""
    colors = int(colors)
    if not 2 <= colors <= 256:
        raise ValueError(f"colors must be 2..256, not {colors}")
    if dither not in DITHERS:
        raise ValueError(f"dither must be one of {sorted(DITHERS)}, not {dither!r}")
    inputs, chain = _frame_chain(
        fps=fps, width=width, overlay_png=overlay_png, overlay_pos=overlay_pos, reverse_append=reverse_append
    )
    use = f"paletteuse=dither={dither}" + (":bayer_scale=5" if dither == "bayer" else "") + ":diff_mode=rectangle"
    graph = (
        f"{chain};[v]split[p][q];"
        f"[p]palettegen=max_colors={colors}:stats_mode=diff[pal];"
        f"[q][pal]{use}[out]"
    )
    return (
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        + _trim_options(from_s, to_s)
        + ["-i", str(src)]
        + inputs
        + ["-filter_complex", graph, "-map", "[out]", "-loop", "0", "-an", str(out)]
    )


def mp4_argv(
    src: str,
    out: str,
    *,
    fps,
    width,
    from_s=None,
    to_s=None,
    overlay_png: str | None = None,
    overlay_pos: str = "bottom",
    reverse_append: bool = False,
) -> list[str]:
    """One ffmpeg command for the H.264 preview: same frame chain, x264 veryfast, faststart, no audio."""
    inputs, chain = _frame_chain(
        fps=fps, width=width, overlay_png=overlay_png, overlay_pos=overlay_pos, reverse_append=reverse_append
    )
    return (
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        + _trim_options(from_s, to_s)
        + ["-i", str(src)]
        + inputs
        + [
            "-filter_complex", chain, "-map", "[v]",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "28", "-preset", "veryfast",
            "-movflags", "+faststart", "-an", str(out),
        ]
    )


def still_hold_argv(src: str, out: str, *, width, fps, duration_s) -> list[str]:
    """Hold a still image as an H.264 clip of ``duration_s`` at ``fps`` × ``width``.

    ``-loop 1`` is an input option (before ``-i``). The filter string is
    numbers and fixed tokens only.
    """
    return (
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-loop", "1", "-t", _seconds(duration_s), "-i", str(src),
         "-vf", f"fps={_num(fps)},scale={even(width)}:-2:flags=lanczos",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(out)]
    )


def kenburns_argv(src: str, out: str, *, width, height, fps, duration_s, zoom=1.4) -> list[str]:
    """Slow zoom (Ken Burns) of a still into an H.264 clip.

    The zoompan expression is a fixed template filled with numbers: no
    caller-supplied text enters the filtergraph.
    """
    frames = max(2, int(round(float(fps) * float(duration_s))))
    z_end = max(1.05, float(zoom))
    z_step = (z_end - 1.0) / (frames - 1)
    w, h = even(width), even(height)
    sw, sh = even(w * 4), even(h * 4)
    vf = (
        f"scale={sw}:{sh}:flags=lanczos,"
        f"zoompan=z='min(zoom+{_num(z_step)},{_num(z_end)})'"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={frames}:s={w}x{h}:fps={_num(fps)}"
    )
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-loop", "1", "-i", str(src),
        "-vf", vf,
        "-frames:v", str(frames),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(out),
    ]


def first_frame_argv(src: str, out: str) -> list[str]:
    """Grab the first frame of a clip (or still) as an image at ``out``."""
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src), "-frames:v", "1", str(out)]


def image_seq_argv(pattern: str, out: str, *, fps) -> list[str]:
    """Encode a ``frame_%04d.png``-style sequence (start number 1) to H.264."""
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-framerate", _num(fps), "-i", str(pattern),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(out),
    ]


def gifsicle_argv(
    src: str,
    out: str,
    *,
    colors: int | None = None,
    lossy: int | None = None,
    optimize: int = 3,
) -> list[str]:
    """gifsicle squeeze: `-O<level>` (0 disables), optional `--colors N`, optional `--lossy=N`."""
    optimize = int(optimize)
    if not 0 <= optimize <= 3:
        raise ValueError(f"optimize must be 0..3, not {optimize}")
    argv = ["gifsicle"]
    if optimize:
        argv.append(f"-O{optimize}")
    if colors is not None:
        colors = int(colors)
        if not 2 <= colors <= 256:
            raise ValueError(f"colors must be 2..256, not {colors}")
        argv += ["--colors", str(colors)]
    if lossy is not None:
        lossy = int(lossy)
        if lossy < 0:
            raise ValueError(f"lossy must be >= 0, not {lossy}")
        argv.append(f"--lossy={lossy}")
    return argv + ["-o", str(out), str(src)]
