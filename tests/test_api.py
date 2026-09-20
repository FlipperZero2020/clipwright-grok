"""Offline tests for clipwrightd.api (fake transport, no sockets) and clipwrightd.config."""

from __future__ import annotations

import io
import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import pytest

from clipwrightd import api as api_mod
from clipwrightd import config as config_mod
from clipwrightd.api import CHUNK, DEFAULT_TIMEOUT, UPLOAD_MIN_RATE, BotAPI, BotAPIError, redact
from clipwrightd.config import Config, ConfigError, load_config, parse_env

TOKEN = "123456789:AAExampleTokenValue_abc-XYZ"
BASE = "https://api.telegram.org"


def ok(result: Any) -> bytes:
    return json.dumps({"ok": True, "result": result}).encode()


def fail(description: str, code: int = 400, **extra: Any) -> bytes:
    return json.dumps({"ok": False, "error_code": code, "description": description, **extra}).encode()


@dataclass
class Call:
    url: str
    data: bytes | None
    headers: dict[str, str]
    timeout: float

    @property
    def json(self) -> Any:
        return json.loads(self.data)


class Recorder:
    """A transport that records every call and replays canned responses in order."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses) or [ok(True)]
        self.calls: list[Call] = []

    def __call__(self, url, data, headers, timeout):
        self.calls.append(Call(url, data, headers, timeout))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    @property
    def last(self) -> Call:
        return self.calls[-1]


def parse_multipart(body: bytes, boundary: str) -> dict[str, dict[str, Any]]:
    """name -> {"filename", "content_type", "content"} for every part in ``body``."""
    delim = b"--" + boundary.encode()
    assert body.startswith(delim), "body must open with the boundary"
    assert body.rstrip().endswith(delim + b"--"), "body must close with boundary--"
    parts: dict[str, dict[str, Any]] = {}
    for raw in body.split(delim)[1:]:
        if raw.startswith(b"--"):
            break
        head, _, content = raw.lstrip(b"\r\n").partition(b"\r\n\r\n")
        headers = head.decode()
        name = re.search(r'name="([^"]+)"', headers).group(1)
        fn = re.search(r'filename="([^"]+)"', headers)
        ct = re.search(r"Content-Type: (\S+)", headers)
        assert content.endswith(b"\r\n")
        parts[name] = {
            "filename": fn.group(1) if fn else None,
            "content_type": ct.group(1) if ct else None,
            "content": content[:-2],
        }
    return parts


def boundary_of(call: Call) -> str:
    ctype = call.headers["Content-Type"]
    assert ctype.startswith("multipart/form-data; boundary=")
    return ctype.split("boundary=", 1)[1]


@pytest.fixture
def rec() -> Recorder:
    return Recorder(ok({"message_id": 1}))


@pytest.fixture
def bot(rec: Recorder) -> BotAPI:
    return BotAPI(TOKEN, transport=rec)


# --- call(): URL, body, headers, timeout ------------------------------------

def test_send_message_posts_json(bot: BotAPI, rec: Recorder) -> None:
    markup = {"inline_keyboard": [[{"text": "• Yellow", "callback_data": "c/Ab3_-z/2/1"}]]}
    result = bot.send_message(42, "hi ☕", reply_markup=markup, reply_to_message_id=7)

    call = rec.last
    assert call.url == f"{BASE}/bot{TOKEN}/sendMessage"
    assert call.headers == {"Content-Type": "application/json"}
    assert call.timeout == DEFAULT_TIMEOUT == 65
    assert call.json == {"chat_id": 42, "text": "hi ☕", "reply_markup": markup,
                         "reply_to_message_id": 7}          # parse_mode=None dropped
    assert result == {"message_id": 1}


def test_custom_base_url() -> None:
    rec = Recorder(ok(True))
    BotAPI(TOKEN, transport=rec, base="http://localhost:8081/").send_chat_action(1, "upload_video")
    assert rec.last.url == f"http://localhost:8081/bot{TOKEN}/sendChatAction"


@pytest.mark.parametrize("invoke, method, body", [
    (lambda b: b.send_chat_action(5, "upload_video"), "sendChatAction",
     {"chat_id": 5, "action": "upload_video"}),
    (lambda b: b.answer_callback_query("cbq1"), "answerCallbackQuery",
     {"callback_query_id": "cbq1"}),
    (lambda b: b.answer_callback_query("cbq1", text="done"), "answerCallbackQuery",
     {"callback_query_id": "cbq1", "text": "done"}),
    (lambda b: b.set_my_commands([{"command": "start", "description": "Begin"}]), "setMyCommands",
     {"commands": [{"command": "start", "description": "Begin"}]}),
    (lambda b: b.get_file("fid"), "getFile", {"file_id": "fid"}),
])
def test_simple_methods(invoke, method, body) -> None:
    rec = Recorder(ok({"file_id": "fid", "file_path": "videos/file_1.mp4"}))
    invoke(BotAPI(TOKEN, transport=rec))
    assert rec.last.url.endswith(f"/bot{TOKEN}/{method}")
    assert rec.last.json == body


def test_get_file_returns_dict(bot: BotAPI, rec: Recorder) -> None:
    rec.responses = [ok({"file_id": "fid", "file_size": 10, "file_path": "videos/file_1.mp4"})]
    assert bot.get_file("fid")["file_path"] == "videos/file_1.mp4"


def test_get_updates_body_and_stretched_timeout(rec: Recorder) -> None:
    rec.responses = [ok([{"update_id": 1}])]
    bot = BotAPI(TOKEN, transport=rec)

    assert bot.get_updates(None) == [{"update_id": 1}]
    assert rec.last.json == {"timeout": 50}            # offset=None and allowed_updates dropped
    assert rec.last.timeout == 65                      # 50 s poll fits inside the 65 s default

    bot.get_updates(123, timeout=100, allowed_updates=["message", "callback_query"])
    assert rec.last.json == {"offset": 123, "timeout": 100,
                             "allowed_updates": ["message", "callback_query"]}
    assert rec.last.timeout == 115                     # stretched past the poll wait


# --- upload(): multipart ------------------------------------------------------

def test_send_animation_multipart_from_bytes(bot: BotAPI, rec: Recorder) -> None:
    payload = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 4
    markup = {"inline_keyboard": [[{"text": "− Size +", "callback_data": "c/Ab3_-z/1/1"}]]}
    bot.send_animation(42, payload, caption="proxy", reply_markup=markup)

    call = rec.last
    assert call.url == f"{BASE}/bot{TOKEN}/sendAnimation"
    boundary = boundary_of(call)
    assert re.fullmatch(r"[0-9a-f]{32}", boundary)      # uuid4().hex
    assert boundary.encode() in call.data
    assert payload in call.data

    parts = parse_multipart(call.data, boundary)
    assert parts["chat_id"]["content"] == b"42"
    assert parts["caption"]["content"] == b"proxy"
    assert json.loads(parts["reply_markup"]["content"]) == markup
    assert parts["animation"] == {"filename": "preview.mp4", "content_type": "video/mp4",
                                  "content": payload}


def test_send_document_from_path(bot: BotAPI, rec: Recorder, tmp_path) -> None:
    gif = tmp_path / "render.gif"
    gif.write_bytes(b"GIF89a" + os.urandom(3000))
    bot.send_document(42, str(gif), "my_sprint_velocity.gif", caption="8 MB fits")

    call = rec.last
    assert call.url.endswith("/sendDocument")
    parts = parse_multipart(call.data, boundary_of(call))
    assert parts["document"]["filename"] == "my_sprint_velocity.gif"
    assert parts["document"]["content_type"] == "image/gif"
    assert parts["document"]["content"] == gif.read_bytes()
    assert parts["caption"]["content"] == b"8 MB fits"
    assert "reply_markup" not in parts


def test_send_document_can_disable_content_type_detection(bot: BotAPI, rec: Recorder) -> None:
    """The Export GIF must stay a document: without this flag Telegram re-types it as an animation."""
    bot.send_document(42, b"GIF89a", "loop.gif", disable_content_type_detection=True)
    parts = parse_multipart(rec.last.data, boundary_of(rec.last))
    assert parts["disable_content_type_detection"]["content"] == b"true"

    bot.send_document(42, b"{}", "loop.json")
    assert "disable_content_type_detection" not in parse_multipart(rec.last.data, boundary_of(rec.last))


def test_upload_timeout_grows_with_the_body(bot: BotAPI, rec: Recorder) -> None:
    """sendall's socket timeout is a total deadline, so a big export gets a bigger budget."""
    bot.send_document(42, b"g" * 100, "small.gif")
    assert rec.last.timeout == DEFAULT_TIMEOUT

    big = os.urandom(8_000_000)                       # a GIF at the telegram budget
    bot.send_document(42, big, "big.gif", disable_content_type_detection=True)
    assert rec.last.timeout == pytest.approx(len(rec.last.data) / UPLOAD_MIN_RATE)
    assert rec.last.timeout >= 400                    # ≥ 20 KB/s assumed, not 125 KB/s
    assert bot.upload_timeout(0) == DEFAULT_TIMEOUT


def test_upload_rejects_missing_path(bot: BotAPI, tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        bot.send_document(42, str(tmp_path / "nope.gif"), "nope.gif")


def test_edit_message_media_uses_attach(bot: BotAPI, rec: Recorder) -> None:
    payload = b"mp4-bytes-" * 100
    markup = {"inline_keyboard": []}
    bot.edit_message_media(42, 99, payload, "preview.mp4", caption="v2", reply_markup=markup)

    call = rec.last
    assert call.url.endswith("/editMessageMedia")
    parts = parse_multipart(call.data, boundary_of(call))
    media = json.loads(parts["media"]["content"])
    assert media == {"type": "animation", "media": "attach://file", "caption": "v2"}
    assert parts["file"] == {"filename": "preview.mp4", "content_type": "video/mp4",
                             "content": payload}
    assert parts["chat_id"]["content"] == b"42"
    assert parts["message_id"]["content"] == b"99"
    assert json.loads(parts["reply_markup"]["content"]) == markup


def test_edit_message_media_without_caption_omits_it(bot: BotAPI, rec: Recorder) -> None:
    bot.edit_message_media(42, 99, b"x", "preview.mp4")
    parts = parse_multipart(rec.last.data, boundary_of(rec.last))
    assert json.loads(parts["media"]["content"]) == {"type": "animation", "media": "attach://file"}


def test_upload_field_encoding(bot: BotAPI, rec: Recorder) -> None:
    bot.upload("sendDocument", {"document": ("a.gif", b"g", "image/gif")},
               chat_id=1, disable_notification=True, tags=["x", "y"], skip=None)
    parts = parse_multipart(rec.last.data, boundary_of(rec.last))
    assert parts["disable_notification"]["content"] == b"true"
    assert parts["tags"]["content"] == b'["x","y"]'
    assert "skip" not in parts


# --- errors -------------------------------------------------------------------

def test_ok_false_raises_bot_api_error(bot: BotAPI, rec: Recorder) -> None:
    rec.responses = [fail("Bad Request: chat not found", 400)]
    with pytest.raises(BotAPIError) as ei:
        bot.send_message(1, "x")
    assert ei.value.error_code == 400
    assert ei.value.retry_after is None
    assert "chat not found" in str(ei.value)
    assert "sendMessage" in str(ei.value)


def test_retry_after_is_exposed(bot: BotAPI, rec: Recorder) -> None:
    rec.responses = [fail("Too Many Requests: retry after 7", 429, parameters={"retry_after": 7})]
    with pytest.raises(BotAPIError) as ei:
        bot.send_chat_action(1, "typing")
    assert ei.value.error_code == 429
    assert ei.value.retry_after == 7


def test_non_json_response_raises(bot: BotAPI, rec: Recorder) -> None:
    rec.responses = [b"<html>502 Bad Gateway</html>"]
    with pytest.raises(BotAPIError, match="non-JSON"):
        bot.get_file("fid")


def test_transport_exception_is_wrapped_and_redacted(caplog) -> None:
    def boom(url, data, headers, timeout):
        raise OSError(f"connect to {url} failed")

    caplog.set_level(logging.DEBUG)
    with pytest.raises(BotAPIError) as ei:
        BotAPI(TOKEN, transport=boom).send_chat_action(1, "typing")
    assert TOKEN not in str(ei.value)
    assert "<token>" in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__
    assert TOKEN not in caplog.text


class Cursed(io.BytesIO):
    """A response whose headers arrived but whose body read fails; remembers whether close() ran."""

    def __init__(self, exc: Exception) -> None:
        super().__init__(b"")
        self.exc = exc
        self.closed_by_client = False

    def read(self, n: int = -1) -> bytes:
        raise self.exc

    def close(self) -> None:
        self.closed_by_client = True
        super().close()


@pytest.mark.parametrize("exc", [
    TimeoutError("The read operation timed out"),
    ConnectionResetError(104, "Connection reset by peer"),
    OSError(f"body from {BASE}/bot{TOKEN}/getUpdates truncated"),
])
def test_body_read_failure_is_a_bot_api_error(exc, caplog) -> None:
    resp = Cursed(exc)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(BotAPIError) as ei:
        BotAPI(TOKEN, transport=Recorder(resp)).get_updates(None)
    assert "transport failure" in str(ei.value) and TOKEN not in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__
    assert resp.closed_by_client and TOKEN not in caplog.text


def test_response_close_failure_is_ignored() -> None:
    class Grumpy(io.BytesIO):
        def close(self) -> None:
            raise OSError("already gone")

    assert BotAPI(TOKEN, transport=Recorder(Grumpy(ok(True)))).send_chat_action(1, "typing") is True


def test_error_description_is_redacted_and_logged_clean(bot: BotAPI, rec: Recorder, caplog) -> None:
    rec.responses = [fail(f"Not Found: {BASE}/bot{TOKEN}/nope", 404)]
    caplog.set_level(logging.DEBUG)
    with pytest.raises(BotAPIError) as ei:
        bot.call("nope")
    assert TOKEN not in str(ei.value)
    assert "<token>" in str(ei.value)
    assert caplog.text and TOKEN not in caplog.text


def test_redact_helper() -> None:
    url = f"{BASE}/bot{TOKEN}/getMe"
    assert redact(TOKEN, url) == f"{BASE}/bot<token>/getMe"
    assert redact(TOKEN, "no token here") == "no token here"
    assert redact("", url) == url
    quoted = TOKEN.replace(":", "%3A")
    assert TOKEN not in redact(TOKEN, quoted) and "%3A" not in redact(TOKEN, quoted)


# --- download_file --------------------------------------------------------------

class Stream(io.BytesIO):
    """A file-like that counts reads and remembers whether it was closed."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.reads = 0
        self.closed_by_client = False

    def read(self, n: int = -1) -> bytes:
        self.reads += 1
        return super().read(n)

    def close(self) -> None:
        self.closed_by_client = True
        super().close()


def test_download_file_streams_to_dest(tmp_path) -> None:
    data = os.urandom(CHUNK * 3 + 17)
    stream = Stream(data)
    rec = Recorder(stream)
    dest = str(tmp_path / "in.mp4")

    n = BotAPI(TOKEN, transport=rec).download_file("videos/file 1.mp4", dest, max_bytes=len(data))

    assert n == len(data)
    assert open(dest, "rb").read() == data
    assert rec.last.url == f"{BASE}/file/bot{TOKEN}/videos/file%201.mp4"
    assert rec.last.data is None and rec.last.timeout == DEFAULT_TIMEOUT
    assert stream.reads >= 4                       # chunked, not slurped
    assert stream.closed_by_client


class Endless:
    """A response that never runs dry: proves the client stops at max_bytes."""

    def __init__(self) -> None:
        self.reads = 0

    def read(self, n: int = -1) -> bytes:
        self.reads += 1
        return b"\xaa" * (n if n > 0 else CHUNK)


def test_download_file_enforces_max_bytes(tmp_path) -> None:
    src = Endless()
    dest = str(tmp_path / "big.mp4")
    limit = CHUNK * 4

    with pytest.raises(BotAPIError) as ei:
        BotAPI(TOKEN, transport=Recorder(src)).download_file("videos/big.mp4", dest, max_bytes=limit)

    assert ei.value.error_code == 413
    assert str(limit) in str(ei.value)
    assert not os.path.exists(dest)                # partial file removed
    assert src.reads <= 6                          # stopped right after crossing the limit


class Snaps(io.BytesIO):
    """Streams one chunk, then the connection dies."""

    def read(self, n: int = -1) -> bytes:
        chunk = super().read(n)
        if chunk:
            return chunk
        raise ConnectionResetError(104, "Connection reset by peer")


def test_download_file_body_failure_is_a_bot_api_error_and_removes_the_partial(tmp_path) -> None:
    dest = str(tmp_path / "half.mp4")
    with pytest.raises(BotAPIError) as ei:
        BotAPI(TOKEN, transport=Recorder(Snaps(b"x" * CHUNK))).download_file("v.mp4", dest, max_bytes=CHUNK * 4)
    assert "transport failure" in str(ei.value) and ei.value.error_code is None
    assert not os.path.exists(dest)


class Announced(Stream):
    """A response that carries headers, like urllib's: the body may fall short of them."""

    def __init__(self, data: bytes, content_length: str | None) -> None:
        super().__init__(data)
        self.headers = {} if content_length is None else {"Content-Length": content_length}


def test_download_file_short_body_is_an_error_and_removes_the_partial(tmp_path) -> None:
    """A clean close before Content-Length is reached must not pass as a complete file."""
    dest = str(tmp_path / "short.mp4")
    resp = Announced(b"m" * CHUNK, str(CHUNK * 4))
    with pytest.raises(BotAPIError) as ei:
        BotAPI(TOKEN, transport=Recorder(resp)).download_file("v.mp4", dest, max_bytes=CHUNK * 8)
    assert "truncated" in str(ei.value) and str(CHUNK * 4) in str(ei.value)
    assert ei.value.error_code is None
    assert not os.path.exists(dest)
    assert resp.closed_by_client


@pytest.mark.parametrize("content_length", [str(CHUNK * 2 + 5), None, "", "soon", "-1"])
def test_download_file_complete_or_unannounced_body_is_accepted(tmp_path, content_length) -> None:
    data = b"m" * (CHUNK * 2 + 5)
    dest = str(tmp_path / "whole.mp4")
    n = BotAPI(TOKEN, transport=Recorder(Announced(data, content_length))).download_file(
        "v.mp4", dest, max_bytes=len(data))
    assert n == len(data) and open(dest, "rb").read() == data


def test_download_file_accepts_bytes_response(tmp_path) -> None:
    dest = str(tmp_path / "small.mp4")
    n = BotAPI(TOKEN, transport=Recorder(b"abc")).download_file("f.mp4", dest, max_bytes=3)
    assert n == 3 and open(dest, "rb").read() == b"abc"

    with pytest.raises(BotAPIError):
        BotAPI(TOKEN, transport=Recorder(b"abcd")).download_file("f.mp4", dest, max_bytes=3)
    assert not os.path.exists(dest)


def test_download_file_http_error_does_not_write(tmp_path) -> None:
    class NotFound(io.BytesIO):
        status = 404

    dest = str(tmp_path / "missing.mp4")
    resp = NotFound(fail("Not Found", 404))
    with pytest.raises(BotAPIError) as ei:
        BotAPI(TOKEN, transport=Recorder(resp)).download_file("gone.mp4", dest, max_bytes=10)
    assert ei.value.error_code == 404
    assert "Not Found" in str(ei.value)
    assert not os.path.exists(dest)


# --- default transport (urllib, patched — still no socket) -----------------------

def test_default_transport_posts_via_urllib(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def fake_urlopen(req, timeout=None):
        seen["req"], seen["timeout"] = req, timeout
        return io.BytesIO(ok(True))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    bot = BotAPI(TOKEN)
    assert bot.transport is api_mod.default_transport
    assert bot.timeout == 65

    assert bot.send_chat_action(3, "typing") is True
    req = seen["req"]
    assert req.full_url == f"{BASE}/bot{TOKEN}/sendChatAction"
    assert req.get_method() == "POST"
    assert json.loads(req.data) == {"chat_id": 3, "action": "typing"}
    assert req.get_header("Content-type") == "application/json"
    assert seen["timeout"] == 65


def test_default_transport_returns_http_error_body(monkeypatch) -> None:
    body = fail("Forbidden: bot was blocked by the user", 403)

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(body))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(BotAPIError) as ei:
        BotAPI(TOKEN).send_message(3, "x")
    assert ei.value.error_code == 403
    assert "blocked" in str(ei.value)
    assert TOKEN not in str(ei.value)


def test_default_transport_gets_for_download(monkeypatch, tmp_path) -> None:
    seen: dict[str, Any] = {}

    def fake_urlopen(req, timeout=None):
        seen["req"] = req
        return io.BytesIO(b"video")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    dest = str(tmp_path / "v.mp4")
    assert BotAPI(TOKEN).download_file("videos/v.mp4", dest, max_bytes=100) == 5
    assert seen["req"].get_method() == "GET"
    assert seen["req"].full_url == f"{BASE}/file/bot{TOKEN}/videos/v.mp4"


def test_default_transport_url_error_is_redacted(monkeypatch) -> None:
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError(f"unreachable {req.full_url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(BotAPIError) as ei:
        BotAPI(TOKEN).get_updates(None)
    assert TOKEN not in str(ei.value)
    assert "unreachable" in str(ei.value)


# --- config -----------------------------------------------------------------------

ENV_TEXT = f"""
# clipwright bot settings
CLIPWRIGHT_BOT_TOKEN="{TOKEN}"
export CLIPWRIGHT_OWNER_ID = 1001
CLIPWRIGHT_FRIEND_IDS='2002, 3003,,4004'
CLIPWRIGHT_MAX_UPLOAD_BYTES=5000000
not a key value line
"""


def write_env(tmp_path, text: str = ENV_TEXT, mode: int = 0o600):
    path = tmp_path / "bot.env"
    path.write_text(text)
    os.chmod(path, mode)
    return path


def test_parse_env_rules() -> None:
    assert parse_env(ENV_TEXT) == {
        "CLIPWRIGHT_BOT_TOKEN": TOKEN,
        "CLIPWRIGHT_OWNER_ID": "1001",
        "CLIPWRIGHT_FRIEND_IDS": "2002, 3003,,4004",
        "CLIPWRIGHT_MAX_UPLOAD_BYTES": "5000000",
    }
    assert parse_env("A=1\nA=2\n=x\nB='mixed\"\n") == {"A": "2", "B": "'mixed\""}


def test_load_config_parses_file(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.delenv("CLIPWRIGHT_HOME", raising=False)
    path = write_env(tmp_path)
    caplog.set_level(logging.DEBUG)

    cfg = load_config(str(path))

    assert isinstance(cfg, Config)
    assert cfg.token == TOKEN
    assert cfg.owner_id == 1001
    assert cfg.friend_ids == {2002, 3003, 4004}
    assert cfg.allowed == {1001, 2002, 3003, 4004}
    assert cfg.home == str(tmp_path)
    assert cfg.max_upload_bytes == 5_000_000            # overridden from the file
    assert (cfg.max_duration_s, cfg.max_dim, cfg.per_day_quota, cfg.queue_depth) == (60.0, 1920, 200, 8)
    assert (cfg.max_user_bytes, cfg.retention_days) == (1_000_000_000, 14.0)
    assert "mode" not in caplog.text                    # 0600: no warning


def test_load_config_reads_disk_limits(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CLIPWRIGHT_HOME", raising=False)
    path = write_env(tmp_path, f"CLIPWRIGHT_BOT_TOKEN={TOKEN}\nCLIPWRIGHT_OWNER_ID=1\n"
                               "CLIPWRIGHT_MAX_USER_BYTES=250000000\nCLIPWRIGHT_RETENTION_DAYS=2.5\n")
    cfg = load_config(str(path))
    assert (cfg.max_user_bytes, cfg.retention_days) == (250_000_000, 2.5)


def test_load_config_warns_on_bad_mode(tmp_path, caplog) -> None:
    path = write_env(tmp_path, mode=0o644)
    caplog.set_level(logging.WARNING, logger="clipwrightd.config")
    cfg = load_config(str(path))
    assert cfg.owner_id == 1001                         # still loads
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "0644" in warnings[0].getMessage()
    assert f"chmod 600 {path}" in warnings[0].getMessage()
    assert TOKEN not in caplog.text


def test_load_config_default_path_from_env(tmp_path, monkeypatch) -> None:
    home = tmp_path / "state"
    home.mkdir()
    write_env(home, f"CLIPWRIGHT_BOT_TOKEN={TOKEN}\nCLIPWRIGHT_OWNER_ID=7\n")
    monkeypatch.setenv("CLIPWRIGHT_HOME", str(home))

    cfg = load_config()
    assert cfg.home == str(home)
    assert cfg.friend_ids == set() and cfg.allowed == {7}
    assert config_mod.default_path() == str(home / "bot.env")


def test_load_config_home_precedence(tmp_path, monkeypatch) -> None:
    path = write_env(tmp_path, f"CLIPWRIGHT_BOT_TOKEN={TOKEN}\nCLIPWRIGHT_OWNER_ID=7\n"
                               f"CLIPWRIGHT_HOME=/srv/from-file\n")
    monkeypatch.delenv("CLIPWRIGHT_HOME", raising=False)
    assert load_config(str(path)).home == "/srv/from-file"
    monkeypatch.setenv("CLIPWRIGHT_HOME", "/srv/from-env")
    assert load_config(str(path)).home == "/srv/from-env"


@pytest.mark.parametrize("text, missing", [
    (f"CLIPWRIGHT_BOT_TOKEN={TOKEN}\n", ["CLIPWRIGHT_OWNER_ID"]),
    ("CLIPWRIGHT_OWNER_ID=1\n", ["CLIPWRIGHT_BOT_TOKEN"]),
    ("# nothing here\nCLIPWRIGHT_BOT_TOKEN=\n", ["CLIPWRIGHT_BOT_TOKEN", "CLIPWRIGHT_OWNER_ID"]),
])
def test_load_config_missing_keys(tmp_path, text, missing) -> None:
    path = write_env(tmp_path, text)
    with pytest.raises(ConfigError) as ei:
        load_config(str(path))
    msg = str(ei.value)
    for key in missing:
        assert key in msg
    assert "CLIPWRIGHT_BOT_TOKEN" in msg and "CLIPWRIGHT_OWNER_ID" in msg   # lists the required keys
    assert "CLIPWRIGHT_FRIEND_IDS" in msg                                    # and the optional one


def test_load_config_missing_file(tmp_path) -> None:
    with pytest.raises(ConfigError) as ei:
        load_config(str(tmp_path / "absent.env"))
    assert "CLIPWRIGHT_BOT_TOKEN" in str(ei.value) and "CLIPWRIGHT_OWNER_ID" in str(ei.value)


@pytest.mark.parametrize("text, fragment", [
    (f"CLIPWRIGHT_BOT_TOKEN={TOKEN}\nCLIPWRIGHT_OWNER_ID=owner\n", "CLIPWRIGHT_OWNER_ID"),
    (f"CLIPWRIGHT_BOT_TOKEN={TOKEN}\nCLIPWRIGHT_OWNER_ID=1\nCLIPWRIGHT_FRIEND_IDS=2,bob\n", "CLIPWRIGHT_FRIEND_IDS"),
    (f"CLIPWRIGHT_BOT_TOKEN={TOKEN}\nCLIPWRIGHT_OWNER_ID=1\nCLIPWRIGHT_QUEUE_DEPTH=many\n", "CLIPWRIGHT_QUEUE_DEPTH"),
    (f"CLIPWRIGHT_BOT_TOKEN={TOKEN}\nCLIPWRIGHT_OWNER_ID=1\nCLIPWRIGHT_RETENTION_DAYS=soon\n", "CLIPWRIGHT_RETENTION_DAYS"),
])
def test_load_config_bad_numbers(tmp_path, text, fragment) -> None:
    path = write_env(tmp_path, text)
    with pytest.raises(ConfigError, match=re.escape(fragment)):
        load_config(str(path))


@pytest.mark.parametrize("line", [
    "CLIPWRIGHT_QUEUE_DEPTH=0",            # RenderQueue(depth=0) would traceback in main()
    "CLIPWRIGHT_QUEUE_DEPTH=-1",
    "CLIPWRIGHT_MAX_UPLOAD_BYTES=-1",      # would refuse every video
    "CLIPWRIGHT_MAX_DURATION_S=nan",       # `duration > nan` is always False: gate silently off
    "CLIPWRIGHT_MAX_DURATION_S=inf",
    "CLIPWRIGHT_MAX_DURATION_S=0",
    "CLIPWRIGHT_PER_DAY_QUOTA=0",
    "CLIPWRIGHT_RETENTION_DAYS=0",
    "CLIPWRIGHT_RETENTION_DAYS=-inf",
])
def test_load_config_rejects_out_of_range_numbers(tmp_path, line) -> None:
    path = write_env(tmp_path, f"CLIPWRIGHT_BOT_TOKEN={TOKEN}\nCLIPWRIGHT_OWNER_ID=1\n{line}\n")
    key = line.split("=", 1)[0]
    with pytest.raises(ConfigError, match=re.escape(key)):
        load_config(str(path))


def test_load_config_accepts_small_positive_limits(tmp_path) -> None:
    path = write_env(tmp_path, f"CLIPWRIGHT_BOT_TOKEN={TOKEN}\nCLIPWRIGHT_OWNER_ID=1\n"
                               "CLIPWRIGHT_RETENTION_DAYS=0.5\nCLIPWRIGHT_QUEUE_DEPTH=1\n"
                               "CLIPWRIGHT_MAX_DURATION_S=2.5\n")
    cfg = load_config(str(path))
    assert (cfg.retention_days, cfg.queue_depth, cfg.max_duration_s) == (0.5, 1, 2.5)


@pytest.mark.parametrize("line", [
    "CLIPWRIGHT_OWNER_ID=000:DUMMYSECRETVALUE",
    "CLIPWRIGHT_OWNER_ID=1\nCLIPWRIGHT_FRIEND_IDS=2,000:DUMMYSECRETVALUE",
    "CLIPWRIGHT_OWNER_ID=1\nCLIPWRIGHT_QUEUE_DEPTH=000:DUMMYSECRETVALUE",
    "CLIPWRIGHT_OWNER_ID=1\nCLIPWRIGHT_RETENTION_DAYS=000:DUMMYSECRETVALUE",
])
def test_config_error_never_echoes_the_value(tmp_path, line) -> None:
    """A token mis-pasted into an id key must not land in stderr or the journal."""
    path = write_env(tmp_path, f"CLIPWRIGHT_BOT_TOKEN={TOKEN}\n{line}\n")
    with pytest.raises(ConfigError) as ei:
        load_config(str(path))
    assert "DUMMYSECRETVALUE" not in str(ei.value)
    assert TOKEN not in str(ei.value)
    assert line.rsplit("\n", 1)[-1].split("=", 1)[0] in str(ei.value)   # the key is named


def test_load_config_unreadable_file_is_a_config_error(tmp_path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can read a mode-0000 file")
    path = write_env(tmp_path, mode=0o000)
    try:
        with pytest.raises(ConfigError, match="cannot read"):
            load_config(str(path))
    finally:
        os.chmod(path, 0o600)


def test_load_config_non_utf8_file_is_a_config_error(tmp_path) -> None:
    path = tmp_path / "bot.env"
    path.write_bytes(b"CLIPWRIGHT_BOT_TOKEN=\xff\xfe\x00broken\n")
    os.chmod(path, 0o600)
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(str(path))


def test_load_config_expands_and_absolutises_home(tmp_path, monkeypatch) -> None:
    """``CLIPWRIGHT_HOME=~/.clipwright`` must mean the home directory, not a literal ``~`` under cwd."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLIPWRIGHT_HOME", raising=False)
    path = write_env(tmp_path, f"CLIPWRIGHT_BOT_TOKEN={TOKEN}\nCLIPWRIGHT_OWNER_ID=7\n"
                               "CLIPWRIGHT_HOME=~/.clipwright\n")
    assert load_config(str(path)).home == str(tmp_path / ".clipwright")

    monkeypatch.setenv("CLIPWRIGHT_HOME", "~/from-env")
    assert load_config(str(path)).home == str(tmp_path / "from-env")
    assert config_mod.home_dir() == str(tmp_path / "from-env")

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("CLIPWRIGHT_HOME", "relative/state")
    assert load_config(str(path)).home == str(cwd / "relative" / "state")
    assert config_mod.default_path() == str(cwd / "relative" / "state" / "bot.env")
