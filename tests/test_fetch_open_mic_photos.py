"""tools/fetch_open_mic_photos.py's download(): streamed to a .part file under a
size cap, image-only, with the 429/5xx retry — all against a fake urlopen, so
nothing here touches Wikimedia Commons."""
import email.message
import io
import os
import sys
import urllib.error

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import fetch_open_mic_photos as fop  # noqa: E402

JPEG = b"\xff\xd8\xff" + bytes(range(256)) * 40 + b"\xff\xd9"


class FakeResponse:
    def __init__(self, body, ctype="image/jpeg", length=None):
        self._buf = io.BytesIO(body)
        self.headers = email.message.Message()
        self.headers["Content-Type"] = ctype
        if length is not None:
            self.headers["Content-Length"] = str(length)

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code):
    return urllib.error.HTTPError("https://example.invalid/x", code, "nope", email.message.Message(), None)


@pytest.fixture
def serve(monkeypatch):
    """Install a urlopen stand-in that answers each call with the next queued item
    (a FakeResponse, or an exception to raise) and records the requests it saw."""
    queue, seen = [], []

    def fake_urlopen(req, timeout=None):
        seen.append(req)
        nxt = queue.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    monkeypatch.setattr(fop.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(fop.time, "sleep", lambda s: None)

    def _serve(*items):
        queue.extend(items)
        return seen
    return _serve


def test_download_streams_the_body_to_dest(tmp_path, serve):
    seen = serve(FakeResponse(JPEG, length=len(JPEG)))
    dest = str(tmp_path / "office_cubicles.jpg")
    assert fop.download("https://example.invalid/a.jpg", dest) == len(JPEG)
    assert open(dest, "rb").read() == JPEG
    assert not os.path.exists(dest + ".part")
    assert seen[0].get_header("User-agent") == fop.USER_AGENT


def test_download_streams_in_chunks_not_one_read(tmp_path, serve, monkeypatch):
    monkeypatch.setattr(fop, "CHUNK", 7)
    reads = []

    class Counting(FakeResponse):
        def read(self, n=-1):
            reads.append(n)
            return super().read(n)

    serve(Counting(JPEG))
    fop.download("https://example.invalid/a.jpg", str(tmp_path / "a.jpg"))
    assert reads and all(n == 7 for n in reads)


def test_non_image_content_type_is_refused(tmp_path, serve):
    serve(FakeResponse(b"<html>rate limited</html>", ctype="text/html"))
    dest = str(tmp_path / "a.jpg")
    with pytest.raises(RuntimeError, match="expected an image, got text/html"):
        fop.download("https://example.invalid/a.jpg", dest)
    assert not os.path.exists(dest)
    assert not os.path.exists(dest + ".part")


def test_oversized_content_length_is_refused_before_reading(tmp_path, serve, monkeypatch):
    monkeypatch.setattr(fop, "MAX_BYTES", 1000)
    serve(FakeResponse(JPEG, length=10 ** 9))
    with pytest.raises(RuntimeError, match="Content-Length 1000000000 exceeds the 1000-byte cap"):
        fop.download("https://example.invalid/a.jpg", str(tmp_path / "a.jpg"))
    assert list(tmp_path.iterdir()) == []


def test_oversized_body_without_content_length_is_cut_off(tmp_path, serve, monkeypatch):
    monkeypatch.setattr(fop, "MAX_BYTES", 100)
    serve(FakeResponse(JPEG))
    with pytest.raises(RuntimeError, match="body exceeds the 100-byte cap"):
        fop.download("https://example.invalid/a.jpg", str(tmp_path / "a.jpg"))
    assert list(tmp_path.iterdir()) == []  # the .part is cleaned up


def test_retries_on_503_then_succeeds(tmp_path, serve):
    seen = serve(_http_error(503), _http_error(429), FakeResponse(JPEG))
    assert fop.download("https://example.invalid/a.jpg", str(tmp_path / "a.jpg")) == len(JPEG)
    assert len(seen) == 3


def test_404_is_not_retried(tmp_path, serve):
    seen = serve(_http_error(404))
    with pytest.raises(urllib.error.HTTPError):
        fop.download("https://example.invalid/a.jpg", str(tmp_path / "a.jpg"))
    assert len(seen) == 1


def test_gives_up_after_the_last_retry(tmp_path, serve):
    seen = serve(*[_http_error(503)] * fop.RETRIES)
    with pytest.raises(urllib.error.HTTPError):
        fop.download("https://example.invalid/a.jpg", str(tmp_path / "a.jpg"))
    assert len(seen) == fop.RETRIES


def test_commons_url_percent_encodes_the_file_name():
    url = fop.commons_url("Gulf Worldwide Sales & Marketing Team.jpg")
    assert url == fop.FILEPATH + "Gulf%20Worldwide%20Sales%20%26%20Marketing%20Team.jpg"


def test_fetch_all_keeps_existing_files_and_downloads_the_rest(tmp_path, serve, monkeypatch):
    names = list(fop.PHOTO_SOURCES)
    (tmp_path / names[0]).write_bytes(b"already here")
    serve(*[FakeResponse(JPEG) for _ in names[1:]])
    got = fop.fetch_all(str(tmp_path))
    assert got[0] == (names[0], len(b"already here"))
    assert got[1:] == [(n, len(JPEG)) for n in names[1:]]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(names)
