#!/usr/bin/env python3
"""Download the three Wikimedia Commons photos open_mic_office.py cuts away to.

The reel opens them by short local names (office_cubicles.jpg, ...) from the
directory open_mic_office.PHOTOS (assets/photos/ by default, or wherever
CLIPWRIGHT_PHOTOS points). The Commons file names, authors and licences live
in one place — open_mic_office.PHOTO_SOURCES — and this tool reads them from
there so the two can't drift apart. Each file is fetched from
https://commons.wikimedia.org/wiki/Special:FilePath/<File name> (which
redirects to the original upload) with a User-Agent set, as Commons asks,
streamed to disk in chunks and refused unless it is an image under MAX_BYTES.
Files already present are left alone; pass --force to re-download.
Prints the credits at the end so they can be pasted wherever the GIF goes.
"""
import argparse
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from open_mic_office import PHOTOS, PHOTO_SOURCES  # noqa: E402

FILEPATH = "https://commons.wikimedia.org/wiki/Special:FilePath/"
USER_AGENT = "clipwright-plan/1.0 (tools/fetch_open_mic_photos.py; local render tooling)"
RETRIES = 4
RETRY_CODES = (429, 500, 502, 503, 504)
MAX_BYTES = 25 * 1024 * 1024  # the three photos are a few MB each; a Commons original can be far larger
CHUNK = 1 << 16


def commons_url(commons_name: str) -> str:
    return FILEPATH + urllib.parse.quote(commons_name)


def _open(req: urllib.request.Request):
    """urlopen, retrying on HTTP 429/5xx with a short backoff."""
    for attempt in range(1, RETRIES + 1):
        try:
            return urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_CODES or attempt == RETRIES:
                raise
            time.sleep(2 * attempt)


def download(url: str, dest: str) -> int:
    """Stream url into dest through a .part file. Refuses anything that is not an
    image or is larger than MAX_BYTES (checked against Content-Length up front and
    again as the body streams, so a redirect to a huge original or an HTML error
    page never lands in dest). Returns bytes written."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest + ".part"
    total = 0
    try:
        with _open(req) as r:
            ctype = r.headers.get_content_type()
            if not ctype.startswith("image/"):
                raise RuntimeError("%s: expected an image, got %s" % (url, ctype))
            declared = r.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > MAX_BYTES:
                raise RuntimeError("%s: Content-Length %s exceeds the %d-byte cap" % (url, declared, MAX_BYTES))
            with open(tmp, "wb") as fh:
                for chunk in iter(lambda: r.read(CHUNK), b""):
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise RuntimeError("%s: body exceeds the %d-byte cap" % (url, MAX_BYTES))
                    fh.write(chunk)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    os.replace(tmp, dest)
    return total


def fetch_all(photos_dir: str, force: bool = False) -> list[tuple[str, int]]:
    """Download every PHOTO_SOURCES entry into photos_dir. Returns [(local name, bytes)]."""
    os.makedirs(photos_dir, exist_ok=True)
    got = []
    for local, (commons, _author, _licence) in PHOTO_SOURCES.items():
        dest = os.path.join(photos_dir, local)
        if os.path.isfile(dest) and not force:
            print("kept     %s (%d bytes)" % (dest, os.path.getsize(dest)))
            got.append((local, os.path.getsize(dest)))
            continue
        url = commons_url(commons)
        print("fetching %s\n      -> %s" % (url, dest))
        size = download(url, dest)
        print("         %d bytes" % size)
        got.append((local, size))
    return got


def credits() -> str:
    lines = ["Photo credits (Wikimedia Commons):"]
    for local, (commons, author, licence) in PHOTO_SOURCES.items():
        lines.append("  %-22s File:%s — %s, %s" % (local, commons, author, licence))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true", help="re-download files that already exist")
    ap.add_argument("--dest", default=PHOTOS.rstrip("/"),
                    help="directory to download into (default: open_mic_office.PHOTOS)")
    args = ap.parse_args(argv)
    fetch_all(args.dest, force=args.force)
    print(credits())
    return 0


if __name__ == "__main__":
    sys.exit(main())
