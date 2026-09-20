"""clipwrightd.api — a stdlib Telegram Bot API client.

One class, ``BotAPI``, with two primitives and a thin method per Bot API
call the daemon needs:

- ``call(method, **params)`` posts a JSON body to ``<base>/bot<token>/<method>``.
- ``upload(method, files, **params)`` posts ``multipart/form-data`` built by
  hand (``uuid`` boundary, no ``email`` package, no third-party lib). Files
  are ``{field: (filename, bytes_or_path, mime)}``; every other param is a
  text field, with dicts/lists JSON-encoded so ``reply_markup`` and
  ``InputMedia`` travel the way Telegram expects.

The wire is an injectable ``transport(url, data, headers, timeout)`` that
returns ``bytes`` or a file-like object. The default is ``urllib.request``
with a 65 s timeout — long polling asks for 50 s, and the socket must
outlive it. A socket timeout is the total deadline for ``sendall``, so an
upload's timeout grows with its body (``UPLOAD_MIN_RATE``): an 8 MB export
must not fail on any uplink slower than 125 KB/s. Tests substitute a
recording fake and never open a socket.

The token is part of every URL, so it is the one thing this module must
never leak: ``redact()`` scrubs it from every ``BotAPIError`` and log line.

Every touch of the wire — opening the request, reading the body, streaming
a download — goes through ``_guarded``, so the only exception this module
lets out is ``BotAPIError``. ``urlopen`` returns once the headers arrive; a
reset or stall while the body is still coming raises ``TimeoutError``,
``ConnectionResetError``, ``IncompleteRead`` and friends, and a daemon that
sits unattended for days must treat those as one more retry, not a crash.
A body cut short by a clean close does not raise (``read(n)`` just returns
``b""``), so ``download_file`` checks what arrived against ``Content-Length``
and treats a shortfall as a failure too — never a silently truncated upload.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from typing import IO, Any, TypeVar

log = logging.getLogger("clipwrightd.api")

DEFAULT_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT = 65.0     # > 50 s long-poll wait, with headroom
POLL_HEADROOM_S = 15.0
UPLOAD_MIN_RATE = 20_000   # bytes/s the uplink is assumed to manage; sizes the upload deadline
CHUNK = 64 * 1024
REDACTED = "<token>"

Transport = Callable[[str, bytes | None, dict[str, str], float], bytes | IO[bytes]]
FileSpec = tuple[str, bytes | str, str]      # (filename, bytes or path, mime)
T = TypeVar("T")


class BotAPIError(RuntimeError):
    """Telegram answered ``ok: false``, the transport failed, or a limit was hit."""

    def __init__(self, description: str, error_code: int | None = None,
                 retry_after: int | None = None) -> None:
        super().__init__(description)
        self.description = description
        self.error_code = error_code
        self.retry_after = retry_after


def redact(token: str, text: str) -> str:
    """Replace every occurrence of ``token`` (raw or URL-quoted) in ``text``."""
    if not token:
        return text
    text = text.replace(token, REDACTED)
    quoted = urllib.parse.quote(token, safe="")
    if quoted != token:
        text = text.replace(quoted, REDACTED)
    return text


def default_transport(url: str, data: bytes | None, headers: dict[str, str],
                      timeout: float) -> IO[bytes]:
    """POST (or GET when ``data`` is None) via urllib; returns the response.

    A 4xx/5xx is returned rather than raised: Telegram puts its
    ``{"ok": false, ...}`` JSON in that body and ``call()`` reads it.
    """
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as err:
        return err


def encode_multipart(boundary: str, fields: dict[str, str],
                     files: dict[str, tuple[str, bytes, str]]) -> bytes:
    """Build a ``multipart/form-data`` body: text ``fields`` then ``files``."""
    out = bytearray()
    for name, value in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += value.encode("utf-8") + b"\r\n"
    for name, (filename, payload, mime) in files.items():
        safe_name = filename.replace("\\", "\\\\").replace('"', '\\"')
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                f'filename="{safe_name}"\r\nContent-Type: {mime}\r\n\r\n').encode()
        out += payload + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out)


def _field_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _file_bytes(payload: bytes | str) -> bytes:
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return bytes(payload)
    if not os.path.isfile(payload):
        raise FileNotFoundError(payload)
    with open(payload, "rb") as fh:
        return fh.read()


def _mime_for(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _drop_none(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}


def _reply_params(reply_to_message_id: int | None) -> dict[str, Any]:
    """The reply fields of a send: the target, and leave to send plainly once that message is gone.

    Without ``allow_sending_without_reply`` Telegram answers a reply to a
    deleted message with a 400, which would strand a group session whose
    ``/gif`` was tidied away. Both are None for a DM, so ``_drop_none``
    leaves that wire payload exactly as before.
    """
    if reply_to_message_id is None:
        return {"reply_to_message_id": None, "allow_sending_without_reply": None}
    return {"reply_to_message_id": reply_to_message_id, "allow_sending_without_reply": True}


def _content_length(resp: Any) -> int | None:
    """The response's announced body size, or None when it has no usable header."""
    headers = getattr(resp, "headers", None)
    get = getattr(headers, "get", None)
    if get is None:
        return None
    try:
        value = get("Content-Length")
        size = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return size if size is not None and size >= 0 else None


class BotAPI:
    """Bot API client bound to one token. Never logs or raises the token."""

    def __init__(self, token: str, transport: Transport | None = None,
                 base: str = DEFAULT_BASE, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._token = token
        self.transport: Transport = transport or default_transport
        self.base = base.rstrip("/")
        self.timeout = timeout

    # --- primitives ---------------------------------------------------------

    def call(self, method: str, **params: Any) -> Any:
        """POST JSON to ``<base>/bot<token>/<method>``; return ``result``."""
        return self._call(method, params, self.timeout)

    def upload(self, method: str, files: dict[str, FileSpec], **params: Any) -> Any:
        """POST multipart: ``files`` as file parts, ``params`` as text fields."""
        boundary = uuid.uuid4().hex
        fields = {k: _field_text(v) for k, v in _drop_none(params).items()}
        parts = {name: (filename, _file_bytes(payload), mime)
                 for name, (filename, payload, mime) in files.items()}
        body = encode_multipart(boundary, fields, parts)
        raw = self._request(self._method_url(method), body,
                            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
                            self.upload_timeout(len(body)))
        return self._result(method, raw)

    def upload_timeout(self, body_len: int) -> float:
        """The deadline for one multipart POST: at least ``timeout``, more for a big body."""
        return max(self.timeout, body_len / UPLOAD_MIN_RATE)

    # --- methods ------------------------------------------------------------

    def get_updates(self, offset: int | None, timeout: int = 50,
                    allowed_updates: list[str] | None = None) -> list[dict]:
        """Long-poll; the transport timeout is stretched past ``timeout``."""
        params = {"offset": offset, "timeout": timeout, "allowed_updates": allowed_updates}
        return self._call("getUpdates", params, max(self.timeout, timeout + POLL_HEADROOM_S))

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None,
                     reply_to_message_id: int | None = None,
                     parse_mode: str | None = None) -> dict:
        """``reply_markup`` is the full markup dict, e.g. ``{"inline_keyboard": rows}``."""
        return self.call("sendMessage", chat_id=chat_id, text=text, reply_markup=reply_markup,
                         parse_mode=parse_mode, **_reply_params(reply_to_message_id))

    def send_animation(self, chat_id: int, path_or_bytes: bytes | str, caption: str | None = None,
                       reply_markup: dict | None = None, filename: str = "preview.mp4",
                       reply_to_message_id: int | None = None) -> dict:
        return self.upload("sendAnimation",
                           {"animation": (filename, path_or_bytes, _mime_for(filename))},
                           chat_id=chat_id, caption=caption, reply_markup=reply_markup,
                           **_reply_params(reply_to_message_id))

    def send_document(self, chat_id: int, path_or_bytes: bytes | str, filename: str,
                      caption: str | None = None, reply_markup: dict | None = None,
                      disable_content_type_detection: bool | None = None,
                      reply_to_message_id: int | None = None) -> dict:
        """``disable_content_type_detection=True`` keeps a ``.gif`` a document: without it
        Telegram re-types the upload as an animation and delivers an MP4 transcode."""
        return self.upload("sendDocument",
                           {"document": (filename, path_or_bytes, _mime_for(filename))},
                           chat_id=chat_id, caption=caption, reply_markup=reply_markup,
                           disable_content_type_detection=disable_content_type_detection,
                           **_reply_params(reply_to_message_id))

    def edit_message_media(self, chat_id: int, message_id: int, path_or_bytes: bytes | str,
                           filename: str, caption: str | None = None,
                           reply_markup: dict | None = None) -> dict:
        """Swap the preview in place: an ``InputMediaAnimation`` via ``attach://file``."""
        media: dict[str, Any] = {"type": "animation", "media": "attach://file"}
        if caption is not None:
            media["caption"] = caption
        return self.upload("editMessageMedia",
                           {"file": (filename, path_or_bytes, _mime_for(filename))},
                           chat_id=chat_id, message_id=message_id, media=media,
                           reply_markup=reply_markup)

    def send_chat_action(self, chat_id: int, action: str) -> bool:
        return self.call("sendChatAction", chat_id=chat_id, action=action)

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> bool:
        return self.call("answerCallbackQuery", callback_query_id=callback_query_id, text=text)

    def set_my_commands(self, commands: list[dict[str, str]]) -> bool:
        """``commands`` is a list of ``{"command": ..., "description": ...}``."""
        return self.call("setMyCommands", commands=commands)

    def get_me(self) -> dict:
        """The bot's own User object (``username`` lets a group's ``/cmd@name`` be told ours from another bot's)."""
        return self.call("getMe")

    def get_file(self, file_id: str) -> dict:
        return self.call("getFile", file_id=file_id)

    def download_file(self, file_path: str, dest: str, max_bytes: int) -> int:
        """Stream ``<base>/file/bot<token>/<file_path>`` to ``dest``; return its size.

        Raises ``BotAPIError`` as soon as more than ``max_bytes`` arrive; the
        partial file is removed. Telegram's ``file_path`` comes from
        ``get_file`` and is only ever used in the URL, never on disk.
        """
        url = f"{self.base}/file/bot{self._token}/{urllib.parse.quote(file_path, safe='/')}"
        resp = self._open(url, None, {}, self.timeout)
        try:
            status = getattr(resp, "status", 200)
            if status >= 400:
                body = self._guarded(lambda: self._read_all(resp))
                raise BotAPIError(self._error_text("download", body), status)
            expected = _content_length(resp)
            total = self._guarded(lambda: self._stream_to(resp, dest, max_bytes, expected))
        finally:
            self._close(resp)
        log.debug("download_file: %d bytes -> %s", total, dest)
        return total

    # --- internals ----------------------------------------------------------

    def _method_url(self, method: str) -> str:
        return f"{self.base}/bot{self._token}/{method}"

    def _call(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        body = json.dumps(_drop_none(params), ensure_ascii=False).encode("utf-8")
        raw = self._request(self._method_url(method), body,
                            {"Content-Type": "application/json"}, timeout)
        return self._result(method, raw)

    def _guarded(self, fn: Callable[[], T]) -> T:
        """Run one wire interaction; whatever it raises comes out as a redacted ``BotAPIError``."""
        try:
            return fn()
        except BotAPIError as err:
            raise BotAPIError(redact(self._token, err.description), err.error_code,
                              err.retry_after) from None
        except Exception as err:
            raise BotAPIError(redact(self._token, f"transport failure: {err}")) from None

    def _open(self, url: str, data: bytes | None, headers: dict[str, str],
              timeout: float) -> bytes | IO[bytes]:
        return self._guarded(lambda: self.transport(url, data, headers, timeout))

    def _request(self, url: str, data: bytes | None, headers: dict[str, str],
                 timeout: float) -> bytes:
        resp = self._open(url, data, headers, timeout)
        try:
            return self._guarded(lambda: self._read_all(resp))
        finally:
            self._close(resp)

    def _result(self, method: str, raw: bytes) -> Any:
        try:
            payload = json.loads(raw)
        except ValueError:
            raise BotAPIError(f"{method}: non-JSON response ({len(raw)} bytes)") from None
        if not isinstance(payload, dict) or not payload.get("ok"):
            info = payload if isinstance(payload, dict) else {}
            raise BotAPIError(self._error_text(method, payload), info.get("error_code"),
                              (info.get("parameters") or {}).get("retry_after"))
        log.debug("%s: ok", method)
        return payload.get("result")

    def _error_text(self, method: str, payload: Any) -> str:
        if isinstance(payload, (bytes, bytearray)):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = payload[:200].decode("utf-8", "replace")
        if isinstance(payload, dict):
            description = payload.get("description", "unknown error")
        else:
            description = str(payload)
        text = redact(self._token, f"{method}: {description}")
        log.warning("%s", text)
        return text

    def _stream_to(self, resp: bytes | IO[bytes], dest: str, max_bytes: int,
                   expected: int | None = None) -> int:
        """Copy ``resp`` to ``dest`` chunk by chunk; on any failure remove the partial file.

        ``expected`` is the announced ``Content-Length``: fewer bytes than that
        means the connection closed early, and the short file is an error.
        """
        total = 0
        try:
            with open(dest, "wb") as out:
                for chunk in self._chunks(resp):
                    total += len(chunk)
                    if total > max_bytes:
                        raise BotAPIError(f"download exceeds {max_bytes} bytes; aborted", 413)
                    out.write(chunk)
            if expected is not None and total < expected:
                raise BotAPIError(f"download truncated: {total} of {expected} bytes arrived")
        except BaseException:
            if os.path.exists(dest):
                os.remove(dest)
            raise
        return total

    @staticmethod
    def _read_all(resp: bytes | IO[bytes]) -> bytes:
        if isinstance(resp, (bytes, bytearray)):
            return bytes(resp)
        return resp.read()

    @staticmethod
    def _chunks(resp: bytes | IO[bytes]) -> Iterator[bytes]:
        if isinstance(resp, (bytes, bytearray)):
            yield bytes(resp)
            return
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                return
            yield chunk

    @staticmethod
    def _close(resp: Any) -> None:
        close = getattr(resp, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception as err:
            log.debug("closing the response failed: %s", err)
