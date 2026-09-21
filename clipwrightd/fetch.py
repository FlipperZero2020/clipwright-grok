"""Wikimedia Commons image search — the daemon's seed-from-web path.

The engine never opens a socket. This module is the one extra egress besides
the Bot API: look up a still on Commons, download one image under a byte cap,
and write it to a hex-named file under the caller's directory. Filenames from
the web are never trusted. ``urlopen`` is injectable so tests never hit the
network.

Failure is a ``FetchError`` with a one-line reason; the daemon then falls
back to the local typecard recipe.
"""
from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

USER_AGENT = "Clipwright/0.1 (https://commons.wikimedia.org/wiki/Commons:API; personal GIF foundry)"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
SEARCH_TIMEOUT_S = 8.0
DOWNLOAD_TIMEOUT_S = 20.0
MAX_BYTES = 8_000_000
MAX_QUERY = 80
SEARCH_LIMIT = 8
CHUNK = 64 * 1024
ACCEPT_MIME = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}

UrlOpen = Callable[..., Any]


class FetchError(RuntimeError):
    """Commons search or download failed; ``str(exc)`` is safe to show the user."""


def fetch_image(query: str, dest_dir: str, *, urlopen: UrlOpen | None = None) -> str:
    """Search Commons for ``query`` and write one image into ``dest_dir``.

    Returns the destination path. Raises FetchError on any failure (empty
    query, no hits, non-image, oversize, timeout, HTTP error). The file is
    named ``<hex>.<ext>`` from the content-type, never from the URL.
    """
    opener = urlopen or urllib.request.urlopen
    q = _clean_query(query)
    if not q:
        raise FetchError("nothing to search for")
    candidates = _search(q, opener)
    if not candidates:
        raise FetchError("Commons had no picture for that")
    os.makedirs(dest_dir, exist_ok=True)
    last = "download failed"
    for url, mime in candidates:
        ext = ACCEPT_MIME.get(mime, "jpg")
        dest = os.path.join(dest_dir, f"{secrets.token_hex(6)}.{ext}")
        try:
            _download(url, dest, opener)
            return dest
        except FetchError as err:
            last = str(err)
            _unlink(dest)
            continue
    raise FetchError(last)


def _clean_query(query: str) -> str:
    text = " ".join((query or "").split())
    if not text:
        return ""
    return text[:MAX_QUERY]


def _search(query: str, urlopen: UrlOpen) -> list[tuple[str, str]]:
    """``[(url, mime), ...]`` in Commons relevance order, images only."""
    params = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "generator": "search",
        "gsrsearch": f"{query} filetype:bitmap",
        "gsrnamespace": "6",
        "gsrlimit": str(SEARCH_LIMIT),
        "prop": "imageinfo",
        "iiprop": "url|mime|size",
        "iiurlwidth": "1280",
    }
    url = COMMONS_API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=SEARCH_TIMEOUT_S) as resp:
            raw = resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as err:
        raise FetchError(f"Commons search HTTP {err.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise FetchError("Commons search timed out or could not connect") from err
    if len(raw) > MAX_BYTES:
        raise FetchError("Commons search response was too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as err:
        raise FetchError("Commons search returned no JSON") from err
    pages = ((data.get("query") or {}).get("pages")) or []
    if isinstance(pages, dict):          # formatversion=1
        pages = list(pages.values())
    out: list[tuple[str, str]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        info = (page.get("imageinfo") or [None])[0] or {}
        mime = str(info.get("mime") or "").lower()
        if mime not in ACCEPT_MIME:
            continue
        size = info.get("size")
        if isinstance(size, int) and size > MAX_BYTES:
            continue
        href = info.get("thumburl") or info.get("url")
        if isinstance(href, str) and href.startswith("https://"):
            out.append((href, mime))
    return out


def _download(url: str, dest: str, urlopen: UrlOpen) -> None:
    """Stream ``url`` into ``dest``; refuse a non-image or a body over MAX_BYTES."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest + ".part"
    total = 0
    try:
        with urlopen(req, timeout=DOWNLOAD_TIMEOUT_S) as resp:
            ctype = _content_type(resp)
            if ctype not in ACCEPT_MIME and not ctype.startswith("image/"):
                raise FetchError(f"expected an image, got {ctype or 'unknown'}")
            declared = _header(resp, "Content-Length")
            if declared.isdigit() and int(declared) > MAX_BYTES:
                raise FetchError("that picture is larger than I will download")
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise FetchError("that picture is larger than I will download")
                    fh.write(chunk)
        if total < 32:
            raise FetchError("that download was empty")
        os.replace(tmp, dest)
    except FetchError:
        _unlink(tmp)
        raise
    except urllib.error.HTTPError as err:
        _unlink(tmp)
        raise FetchError(f"image HTTP {err.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        _unlink(tmp)
        raise FetchError("image download timed out or could not connect") from err


def _content_type(resp: Any) -> str:
    headers = getattr(resp, "headers", None)
    if headers is not None and hasattr(headers, "get_content_type"):
        try:
            return str(headers.get_content_type() or "").lower()
        except (TypeError, ValueError):
            pass
    raw = _header(resp, "Content-Type")
    return raw.split(";", 1)[0].strip().lower()


def _header(resp: Any, name: str) -> str:
    headers = getattr(resp, "headers", None)
    if headers is None:
        return ""
    try:
        value = headers.get(name)
    except (TypeError, ValueError, KeyError):
        return ""
    return str(value or "")


def _unlink(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
