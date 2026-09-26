"""clipwrightd.poll + clipwrightd.queue — offline, with a recording fake Bot API.

Nothing here opens a socket or runs ffmpeg: ``FakeAPI`` stands in for
``BotAPI`` (same method names, records every call), ``FakeCook`` writes
tiny real media files plus the sidecar into the daemon's render dir the way
``clipwright.cook`` does, and ``FakeProbe`` answers with a ``Probe``. The
daemon's queue runs inline so each update is fully handled by the time
``handle_update`` returns; the tests about admission swap in a threaded
queue whose worker is parked on an event.
"""

from __future__ import annotations

import copy
import io
import itertools
import json
import logging
import os
import re
import shlex
import shutil
import signal
import sqlite3
import threading
import time
from dataclasses import replace

import pytest
from PIL import Image

from clipwright import recipe
from clipwright.ffmpeg import FFmpegError, Probe
from clipwright.pipelines import CookResult
from clipwrightd import poll as poll_mod
from clipwrightd.api import BotAPI, BotAPIError
from clipwrightd.config import Config
from clipwrightd.keyboards import MAX_CB_BYTES, decode_cb
from clipwrightd.poll import (ANON_HINT, CLIP_END, COMMANDS, GRID_TOAST, GROUP_OFF, GROUP_START,
                              HELP_TEXT, NAG_COOLDOWN_S, QUEUE_FULL, SEGMENT_CAP_S, SHUT_DOWN_MID_RENDER,
                              STALE_BUTTON, STILL_RENDERING, USERNAME_RETRY_S, BARE_CAPTIONS, Daemon,
                              DaemonAlreadyRunning, main)
from clipwrightd.fetch import FetchError
from clipwrightd.queue import RenderQueue
from clipwrightd.session import Store

OWNER = 1001
FRIEND = 1002
STRANGER = 4242
GUEST = 4243          # a group member off the allowlist
GUEST2 = 4244
CHAT = 555
GROUP = -100777       # a supergroup the bot is in
CHANNEL = -100200     # a broadcast channel (Test2-style); members are trusted like weir/bencho
BOT_NAME = "clipwright_bot"             # as the daemon keeps it: lowercased from getMe's "Clipwright_Bot"
CAP = 1_000_000
COOKBOOK = recipe.load_cookbook()
DAY_S = 86400.0

_ids = itertools.count(1)


# -- fakes ----------------------------------------------------------------------

class Spinning(BaseException):
    """Raised by FakeAPI once a run() loop has polled an exhausted batch list too often.

    A BaseException so the daemon's retry-on-Exception loop cannot swallow it;
    the alternative was the poll loop growing ``calls`` until the VM ran out of memory.
    """


class FakeAPI:
    """Records every Bot API call; returns Telegram-shaped results; can fail on demand.

    ``fail[method]`` raises on every call of ``method``, or only on the
    ``fail_at[method]``-th (1-based) call when that is set too.
    """

    def __init__(self, upload_src: str) -> None:
        self.upload_src = upload_src
        self.calls: list[tuple[str, dict]] = []
        self.batches: list = []           # what successive get_updates calls return (or raise)
        self.fail: dict[str, Exception] = {}
        self.fail_at: dict[str, int] = {}
        self.sent_docs: list[dict] = []   # the Document objects returned by send_document
        self.members: dict[tuple[int, int], dict | BaseException] = {}
        self._msg_id = 100

    def of(self, method: str) -> list[dict]:
        return [kw for m, kw in self.calls if m == method]

    def texts(self) -> list[str]:
        return [kw["text"] for kw in self.of("sendMessage")]

    def _record(self, method: str, **kw: object) -> None:
        self.calls.append((method, kw))
        if method in self.fail and self.fail_at.get(method, len(self.of(method))) == len(self.of(method)):
            raise self.fail[method]

    def _next(self) -> int:
        self._msg_id += 1
        return self._msg_id

    def get_updates(self, offset, timeout=50, allowed_updates=None):
        self._record("getUpdates", offset=offset, timeout=timeout)
        if not self.batches:                  # run() loops on empty batches: a spinning test must die, not eat RAM
            self.idle_polls = getattr(self, "idle_polls", 0) + 1
            if self.idle_polls > 50:          # BaseException: run() retries any Exception from get_updates
                raise Spinning("daemon.run() kept polling after the fake batches ran out")
            return []
        batch = self.batches.pop(0)
        if isinstance(batch, BaseException):
            raise batch
        return batch

    def send_message(self, chat_id, text, reply_markup=None, reply_to_message_id=None, parse_mode=None):
        self._record("sendMessage", chat_id=chat_id, text=text, reply_markup=reply_markup,
                     reply_to_message_id=reply_to_message_id)
        return {"message_id": self._next()}

    def send_animation(self, chat_id, path_or_bytes, caption=None, reply_markup=None, filename="preview.mp4",
                       reply_to_message_id=None):
        assert os.path.isfile(path_or_bytes), path_or_bytes
        self._record("sendAnimation", chat_id=chat_id, path=path_or_bytes, caption=caption,
                     reply_markup=reply_markup, filename=filename, reply_to_message_id=reply_to_message_id)
        mid = self._next()
        return {"message_id": mid, "animation": {"file_id": f"A{mid}", "file_unique_id": f"UA{mid}"}}

    def send_document(self, chat_id, path_or_bytes, filename, caption=None, reply_markup=None,
                      disable_content_type_detection=None, reply_to_message_id=None):
        assert os.path.isfile(path_or_bytes), path_or_bytes
        with open(path_or_bytes, "rb") as fh:
            content = fh.read()                # the daemon removes the file once it is sent
        self._record("sendDocument", chat_id=chat_id, path=path_or_bytes, filename=filename,
                     caption=caption, content=content,
                     disable_content_type_detection=disable_content_type_detection,
                     reply_to_message_id=reply_to_message_id)
        mid = self._next()
        doc = {"file_id": f"D{mid}", "file_unique_id": f"UD{mid}", "file_name": filename}
        self.sent_docs.append(doc)
        return {"message_id": mid, "document": doc}

    def edit_message_media(self, chat_id, message_id, path_or_bytes, filename, caption=None, reply_markup=None):
        assert os.path.isfile(path_or_bytes), path_or_bytes
        self._record("editMessageMedia", chat_id=chat_id, message_id=message_id, path=path_or_bytes,
                     caption=caption, reply_markup=reply_markup)
        return {"message_id": message_id}

    def send_chat_action(self, chat_id, action):
        self._record("sendChatAction", chat_id=chat_id, action=action)
        return True

    def answer_callback_query(self, callback_query_id, text=None):
        self._record("answerCallbackQuery", callback_query_id=callback_query_id, text=text)
        return True

    def set_my_commands(self, commands):
        self._record("setMyCommands", commands=commands)
        return True

    def get_me(self):
        self._record("getMe")
        return {"id": 42, "is_bot": True, "first_name": "Clipwright", "username": "Clipwright_Bot"}

    def get_chat_member(self, chat_id, user_id):
        self._record("getChatMember", chat_id=chat_id, user_id=user_id)
        result = self.members.get((chat_id, user_id), {"user": {"id": user_id}, "status": "left"})
        if isinstance(result, BaseException):
            raise result
        return result

    def get_file(self, file_id):
        self._record("getFile", file_id=file_id)
        return {"file_id": file_id, "file_unique_id": f"U{file_id}", "file_path": f"videos/{file_id}.mp4"}

    def download_file(self, file_path, dest, max_bytes):
        self._record("downloadFile", file_path=file_path, dest=dest, max_bytes=max_bytes)
        shutil.copyfile(self.upload_src, dest)
        return os.path.getsize(dest)


class FakeProbe:
    """``probe_fn`` stand-in: answers ``result`` or raises ``fail``."""

    def __init__(self) -> None:
        self.result = Probe(path="", duration=3.0, width=320, height=240, fps=30.0, nb_frames=90,
                            vcodec="h264", has_audio=True, size_bytes=1234)
        self.fail: Exception | None = None
        self.paths: list[str] = []

    def __call__(self, path: str) -> Probe:
        self.paths.append(path)
        if self.fail is not None:
            raise self.fail
        return replace(self.result, path=path)


class FakeCook:
    """``cook_fn`` stand-in: copies tiny real media into ``out_dir``, writes the sidecar, records the call.

    Like the engine, the sidecar names the input by ``public_input`` and the
    report carries ``sidecar``; ``report_extra`` lets a test add ``from``/
    ``to``/``capped`` and ``delay`` makes a cook take a while.
    """

    def __init__(self, mp4: str, gif: str) -> None:
        self.mp4, self.gif = mp4, gif
        self.calls: list[tuple[dict, str, bool]] = []
        self.public_inputs: list[str | None] = []
        self.fail: Exception | None = None
        self.report_extra: dict = {}
        self.delay = 0.0

    def __call__(self, inst: dict, *, out_dir: str, proxy: bool, cookbook: dict,
                 public_input: str | None = None) -> CookResult:
        self.calls.append((copy.deepcopy(inst), out_dir, proxy))
        self.public_inputs.append(public_input)
        if self.fail is not None:
            raise self.fail
        if self.delay:
            time.sleep(self.delay)
        assert os.path.isdir(out_dir)
        stem = f"{inst['recipe']}-{len(self.calls)}"
        mp4 = shutil.copyfile(self.mp4, os.path.join(out_dir, stem + ".mp4"))
        gif = None if proxy else shutil.copyfile(self.gif, os.path.join(out_dir, stem + ".gif"))
        shown = inst if public_input is None else {**inst, "input": public_input}
        sidecar = os.path.join(out_dir, stem + ".recipe.toml")
        recipe.dump_instance(shown, sidecar)
        report = {"bytes": os.path.getsize(gif) if gif else 0, "fits": True, "budget": 8_000_000,
                  "sidecar": sidecar, **self.report_extra}
        return CookResult(gif=gif, mp4=mp4, report=report, argv_log=[])


class FakeFetch:
    """``fetch_fn`` stand-in: copies ``src`` into dest_dir, or raises ``fail``. Never opens a socket."""

    def __init__(self, src: str | None = None) -> None:
        self.src = src
        self.fail: Exception | None = FetchError("mocked offline")
        self.queries: list[tuple[str, str]] = []

    def __call__(self, query: str, dest_dir: str) -> str:
        self.queries.append((query, dest_dir))
        if self.fail is not None:
            raise self.fail
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "fetched.jpg")
        shutil.copyfile(self.src, dest)
        return dest


# -- update builders --------------------------------------------------------------

def _from(uid: int) -> dict:
    return {"id": uid, "is_bot": False, "first_name": "T"}


def _chat(chat: int) -> dict:
    return {"id": chat, "type": "supergroup" if chat < 0 else "private"}


def msg(text: str | None = None, uid: int = OWNER, chat: int = CHAT, **extra: object) -> dict:
    m: dict = {"message_id": next(_ids), "from": _from(uid), "chat": _chat(chat), "date": 0}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return {"update_id": next(_ids), "message": m}


def gmsg(text: str | None = None, uid: int = GUEST, **extra: object) -> dict:
    """A message in the GROUP (a supergroup), from a guest unless told otherwise."""
    return msg(text, uid=uid, chat=GROUP, **extra)


def answer(text: str, uid: int = GUEST, bot: str = "Clipwright_Bot", chat: int = GROUP) -> dict:
    """A reply to one of the bot's force-reply prompts, as a client sends it (in the GROUP by default)."""
    prompt = {"message_id": next(_ids), "from": {"id": 42, "is_bot": True, "username": bot}, "text": "Send the …"}
    return msg(text, uid=uid, chat=chat, reply_to_message=prompt)


def gif_cmd(uid: int = GUEST, chat: int = GROUP, text: str = "/gif", **kw: object) -> dict:
    """``/gif`` in reply to a video message in ``chat``."""
    return msg(text, uid=uid, chat=chat, reply_to_message={"message_id": next(_ids), **video(**kw)})


def _channel_chat(chat: int = CHANNEL, title: str = "Test2") -> dict:
    return {"id": chat, "type": "channel", "title": title}


def channel_post(text: str | None = None, uid: int | None = GUEST, chat: int = CHANNEL,
                 **extra: object) -> dict:
    """A signed ``channel_post`` (Test2 / weir / bencho style). ``uid is None`` is unsigned."""
    m: dict = {"message_id": next(_ids), "chat": _channel_chat(chat), "date": 0,
               "sender_chat": _channel_chat(chat)}
    if uid is not None:
        m["from"] = _from(uid)
    if text is not None:
        m["text"] = text
    m.update(extra)
    return {"update_id": next(_ids), "channel_post": m}


def channel_gif(uid: int | None = GUEST, chat: int = CHANNEL, text: str = "/gif", **kw: object) -> dict:
    """``/gif`` as a channel_post, optionally in reply to a video."""
    return channel_post(text, uid=uid, chat=chat, reply_to_message={"message_id": next(_ids), **video(**kw)})


def cb(data: str, uid: int = OWNER, chat: int = CHAT, message_id: int = 101) -> dict:
    return {"update_id": next(_ids), "callback_query": {
        "id": f"cq{next(_ids)}", "from": _from(uid), "data": data,
        "message": {"message_id": message_id, "chat": _chat(chat)}}}


def video(size: int = 5000, mime: str = "video/mp4", key: str = "video") -> dict:
    return {key: {"file_id": "vid1", "file_unique_id": "Uvid1", "file_size": size, "mime_type": mime,
                  "width": 320, "height": 240, "duration": 3}}


def photo(size: int = 4000) -> dict:
    """A Telegram photo message payload: two sizes, the larger one is what we ingest."""
    return {"photo": [
        {"file_id": "ph1", "file_unique_id": "Uph1", "width": 90, "height": 56, "file_size": 400},
        {"file_id": "ph2", "file_unique_id": "Uph2", "width": 320, "height": 200, "file_size": size},
    ]}


def recipe_switch_row(current: str) -> list[str]:
    """The first keyboard row of four recipe buttons, with ``current`` marked."""
    names = [("🪃", "boomerang"), ("💬", "caption-loop"), ("🎞️", "gifify"), ("📷", "ken-burns")]
    return [f"{'• ' if n == current else ''}{e} {n}" for e, n in names]


def keyboard_of(markup: dict) -> list[list[dict]]:
    assert set(markup) == {"inline_keyboard"}
    return markup["inline_keyboard"]


def buttons_of(markup: dict) -> list[str]:
    return [b["text"] for row in keyboard_of(markup) for b in row]


def token_of(markup: dict) -> str:
    return decode_cb(keyboard_of(markup)[0][0]["callback_data"]).session


def remix_reply(unique_id: str) -> dict:
    return {"message_id": 9, "document": {"file_id": "x", "file_unique_id": unique_id}}


# -- fixtures ----------------------------------------------------------------------

@pytest.fixture
def media(tmp_path) -> tuple[str, str]:
    """A tiny real GIF (Pillow) and a tiny MP4-shaped file, used as upload and render output."""
    gif = str(tmp_path / "tiny.gif")
    frames = [Image.new("RGB", (8, 8), c) for c in ("red", "blue")]
    frames[0].save(gif, save_all=True, append_images=frames[1:], loop=0, duration=100)
    mp4 = str(tmp_path / "tiny.mp4")
    with open(mp4, "wb") as fh:
        fh.write(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41" + b"\x00" * 64)
    return mp4, gif


@pytest.fixture
def home(tmp_path) -> str:
    return str(tmp_path / "home")


@pytest.fixture
def cfg(home) -> Config:
    return Config(token="123:abc", owner_id=OWNER, friend_ids={FRIEND}, home=home,
                  max_upload_bytes=CAP, max_duration_s=10.0, max_dim=640, per_day_quota=1, queue_depth=4)


@pytest.fixture
def api(media) -> FakeAPI:
    return FakeAPI(upload_src=media[0])


@pytest.fixture
def cook(media) -> FakeCook:
    return FakeCook(*media)


@pytest.fixture
def probe() -> FakeProbe:
    return FakeProbe()


@pytest.fixture
def store(home) -> Store:
    s = Store(os.path.join(home, "state.db"))
    yield s
    s.close()


@pytest.fixture
def still(tmp_path) -> str:
    path = str(tmp_path / "still.jpg")
    Image.new("RGB", (64, 48), (10, 40, 80)).save(path, format="JPEG")
    return path


@pytest.fixture
def fetch(still) -> FakeFetch:
    return FakeFetch(src=still)


@pytest.fixture
def daemon(cfg, api, store, cook, probe, fetch) -> Daemon:
    return Daemon(cfg, api, store, RenderQueue(depth=cfg.queue_depth, inline=True), COOKBOOK,
                  cook_fn=cook, probe_fn=probe, fetch_fn=fetch)


def upload(daemon: Daemon, api: FakeAPI, uid: int = OWNER, **kw: object) -> str:
    """Send a valid video through the daemon; return the new session's token (from its keyboard)."""
    daemon.handle_update(msg(uid=uid, **video(**kw)))
    return token_of(api.of("sendAnimation")[-1]["reply_markup"])


def gif_in_group(daemon: Daemon, api: FakeAPI, uid: int = GUEST, **kw: object) -> tuple[str, int]:
    """``/gif`` on a video in the GROUP; return (session token, the /gif message's id)."""
    update = gif_cmd(uid=uid, **kw)
    daemon.handle_update(update)
    return token_of(api.of("sendAnimation")[-1]["reply_markup"]), update["message"]["message_id"]


@pytest.fixture
def park(daemon):
    """A callable that swaps in a threaded depth-1 queue with its worker parked on OWNER's job.

    Returns the gate that releases the worker; every other user then sees a
    full queue. Call it after any inline ``upload`` the test needs first.
    """
    gate = threading.Event()

    def start() -> threading.Event:
        daemon.queue = RenderQueue(depth=1)
        assert daemon.queue.submit(OWNER, gate.wait) == 1
        return gate

    yield start
    gate.set()
    daemon.queue.stop()


def release(daemon: Daemon, gate: threading.Event) -> None:
    """Let the parked worker finish, then put an inline queue back so later updates run synchronously."""
    gate.set()
    daemon.queue.stop()
    daemon.queue = RenderQueue(depth=1, inline=True)


# -- allowlist and commands --------------------------------------------------------

def test_stranger_gets_silence_logged_once(daemon, api, caplog):
    with caplog.at_level(logging.WARNING, logger="clipwrightd.poll"):
        daemon.handle_update(msg("/start", uid=STRANGER))
        daemon.handle_update(msg(uid=STRANGER, **video()))
        daemon.handle_update(cb("a/AAAAAA/grid", uid=STRANGER))
    assert api.calls == []
    assert sum("non-allowlisted" in r.getMessage() for r in caplog.records) == 1


def test_start_help_recipes_reply(daemon, api):
    daemon.handle_update(msg("/start@clipwright_bot"))
    daemon.handle_update(msg("/help", uid=FRIEND))
    daemon.handle_update(msg("/recipes"))
    daemon.handle_update(msg("/bogus"))
    start, help_, recipes, bogus = api.texts()
    assert "Send a photo" in start and "1.0 MB" in start and "10 s" in start
    assert "Grok Bot" in start and "clipwright-grok" in start
    assert "/remix" in help_ and f"{SEGMENT_CAP_S:g} s" in help_
    assert "gifify" in recipes and "caption-loop" in recipes
    assert "/bogus" in bogus
    assert all(kw["chat_id"] == CHAT for kw in api.of("sendMessage"))


def test_non_video_message_gets_a_hint(daemon, api):
    daemon.handle_update(msg(sticker={"file_id": "s"}))
    daemon.handle_update(msg("just some words"))
    assert all("video" in t for t in api.texts()) and len(api.texts()) == 2


# -- groups: /gif, guests, replies ------------------------------------------------------

SOMEONE_ELSE = "That's someone else's session."


def test_group_gif_from_a_guest_downloads_probes_and_previews_as_a_reply(daemon, api, store, cook, probe):
    token, asked = gif_in_group(daemon, api)             # GUEST is not on the allowlist

    (dl,) = api.of("downloadFile")
    assert api.of("getFile") == [{"file_id": "vid1"}] and dl["max_bytes"] == CAP
    assert os.path.dirname(dl["dest"]) == os.path.join(daemon.home, "uploads", str(GUEST))
    assert probe.paths == [dl["dest"]]
    sess = store.get(token)
    assert (sess.user_id, sess.chat_id, sess.origin_message_id) == (GUEST, GROUP, asked)
    assert sess.recipe["input"] == dl["dest"] and cook.calls[-1][2] is True

    (sent,) = api.of("sendAnimation")
    assert sent["chat_id"] == GROUP and sent["reply_to_message_id"] == asked
    assert sess.message_id == 101                  # the preview lives in the group message
    rows = keyboard_of(sent["reply_markup"])
    assert [b["text"] for b in rows[0]] == recipe_switch_row("gifify")
    assert [b["text"] for b in rows[1]] == ["🃏 typecard"]
    assert [b["text"] for b in rows[-1]] == ["↩ Undo", "⌘ Show CLI", "⬇ Export", "🎲 Grid"]
    assert all(decode_cb(b["callback_data"]).session == token for row in rows for b in row)
    assert api.texts() == []
    assert all(b["chat_id"] == GROUP for b in api.of("sendChatAction"))


def test_group_ignores_everything_that_is_not_a_command(daemon, api):
    daemon.handle_update(gmsg(**video()))                    # a bare video, even from a guest ...
    daemon.handle_update(gmsg(uid=OWNER, **video()))         # ... or the owner: a room, not an inbox
    daemon.handle_update(gmsg("just some words"))
    daemon.handle_update(gmsg("just some words", uid=OWNER))
    daemon.handle_update(gmsg(sticker={"file_id": "s"}))
    daemon.handle_update(gmsg(new_chat_members=[_from(GUEST2)]))
    daemon.handle_update(channel_post("just some words"))     # channel chatter is not a job either
    assert api.calls == []


def test_ignored_room_text_is_kept_for_the_digest_without_a_reply(daemon, api, home):
    """Chatter stays silent in the room. The digest ring gets a short preview only."""
    from clipwrightd.ops import PREVIEW_CHARS, load_events

    token_shape = "123456789:AAHsecretTokenValueDontPrintThis000"
    daemon.config.token = token_shape
    daemon.handle_update(gmsg("did the deploy land"))
    daemon.handle_update(gmsg("x" * 400))
    daemon.handle_update(gmsg(f"leak {token_shape} please"))
    daemon.handle_update(gmsg(**video()))
    daemon.handle_update(gmsg(sticker={"file_id": "AgADSECRET"}))
    daemon.handle_update(gmsg("/otherbot do a thing"))
    daemon.handle_update(channel_post("channel aside"))
    daemon.handle_update(channel_post("unsigned", uid=None))
    daemon.handle_update(msg("dm words"))
    daemon.config.group_ids = {GROUP}
    daemon.handle_update(msg("outside the list", uid=GUEST, chat=-100999))
    assert api.of("getUpdates") == []
    assert api.texts() == ["Send a photo or a video, or /gif some words. /help for how."]
    events = load_events(home)
    chatter = [event["text_preview"] for event in events if event["kind"] == "chatter"]
    assert chatter[0] == "did the deploy land"
    assert len(chatter[1]) == PREVIEW_CHARS and chatter[1].endswith("…")
    assert token_shape not in json.dumps(events) and "<token>" in chatter[2]
    assert "channel aside" in chatter
    assert "dm words" not in chatter and "outside the list" not in chatter
    assert "unsigned" not in chatter and "/otherbot" not in "".join(chatter)
    assert "AgADSECRET" not in json.dumps(events) and "file_id" not in json.dumps(events)
    assert all(event["kind"] == "chatter" for event in events)


def test_a_prompt_reply_is_not_digest_chatter(daemon, api, store, cook, home):
    from clipwrightd.ops import load_events

    token, _asked = gif_in_group(daemon, api)
    daemon.handle_update(cb(f"r/{token}/caption-loop", uid=GUEST, chat=GROUP))
    daemon.handle_update(cb(f"t/{token}/0", uid=GUEST, chat=GROUP))
    daemon.username = BOT_NAME
    daemon.handle_update(gmsg("lol same", uid=GUEST))
    daemon.handle_update(answer("mine", uid=GUEST))
    assert store.get(token).recipe["caption"]["text"] == "mine"
    chatter = [event["text_preview"] for event in load_events(home) if event["kind"] == "chatter"]
    assert chatter == ["lol same"]
    assert api.of("getUpdates") == []


def test_session_open_seed_fail_and_render_error_are_digest_events(daemon, api, store, cook, fetch, home):
    from clipwrightd.ops import load_events

    fetch.fail = FetchError("Commons had no picture")
    daemon.handle_update(gmsg("/gif dog"))
    cook.fail = RuntimeError("palette exploded")
    daemon.handle_update(msg(**video()))
    events = load_events(home)
    kinds = [event["kind"] for event in events]
    assert "seed_fail" in kinds and kinds.count("session") == 2 and "error" in kinds
    failed = next(event for event in events if event["kind"] == "seed_fail")
    assert failed["text_preview"] == "dog" and "Commons had no picture" in failed["detail"]
    opened = next(event for event in events if event["kind"] == "session" and event["recipe"] == "typecard")
    assert opened["text_preview"] == "dog" and opened["user_id"] == GUEST
    assert store.get(opened["session"]).recipe["recipe"] == "typecard"
    err = next(event for event in events if event["kind"] == "error")
    assert "palette exploded" in err["text_preview"] and err["user_id"] == OWNER
    assert any("Couldn't fetch a picture" in text for text in api.texts())
    assert "That render didn't work out" in api.texts()[-1]
    assert "file_id" not in json.dumps(events)


def test_a_group_member_can_dm_after_being_seen_in_the_room(daemon, api, store, cook):
    """Once they /gif in the group, DMs work like a friend's: photo/video, no /gif needed."""
    gif_in_group(daemon, api)
    assert store.is_member(GUEST)
    daemon.handle_update(msg("/start", uid=GUEST))
    assert "Hi!" in api.texts()[-1] and api.of("sendMessage")[-1]["chat_id"] == CHAT
    daemon.handle_update(msg(uid=GUEST, **video()))
    dm = api.of("sendAnimation")[-1]
    assert dm["chat_id"] == CHAT and dm.get("reply_to_message_id") is None
    daemon.handle_update(gif_cmd(uid=GUEST, chat=CHAT))
    assert api.of("sendAnimation")[-1]["chat_id"] == CHAT
    daemon.handle_update(cb("a/AAAAAA/grid", uid=GUEST, chat=CHAT))
    assert "expired" in (api.of("answerCallbackQuery")[-1].get("text") or "")


def test_group_chatter_is_ignored_but_unlocks_dms(daemon, api, store):
    daemon.handle_update(gmsg("just some words"))
    assert api.calls == []
    assert store.is_member(GUEST) and store.list_groups() == [GROUP]
    daemon.handle_update(msg("/help", uid=GUEST))
    assert "/gif" in api.texts()[-1] and "DM me too" in api.texts()[-1]


def test_joiners_are_remembered_without_speaking(daemon, api, store):
    daemon.handle_update(gmsg(uid=OWNER, new_chat_members=[_from(GUEST2)]))
    assert api.calls == []
    assert store.is_member(GUEST2) and store.is_member(OWNER)
    daemon.handle_update(msg("/start", uid=GUEST2))
    assert "Hi!" in api.texts()[-1]


def test_a_signed_channel_gif_is_served_like_a_group(daemon, api, store, cook):
    """Test2-style channels are rooms: signed /gif gets a preview and trusts the member."""
    update = channel_gif()
    daemon.handle_update(update)
    (sent,) = api.of("sendAnimation")
    assert sent["chat_id"] == CHANNEL and sent["reply_to_message_id"] == update["channel_post"]["message_id"]
    token = token_of(sent["reply_markup"])
    sess = store.get(token)
    assert (sess.user_id, sess.chat_id, sess.origin_message_id) == (GUEST, CHANNEL, update["channel_post"]["message_id"])
    assert store.is_member(GUEST) and store.list_groups() == [CHANNEL]
    daemon.handle_update(msg("/start", uid=GUEST))            # later DM works
    assert "Hi!" in api.texts()[-1]


def test_bare_gif_in_a_channel_still_answers(daemon, api, store, fetch):
    daemon.handle_update(channel_post("/gif"))
    assert api.of("sendAnimation")
    assert api.of("sendAnimation")[-1]["chat_id"] == CHANNEL
    assert store.get(token_of(api.of("sendAnimation")[-1]["reply_markup"])).recipe["recipe"] == "typecard"


def test_unsigned_channel_gif_gets_the_anon_hint(daemon, api, store, cook):
    asked = channel_gif(uid=None)
    daemon.handle_update(asked)
    assert api.texts() == [ANON_HINT]
    assert api.of("sendMessage")[-1]["reply_to_message_id"] == asked["channel_post"]["message_id"]
    assert api.of("getFile") == [] and cook.calls == [] and store.session_tokens() == set()


def test_poll_asks_telegram_for_channel_posts():
    assert "channel_post" in poll_mod.ALLOWED_UPDATES


def test_lurker_is_admitted_when_getchatmember_says_they_are_in_the_room(daemon, api, store):
    daemon.handle_update(gmsg("noise"))                   # learn the group; GUEST is remembered, GUEST2 is not
    api.members[(GROUP, GUEST2)] = {"user": {"id": GUEST2}, "status": "member"}
    daemon.handle_update(msg("/start", uid=GUEST2))
    assert "Hi!" in api.texts()[-1]
    assert api.of("getChatMember") == [{"chat_id": GROUP, "user_id": GUEST2}]
    assert store.is_member(GUEST2)
    n = len(api.of("getChatMember"))
    daemon.handle_update(msg("/help", uid=GUEST2))        # remembered: no second lookup
    assert len(api.of("getChatMember")) == n


def test_lurker_who_left_stays_a_stranger(daemon, api, caplog):
    daemon.handle_update(gmsg("noise"))
    api.members[(GROUP, STRANGER)] = {"user": {"id": STRANGER}, "status": "left"}
    with caplog.at_level(logging.WARNING, logger="clipwrightd.poll"):
        daemon.handle_update(msg("/start", uid=STRANGER))
        daemon.handle_update(msg(uid=STRANGER, **video()))
    assert api.of("sendMessage") == [] and api.of("sendAnimation") == []
    assert sum("non-allowlisted" in r.getMessage() for r in caplog.records) == 1
    assert len(api.of("getChatMember")) == 1              # same room set: no re-check


def test_getchatmember_error_does_not_admit_or_crash(daemon, api, caplog):
    daemon.handle_update(gmsg("noise"))
    api.members[(GROUP, GUEST2)] = BotAPIError("getChatMember: user not found", 400)
    with caplog.at_level(logging.WARNING, logger="clipwrightd.poll"):
        daemon.handle_update(msg("/start", uid=GUEST2))
        daemon.handle_update(msg("/help", uid=GUEST2))
    assert api.of("sendMessage") == []
    assert sum("non-allowlisted" in r.getMessage() for r in caplog.records) == 1
    assert len(api.of("getChatMember")) == 1


def test_a_member_of_an_ignored_group_is_still_a_stranger_in_a_dm(daemon, api, store, caplog):
    daemon.config.group_ids = {GROUP}
    other = -100999
    daemon.handle_update(msg("hi", uid=GUEST, chat=other))
    assert not store.is_member(GUEST) and store.list_groups() == []
    with caplog.at_level(logging.WARNING, logger="clipwrightd.poll"):
        daemon.handle_update(msg("/start", uid=GUEST))
    assert api.of("sendMessage") == []
    assert sum("non-allowlisted" in r.getMessage() for r in caplog.records) == 1


def test_pinned_groups_let_a_lurker_dm_without_anyone_speaking(cfg, api, store, cook, probe, fetch):
    cfg = replace(cfg, group_ids={GROUP})
    daemon = Daemon(cfg, api, store, RenderQueue(depth=cfg.queue_depth, inline=True), COOKBOOK,
                    cook_fn=cook, probe_fn=probe, fetch_fn=fetch)
    assert store.list_groups() == [GROUP]
    api.members[(GROUP, GUEST)] = {"user": {"id": GUEST}, "status": "administrator"}
    daemon.handle_update(msg("/start", uid=GUEST))
    assert "Hi!" in api.texts()[-1]
    assert api.of("getChatMember") == [{"chat_id": GROUP, "user_id": GUEST}]


def charged(store: Store, uid: int) -> int:
    """What the quota has counted for ``uid``: renders, and refusals too (it counts before comparing)."""
    return store._conn.execute("SELECT COALESCE(SUM(count), 0) AS n FROM quota WHERE user_id = ?",
                               (uid,)).fetchone()["n"]


def test_group_members_preview_freely_and_pay_the_export_quota(daemon, api, store, cook):
    """Everyone in the room is trusted: guest quota is not a gate. Exports use per_day_quota."""
    daemon.config.guest_per_day_quota = 0          # even 0 must not turn groups off
    token, _ = gif_in_group(daemon, api)           # preview: free
    big = gif_cmd(size=CAP + 1)
    daemon.handle_update(big)
    assert "Trim it and resend" in api.texts()[-1]
    assert api.of("sendMessage")[-1]["reply_to_message_id"] == big["message"]["message_id"]
    assert charged(store, GUEST) == 0 and len(api.of("getFile")) == 1
    daemon.handle_update(cb(f"c/{token}/3/1", uid=GUEST, chat=GROUP))   # knob: free
    assert charged(store, GUEST) == 0 and store.get(token).recipe["loop"] == "boomerang"

    daemon.handle_update(cb(f"a/{token}/export", uid=GUEST, chat=GROUP))
    assert len(api.of("sendDocument")) == 2 and charged(store, GUEST) == 1
    daemon.handle_update(cb(f"a/{token}/export", uid=GUEST, chat=GROUP))
    assert api.of("answerCallbackQuery")[-1]["text"] == "Daily export limit reached."

    mine, _ = gif_in_group(daemon, api, uid=OWNER)
    daemon.handle_update(cb(f"a/{mine}/export", uid=OWNER, chat=GROUP))
    assert charged(store, OWNER) == 1


def test_group_member_can_walk_the_whole_foundry(daemon, api, store, cook):
    daemon.config.per_day_quota = 200
    for update in (gif_cmd(),                                                      # preview
                   None,
                   cb("c/{t}/3/1", uid=GUEST, chat=GROUP),                         # knob
                   cb("r/{t}/caption-loop", uid=GUEST, chat=GROUP),                # recipe switch
                   cb("t/{t}/0", uid=GUEST, chat=GROUP),                           # a prompt is not a cook
                   answer("hello there"),                                          # text knob
                   cb("a/{t}/undo", uid=GUEST, chat=GROUP),                        # undo
                   cb("a/{t}/export", uid=GUEST, chat=GROUP)):                     # export
        if update is None:
            token = token_of(api.of("sendAnimation")[-1]["reply_markup"])
            continue
        if "callback_query" in update:
            update["callback_query"]["data"] = update["callback_query"]["data"].format(t=token)
        daemon.handle_update(update)
    assert len(cook.calls) == 6 and len(api.of("sendDocument")) == 2
    assert store.get(token).recipe["caption"]["text"] == "" and store.get(token).undo != []
    assert charged(store, GUEST) == 1                                              # export only
    assert [kw["text"] for kw in api.of("answerCallbackQuery")] == ["Rendering…", "Rendering…", None,
                                                                    "Rendering…", "Rendering…"]


def test_guest_quota_zero_does_not_turn_groups_off(daemon, api, store, cook):
    daemon.config.guest_per_day_quota = 0
    gif_in_group(daemon, api)
    gif_in_group(daemon, api, uid=GUEST2)
    assert len(api.of("sendAnimation")) == 2
    assert GROUP_OFF not in api.texts()


def test_group_buttons_belong_to_whoever_sent_gif(daemon, api, store, cook):
    token, _ = gif_in_group(daemon, api)
    cooks = len(cook.calls)
    daemon.handle_update(cb(f"c/{token}/3/1", uid=GUEST2, chat=GROUP))
    daemon.handle_update(cb(f"a/{token}/export", uid=OWNER, chat=GROUP))    # not even the owner
    daemon.handle_update(cb(f"t/{token}/0", uid=GUEST2, chat=GROUP))
    assert [kw["text"] for kw in api.of("answerCallbackQuery")] == [SOMEONE_ELSE] * 3
    assert store.get(token).recipe["loop"] == "seamless" and len(cook.calls) == cooks
    assert api.of("sendDocument") == [] and api.texts() == [] and daemon._pending_text == {}

    daemon.handle_update(cb(f"c/{token}/3/1", uid=GUEST, chat=GROUP))
    assert api.of("answerCallbackQuery")[-1]["text"] == "Rendering…"
    assert store.get(token).recipe["loop"] == "boomerang" and len(cook.calls) == cooks + 1
    (edit,) = api.of("editMessageMedia")
    assert edit["chat_id"] == GROUP and edit["message_id"] == store.get(token).message_id


def test_group_text_prompts_are_per_user_and_reply_to_each_gif(daemon, api, store, cook):
    first, asked1 = gif_in_group(daemon, api, uid=GUEST)
    second, asked2 = gif_in_group(daemon, api, uid=GUEST2)
    for token, uid in ((first, GUEST), (second, GUEST2)):
        daemon.handle_update(cb(f"r/{token}/caption-loop", uid=uid, chat=GROUP))
        daemon.handle_update(cb(f"t/{token}/0", uid=uid, chat=GROUP))
    prompts = api.of("sendMessage")
    assert [p["reply_to_message_id"] for p in prompts] == [asked1, asked2]
    assert all(p["reply_markup"]["force_reply"] is True and p["reply_markup"]["selective"] is True for p in prompts)
    assert daemon._pending_text == {(GROUP, GUEST): (first, 0), (GROUP, GUEST2): (second, 0)}

    calls = len(api.calls)
    daemon.handle_update(gmsg("not mine", uid=4245))         # a member with no prompt open: not a word
    daemon.handle_update(gmsg("not mine", uid=OWNER))
    assert len(api.calls) == calls and daemon._pending_text != {}

    # with privacy mode off the bot hears everything: only a reply to our prompt is an answer
    daemon.username = BOT_NAME
    spent = charged(store, GUEST)
    daemon.handle_update(gmsg("lol same", uid=GUEST))                            # chatter to a friend
    daemon.handle_update(msg("lol", uid=GUEST, chat=GROUP, reply_to_message={"message_id": 5, "from": _from(GUEST2)}))
    daemon.handle_update(answer("wrong bot", uid=GUEST, bot="OtherBot"))
    assert len(api.calls) == calls and charged(store, GUEST) == spent           # not an answer, not a render
    assert daemon._pending_text == {(GROUP, GUEST): (first, 0), (GROUP, GUEST2): (second, 0)}

    daemon.handle_update(answer("theirs", uid=GUEST2))
    daemon.handle_update(answer("mine", uid=GUEST))
    assert store.get(first).recipe["caption"]["text"] == "mine"
    assert store.get(second).recipe["caption"]["text"] == "theirs"
    assert [c[0]["caption"]["text"] for c in cook.calls[-2:]] == ["theirs", "mine"]
    assert daemon._pending_text == {} and len(api.of("sendMessage")) == 2      # the two prompts; no chatter
    assert len(api.of("editMessageMedia")) == 4                                 # two recipe switches, two captions


def test_group_unknown_commands_are_left_alone_unless_addressed_to_us(daemon, api):
    daemon.username = daemon._bot_username()                  # as run() fetches it: getMe's "Clipwright_Bot"
    assert daemon.username == BOT_NAME and api.of("getMe") == [{}]
    api.calls.clear()
    daemon.handle_update(gmsg("/foo"))
    daemon.handle_update(gmsg("/foo@otherbot"))
    daemon.handle_update(gmsg("/help@otherbot"))
    daemon.handle_update(gif_cmd(text="/gif@otherbot"))
    assert api.calls == []

    ours = gmsg(f"/foo@{BOT_NAME}")
    daemon.handle_update(ours)
    assert "/foo" in api.texts()[-1]
    assert api.of("sendMessage")[-1]["reply_to_message_id"] == ours["message"]["message_id"]
    daemon.handle_update(gmsg("/Help@Clipwright_Bot"))       # Telegram's case, not ours
    assert api.texts()[-1] == HELP_TEXT
    _token, asked = gif_in_group(daemon, api, text=f"/gif@{BOT_NAME}")
    assert api.of("sendAnimation")[-1]["reply_to_message_id"] == asked

    daemon.handle_update(msg("/foo"))                        # a DM answers as it always did
    assert "/foo" in api.texts()[-1] and api.of("sendMessage")[-1]["reply_to_message_id"] is None


def test_bare_gif_opens_a_typecard_not_a_crash(daemon, api, store, fetch):
    bare = gmsg("/gif")
    daemon.handle_update(bare)
    daemon.handle_update(msg("/gif"))
    assert fetch.queries == []                                 # nothing to search; skip the web
    assert api.of("getFile") == []
    anims = api.of("sendAnimation")
    assert len(anims) == 2
    assert anims[0]["reply_to_message_id"] == bare["message"]["message_id"]
    assert anims[1]["reply_to_message_id"] is None
    for sent in anims:
        token = token_of(sent["reply_markup"])
        sess = store.get(token)
        assert sess.recipe["recipe"] == "typecard"
        assert sess.recipe["caption"]["text"] in BARE_CAPTIONS


def test_gif_words_and_text_reply_fall_back_to_typecard_when_fetch_fails(daemon, api, store, fetch):
    fetch.fail = FetchError("Commons had no picture for that")
    words = gmsg("/gif dog")
    daemon.handle_update(words)
    replied = gmsg("/gif", uid=GUEST2, reply_to_message={"message_id": next(_ids), "text": "a sleepy pug"})
    daemon.handle_update(replied)
    assert [q[0] for q in fetch.queries] == ["dog", "a sleepy pug"]
    notes = [t for t in api.texts() if "Couldn't fetch a picture" in t]
    assert len(notes) == 2
    assert api.of("sendMessage")[0]["reply_to_message_id"] == words["message"]["message_id"]
    dog, pug = (token_of(s["reply_markup"]) for s in api.of("sendAnimation"))
    assert store.get(dog).recipe["recipe"] == "typecard"
    assert store.get(dog).recipe["caption"]["text"] == "dog"
    assert store.get(pug).recipe["caption"]["text"] == "a sleepy pug"


def test_gif_words_uses_the_web_still_when_fetch_works(daemon, api, store, fetch):
    fetch.fail = None
    daemon.handle_update(gmsg("/gif corgi"))
    assert fetch.queries[0][0] == "corgi"
    token = token_of(api.of("sendAnimation")[-1]["reply_markup"])
    sess = store.get(token)
    assert sess.recipe["recipe"] == "ken-burns"
    assert sess.recipe["caption"]["text"] == "corgi"
    assert os.path.isfile(sess.recipe["input"])
    assert "Couldn't fetch" not in "\n".join(api.texts())


def test_gif_reply_to_a_photo_opens_ken_burns(daemon, api, store, probe):
    probe.result = replace(probe.result, duration=0.0, nb_frames=1, still=True, vcodec="mjpeg")
    update = gmsg("/gif", reply_to_message={"message_id": next(_ids), **photo()})
    daemon.handle_update(update)
    token = token_of(api.of("sendAnimation")[-1]["reply_markup"])
    sess = store.get(token)
    assert sess.recipe["recipe"] == "ken-burns"
    assert api.of("getFile") == [{"file_id": "ph2"}]            # the largest size
    assert api.of("sendAnimation")[-1]["reply_to_message_id"] == update["message"]["message_id"]
    assert [b["text"] for b in keyboard_of(api.of("sendAnimation")[-1]["reply_markup"])[0]] == recipe_switch_row("ken-burns")


def test_dm_photo_opens_ken_burns_without_slash_gif(daemon, api, store, probe):
    probe.result = replace(probe.result, duration=0.0, nb_frames=1, still=True, vcodec="mjpeg")
    daemon.handle_update(msg(uid=OWNER, **photo()))
    token = token_of(api.of("sendAnimation")[-1]["reply_markup"])
    assert store.get(token).recipe["recipe"] == "ken-burns"
    assert store.get(token).origin_message_id is None


def test_gif_in_a_dm_is_an_upload_without_reply_threading(daemon, api, store):
    daemon.handle_update(gif_cmd(uid=OWNER, chat=CHAT))
    (sent,) = api.of("sendAnimation")
    token = token_of(sent["reply_markup"])
    assert sent["chat_id"] == CHAT and sent["reply_to_message_id"] is None
    assert store.get(token).origin_message_id is None
    daemon.handle_update(cb(f"a/{token}/export"))
    assert [kw["reply_to_message_id"] for kw in api.of("sendDocument")] == [None, None]
    daemon.handle_update(cb(f"a/{token}/cli"))
    daemon.handle_update(cb(f"r/{token}/caption-loop"))
    daemon.handle_update(cb(f"t/{token}/0"))
    cli, prompt = api.of("sendMessage")
    assert cli["text"].startswith("clipwright cook") and prompt["reply_markup"]["force_reply"] is True
    assert [kw["reply_to_message_id"] for kw in api.of("sendMessage")] == [None, None]


def test_group_start_help_recipes_reply_to_the_asker(daemon, api):
    asks = [gmsg("/start"), gmsg("/help", uid=OWNER), gmsg("/recipes", uid=GUEST2)]
    for update in asks:
        daemon.handle_update(update)
    start, help_, recipes = api.of("sendMessage")
    assert start["text"] == GROUP_START and "/gif" in GROUP_START
    assert "Grok Bot" in GROUP_START and "clipwright-grok" in GROUP_START
    assert help_["text"] == HELP_TEXT and "In groups" in HELP_TEXT and "/gif" in HELP_TEXT
    assert "Grok Bot" in HELP_TEXT and "clipwright-grok" in HELP_TEXT
    assert "gifify" in recipes["text"] and "caption-loop" in recipes["text"]
    assert [kw["chat_id"] for kw in api.of("sendMessage")] == [GROUP] * 3
    assert [kw["reply_to_message_id"] for kw in api.of("sendMessage")] == [u["message"]["message_id"] for u in asks]
    assert {"command": "gif", "description": "GIF a photo, video, or some words (or reply to one)"} in COMMANDS
    assert {"command": "start", "description": "What Grok Bot's Clipwright fork does"} in COMMANDS


def test_everything_a_group_session_sends_replies_to_its_gif(daemon, api, store, cook, probe):
    token, asked = gif_in_group(daemon, api)
    daemon.handle_update(cb(f"a/{token}/cli", uid=GUEST, chat=GROUP))
    daemon.handle_update(cb(f"r/{token}/caption-loop", uid=GUEST, chat=GROUP))
    daemon.handle_update(cb(f"t/{token}/0", uid=GUEST, chat=GROUP))
    too_long = answer("x" * 61)
    daemon.handle_update(too_long)                           # the retry prompt replies to the words themselves
    daemon.handle_update(cb(f"a/{token}/export", uid=GUEST, chat=GROUP))
    cook.fail = RuntimeError("palette exploded")
    daemon.handle_update(cb(f"c/{token}/1/1", uid=GUEST, chat=GROUP))

    assert api.of("sendAnimation")[0]["reply_to_message_id"] == asked
    gif_doc, sidecar_doc = api.of("sendDocument")
    assert gif_doc["reply_to_message_id"] == asked and sidecar_doc["reply_to_message_id"] == asked
    cli, prompt, retry, failed = api.of("sendMessage")
    assert cli["text"].startswith("clipwright cook") and cli["reply_to_message_id"] == asked
    assert prompt["reply_markup"]["force_reply"] is True and prompt["reply_to_message_id"] == asked
    assert "over the 60 limit" in retry["text"] and retry["reply_to_message_id"] == too_long["message"]["message_id"]
    assert "didn't work out" in failed["text"] and failed["reply_to_message_id"] == asked

    probe.fail = FFmpegError(["ffprobe", "x"], "moov atom not found", 1)
    second = gif_cmd(uid=GUEST2)
    daemon.handle_update(second)                             # an ingest refusal replies to that /gif
    assert "couldn't read that as a photo or video" in api.texts()[-1]
    assert api.of("sendMessage")[-1]["reply_to_message_id"] == second["message"]["message_id"]


def test_run_fetches_the_bot_username_once(daemon, api):
    api.batches = [[gmsg(f"/foo@{BOT_NAME}")]]
    assert daemon.run(once=True) == 0
    assert daemon.username == BOT_NAME and api.of("getMe") == [{}]
    assert "/foo" in api.texts()[-1]


def test_getme_failure_leaves_addressed_commands_to_other_bots(daemon, api, caplog):
    api.fail["getMe"] = BotAPIError("getMe: Bad Gateway", 502)
    api.batches = [[gmsg(f"/foo@{BOT_NAME}"), gmsg(f"/help@{BOT_NAME}"), gmsg("/help"), msg(f"/help@{BOT_NAME}")]]
    with caplog.at_level(logging.WARNING, logger="clipwrightd.poll"):
        assert daemon.run(once=True) == 0
    assert daemon.username is None
    assert any("getMe failed" in r.getMessage() for r in caplog.records)
    assert [kw["chat_id"] for kw in api.of("sendMessage")] == [GROUP, CHAT]   # only the unaddressed forms
    assert all(t == HELP_TEXT for t in api.texts())
    assert len(api.of("getMe")) == 1            # the addressed commands arrived within the retry cooldown


def test_failed_startup_getme_is_retried_for_the_next_addressed_command(daemon, api):
    """A getMe that failed at boot must not silence every menu-tapped /gif@bot until a restart."""
    now = [1000.0]
    daemon.clock = lambda: now[0]
    api.fail["getMe"], api.fail_at["getMe"] = BotAPIError("getMe: Bad Gateway", 502), 1
    api.batches = [[gmsg("/help")]]
    assert daemon.run(once=True) == 0 and daemon.username is None
    daemon.handle_update(gmsg(f"/help@{BOT_NAME}"))               # too soon: no retry, still not ours
    assert len(api.of("getMe")) == 1 and len(api.texts()) == 1
    now[0] += USERNAME_RETRY_S
    _token, asked = gif_in_group(daemon, api, text=f"/gif@{BOT_NAME}")
    assert daemon.username == BOT_NAME and len(api.of("getMe")) == 2
    assert api.of("sendAnimation")[-1]["reply_to_message_id"] == asked


def test_interrupt_during_the_startup_getme_still_shuts_down_cleanly(daemon, api, caplog):
    """A Ctrl-C while getMe hangs takes the same path as one during a poll: exit 0, pidfile released."""
    api.fail["getMe"] = KeyboardInterrupt()
    with caplog.at_level(logging.INFO, logger="clipwrightd.poll"):
        assert daemon.run(once=True) == 0
    assert daemon._pidfile is None and api.of("getUpdates") == []
    assert any("interrupted" in r.getMessage() for r in caplog.records)


def test_group_ids_restrict_the_bot_to_the_groups_it_was_given(daemon, api, store, caplog):
    other = -100999
    daemon.config.group_ids = {GROUP}
    with caplog.at_level(logging.WARNING, logger="clipwrightd.poll"):
        for update in (gif_cmd(chat=other), gif_cmd(uid=OWNER, chat=other), msg("/help", uid=OWNER, chat=other),
                       cb("a/AAAAAA/grid", uid=OWNER, chat=other)):
            daemon.handle_update(update)
    assert api.calls == [] and store.session_tokens() == set()
    assert sum(str(other) in r.getMessage() and "CLIPWRIGHT_GROUP_IDS" in r.getMessage() for r in caplog.records) == 1
    gif_in_group(daemon, api)                                 # the listed group, as before
    assert len(api.of("sendAnimation")) == 1

    daemon.config.group_ids = set()                           # unset: any group the bot is in
    gif_in_group(daemon, api, chat=other)
    assert api.of("sendAnimation")[-1]["chat_id"] == other


def test_anonymous_admins_and_channels_are_not_served(daemon, api, store, cook):
    """Telegram gives every anonymous admin one from.id (and every channel poster another): no sessions there."""
    anon = {"id": 1087968824, "is_bot": True, "first_name": "Group", "username": "GroupAnonymousBot"}
    as_channel = {"id": 136817688, "is_bot": True, "first_name": "Channel", "username": "Channel_Bot"}
    asked = gif_cmd()
    asked["message"]["from"], asked["message"]["sender_chat"] = anon, _chat(GROUP)
    daemon.handle_update(asked)
    assert api.texts() == [ANON_HINT]
    assert api.of("sendMessage")[-1]["reply_to_message_id"] == asked["message"]["message_id"]
    posted = gif_cmd(uid=OWNER)                               # even the owner, posting as a channel
    posted["message"]["from"], posted["message"]["sender_chat"] = as_channel, {"id": -1009, "type": "channel"}
    daemon.handle_update(posted)
    assert api.texts() == [ANON_HINT] * 2 and api.of("getFile") == [] and cook.calls == []
    assert store.session_tokens() == set()
    daemon.handle_update(copy.deepcopy(asked))                # the hint is rate-limited like a guest's
    assert len(api.texts()) == 2

    quiet = len(api.calls)
    for update in (gmsg("/help"), gmsg("hello"), gmsg(**video()), msg("/gif", uid=OWNER)):
        update["message"]["from"] = dict(anon)
        daemon.handle_update(update)
    forwarded = gmsg("/help")
    forwarded["message"]["from"] = {"id": 777000, "is_bot": False, "first_name": "Telegram"}
    daemon.handle_update(forwarded)
    press = cb("a/AAAAAA/grid", uid=OWNER, chat=GROUP)
    press["callback_query"]["from"] = dict(anon)
    daemon.handle_update(press)
    assert len(api.calls) == quiet                            # anything else from them: not a word


def test_anonymous_gif_hints_are_rate_limited_per_chat(daemon, api):
    """Anonymous /gif still nags, once a minute — a loop must not spend the group's send allowance."""
    now = [5000.0]
    daemon.clock = lambda: now[0]
    anon = {"id": 1087968824, "is_bot": False, "first_name": "GroupAnonymousBot"}

    def ping(uid_msg=None):
        update = gmsg("/gif")
        update["message"]["from"] = dict(anon)
        update["message"]["sender_chat"] = {"id": GROUP, "type": "channel"}
        daemon.handle_update(update)

    ping()
    ping()
    ping()
    assert api.texts() == [ANON_HINT]
    ping()  # still inside the cooldown
    assert api.texts() == [ANON_HINT]
    now[0] += NAG_COOLDOWN_S
    ping()
    assert api.texts() == [ANON_HINT, ANON_HINT]


# -- uploads -----------------------------------------------------------------------

def test_oversized_video_is_refused_without_download(daemon, api, store):
    daemon.handle_update(msg(**video(size=CAP + 1)))
    (text,) = api.texts()
    assert "Trim it and resend" in text and "1.0 MB" in text
    assert api.of("getFile") == [] and api.of("downloadFile") == []
    assert not os.path.exists(os.path.join(daemon.home, "uploads"))


@pytest.mark.parametrize("kw", [
    {}, {"key": "animation"}, {"key": "document", "mime": "video/quicktime"}, {"key": "document", "mime": "video/webm"},
])
def test_valid_video_downloads_probes_and_previews(daemon, api, store, cook, probe, kw):
    token = upload(daemon, api, **kw)

    (dl,) = api.of("downloadFile")
    ext = {"video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm"}[kw.get("mime", "video/mp4")]
    assert dl["file_path"] == "videos/vid1.mp4" and dl["max_bytes"] == CAP
    assert os.path.dirname(dl["dest"]) == os.path.join(daemon.home, "uploads", str(OWNER))
    assert dl["dest"].endswith("." + ext) and os.path.isfile(dl["dest"])
    assert probe.paths == [dl["dest"]]

    sess = store.get(token)
    assert sess.user_id == OWNER and sess.chat_id == CHAT
    expected = recipe.defaults(COOKBOOK["gifify"]) | {"input": dl["dest"], "from": "0:00.0", "to": "0:03.0"}
    assert sess.recipe == expected
    assert recipe.validate(sess.recipe, COOKBOOK["gifify"]) == []

    ((inst, out_dir, proxy),) = cook.calls
    assert inst == expected and proxy is True
    assert out_dir == os.path.join(daemon.home, "renders", str(OWNER), token)
    assert cook.public_inputs == [os.path.basename(dl["dest"])]

    (sent,) = api.of("sendAnimation")
    assert sent["chat_id"] == CHAT and sent["filename"] == "preview.mp4"
    assert "gifify" in sent["caption"] and "0:00.0–0:03.0" in sent["caption"]
    assert sess.message_id == 101
    assert api.texts() == []                      # position 1: no "queued" chatter
    assert os.listdir(out_dir) == []              # the proxy went to Telegram; nothing kept


def test_preview_keyboard_is_complete_and_small(daemon, api):
    token = upload(daemon, api)
    rows = keyboard_of(api.of("sendAnimation")[0]["reply_markup"])
    flat = [b for row in rows for b in row]
    assert all(len(b["callback_data"].encode()) <= MAX_CB_BYTES for b in flat)
    assert all(decode_cb(b["callback_data"]).session == token for b in flat)
    assert [b["text"] for b in rows[0]] == recipe_switch_row("gifify")
    assert [b["text"] for b in rows[1]] == ["🃏 typecard"]
    assert [b["text"] for b in rows[-1]] == ["↩ Undo", "⌘ Show CLI", "⬇ Export", "🎲 Grid"]
    assert any(b["text"] == "• 🔁 Seamless" for b in flat)


@pytest.mark.parametrize("bad, phrase", [
    (dict(duration=75.0), "Trim it and resend"),
    (dict(width=1920, height=1080), "Downscale it and resend"),
])
def test_probe_gate_rejects_and_removes_upload(daemon, api, store, cook, probe, bad, phrase):
    probe.result = replace(probe.result, **bad)
    daemon.handle_update(msg(**video()))
    (text,) = api.texts()
    assert phrase in text
    assert not os.path.exists(api.of("downloadFile")[0]["dest"])
    assert cook.calls == [] and api.of("sendAnimation") == []


def test_unreadable_upload_is_refused(daemon, api, probe):
    probe.fail = FFmpegError(["ffprobe", "x"], "moov atom not found", 1)
    daemon.handle_update(msg(**video()))
    assert "couldn't read that as a photo or video" in api.texts()[0]


def test_download_overrunning_the_cap_is_a_trim_message(daemon, api):
    api.fail["downloadFile"] = BotAPIError("download exceeds 1000000 bytes; aborted", 413)
    daemon.handle_update(msg(**video(size=None)))
    (text,) = api.texts()
    assert "Trim it and resend" in text and "1.0 MB" in text
    assert api.of("sendAnimation") == []


def test_long_clip_starts_at_the_segment_cap(daemon, api, store, probe):
    daemon.config.max_duration_s = 60.0
    probe.result = replace(probe.result, duration=SEGMENT_CAP_S + 5)
    token = upload(daemon, api)
    got = store.get(token).recipe
    assert (got["from"], got["to"]) == ("0:00.0", recipe.fmt_time(SEGMENT_CAP_S))
    assert f"0:00.0–{recipe.fmt_time(SEGMENT_CAP_S)}" in api.of("sendAnimation")[-1]["caption"]


def test_storage_budget_refuses_uploads_before_download(daemon, api, media):
    # video() declares file_size=5000, which the gate counts on top of what is already on disk
    daemon.config.max_user_bytes = 5000 + os.path.getsize(media[0]) // 2
    upload(daemon, api)
    daemon.handle_update(msg(**video(size=5000)))
    refusal = api.texts()[-1]
    assert "keep at most" in refusal and "14 days" in refusal
    # exported clips are pinned by the ledger for good, so the refusal must not promise they clear
    assert "Clips behind your exports stay" in refusal and "/remix" in refusal
    assert len(api.of("getFile")) == 1 and len(api.of("sendAnimation")) == 1
    upload(daemon, api, uid=FRIEND)               # the budget is per user
    assert len(api.of("sendAnimation")) == 2


def test_upload_names_are_hex_so_the_cli_line_never_starts_with_a_dash(daemon, api):
    # token_urlsafe can start with "-", which argparse reads as an option in the ⌘ Show CLI line
    for _ in range(3):
        token = upload(daemon, api)
        daemon.handle_update(cb(f"a/{token}/cli"))
    for dl in api.of("downloadFile"):
        assert re.fullmatch(r"[0-9a-f]{12}\.mp4", os.path.basename(dl["dest"]))
    for line in api.texts():
        argv = shlex.split(line)
        assert argv[:3] == ["clipwright", "cook", "gifify"] and not argv[3].startswith("-")


def test_ingest_runs_on_the_worker_and_holds_the_users_slot(daemon, api, store, cook):
    """The poll thread hands the download to the queue; while it runs the user is 'still rendering'."""
    gate = threading.Event()
    started = threading.Event()
    real_download = api.download_file

    def slow_download(file_path, dest, max_bytes):
        started.set()
        assert gate.wait(5)
        return real_download(file_path, dest, max_bytes)

    api.download_file = slow_download
    daemon.queue = RenderQueue(depth=2)
    try:
        daemon.handle_update(msg(**video()))          # returns at once: the download is the worker's job
        assert started.wait(5) and daemon.queue.has(OWNER)
        assert api.of("sendAnimation") == [] and store.session_tokens() == set()

        daemon.handle_update(msg(**video()))          # a second upload from the same person: refused, unfetched
        assert api.texts()[-1] == STILL_RENDERING and len(api.of("getFile")) == 1
        daemon.handle_update(msg("/help", uid=FRIEND))   # everyone else is still served meanwhile
        assert "/remix" in api.texts()[-1]

        gate.set()
        daemon.queue.stop()
    finally:
        gate.set()
        daemon.queue.stop()
    (sent,) = api.of("sendAnimation")
    assert store.get(token_of(sent["reply_markup"])).user_id == OWNER and len(cook.calls) == 1
    assert not daemon.queue.has(OWNER)


# -- callbacks ---------------------------------------------------------------------

def test_enum_press_updates_recipe_and_edits_preview(daemon, api, store, cook):
    token = upload(daemon, api)
    update = cb(f"c/{token}/3/1")           # gifify knob 3 = loop, value 1 = boomerang
    daemon.handle_update(update)

    assert store.get(token).recipe["loop"] == "boomerang"
    assert cook.calls[-1][0]["loop"] == "boomerang" and cook.calls[-1][2] is True
    (edit,) = api.of("editMessageMedia")
    assert edit["chat_id"] == CHAT and edit["message_id"] == 101
    flat = [b for row in keyboard_of(edit["reply_markup"]) for b in row]
    assert any(b["text"] == "• 🪃 Boomerang" for b in flat)
    assert not any(b["text"] == "• 🔁 Seamless" for b in flat)
    (answer,) = api.of("answerCallbackQuery")
    assert answer["callback_query_id"] == update["callback_query"]["id"]
    assert not os.path.exists(edit["path"])       # delivered, then discarded


def test_step_and_range_presses(daemon, api, store):
    token = upload(daemon, api)
    daemon.handle_update(cb(f"c/{token}/0/1"))   # fps +
    daemon.handle_update(cb(f"c/{token}/5/1"))   # trim: in +
    got = store.get(token).recipe
    assert got["fps"] == 17 and got["from"] == "0:00.1" and got["to"] == "0:03.0"
    assert len(api.of("editMessageMedia")) == 2


def test_out_point_cannot_exceed_the_segment_cap(daemon, api, store, probe):
    daemon.config.max_duration_s = 60.0
    probe.result = replace(probe.result, duration=SEGMENT_CAP_S + 5)
    token = upload(daemon, api)
    before = store.get(token).recipe
    daemon.handle_update(cb(f"c/{token}/5/3"))   # out + past the cap
    assert "capped" in api.of("answerCallbackQuery")[-1]["text"]
    assert store.get(token).recipe == before and api.of("editMessageMedia") == []

    daemon.handle_update(cb(f"c/{token}/5/1"))   # in + ...
    daemon.handle_update(cb(f"c/{token}/5/3"))   # ... then out + slides the window
    got = store.get(token).recipe
    assert (got["from"], got["to"]) == ("0:00.1", recipe.fmt_time(SEGMENT_CAP_S + 0.1))
    assert len(api.of("editMessageMedia")) == 2


def test_a_full_window_slides_the_whole_clip_without_a_spurious_cap_refusal(daemon, api, store, probe):
    """0:01.1..0:16.1 is 15.000000000000002 in floats; the gate must reason in milliseconds."""
    daemon.config.max_duration_s = 60.0
    probe.result = replace(probe.result, duration=45.0)
    token = upload(daemon, api)
    refused = []
    for _ in range(60):                                  # 0:00.0–0:15.0 slid to 0:06.0–0:21.0, 0.1 s at a time
        daemon.handle_update(cb(f"c/{token}/5/1"))       # in +
        daemon.handle_update(cb(f"c/{token}/5/3"))       # out +
        toast = api.of("answerCallbackQuery")[-1]["text"]
        if toast != "Rendering…":
            refused.append((store.get(token).recipe["from"], toast))
    assert refused == []
    got = store.get(token).recipe
    assert recipe.parse_time(got["to"]) - recipe.parse_time(got["from"]) == pytest.approx(SEGMENT_CAP_S)


def test_out_point_stops_at_the_end_of_the_clip(daemon, api, store, cook, probe):
    token = upload(daemon, api)                          # a 3.0 s clip: the session opens at 0:00.0–0:03.0
    before = store.get(token)
    daemon.handle_update(cb(f"c/{token}/5/3"))           # out + past the end
    assert api.of("answerCallbackQuery")[-1]["text"] == CLIP_END
    assert store.get(token).recipe == before.recipe and store.get(token).undo == []
    assert api.of("editMessageMedia") == [] and len(cook.calls) == 1

    daemon.handle_update(cb(f"c/{token}/5/2"))           # out − ...
    daemon.handle_update(cb(f"c/{token}/5/3"))           # ... and back to the end renders
    assert store.get(token).recipe["to"] == "0:03.0" and len(api.of("editMessageMedia")) == 2
    daemon.handle_update(cb(f"a/{token}/cli"))
    assert "--to 0:03.0" in api.texts()[-1]              # the sidecar/CLI never claim a frame past the end

    # after a restart the duration is not cached: the first out-point press re-probes the upload once
    fresh = Daemon(daemon.config, api, store, RenderQueue(inline=True), COOKBOOK, cook_fn=cook, probe_fn=probe)
    probed = len(probe.paths)
    fresh.handle_update(cb(f"c/{token}/5/3"))
    fresh.handle_update(cb(f"c/{token}/5/3"))
    assert [kw["text"] for kw in api.of("answerCallbackQuery")[-2:]] == [CLIP_END, CLIP_END]
    assert probe.paths[probed:] == [before.recipe["input"]]


def test_caption_shows_the_range_the_engine_rendered(daemon, api, cook):
    cook.report_extra = {"from": 0.0, "to": 2.8, "capped": True, "loop_nudge_frames": -2,
                         "loop": "boomerang (degraded)"}
    upload(daemon, api)
    caption = api.of("sendAnimation")[-1]["caption"]
    assert "0:00.0–0:02.8" in caption and f"capped to {SEGMENT_CAP_S:g} s" in caption
    assert "loop nudged -2f" in caption
    assert "loop: boomerang (degraded)" in caption  # a degrade is never hidden from the user


def test_caption_stays_quiet_for_a_seamless_loop(daemon, api, cook):
    cook.report_extra = {"from": 0.0, "to": 2.8, "loop": "seamless"}
    upload(daemon, api)
    assert "loop:" not in api.of("sendAnimation")[-1]["caption"]


def test_undo_restores_previous_recipe(daemon, api, store):
    token = upload(daemon, api)
    daemon.handle_update(cb(f"c/{token}/3/1"))
    daemon.handle_update(cb(f"a/{token}/undo"))
    assert store.get(token).recipe["loop"] == "seamless"
    edits = api.of("editMessageMedia")
    assert len(edits) == 2
    assert any(b["text"] == "• 🔁 Seamless" for row in keyboard_of(edits[-1]["reply_markup"]) for b in row)

    daemon.handle_update(cb(f"a/{token}/undo"))
    assert api.of("answerCallbackQuery")[-1]["text"] == "Nothing to undo."
    assert len(api.of("editMessageMedia")) == 2


def test_export_sends_gif_and_sidecar_and_ledgers_it(daemon, api, store, cook):
    token = upload(daemon, api)
    daemon.handle_update(cb(f"a/{token}/export"))

    assert cook.calls[-1][2] is False
    gif_doc, sidecar_doc = api.of("sendDocument")
    assert gif_doc["filename"].endswith(".gif") and "fits" in gif_doc["caption"]
    # without this Telegram re-types the .gif as an animation and the user gets an MP4 transcode
    assert gif_doc["disable_content_type_detection"] is True
    assert sidecar_doc["disable_content_type_detection"] is None
    assert sidecar_doc["filename"] == gif_doc["filename"][:-4] + ".recipe.toml"
    live = store.get(token).recipe
    shown = recipe.loads_instance(sidecar_doc["content"].decode())
    assert shown == live | {"input": os.path.basename(live["input"])}
    assert not os.path.exists(gif_doc["path"]) and not os.path.exists(sidecar_doc["path"])

    gif_sent, sidecar_sent = api.sent_docs
    entry = store.ledger_entry(gif_sent["file_unique_id"])
    assert entry.recipe == live and entry.user_id == OWNER and entry.token == token
    assert store.ledger_get(sidecar_sent["file_unique_id"]) is None
    assert api.of("answerCallbackQuery")[-1]["text"] == "Rendering…"


def test_export_ledgers_the_gif_even_when_the_sidecar_upload_fails(daemon, api, store):
    token = upload(daemon, api)
    api.fail["sendDocument"] = BotAPIError("sendDocument: flood", 429, retry_after=3)
    api.fail_at["sendDocument"] = 2
    daemon.handle_update(cb(f"a/{token}/export"))
    assert len(api.of("sendDocument")) == 2
    assert store.ledger_get(api.sent_docs[0]["file_unique_id"]) == store.get(token).recipe
    assert "sidecar didn't go through" in api.texts()[-1] and "/remix" in api.texts()[-1]
    assert not any("wouldn't take" in t for t in api.texts())


def test_remix_reopens_your_own_export_and_the_owner_can_reopen_anyones(daemon, api, store, cook):
    token = upload(daemon, api, uid=FRIEND)
    daemon.handle_update(cb(f"c/{token}/3/1", uid=FRIEND))
    daemon.handle_update(cb(f"a/{token}/export", uid=FRIEND))
    unique = api.sent_docs[0]["file_unique_id"]
    previews = len(api.of("sendAnimation"))

    daemon.handle_update(msg("/remix", uid=FRIEND, reply_to_message=remix_reply(unique)))
    new_token = token_of(api.of("sendAnimation")[-1]["reply_markup"])
    assert new_token != token and len(api.of("sendAnimation")) == previews + 1
    fresh = store.get(new_token)
    assert fresh.user_id == FRIEND and fresh.recipe == store.get(token).recipe
    assert fresh.recipe["loop"] == "boomerang" and cook.calls[-1][2] is True

    daemon.handle_update(msg("/remix", uid=OWNER, reply_to_message=remix_reply(unique)))
    assert len(api.of("sendAnimation")) == previews + 2
    assert store.get(token_of(api.of("sendAnimation")[-1]["reply_markup"])).user_id == OWNER


def test_remix_of_someone_elses_export_is_refused(daemon, api, store, cook, caplog):
    token = upload(daemon, api)
    daemon.handle_update(cb(f"a/{token}/export"))
    unique = api.sent_docs[0]["file_unique_id"]
    sessions = store.session_tokens()

    with caplog.at_level(logging.WARNING, logger="clipwrightd.poll"):
        daemon.handle_update(msg("/remix", uid=FRIEND, reply_to_message=remix_reply(unique)))
    assert "belongs to someone else" in api.texts()[-1]
    assert store.session_tokens() == sessions and cook.calls[-1][2] is False
    assert any("asked to remix" in r.getMessage() for r in caplog.records)


def test_remix_without_a_known_file_explains(daemon, api):
    daemon.handle_update(msg("/remix"))
    daemon.handle_update(msg("/remix", reply_to_message={"document": {"file_unique_id": "nope"}}))
    first, second = api.texts()
    assert "Reply /remix" in first and "wasn't one of my exports" in second


def test_remix_of_an_export_whose_clip_is_gone_says_so(daemon, api, store):
    token = upload(daemon, api)
    daemon.handle_update(cb(f"a/{token}/export"))
    os.remove(store.get(token).recipe["input"])
    daemon.handle_update(msg("/remix", reply_to_message=remix_reply(api.sent_docs[0]["file_unique_id"])))
    assert "no longer on disk" in api.texts()[-1] and len(api.of("sendAnimation")) == 1


def test_quota_refuses_second_export_politely(daemon, api, store, cook):
    token = upload(daemon, api)
    daemon.handle_update(cb(f"a/{token}/export"))
    cooks = len(cook.calls)
    daemon.handle_update(cb(f"a/{token}/export"))
    assert len(cook.calls) == cooks
    assert len(api.of("sendDocument")) == 2
    assert "exports" in api.texts()[-1] and "1" in api.texts()[-1]
    assert api.of("answerCallbackQuery")[-1]["text"] == "Daily export limit reached."


def test_grid_and_noop_and_cli_answers(daemon, api):
    token = upload(daemon, api)
    daemon.handle_update(cb(f"a/{token}/grid"))
    assert api.of("answerCallbackQuery")[-1]["text"] == GRID_TOAST

    daemon.handle_update(cb(f"n/{token}"))
    assert api.of("answerCallbackQuery")[-1]["text"] is None

    daemon.handle_update(cb(f"a/{token}/cli"))
    dest = api.of("downloadFile")[0]["dest"]
    assert api.texts()[-1] == f"clipwright cook gifify {os.path.basename(dest)} --from 0:00.0 --to 0:03.0"
    assert daemon.home not in api.texts()[-1]


def test_every_callback_is_answered(daemon, api, store):
    token = upload(daemon, api)
    daemon.handle_update(cb("garbage"))
    daemon.handle_update(cb("c/ZZZZZZ/0/0"))
    daemon.handle_update(cb(f"c/{token}/0/0", uid=FRIEND))
    daemon.handle_update(cb(f"c/{token}/99/0"))
    daemon.handle_update(cb(f"t/{token}/3"))          # loop is an enum, not a text knob
    daemon.handle_update(cb(f"t/{token}/99"))
    daemon.handle_update(cb(f"r/{token}/nope"))
    answers = [kw["text"] for kw in api.of("answerCallbackQuery")]
    expired = "That session has expired — send a photo or /gif again."
    assert answers == [STALE_BUTTON, expired,
                       "That's someone else's session.", "gifify has no knob #99",
                       STALE_BUTTON, STALE_BUTTON, STALE_BUTTON]
    assert api.of("editMessageMedia") == [] and api.texts() == []
    assert daemon._pending_text == {}


def test_failed_callback_answer_is_logged_not_told(daemon, api, store, caplog):
    token = upload(daemon, api)
    api.fail["answerCallbackQuery"] = BotAPIError("query is too old and response timeout expired", 400)
    with caplog.at_level(logging.WARNING, logger="clipwrightd.poll"):
        daemon.handle_update(cb(f"c/{token}/3/1"))
    assert store.get(token).recipe["loop"] == "boomerang"
    assert len(api.of("editMessageMedia")) == 1 and api.texts() == []
    assert any("could not answer callback" in r.getMessage() for r in caplog.records)


# -- recipe switching -----------------------------------------------------------------

def test_recipe_switch_reaches_caption_loop_from_a_plain_upload(daemon, api, store, cook):
    token = upload(daemon, api)
    base = store.get(token).recipe
    daemon.handle_update(cb(f"r/{token}/caption-loop"))
    got = store.get(token).recipe
    assert got == recipe.defaults(COOKBOOK["caption-loop"]) | {k: base[k] for k in ("input", "from", "to")}
    assert api.of("answerCallbackQuery")[-1]["text"] == "Rendering…"
    (edit,) = api.of("editMessageMedia")
    assert "caption-loop" in edit["caption"]
    labels = buttons_of(edit["reply_markup"])
    assert "• 💬 caption-loop" in labels and "✎ Text" in labels and "🎞️ gifify" in labels

    daemon.handle_update(cb(f"r/{token}/caption-loop"))      # already there: no re-render
    assert api.of("answerCallbackQuery")[-1]["text"] is None and len(api.of("editMessageMedia")) == 1

    daemon.handle_update(cb(f"t/{token}/0"))
    daemon.handle_update(msg("hello there"))
    assert store.get(token).recipe["caption"]["text"] == "hello there"
    assert cook.calls[-1][0]["caption"]["text"] == "hello there"

    daemon.handle_update(cb(f"a/{token}/undo"))
    daemon.handle_update(cb(f"a/{token}/undo"))
    assert store.get(token).recipe == base


def test_text_prompt_is_dropped_when_the_recipe_changes_under_it(daemon, api, store, cook):
    token = upload(daemon, api)
    daemon.handle_update(cb(f"r/{token}/caption-loop"))
    daemon.handle_update(cb(f"t/{token}/0"))
    daemon.handle_update(cb(f"r/{token}/gifify"))            # knob 0 is now the fps step
    assert daemon._pending_text == {}
    daemon.handle_update(msg("late reply"))
    assert "Send a photo" in api.texts()[-1]
    assert store.get(token).recipe == recipe.defaults(COOKBOOK["gifify"]) | {
        k: store.get(token).recipe[k] for k in ("input", "from", "to")}


def test_typecard_cannot_switch_to_a_clip_recipe_without_input(daemon, api, store, fetch):
    daemon.handle_update(msg("/gif"))
    token = token_of(api.of("sendAnimation")[-1]["reply_markup"])
    assert store.get(token).recipe["recipe"] == "typecard"
    daemon.handle_update(cb(f"r/{token}/gifify"))
    assert api.of("answerCallbackQuery")[-1]["text"].startswith("That recipe needs a photo or clip")
    assert store.get(token).recipe["recipe"] == "typecard"
    daemon.handle_update(cb(f"r/{token}/ken-burns"))
    assert store.get(token).recipe["recipe"] == "typecard"


# -- text knobs via force_reply -------------------------------------------------------

@pytest.fixture
def caption_session(store, media) -> str:
    inst = recipe.defaults(COOKBOOK["caption-loop"]) | {"input": media[0], "from": "0:00.0", "to": "0:03.0"}
    token = store.create_session(OWNER, CHAT, inst)
    store.update(token, inst, message_id=777)
    return token


def test_text_knob_prompts_then_takes_the_reply(daemon, api, store, cook, caption_session):
    token = caption_session
    daemon.handle_update(cb(f"t/{token}/0", message_id=777))
    prompt = api.of("sendMessage")[-1]
    assert "Text" in prompt["text"] and "60" in prompt["text"]
    assert prompt["reply_markup"]["force_reply"] is True and prompt["reply_markup"]["selective"] is True

    daemon.handle_update(msg("x" * 61))
    retry = api.of("sendMessage")[-1]
    assert "over the 60 limit" in retry["text"] and retry["reply_markup"]["force_reply"] is True
    assert store.get(token).recipe["caption"]["text"] == "" and cook.calls == []

    daemon.handle_update(msg("  my sprint velocity  "))
    assert store.get(token).recipe["caption"]["text"] == "my sprint velocity"
    (edit,) = api.of("editMessageMedia")
    assert edit["message_id"] == 777 and cook.calls[-1][0]["caption"]["text"] == "my sprint velocity"

    daemon.handle_update(msg("more words"))          # nothing pending any more
    assert "Send a photo" in api.texts()[-1]


def test_text_reply_from_another_user_is_not_taken(daemon, api, store, caption_session):
    daemon.handle_update(cb(f"t/{caption_session}/0", message_id=777))
    daemon.handle_update(msg("hijack", uid=FRIEND))
    assert store.get(caption_session).recipe["caption"]["text"] == ""
    assert "Send a photo" in api.texts()[-1]      # prompts are keyed per user: FRIEND has none pending
    daemon.handle_update(msg("mine"))                # the owner's prompt is still pending
    assert store.get(caption_session).recipe["caption"]["text"] == "mine"


# -- admission comes before mutation ------------------------------------------------------

def test_busy_user_presses_change_nothing(daemon, api, store, cook, park):
    token = upload(daemon, api)                      # rendered inline, before the queue is swapped
    gate = park()
    before = store.get(token)
    cooks = len(cook.calls)

    daemon.handle_update(cb(f"c/{token}/0/1"))       # fps +
    daemon.handle_update(cb(f"c/{token}/0/1"))
    daemon.handle_update(cb(f"a/{token}/undo"))
    daemon.handle_update(cb(f"a/{token}/export"))
    daemon.handle_update(cb(f"r/{token}/boomerang"))
    assert [kw["text"] for kw in api.of("answerCallbackQuery")] == [STILL_RENDERING] * 5
    assert store.get(token).recipe == before.recipe and store.get(token).undo == before.undo
    assert len(cook.calls) == cooks and api.texts() == []

    daemon.handle_update(msg(**video()))
    assert api.texts()[-1] == STILL_RENDERING and len(api.of("getFile")) == 1

    release(daemon, gate)
    daemon.handle_update(cb(f"a/{token}/export"))    # the refused export did not cost the daily quota
    assert len(api.of("sendDocument")) == 2 and api.of("answerCallbackQuery")[-1]["text"] == "Rendering…"


def test_full_queue_presses_change_nothing_for_other_users(daemon, api, store, cook, park):
    token = upload(daemon, api, uid=FRIEND)
    park()
    before = store.get(token).recipe
    daemon.handle_update(cb(f"c/{token}/0/1", uid=FRIEND))
    daemon.handle_update(cb(f"a/{token}/undo", uid=FRIEND))
    daemon.handle_update(cb(f"a/{token}/export", uid=FRIEND))
    assert [kw["text"] for kw in api.of("answerCallbackQuery")] == [QUEUE_FULL] * 3
    assert store.get(token).recipe == before and store.get(token).undo == []
    daemon.handle_update(msg(uid=FRIEND, **video()))
    assert api.texts()[-1] == QUEUE_FULL


def test_text_reply_while_busy_keeps_the_prompt(daemon, api, store, cook, caption_session, park):
    token = caption_session
    gate = park()
    daemon.handle_update(cb(f"t/{token}/0", message_id=777))
    daemon.handle_update(msg("my sprint velocity"))
    reply = api.of("sendMessage")[-1]
    assert reply["text"] == STILL_RENDERING and reply["reply_markup"]["force_reply"] is True
    assert store.get(token).recipe["caption"]["text"] == "" and store.get(token).undo == []
    assert daemon._pending_text == {(CHAT, OWNER): (token, 0)} and cook.calls == []

    release(daemon, gate)
    daemon.handle_update(msg("my sprint velocity"))
    assert store.get(token).recipe["caption"]["text"] == "my sprint velocity"
    assert len(api.of("editMessageMedia")) == 1 and daemon._pending_text == {}


def test_queue_refusal_is_reported(daemon, api, cook, park):
    park()
    daemon.handle_update(msg(**video()))
    assert api.texts()[-1] == STILL_RENDERING
    daemon.handle_update(msg(uid=FRIEND, **video()))
    assert api.texts()[-1] == QUEUE_FULL
    assert api.of("getFile") == [] and cook.calls == []


# -- failures never escape --------------------------------------------------------------

def test_cook_failure_becomes_one_message(daemon, api, cook, caplog):
    cook.fail = RuntimeError("palette exploded")
    with caplog.at_level(logging.ERROR, logger="clipwrightd.poll"):
        daemon.handle_update(msg(**video()))
    assert api.of("sendAnimation") == []
    assert api.texts() == ["That render didn't work out. Try a different setting."]
    assert any(r.exc_info and "palette exploded" in str(r.exc_info[1]) for r in caplog.records)

    cook.fail = FFmpegError(["ffmpeg"], "boom", 1)
    daemon.handle_update(msg(**video()))
    assert "ffmpeg" in api.texts()[-1]


def test_telegram_failure_in_a_handler_is_contained(daemon, api):
    api.fail["sendMessage"] = BotAPIError("sendMessage: Bad Request", 400)
    daemon.handle_update(msg("/help"))            # raises inside the handler, then in _tell
    assert [m for m, _ in api.calls] == ["sendMessage", "sendMessage"]

    del api.fail["sendMessage"]
    api.fail["editMessageMedia"] = BotAPIError("editMessageMedia: too big", 413)
    token = upload(daemon, api)
    daemon.handle_update(cb(f"c/{token}/3/1"))
    assert "Telegram wouldn't take that upload" in api.texts()[-1]
    assert api.of("answerCallbackQuery")[-1]["text"] == "Rendering…"
    assert not os.path.exists(api.of("editMessageMedia")[-1]["path"])   # refused, still discarded


def test_heartbeat_keeps_the_chat_action_alive_during_a_cook(daemon, api, cook, monkeypatch):
    monkeypatch.setattr(poll_mod, "HEARTBEAT_S", 0.01)
    cook.delay = 0.08
    upload(daemon, api)
    beats = api.of("sendChatAction")
    assert len(beats) >= 4 and all(b == {"chat_id": CHAT, "action": "upload_video"} for b in beats)
    assert not any(t.isalive() for t in threading.enumerate() if t.name == "clipwright-heartbeat")


# -- disk: sweep --------------------------------------------------------------------------

def test_sweep_expires_idle_sessions_and_keeps_exported_clips(daemon, api, store, cook):
    idle = upload(daemon, api)
    exported = upload(daemon, api, uid=FRIEND)
    daemon.handle_update(cb(f"a/{exported}/export", uid=FRIEND))
    idle_clip = store.get(idle).recipe["input"]
    kept_clip = store.get(exported).recipe["input"]
    render_dirs = [c[1] for c in cook.calls]
    assert all(os.path.isdir(d) for d in render_dirs)

    daemon.sweep(now=time.time() + 15 * DAY_S)
    assert store.get(idle) is None and store.get(exported) is None
    assert not os.path.exists(idle_clip) and os.path.isfile(kept_clip)
    assert not any(os.path.exists(d) for d in render_dirs)

    daemon.handle_update(cb(f"c/{idle}/3/1"))
    assert api.of("answerCallbackQuery")[-1]["text"] == "That session has expired — send a photo or /gif again."
    daemon.handle_update(msg("/remix", uid=FRIEND, reply_to_message=remix_reply(api.sent_docs[0]["file_unique_id"])))
    assert store.get(token_of(api.of("sendAnimation")[-1]["reply_markup"])).recipe["input"] == kept_clip


def test_sweep_leaves_fresh_sessions_alone(daemon, api, store):
    token = upload(daemon, api)
    daemon.sweep()
    assert store.get(token) is not None and os.path.isfile(store.get(token).recipe["input"])


def test_sweep_failure_is_logged_not_fatal(daemon, api, store, caplog, monkeypatch):
    def broken(cutoff):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "expire_sessions", broken)
    api.batches = [[msg("/start")]]
    with caplog.at_level(logging.ERROR, logger="clipwrightd.poll"):
        assert daemon.run(once=True) == 0             # the sweep at startup blew up; the batch was still served
    assert "Send a photo" in api.texts()[0]
    assert any("sweep failed" in r.getMessage() and r.exc_info for r in caplog.records)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_sweep_skips_an_unreadable_user_dir_and_finishes(daemon, api, home, caplog):
    junk_dir = os.path.join(home, "uploads", str(FRIEND))
    os.makedirs(junk_dir)
    junk = os.path.join(junk_dir, "orphan.mp4")
    open(junk, "wb").close()
    locked = os.path.join(home, "uploads", str(OWNER))
    os.makedirs(locked)
    os.chmod(locked, 0)
    try:
        with caplog.at_level(logging.WARNING, logger="clipwrightd.poll"):
            daemon.sweep()
    finally:
        os.chmod(locked, 0o700)
    assert any("cannot list" in r.getMessage() for r in caplog.records)
    assert not os.path.exists(junk)                   # the sweep went on past the bad directory


# -- the loop: offset, backoff, pidfile, shutdown --------------------------------------------

def test_run_once_handles_a_batch_and_persists_offset(daemon, api, home):
    batch = [msg("/start"), msg("/help")]
    api.batches = [batch]
    assert daemon.run(once=True) == 0
    assert len(api.texts()) == 2
    expected = batch[-1]["update_id"] + 1
    with open(os.path.join(home, "offset")) as fh:
        assert int(fh.read()) == expected
    assert not os.path.exists(os.path.join(home, "offset.tmp"))
    assert api.of("getUpdates")[0] == {"offset": None, "timeout": 50}

    api.batches = [[]]
    assert daemon.run(once=True) == 0
    assert api.of("getUpdates")[-1] == {"offset": expected, "timeout": 50}


def test_empty_poll_refreshes_offset_mtime_without_changing_the_integer(daemon, api, home):
    """An empty getUpdates still rewrites offset so its mtime is the last poll that returned."""
    api.batches = [[msg("/start")]]
    assert daemon.run(once=True) == 0
    path = os.path.join(home, "offset")
    stored = int(open(path, encoding="utf-8").read())
    old = time.time() - 3600
    os.utime(path, (old, old))
    api.batches = [[]]
    assert daemon.run(once=True) == 0
    assert int(open(path, encoding="utf-8").read()) == stored
    assert os.stat(path).st_mtime > time.time() - 30
    assert not os.path.exists(path + ".tmp")


def test_first_poll_with_no_updates_leaves_an_empty_offset_file(daemon, api, home):
    api.batches = [[]]
    assert daemon.run(once=True) == 0
    path = os.path.join(home, "offset")
    assert os.path.isfile(path)
    assert open(path, encoding="utf-8").read().strip() == ""
    assert daemon.load_offset() is None


def test_offset_is_saved_per_update_so_an_interrupted_batch_replays_only_the_one_in_flight(
        daemon, api, home, monkeypatch):
    batch = [msg("/start"), msg("/help"), msg("/recipes")]
    api.batches = [batch]
    real = daemon._handle_command

    def interrupted(command, *a, **kw):
        if command == "help":
            raise KeyboardInterrupt             # Ctrl-C lands while the second update is being handled
        real(command, *a, **kw)

    monkeypatch.setattr(daemon, "_handle_command", interrupted)
    assert daemon.run() == 0
    assert len(api.texts()) == 1                # /start was answered; /help and /recipes were not
    with open(os.path.join(home, "offset")) as fh:
        assert int(fh.read()) == batch[0]["update_id"] + 1      # so /help is replayed on restart, /start is not
    assert daemon._pidfile is None


@pytest.mark.parametrize("code", [401, 404])
def test_rejected_token_is_fatal_not_retried(daemon, api, caplog, code):
    api.batches = [BotAPIError("getUpdates: Unauthorized", code)]
    waits: list[float] = []
    daemon.sleep = waits.append
    with caplog.at_level(logging.ERROR, logger="clipwrightd.poll"):
        assert daemon.run() == 2
    assert waits == [] and daemon._pidfile is None
    assert any("rejected the bot token" in r.getMessage() and "bot.env" in r.getMessage() for r in caplog.records)


def test_conflict_and_flood_still_back_off(daemon, api):
    api.batches = [BotAPIError("getUpdates: Conflict", 409), BotAPIError("flood", 429, retry_after=2),
                   KeyboardInterrupt()]
    waits: list[float] = []
    daemon.sleep = waits.append
    assert daemon.run() == 0
    assert waits == [1.0, 2.0]


def test_run_backs_off_on_transport_errors(daemon, api):
    api.batches = [BotAPIError("transport failure: boom"), BotAPIError("flood", 429, retry_after=5),
                   BotAPIError("again"), [], KeyboardInterrupt()]
    waits: list[float] = []
    daemon.sleep = waits.append
    assert daemon.run() == 0
    assert waits == [1.0, 5.0, 4.0]
    assert daemon._pidfile is None


def test_run_once_returns_1_on_transport_error(daemon, api):
    api.batches = [BotAPIError("down")]
    daemon.sleep = lambda s: None
    assert daemon.run(once=True) == 1


def test_run_survives_a_raw_exception_from_get_updates(daemon, api, caplog):
    api.batches = [ConnectionResetError("peer went away")]
    waits: list[float] = []
    daemon.sleep = waits.append
    with caplog.at_level(logging.WARNING, logger="clipwrightd.poll"):
        assert daemon.run(once=True) == 1
    assert waits == [1.0] and daemon._pidfile is None
    assert any(r.exc_info and "peer went away" in str(r.exc_info[1]) for r in caplog.records)


def test_run_survives_a_body_read_failure(cfg, store, cook, probe):
    class Stalls(io.BytesIO):
        def read(self, n: int = -1) -> bytes:
            raise TimeoutError("the body never came")

    api = BotAPI("123:abc", transport=lambda url, data, headers, timeout: Stalls())
    daemon = Daemon(cfg, api, store, RenderQueue(inline=True), COOKBOOK, cook_fn=cook, probe_fn=probe)
    waits: list[float] = []
    daemon.sleep = waits.append
    assert daemon.run(once=True) == 1
    assert waits == [1.0]


def test_shutdown_waits_for_the_running_render_and_tells_the_queued(daemon, api, store, cook):
    other = upload(daemon, api, uid=FRIEND)
    gate = threading.Event()
    daemon.queue = RenderQueue(depth=3)
    daemon.queue.submit(OWNER, gate.wait)
    daemon.handle_update(cb(f"c/{other}/3/1", uid=FRIEND))
    assert api.of("answerCallbackQuery")[-1]["text"] == "Queued — #2 in line."
    assert store.get(other).recipe["loop"] == "boomerang"

    threading.Timer(0.05, gate.set).start()
    daemon._shutdown()                            # returns only once OWNER's job has finished
    assert gate.is_set() and daemon.queue.size == 0
    assert api.texts()[-1].startswith("I'm shutting down") and api.of("sendMessage")[-1]["chat_id"] == CHAT
    assert len(api.of("editMessageMedia")) == 0   # FRIEND's render never ran


def test_shutdown_notice_for_a_queued_group_press_replies_to_the_gif(daemon, api, store, cook):
    token, asked = gif_in_group(daemon, api)
    gate = threading.Event()
    daemon.queue = RenderQueue(depth=3)
    daemon.queue.submit(OWNER, gate.wait)
    daemon.handle_update(cb(f"a/{token}/export", uid=GUEST, chat=GROUP))
    assert api.of("answerCallbackQuery")[-1]["text"] == "Queued — #2 in line."

    threading.Timer(0.05, gate.set).start()
    daemon._shutdown()
    notice = api.of("sendMessage")[-1]
    assert notice["text"].startswith("I'm shutting down")
    assert (notice["chat_id"], notice["reply_to_message_id"]) == (GROUP, asked)
    assert api.of("sendDocument") == []


def test_render_that_dies_during_shutdown_is_not_blamed_on_the_clip(daemon, api, store, cook):
    """A terminal's Ctrl-C reaches the ffmpeg child too: its job fails, and the user must hear 'I was stopped'."""
    token = upload(daemon, api)
    gate = threading.Event()

    def cook_killed_by_sigint(*args, **kwargs):
        assert gate.wait(5)
        raise FFmpegError(["ffmpeg"], "Exiting normally, received signal 2.", 255)

    daemon.cook_fn = cook_killed_by_sigint
    daemon.queue = RenderQueue(depth=2)
    api.batches = [[cb(f"c/{token}/3/1")], KeyboardInterrupt()]
    threading.Timer(0.1, gate.set).start()      # the render "dies" once the shutdown is under way
    assert daemon.run() == 0
    assert api.texts()[-1] == SHUT_DOWN_MID_RENDER
    assert not any("ffmpeg" in t for t in api.texts())


def test_sigterm_takes_the_ctrl_c_path(tmp_path, api, monkeypatch, caplog):
    """`kill <pid>` must finish like Ctrl-C: log, tell the queue, save the offset, release the pidfile."""
    home = tmp_path / "h"
    home.mkdir()
    env = home / "bot.env"
    env.write_text(f"CLIPWRIGHT_BOT_TOKEN=123:abc\nCLIPWRIGHT_OWNER_ID={OWNER}\n")
    env.chmod(0o600)
    monkeypatch.delenv("CLIPWRIGHT_HOME", raising=False)
    monkeypatch.setattr(poll_mod, "BotAPI", lambda token: api)
    first = msg("/start")
    real_get_updates = api.get_updates
    polls: list[int | None] = []

    def get_updates(offset, timeout=50, allowed_updates=None):
        polls.append(offset)
        if len(polls) == 2:                         # second poll: the operator runs `kill <pid>`
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.5)                         # the daemon's handler fires before this returns
            raise AssertionError("SIGTERM did not interrupt the poll")
        if len(polls) > 2:
            raise KeyboardInterrupt                 # safety net so a missing handler cannot loop forever
        return real_get_updates(offset, timeout, allowed_updates)

    api.get_updates = get_updates
    api.batches = [[first]]
    def noop(*_: object) -> None:
        pass

    previous = signal.signal(signal.SIGTERM, noop)   # a missing handler must not kill pytest
    root = logging.getLogger()
    handlers = root.handlers[:]
    try:
        with caplog.at_level(logging.INFO, logger="clipwrightd.poll"):
            assert main(["--home", str(home)]) == 0
        assert signal.getsignal(signal.SIGTERM) is noop         # main() put back what it found
    finally:
        signal.signal(signal.SIGTERM, previous)
        for h in root.handlers[:]:
            if h not in handlers:
                root.removeHandler(h)
                h.close()
    assert polls == [None, first["update_id"] + 1]
    assert any("interrupted; shutting down" in r.getMessage() for r in caplog.records)
    assert (home / "offset").read_text().strip() == str(first["update_id"] + 1)


def test_pidfile_blocks_a_second_daemon(cfg, api, store, cook, probe, home):
    first = Daemon(cfg, api, store, RenderQueue(inline=True), COOKBOOK, cook_fn=cook, probe_fn=probe)
    second = Daemon(cfg, api, store, RenderQueue(inline=True), COOKBOOK, cook_fn=cook, probe_fn=probe)
    first.acquire_pidfile()
    with open(os.path.join(home, "daemon.pid")) as fh:
        assert int(fh.read()) == os.getpid()
    with pytest.raises(DaemonAlreadyRunning):
        second.run(once=True)
    assert api.of("getUpdates") == []
    first.release_pidfile()
    assert second.run(once=True) == 0
    assert len(api.of("getUpdates")) == 1


# -- main ---------------------------------------------------------------------------------

def test_main_help_needs_no_token(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--once" in out and "--home" in out


def test_main_reports_missing_config(tmp_path, capsys):
    assert main(["--home", str(tmp_path / "nowhere")]) == 2
    assert "bot.env" in capsys.readouterr().err


def test_main_wires_everything_and_runs_once(tmp_path, api, monkeypatch):
    home = tmp_path / "h"
    home.mkdir()
    env = home / "bot.env"
    env.write_text(f"CLIPWRIGHT_BOT_TOKEN=123:abc\nCLIPWRIGHT_OWNER_ID={OWNER}\nCLIPWRIGHT_QUEUE_DEPTH=2\n")
    env.chmod(0o600)
    monkeypatch.delenv("CLIPWRIGHT_HOME", raising=False)
    built: list[str] = []
    monkeypatch.setattr(poll_mod, "BotAPI", lambda token: built.append(token) or api)
    api.batches = [[msg("/start")]]
    root = logging.getLogger()
    before = root.handlers[:]
    try:
        assert main(["--home", str(home), "--once"]) == 0
    finally:
        for h in root.handlers[:]:
            if h not in before:
                root.removeHandler(h)
                h.close()
    assert built == ["123:abc"]
    assert api.of("setMyCommands") == [{"commands": COMMANDS}]
    assert "Send a photo" in api.texts()[0]
    assert (home / "daemon.log").read_text().count("polling from offset") == 1
    assert (home / "offset").exists() and (home / "state.db").exists()


def test_main_exits_2_on_a_rejected_token_before_polling(tmp_path, api, monkeypatch):
    home = tmp_path / "h"
    home.mkdir()
    env = home / "bot.env"
    env.write_text(f"CLIPWRIGHT_BOT_TOKEN=123:wrong\nCLIPWRIGHT_OWNER_ID={OWNER}\n")
    env.chmod(0o600)
    monkeypatch.delenv("CLIPWRIGHT_HOME", raising=False)
    monkeypatch.setattr(poll_mod, "BotAPI", lambda token: api)
    api.fail["setMyCommands"] = BotAPIError("setMyCommands: Unauthorized", 401)
    api.batches = [[msg("/start")]]
    root = logging.getLogger()
    before = root.handlers[:]
    try:
        assert main(["--home", str(home)]) == 2
    finally:
        for h in root.handlers[:]:
            if h not in before:
                root.removeHandler(h)
                h.close()
    assert api.of("getUpdates") == [] and api.texts() == []
    log_text = (home / "daemon.log").read_text()
    assert "rejected the bot token" in log_text and "bot.env" in log_text and "wrong" not in log_text


# -- RenderQueue --------------------------------------------------------------------------

def test_queue_positions_and_per_user_limit():
    q = RenderQueue(depth=3)
    gate, all_done = threading.Event(), threading.Event()
    done: list[int] = []

    def job(uid: int):
        def run() -> None:
            gate.wait(5)
            done.append(uid)
            if len(done) == 3:
                all_done.set()
        return run

    try:
        assert q.can_accept(1)
        assert q.submit(1, job(1)) == 1
        assert q.submit(2, job(2)) == 2
        assert not q.can_accept(1) and q.submit(1, job(1)) is None      # user 1 already has a job
        assert q.submit(3, job(3)) == 3
        assert not q.can_accept(4) and q.submit(4, job(4)) is None      # depth reached
        assert q.has(1) and q.has(3) and not q.has(4) and q.size == 3
        gate.set()
        assert all_done.wait(5)
    finally:
        gate.set()
        assert q.stop(timeout=5) == []
    assert done == [1, 2, 3] and q.size == 0 and not q.has(1) and q.can_accept(1)


def test_queue_logs_failing_jobs_and_frees_the_user(caplog):
    q = RenderQueue(depth=2)
    finished = threading.Event()

    def bad() -> None:
        try:
            raise ValueError("kaboom")
        finally:
            finished.set()

    with caplog.at_level(logging.ERROR, logger="clipwrightd.queue"):
        assert q.submit(7, bad) == 1
        assert finished.wait(5)
        q.stop(timeout=5)
    assert any(r.exc_info and "kaboom" in str(r.exc_info[1]) for r in caplog.records)
    assert not q.has(7) and q.size == 0


def test_queue_stop_finishes_the_running_job_and_returns_the_rest():
    q = RenderQueue(depth=3)
    gate = threading.Event()
    ran: list[int] = []
    q.submit(1, lambda: (gate.wait(5), ran.append(1)))
    later = lambda: ran.append(2)                    # noqa: E731 — identity matters below
    assert q.submit(2, later) == 2

    assert q.stop(timeout=0.05) == []                # worker still busy: nothing drained
    assert q.size == 2 and q.has(2)
    threading.Timer(0.05, gate.set).start()
    dropped = q.stop()
    assert ran == [1] and dropped == [(2, later)]
    assert q.size == 0 and not q.has(2) and q.can_accept(2)


def test_queue_inline_runs_immediately_and_rejects_bad_depth():
    q = RenderQueue(depth=1, inline=True)
    ran: list[int] = []
    assert q.submit(1, lambda: ran.append(1)) == 1
    assert ran == [1] and q.size == 0
    assert q.stop() == []
    with pytest.raises(ValueError):
        RenderQueue(depth=0)
