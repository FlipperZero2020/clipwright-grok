import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture(scope="session")
def test_clip(tmp_path_factory):
    """A 3 s, 320x240, 30 fps synthetic H.264 clip with a sine audio track."""
    d = tmp_path_factory.mktemp("clip")
    path = str(d / "test_clip.mp4")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path],
        check=True,
    )
    return path


@pytest.fixture
def tmp_out(tmp_path):
    d = tmp_path / "out"
    d.mkdir()
    return str(d)
