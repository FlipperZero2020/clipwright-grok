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
from clipwrightd.poll import (CLIP_END, COMMANDS, GRID_TOAST, QUEUE_FULL, SEGMENT_CAP_S,
                              SHUT_DOWN_MID_RENDER, STALE_BUTTON, STILL_RENDERING, Daemon,
                              DaemonAlreadyRunning, main)
from clipwrightd.queue import RenderQueue
from clipwrightd.session import Store

OWNER = 1001
FRIEND = 1002
STRANGER = 4242
CHAT = 555
CAP = 1_000_000
COOKBOOK = recipe.load_cookbook()
DAY_S = 86400.0

_ids = itertools.count(1)


# -- fakes ----------------------------------------------------------------------

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
        batch = self.batches.pop(0) if self.batches else []
        if isinstance(batch, BaseException):
            raise batch
        return batch

    def send_message(self, chat_id, text, reply_markup=None, reply_to_message_id=None, parse_mode=None):
        self._record("sendMessage", chat_id=chat_id, text=text, reply_markup=reply_markup)
        return {"message_id": self._next()}

    def send_animation(self, chat_id, path_or_bytes, caption=None, reply_markup=None, filename="preview.mp4"):
        assert os.path.isfile(path_or_bytes), path_or_bytes
        self._record("sendAnimation", chat_id=chat_id, path=path_or_bytes, caption=caption,
                     reply_markup=reply_markup, filename=filename)
        mid = self._next()
        return {"message_id": mid, "animation": {"file_id": f"A{mid}", "file_unique_id": f"UA{mid}"}}

    def send_document(self, chat_id, path_or_bytes, filename, caption=None, reply_markup=None,
                      disable_content_type_detection=None):
        assert os.path.isfile(path_or_bytes), path_or_bytes
        with open(path_or_bytes, "rb") as fh:
            content = fh.read()                # the daemon removes the file once it is sent
        self._record("sendDocument", chat_id=chat_id, path=path_or_bytes, filename=filename,
                     caption=caption, content=content,
                     disable_content_type_detection=disable_content_type_detection)
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


# -- update builders --------------------------------------------------------------

def _from(uid: int) -> dict:
    return {"id": uid, "is_bot": False, "first_name": "T"}


def msg(text: str | None = None, uid: int = OWNER, chat: int = CHAT, **extra: object) -> dict:
    m: dict = {"message_id": next(_ids), "from": _from(uid), "chat": {"id": chat, "type": "private"}, "date": 0}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return {"update_id": next(_ids), "message": m}


def cb(data: str, uid: int = OWNER, chat: int = CHAT, message_id: int = 101) -> dict:
    return {"update_id": next(_ids), "callback_query": {
        "id": f"cq{next(_ids)}", "from": _from(uid), "data": data,
        "message": {"message_id": message_id, "chat": {"id": chat, "type": "private"}}}}


def video(size: int = 5000, mime: str = "video/mp4", key: str = "video") -> dict:
    return {key: {"file_id": "vid1", "file_unique_id": "Uvid1", "file_size": size, "mime_type": mime,
                  "width": 320, "height": 240, "duration": 3}}


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
def daemon(cfg, api, store, cook, probe) -> Daemon:
    return Daemon(cfg, api, store, RenderQueue(depth=cfg.queue_depth, inline=True), COOKBOOK,
                  cook_fn=cook, probe_fn=probe)


def upload(daemon: Daemon, api: FakeAPI, uid: int = OWNER, **kw: object) -> str:
    """Send a valid video through the daemon; return the new session's token (from its keyboard)."""
    daemon.handle_update(msg(uid=uid, **video(**kw)))
    return token_of(api.of("sendAnimation")[-1]["reply_markup"])


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
    assert "Send me a video" in start and "1.0 MB" in start and "10 s" in start
    assert "/remix" in help_ and f"{SEGMENT_CAP_S:g} s" in help_
    assert "gifify" in recipes and "caption-loop" in recipes
    assert "/bogus" in bogus
    assert all(kw["chat_id"] == CHAT for kw in api.of("sendMessage"))


def test_non_video_message_gets_a_hint(daemon, api):
    daemon.handle_update(msg(sticker={"file_id": "s"}))
    daemon.handle_update(msg("just some words"))
    assert all("video" in t for t in api.texts()) and len(api.texts()) == 2


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
    assert [b["text"] for b in rows[0]] == ["🪃 boomerang", "💬 caption-loop", "• 🎞️ gifify"]
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
    assert "couldn't read that as a video" in api.texts()[0]


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
    assert answers == [STALE_BUTTON, "That session has expired — send the video again.",
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
    assert "Send me a video" in api.texts()[-1]
    assert store.get(token).recipe == recipe.defaults(COOKBOOK["gifify"]) | {
        k: store.get(token).recipe[k] for k in ("input", "from", "to")}


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
    assert "Send me a video" in api.texts()[-1]


def test_text_reply_from_another_user_is_not_taken(daemon, api, store, caption_session):
    daemon.handle_update(cb(f"t/{caption_session}/0", message_id=777))
    daemon.handle_update(msg("hijack", uid=FRIEND))
    assert store.get(caption_session).recipe["caption"]["text"] == ""
    assert "someone else" in api.texts()[-1]
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
    assert daemon._pending_text == {CHAT: (token, 0)} and cook.calls == []

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
    assert api.of("answerCallbackQuery")[-1]["text"] == "That session has expired — send the video again."
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
    assert "Send me a video" in api.texts()[0]
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


def test_offset_is_saved_per_update_so_an_interrupted_batch_replays_only_the_one_in_flight(
        daemon, api, home, monkeypatch):
    batch = [msg("/start"), msg("/help"), msg("/recipes")]
    api.batches = [batch]
    real = daemon._handle_command

    def interrupted(command, *a):
        if command == "help":
            raise KeyboardInterrupt             # Ctrl-C lands while the second update is being handled
        real(command, *a)

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
    assert "Send me a video" in api.texts()[0]
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
