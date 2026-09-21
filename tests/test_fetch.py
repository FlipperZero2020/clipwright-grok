"""clipwrightd.fetch — mocked network only. Never opens a socket."""
from __future__ import annotations

import io
import json
import os
import re

import pytest

from clipwrightd.fetch import FetchError, fetch_image, _clean_query

JPEG_HEAD = b"\xff\xd8\xff\xe0" + b"\x00" * 64


class FakeHeaders(dict):
    def get_content_type(self) -> str:
        return str(self.get("Content-Type", "")).split(";", 1)[0].strip()


class FakeResp:
    def __init__(self, body: bytes, headers: dict | None = None, url: str = "https://example.test/x.jpg"):
        self._buf = io.BytesIO(body)
        self.headers = FakeHeaders(headers or {"Content-Type": "image/jpeg", "Content-Length": str(len(body))})
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n: int = -1) -> bytes:
        return self._buf.read() if n < 0 else self._buf.read(n)

    def geturl(self) -> str:
        return self.url


def _search_body(url: str = "https://upload.wikimedia.org/wikipedia/commons/d/d0/Dog.jpg",
                 mime: str = "image/jpeg", size: int = 1000) -> bytes:
    return json.dumps({"query": {"pages": [{
        "title": "File:Some Dog.jpg",
        "imageinfo": [{"url": url, "mime": mime, "size": size, "thumburl": url}],
    }]}}).encode()


def test_clean_query_collapses_and_caps():
    assert _clean_query("  a   sleepy\ndog  ") == "a sleepy dog"
    assert _clean_query("") == ""
    assert len(_clean_query("x" * 200)) == 80


def test_fetch_writes_a_hex_name_and_never_trusts_the_url(tmp_path):
    calls: list[str] = []

    def urlopen(req, timeout=None):
        calls.append(_req_url(req))
        url = _req_url(req)
        if "api.php" in url:
            return FakeResp(_search_body(), {"Content-Type": "application/json"})
        return FakeResp(JPEG_HEAD, {"Content-Type": "image/jpeg"})

    dest = fetch_image("a dog", str(tmp_path), urlopen=urlopen)
    assert dest.startswith(str(tmp_path))
    assert re.fullmatch(r"[0-9a-f]{12}\.jpg", os.path.basename(dest))
    assert "Dog.jpg" not in dest
    assert os.path.isfile(dest) and os.path.getsize(dest) == len(JPEG_HEAD)
    assert any("api.php" in u for u in calls)


def test_fetch_empty_query_is_a_fetch_error(tmp_path):
    def never(*a, **k):
        raise AssertionError("must not hit the network")
    with pytest.raises(FetchError, match="nothing to search"):
        fetch_image("   ", str(tmp_path), urlopen=never)


def test_fetch_non_image_content_type_is_refused(tmp_path):
    def urlopen(req, timeout=None):
        url = _req_url(req)
        if "api.php" in url:
            return FakeResp(_search_body(), {"Content-Type": "application/json"})
        return FakeResp(b"<html>nope</html>", {"Content-Type": "text/html"})

    with pytest.raises(FetchError, match="expected an image"):
        fetch_image("dog", str(tmp_path), urlopen=urlopen)
    assert list(tmp_path.iterdir()) == [] or all(not p.name.endswith(".part") for p in tmp_path.iterdir())


def _req_url(req) -> str:
    if isinstance(req, str):
        return req
    return getattr(req, "full_url", None) or req.get_full_url()


def test_fetch_oversize_body_is_refused(tmp_path):
    from clipwrightd.fetch import MAX_BYTES

    def urlopen(req, timeout=None):
        url = _req_url(req)
        if "api.php" in url:
            body = _search_body(size=10)
            return FakeResp(body, {"Content-Type": "application/json", "Content-Length": str(len(body))})
        return FakeResp(b"x", {"Content-Type": "image/jpeg", "Content-Length": str(MAX_BYTES + 1)})

    with pytest.raises(FetchError, match="larger than I will download"):
        fetch_image("dog", str(tmp_path), urlopen=urlopen)


def test_fetch_no_hits_is_a_one_line_error(tmp_path):
    def urlopen(req, timeout=None):
        return FakeResp(json.dumps({"query": {"pages": []}}).encode(), {"Content-Type": "application/json"})

    with pytest.raises(FetchError, match="no picture"):
        fetch_image("xyzzy-no-such", str(tmp_path), urlopen=urlopen)
