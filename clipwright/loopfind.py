"""Perfect-loop finder: pick the in/out frame pair whose seam is least visible.

Role in the engine: a GIF only looks "seamless" when its last frame resembles
its first. `sample_frames` pulls a low-res greyscale strip of the trimmed
range out of ffmpeg (rawvideo over a pipe, no temp files), `best_loop` scores
every early frame against every late frame and returns the closest pair, and
`degrade` turns that score into a loop mode: keep "seamless" when the seam is
under the threshold, otherwise fall back to "boomerang" explicitly rather than
shipping a visible jump.

Scores are mean absolute pixel differences, 0 (identical) .. 255 (black vs
white), computed with Pillow only. The ffmpeg filtergraph here is built from
numbers alone; the input path is a separate `-i` argument.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

from PIL import Image, ImageChops, ImageStat

from clipwright import ffmpeg
from clipwright.ffmpeg import FFmpegError, _num, even


def frame_diff(a: Image.Image, b: Image.Image) -> float:
    """Mean absolute pixel difference of two same-size images, 0..255.

    Images that are not already greyscale ("L") are converted first.
    Raises ValueError when the sizes differ.
    """
    if a.size != b.size:
        raise ValueError(f"frame sizes differ: {a.size} vs {b.size}")
    if a.mode != "L":
        a = a.convert("L")
    if b.mode != "L":
        b = b.convert("L")
    return float(ImageStat.Stat(ImageChops.difference(a, b)).mean[0])


def best_loop(frames: Sequence[Image.Image], *, window: int = 10) -> tuple[int, int, float]:
    """Find the (in_idx, out_idx, score) pair that makes the best loop seam.

    Each of the first `window` frames is compared with each of the last
    `window` frames, keeping only pairs more than half the clip apart so the
    loop stays long. Ties go to the longest loop, then the earliest in-point.
    Fewer than four frames cannot be scored: (0, len - 1, 255.0).
    """
    if window < 1:
        raise ValueError(f"window must be >= 1, not {window}")
    n = len(frames)
    if n < 4:
        return 0, n - 1, 255.0
    min_gap = n // 2
    # Tuple order is the tie-break: lowest score, then longest span (i - j most
    # negative), then smallest in-point. (0, n - 1) is always a candidate.
    scored = (
        (frame_diff(frames[i], frames[j]), i - j, i, j)
        for i in range(min(window, n))
        for j in range(max(n - window, i + min_gap + 1), n)
    )
    score, _, i, j = min(scored)
    return i, j, score


def degrade(score: float, *, threshold: float = 18.0) -> str:
    """Loop mode for a seam score: "seamless" at or under the threshold, else "boomerang"."""
    return "seamless" if score <= threshold else "boomerang"


def _finite(value, name: str) -> float:
    f = float(value)
    if not math.isfinite(f):
        raise ValueError(f"{name} must be finite, not {value!r}")
    return f


def sample_frames(
    path: str,
    from_s: float,
    to_s: float,
    *,
    fps: float = 10,
    width: int = 64,
    log: list | None = None,
) -> list[Image.Image]:
    """Decode `path` between `from_s` and `to_s` as small greyscale frames.

    One ffmpeg run streams rawvideo gray over a pipe. Frames keep the source's
    aspect ratio inside a `width` x `width` box (landscape sources are `width`
    wide, portrait ones `width` tall, both sides even and at least 2), so a
    2x1920 source samples as 2x64 rather than 64x61440. The size is passed to
    ffmpeg explicitly, and rotation metadata is ignored (`-noautorotate`), so
    the byte stream splits exactly into whole frames; the box is therefore
    fitted to the *stored* frame, undoing the swap `ffmpeg.probe` applies for
    a rotated clip (a seam score does not care which way up the frames are).
    `log`, when given, receives the argv like every other `ffmpeg.run` call.

    Raises ValueError for a bad range, fps or width, and FFmpegError when the
    input has no usable frame size, ffmpeg fails, or nothing was decoded.
    """
    from_f = _finite(from_s, "from_s")
    to_f = _finite(to_s, "to_s")
    if from_f < 0 or to_f <= from_f:
        raise ValueError(f"need 0 <= from_s < to_s, got {from_s!r}..{to_s!r}")
    fps_f = _finite(fps, "fps")
    if fps_f <= 0:
        raise ValueError(f"fps must be positive, not {fps!r}")
    width = int(width)
    if width < 2:
        raise ValueError(f"width must be >= 2, not {width}")

    info = ffmpeg.probe(path)
    if info.width < 1 or info.height < 1:
        raise FFmpegError(["ffprobe", path], f"no usable frame size: {info.width}x{info.height}")
    stored_w, stored_h = (info.width, info.height)
    if info.rotation in (90, 270):
        stored_w, stored_h = stored_h, stored_w
    w, h = _fit_in_box(stored_w, stored_h, width)
    argv = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-noautorotate",
        "-ss", f"{from_f:.3f}", "-to", f"{to_f:.3f}", "-i", path,
        "-vf", f"fps={_num(fps_f)},scale={w}:{h},format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]
    raw = ffmpeg.run(argv, log=log).stdout
    frame_bytes = w * h
    if not raw or len(raw) % frame_bytes:
        raise FFmpegError(argv, f"expected whole {w}x{h} gray frames, got {len(raw)} bytes")
    return [
        Image.frombytes("L", (w, h), raw[k:k + frame_bytes])
        for k in range(0, len(raw), frame_bytes)
    ]


def _fit_in_box(src_w: int, src_h: int, box: int) -> tuple[int, int]:
    """Even (w, h) with the source's aspect ratio whose longer side is `box`."""
    if src_h > src_w:
        return even(round(box * src_w / src_h)), box
    return box, even(round(box * src_h / src_w))
