"""clipwrightd.poll — the getUpdates loop and everything a Telegram update does.

``Daemon`` is the whole bot: it owns the pidfile (``<home>/daemon.pid``,
``fcntl.flock`` — a second poller on the same token makes updates vanish, so
the second one refuses to start), persists the update offset to
``<home>/offset`` after every update it has handled (so a restart replays
at most the one in flight), and dispatches each update to one handler. The
engine is reached only through ``cook_fn`` / ``probe_fn``, both injectable
so tests run with fakes and never touch ffmpeg or a socket.

Flow, per the plan: a video arrives → size gate (no download past the cap)
→ queue and disk-budget gates → *on the render worker*: download to
``<home>/uploads/<user_id>/<token>.<ext>`` → ffprobe gate (duration,
resolution) → a ``gifify`` session → a proxy MP4 sent with the knob
keyboard. The poll thread never waits on a download or a probe, and the
user's one queue slot is taken for the whole ingest, so a second upload
from the same person is refused before a byte of it is fetched. Every
button press edits that one message in place; the top row
switches recipes. Export cooks the real GIF, sends it as a document with
its ``.recipe.toml`` sidecar, and ledgers the sent ``file_unique_id`` so
``/remix`` can reopen it later — for the user who exported it (or the
owner): the recipe names that user's private upload. Anyone not on the
allowlist gets silence (logged once).

Three rules keep what the user sees true:

- A handler asks the queue whether it can take a job *before* it edits the
  session, so a refusal never leaves the recipe ahead of the preview, a
  daily export charged for nothing, or a text prompt swallowed.
- The daemon never lets a session's segment exceed the engine's
  ``SEGMENT_CAP_S``, which the engine would otherwise apply silently, nor
  its out-point run past the clip's probed end, so the out-point on the
  keyboard is the one that gets rendered and the caption's times are real.
- Disk is bounded: a cook's outputs are removed once Telegram has them (the
  ledger, not the file, is the memory), each user has a byte budget across
  their uploads and renders, and ``sweep`` expires idle sessions and the
  uploads nothing references any more. An upload behind an export is kept
  (``/remix`` needs it) and counts against its owner's budget for good.

Every handler is wrapped: a network or cook failure becomes one short
message to the user and a log line, never a dead loop; so is the hourly
sweep. Any error on ``getUpdates`` backs off 1 s → 60 s — except a 401/404,
which means the token itself is wrong, and no retry fixes that: the daemon
exits 2 so a supervisor (or the operator's terminal) sees it. ``SIGTERM``
takes the same path as Ctrl-C: the running render finishes, anyone still
queued is told, the offset is saved, the pidfile released.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import logging
import os
import secrets
import shutil
import signal
import sys
import threading
import time
from collections.abc import Callable

from clipwright import ffmpeg, recipe
from clipwright.cook import cli_command, cook
from clipwright.pipelines import CookResult
from clipwright.pipelines.common import SEGMENT_CAP_S
from clipwrightd import config as config_mod
from clipwrightd.api import BotAPI, BotAPIError
from clipwrightd.keyboards import Callback, build_keyboard, decode_cb
from clipwrightd.queue import RenderQueue
from clipwrightd.session import LedgerEntry, Session, Store

log = logging.getLogger("clipwrightd.poll")

DEFAULT_RECIPE = "gifify"
POLL_TIMEOUT_S = 50
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 60.0
HEARTBEAT_S = 4.0            # Telegram shows a chat action for ~5 s; keep it alive while a cook runs
SWEEP_INTERVAL_S = 3600.0
ALLOWED_UPDATES = ["message", "callback_query"]
PIDFILE = "daemon.pid"
OFFSET_FILE = "offset"
LOG_FILE = "daemon.log"
STATE_DB = "state.db"
PREVIEW_NAME = "preview.mp4"
GRID_TOAST = "Grid lands in a later milestone."
STILL_RENDERING = "Still rendering your last one — try again in a moment."
QUEUE_FULL = "The queue is full right now — try again in a minute."
STALE_BUTTON = "That button is stale."
CLIP_END = "That's the end of the clip."
SHUT_DOWN_MID_RENDER = "I was shut down mid-render — send that again in a minute."
FATAL_API_CODES = (401, 404)   # Unauthorized / Not Found on the bot URL: the token itself is wrong
_EXT_FOR_MIME = {"video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm"}

COMMANDS = [
    {"command": "start", "description": "What this bot does"},
    {"command": "help", "description": "How to use it"},
    {"command": "recipes", "description": "List the cookbook"},
    {"command": "remix", "description": "Reply to a GIF I sent to reopen its knobs"},
]

HELP_TEXT = (
    "Send me a video and I turn it into a looping GIF you tune with buttons.\n\n"
    "• Every button press re-renders the preview in place.\n"
    "• The top row of buttons switches recipes; /recipes describes them.\n"
    "• ✎ buttons ask for text — just reply to the prompt.\n"
    "• ⬇ Export sends the real GIF plus its .recipe.toml.\n"
    "• Reply /remix to any GIF I sent you to reopen its knobs.\n"
    "• ⌘ Show CLI prints the command that reproduces the render.\n\n"
    f"Clips are rendered {SEGMENT_CAP_S:g} s at a time — slide the in/out points to pick the part."
)

CookFn = Callable[..., CookResult]
ProbeFn = Callable[[str], ffmpeg.Probe]
Job = Callable[[], None]


class DaemonAlreadyRunning(RuntimeError):
    """Another clipwrightd holds the pidfile lock for this home."""


class _Rejected(Exception):
    """An upload the gates refuse; ``str(exc)`` is the sentence the user sees."""


def _mb(n: int | float) -> str:
    return f"{n / 1_000_000:.1f} MB"


def _media_of(msg: dict) -> dict | None:
    """The video-like attachment of a message: video, animation, or video/* document."""
    for key in ("video", "animation"):
        if isinstance(msg.get(key), dict):
            return msg[key]
    doc = msg.get("document")
    if isinstance(doc, dict) and str(doc.get("mime_type", "")).startswith("video/"):
        return doc
    return None


def _sent_file(resp: dict) -> dict | None:
    """The file object Telegram attached to a sent message, whatever it re-typed it as."""
    for key in ("document", "animation", "video"):
        if isinstance(resp.get(key), dict):
            return resp[key]
    return None


def _command_of(text: str) -> str | None:
    """``"/start@bot arg"`` -> ``"start"``; None for non-commands."""
    if not text.startswith("/"):
        return None
    word = text.split(None, 1)[0][1:]
    return word.split("@", 1)[0].lower()


def _user_error(exc: BaseException) -> str:
    if isinstance(exc, ffmpeg.FFmpegError):
        return "The render failed in ffmpeg — try a different clip or trim it shorter."
    if isinstance(exc, BotAPIError):
        return "Telegram wouldn't take that upload — try again in a moment."
    return "That render didn't work out. Try a different setting."


def _force_reply(knob: recipe.Knob) -> dict:
    return {"force_reply": True, "selective": True, "input_field_placeholder": knob.label}


def _export_caption(report: dict, size: int) -> str:
    fits = report.get("fits")
    budget = report.get("budget")
    if fits is True:
        return f"{_mb(size)} · fits ✓"
    if fits is False and budget:
        return f"{_mb(size)} · over the {_mb(budget)} budget"
    return _mb(size)


def _text_knob(defn: recipe.RecipeDef, knob_idx: int) -> recipe.Knob | None:
    """The text knob at ``knob_idx``, or None when the index is out of range or not a text knob."""
    if not 0 <= knob_idx < len(defn.knobs):
        return None
    knob = defn.knobs[knob_idx]
    return knob if knob.type == "text" else None


def _ms(seconds: float) -> int:
    """Whole milliseconds — the keyboard's own resolution (``fmt_time`` rounds the same way)."""
    return round(seconds * 1000)


def _segment_s(inst: dict) -> float:
    """Length of the instance's ``from``..``to`` range; 0 when either end is unset.

    Computed in whole milliseconds, so a window slid along in 0.1 s steps
    (``0:01.1``..``0:16.1``) is exactly 15.0 s, not ``15.000000000000002``.
    """
    if inst.get("from") is None or inst.get("to") is None:
        return 0.0
    return (_ms(recipe.parse_time(inst["to"])) - _ms(recipe.parse_time(inst["from"]))) / 1000


def _clamp_segment(inst: dict) -> None:
    """Pull ``to`` in so the range never exceeds SEGMENT_CAP_S (the engine would cap it silently)."""
    if _segment_s(inst) > SEGMENT_CAP_S:
        inst["to"] = recipe.fmt_time(recipe.parse_time(inst["from"]) + SEGMENT_CAP_S)


def _public(inst: dict) -> dict:
    """A copy whose ``input`` is just the file name: the server path, and the uploader's id in it, stay here."""
    out = copy.deepcopy(inst)
    if isinstance(out.get("input"), str):
        out["input"] = os.path.basename(out["input"])
    return out


def _unlink(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _subdirs(path: str) -> list[str]:
    try:
        names = os.listdir(path)
    except OSError:
        return []
    return [p for p in (os.path.join(path, n) for n in sorted(names)) if os.path.isdir(p)]


def configure_logging(home: str) -> None:
    """Root logging to stderr and ``<home>/daemon.log``. Called from ``main`` only."""
    os.makedirs(home, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (logging.StreamHandler(sys.stderr),
                    logging.FileHandler(os.path.join(home, LOG_FILE), encoding="utf-8")):
        handler.setFormatter(fmt)
        root.addHandler(handler)


class Daemon:
    """The long-polling bot. Construct, then ``run()``; or feed ``handle_update`` directly."""

    def __init__(self, config: config_mod.Config, api: BotAPI, store: Store, queue: RenderQueue,
                 cookbook: dict[str, recipe.RecipeDef], cook_fn: CookFn | None = None,
                 probe_fn: ProbeFn | None = None) -> None:
        self.config = config
        self.api = api
        self.store = store
        self.queue = queue
        self.cookbook = cookbook
        self.cook_fn: CookFn = cook_fn or cook
        self.probe_fn: ProbeFn = probe_fn or ffmpeg.probe
        self.home = config.home
        self.pid_path = os.path.join(self.home, PIDFILE)
        self.offset_path = os.path.join(self.home, OFFSET_FILE)
        self.offset: int | None = None
        self.sleep: Callable[[float], None] = time.sleep
        self._pidfile = None
        self._silenced: set[int | None] = set()
        self._pending_text: dict[int, tuple[str, int]] = {}   # chat_id -> (token, knob_idx)
        self._waiting: dict[int, int] = {}                    # user_id -> chat_id of their queued render
        self._clip_ends: dict[str, float] = {}                # upload path -> probed duration (s)
        self._stopping = False                                # set once shutdown begins; jobs word failures by it
        self._last_sweep = 0.0

    # -- pidfile / offset ------------------------------------------------------

    def acquire_pidfile(self) -> None:
        """Take an exclusive flock on ``<home>/daemon.pid``; raise if another daemon holds it."""
        os.makedirs(self.home, exist_ok=True)
        fh = open(self.pid_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            raise DaemonAlreadyRunning(f"another clipwrightd holds {self.pid_path}") from None
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._pidfile = fh

    def release_pidfile(self) -> None:
        if self._pidfile is None:
            return
        fcntl.flock(self._pidfile, fcntl.LOCK_UN)
        self._pidfile.close()
        self._pidfile = None

    def load_offset(self) -> int | None:
        try:
            with open(self.offset_path, encoding="utf-8") as fh:
                return int(fh.read().strip())
        except (OSError, ValueError):
            return None

    def save_offset(self) -> None:
        if self.offset is None:
            return
        tmp = self.offset_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(f"{self.offset}\n")
        os.replace(tmp, self.offset_path)

    # -- the loop ------------------------------------------------------------

    def run(self, once: bool = False) -> int:
        """Poll forever (or one batch with ``once``). Returns a process exit code.

        0 after a clean stop (Ctrl-C / SIGTERM, or the batch with ``once``),
        1 when ``once`` hit a transient error, 2 when Telegram rejected the
        token — that one is fatal at any time, since the token is read once
        at startup and no amount of backoff will make it valid.
        """
        self.acquire_pidfile()
        self.offset = self.load_offset()
        backoff = BACKOFF_MIN_S
        log.info("clipwrightd polling from offset %s (home %s)", self.offset, self.home)
        try:
            self._maybe_sweep()
            while True:
                try:
                    updates = self.api.get_updates(self.offset, timeout=POLL_TIMEOUT_S,
                                                   allowed_updates=ALLOWED_UPDATES)
                except Exception as err:
                    expected = isinstance(err, BotAPIError)
                    if expected and err.error_code in FATAL_API_CODES:
                        log.error("Telegram rejected the bot token (%s) — fix CLIPWRIGHT_BOT_TOKEN in %s",
                                  err, os.path.join(self.home, config_mod.ENV_FILE))
                        return 2
                    wait = max(backoff, float(err.retry_after or 0)) if expected else backoff
                    log.warning("getUpdates failed (%s); retrying in %.0f s", err, wait,
                                exc_info=not expected)
                    self.sleep(wait)
                    backoff = min(BACKOFF_MAX_S, backoff * 2)
                    if once:
                        return 1
                    continue
                backoff = BACKOFF_MIN_S
                for update in updates or []:
                    self.handle_update(update)
                    self.offset = int(update["update_id"]) + 1
                    self.save_offset()          # per update: an interrupted batch replays only the one in flight
                self._maybe_sweep()
                if once:
                    return 0
        except KeyboardInterrupt:
            self._stopping = True    # first: a tty's Ctrl-C reached the ffmpeg child too, and its job is about to fail
            log.info("interrupted; shutting down")
            return 0
        finally:
            self._stopping = True
            try:
                self.save_offset()
            except OSError as err:
                log.warning("could not save the update offset: %s", err)
            self._shutdown()
            self.release_pidfile()

    def _shutdown(self) -> None:
        """Let the running render finish, then tell anyone still in line that theirs was dropped."""
        if self.queue.size:
            log.info("waiting for the running render to finish (Ctrl-C again to abandon it)")
        for uid, _job in self.queue.stop():
            chat_id = self._waiting.get(uid)
            if chat_id is not None:
                self._tell(chat_id, "I'm shutting down before your render ran — send that again in a minute.")

    def handle_update(self, update: dict) -> None:
        """Dispatch one update. Never raises: failures are logged and told to the user."""
        try:
            if isinstance(update.get("callback_query"), dict):
                self._handle_callback(update["callback_query"])
            elif isinstance(update.get("message"), dict):
                self._handle_message(update["message"])
        except Exception:
            log.exception("update %s failed", update.get("update_id"))
            chat_id = self._chat_of(update)
            if chat_id is not None:
                self._tell(chat_id, "Something went wrong on my side — try that again.")

    # -- disk ------------------------------------------------------------------

    def _maybe_sweep(self) -> None:
        """Run ``sweep`` once an hour. It touches the disk and the database, so a
        failure (a directory the daemon cannot read, a full disk, a locked
        database) is logged and tried again next hour — never let out of the loop."""
        if time.time() - self._last_sweep < SWEEP_INTERVAL_S:
            return
        try:
            self.sweep()
        except Exception:
            log.exception("sweep failed; next try in %.0f s", SWEEP_INTERVAL_S)

    def sweep(self, now: float | None = None) -> None:
        """Reclaim disk: expire idle sessions, drop dead render dirs, remove unreferenced uploads.

        A session idle for ``retention_days`` is deleted; its render dir goes
        with it (as does any render dir with no session at all). An upload
        survives while a live session or a ledger entry names it, so every
        export stays remixable; the un-exported ones age out with their
        session. One unreadable directory or undeletable file is logged and
        skipped, not the end of the sweep.
        """
        now = time.time() if now is None else now
        self._last_sweep = now
        expired = self.store.expire_sessions(now - self.config.retention_days * 86400)
        live = self.store.session_tokens()
        dropped_dirs = 0
        for user_dir in _subdirs(os.path.join(self.home, "renders")):
            for token_dir in _subdirs(user_dir):
                if os.path.basename(token_dir) not in live:
                    shutil.rmtree(token_dir, ignore_errors=True)
                    dropped_dirs += 1
        keep = {os.path.realpath(p) for p in self.store.referenced_inputs()}
        dropped_uploads = 0
        for user_dir in _subdirs(os.path.join(self.home, "uploads")):
            try:
                names = os.listdir(user_dir)
            except OSError as err:
                log.warning("sweep: cannot list %s: %s", user_dir, err)
                continue
            for name in names:
                path = os.path.join(user_dir, name)
                if os.path.isfile(path) and os.path.realpath(path) not in keep:
                    try:
                        _unlink(path)
                    except OSError as err:
                        log.warning("sweep: cannot remove %s: %s", path, err)
                        continue
                    self._clip_ends.pop(path, None)
                    dropped_uploads += 1
        if expired or dropped_dirs or dropped_uploads:
            log.info("sweep: expired %d session(s), removed %d render dir(s) and %d upload(s)",
                     len(expired), dropped_dirs, dropped_uploads)

    def _user_bytes(self, uid: int) -> int:
        """Bytes on disk under this user's uploads and renders."""
        total = 0
        for sub in ("uploads", "renders"):
            for root, _dirs, files in os.walk(os.path.join(self.home, sub, str(uid))):
                for name in files:
                    try:
                        total += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        continue
        return total

    # -- gates ---------------------------------------------------------------

    def _allowed(self, from_user: dict | None) -> int | None:
        """The sender's id when allowlisted; otherwise None, logged once per stranger."""
        uid = (from_user or {}).get("id")
        if isinstance(uid, int) and uid in self.config.allowed:
            return uid
        if uid not in self._silenced:
            self._silenced.add(uid)
            log.warning("dropping update from non-allowlisted user %s", uid)
        return None

    def _busy(self, uid: int) -> str | None:
        """Why the queue would refuse a job for ``uid`` right now, or None when it would take one."""
        if self.queue.has(uid):
            return STILL_RENDERING
        if not self.queue.can_accept(uid):
            return QUEUE_FULL
        return None

    @staticmethod
    def _chat_of(update: dict) -> int | None:
        msg = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
        return (msg.get("chat") or {}).get("id")

    def _tell(self, chat_id: int, text: str) -> None:
        """send_message that swallows its own failure — used only for last-resort notices."""
        try:
            self.api.send_message(chat_id, text)
        except BotAPIError as err:
            log.warning("could not message chat %s: %s", chat_id, err)

    # -- messages ------------------------------------------------------------

    def _handle_message(self, msg: dict) -> None:
        uid = self._allowed(msg.get("from"))
        if uid is None:
            return
        chat_id = msg["chat"]["id"]
        text = msg.get("text")
        if isinstance(text, str) and (command := _command_of(text)) is not None:
            self._handle_command(command, msg, uid, chat_id)
            return
        media = _media_of(msg)
        if media is not None:
            self._on_video(media, uid, chat_id)
        elif isinstance(text, str):
            self._on_text(text, uid, chat_id)
        else:
            self.api.send_message(chat_id, "Send me a video (mp4/mov/webm) to start, or /help.")

    def _handle_command(self, command: str, msg: dict, uid: int, chat_id: int) -> None:
        if command == "start":
            self.api.send_message(
                chat_id,
                f"Hi! Send me a video (up to {_mb(self.config.max_upload_bytes)}, "
                f"{self.config.max_duration_s:.0f} s) and I'll turn it into a looping GIF "
                "you can tune with buttons. /help for the details.")
        elif command == "help":
            self.api.send_message(chat_id, HELP_TEXT)
        elif command == "recipes":
            lines = [f"{d.emoji} {name} — {d.blurb}" for name, d in self.cookbook.items()]
            self.api.send_message(chat_id, "Cookbook:\n" + "\n".join(lines))
        elif command == "remix":
            self._on_remix(msg, uid, chat_id)
        else:
            self.api.send_message(chat_id, f"I don't know /{command}. Try /help.")

    def _on_video(self, media: dict, uid: int, chat_id: int) -> None:
        """Gate an upload on what Telegram already told us, then hand the ingest to the worker.

        Nothing here waits on the network or on ffprobe: the size, queue and
        disk-budget gates need only the message, and the download + probe
        run as the user's queued job, so a slow or hostile upload costs its
        sender their one slot, not everyone the poll loop.
        """
        cap = self.config.max_upload_bytes
        size = media.get("file_size")
        if isinstance(size, int) and size > cap:
            self.api.send_message(
                chat_id, f"That's {_mb(size)}; I can take up to {_mb(cap)}. Trim it and resend.")
            return
        if (busy := self._busy(uid)) is not None:
            self.api.send_message(chat_id, busy)
            return
        used = self._user_bytes(uid)
        if used + (size if isinstance(size, int) else 0) > self.config.max_user_bytes:
            self.api.send_message(
                chat_id, f"Your clips here add up to {_mb(used)}, and I keep at most "
                f"{_mb(self.config.max_user_bytes)} per person. Clips behind your exports stay "
                f"so /remix keeps working; the rest clears once its session has sat idle for "
                f"{self.config.retention_days:g} days.")
            return
        self._pending_text.pop(chat_id, None)
        self._submit(uid, chat_id, self._ingest_job(media, uid, chat_id))

    def _ingest_job(self, media: dict, uid: int, chat_id: int) -> Job:
        """The queued half of an upload: fetch, probe, open the session, render its first preview."""
        def run() -> None:
            try:
                token = self._ingest(media, uid, chat_id)
            except _Rejected as why:
                self._tell(chat_id, str(why))
                return
            except Exception as exc:
                self._report_failure(chat_id, exc, f"upload from user {uid}")
                return
            self._job(token, self._render_preview)()
        return run

    def _ingest(self, media: dict, uid: int, chat_id: int) -> str:
        """Download and probe one upload; return the token of its new session (``_Rejected`` otherwise)."""
        cap = self.config.max_upload_bytes
        self.api.send_chat_action(chat_id, "upload_video")
        info = self.api.get_file(media["file_id"])
        ext = _EXT_FOR_MIME.get(str(media.get("mime_type", "")).lower(), "mp4")
        upload_dir = os.path.join(self.home, "uploads", str(uid))
        os.makedirs(upload_dir, exist_ok=True)
        # hex, never token_urlsafe: a name starting with "-" would read as an option in the ⌘ Show CLI line
        dest = os.path.join(upload_dir, f"{secrets.token_hex(6)}.{ext}")
        try:
            self._download(info["file_path"], dest, cap)
            probe = self._probe_checked(dest)
        except _Rejected:
            _unlink(dest)
            raise

        inst = recipe.defaults(self.cookbook[DEFAULT_RECIPE])
        inst["input"] = dest
        if probe.duration > 0:
            self._clip_ends[dest] = probe.duration
            inst["from"] = recipe.fmt_time(0.0)
            inst["to"] = recipe.fmt_time(probe.duration)
            _clamp_segment(inst)
        token = self.store.create_session(uid, chat_id, inst)
        log.info("session %s for user %s: %s (%.1fs %dx%d)", token, uid, dest,
                 probe.duration, probe.width, probe.height)
        return token

    def _download(self, file_path: str, dest: str, cap: int) -> None:
        try:
            self.api.download_file(file_path, dest, max_bytes=cap)
        except BotAPIError as err:
            if err.error_code == 413:      # the stream ran past the cap; the partial file is gone
                raise _Rejected(f"That file is over {_mb(cap)}. Trim it and resend.") from None
            raise

    def _probe_checked(self, path: str) -> ffmpeg.Probe:
        """Probe an upload and refuse it (``_Rejected``) when it breaks a limit."""
        try:
            probe = self.probe_fn(path)
        except ffmpeg.FFmpegError as err:
            if self._stopping:            # a terminal's Ctrl-C reached ffprobe too: not the clip's fault
                raise _Rejected(SHUT_DOWN_MID_RENDER) from None
            log.warning("probe rejected %s: %s", path, err)
            raise _Rejected("I couldn't read that as a video. Send an mp4, mov or webm.") from None
        if probe.duration > self.config.max_duration_s:
            raise _Rejected(f"That clip is {probe.duration:.1f} s; I take up to "
                            f"{self.config.max_duration_s:.0f} s. Trim it and resend.")
        if max(probe.width, probe.height) > self.config.max_dim:
            raise _Rejected(f"That clip is {probe.width}×{probe.height}; I take up to "
                            f"{self.config.max_dim} px on the long side. Downscale it and resend.")
        return probe

    def _on_text(self, text: str, uid: int, chat_id: int) -> None:
        pending = self._pending_text.get(chat_id)
        if pending is None:
            self.api.send_message(chat_id, "Send me a video to start, or /help.")
            return
        token, knob_idx = pending
        sess = self.store.get(token)
        if sess is None:
            del self._pending_text[chat_id]
            self.api.send_message(chat_id, "That session has expired — send the video again.")
            return
        if sess.user_id != uid:
            self.api.send_message(chat_id, "That prompt is for someone else's session.")
            return
        defn = self._defn(sess)
        knob = _text_knob(defn, knob_idx)
        if knob is None:                      # the session changed recipe under the prompt
            del self._pending_text[chat_id]
            self.api.send_message(chat_id, "That prompt no longer matches the session — use the buttons under the preview.")
            return
        new = copy.deepcopy(sess.recipe)
        recipe.set_(new, knob.key, text.strip())
        problems = recipe.validate(new, defn)
        if problems:
            self.api.send_message(chat_id, "; ".join(problems) + ". Try again:",
                                  reply_markup=_force_reply(knob))
            return
        if (busy := self._busy(uid)) is not None:   # the prompt stays pending; the retry is taken
            self.api.send_message(chat_id, busy, reply_markup=_force_reply(knob))
            return
        del self._pending_text[chat_id]
        self.store.update(token, new)
        self._submit(uid, chat_id, self._job(token, self._render_again))

    def _on_remix(self, msg: dict, uid: int, chat_id: int) -> None:
        target = msg.get("reply_to_message") or {}
        sent = next((target[k] for k in ("animation", "document") if isinstance(target.get(k), dict)), None)
        if sent is None or not sent.get("file_unique_id"):
            self.api.send_message(chat_id, "Reply /remix to a GIF I sent and I'll reopen its knobs.")
            return
        entry = self.store.ledger_entry(sent["file_unique_id"])
        if entry is None:
            self.api.send_message(chat_id, "I don't have a recipe for that file — it wasn't one of my exports.")
            return
        if not self._may_remix(uid, entry):
            log.warning("user %s asked to remix %s, exported by user %s", uid, entry.file_unique_id, entry.user_id)
            self.api.send_message(chat_id, "That export belongs to someone else — only they (or the owner) can remix it.")
            return
        inst = entry.recipe
        src = inst.get("input")
        if isinstance(src, str) and not os.path.isfile(src):
            self.api.send_message(chat_id, "The clip behind that export is no longer on disk — send it again to start over.")
            return
        if (busy := self._busy(uid)) is not None:
            self.api.send_message(chat_id, busy)
            return
        _clamp_segment(inst)
        token = self.store.create_session(uid, chat_id, inst)
        log.info("remix %s -> session %s for user %s", sent["file_unique_id"], token, uid)
        self._submit(uid, chat_id, self._job(token, self._render_preview))

    def _may_remix(self, uid: int, entry: LedgerEntry) -> bool:
        """The owner may remix anything; anyone else only what they exported themselves."""
        if uid == self.config.owner_id:
            return True
        exporter = entry.user_id
        if exporter is None and entry.token:          # a row from before the column existed
            sess = self.store.get(entry.token)
            exporter = sess.user_id if sess else None
        return exporter is not None and exporter == uid

    # -- callbacks -----------------------------------------------------------

    def _handle_callback(self, cq: dict) -> None:
        uid = self._allowed(cq.get("from"))
        if uid is None:
            return
        try:
            toast = self._dispatch_callback(cq, uid)
        except Exception:
            log.exception("callback %s failed", cq.get("data"))
            toast = "Something went wrong on my side — try that again."
        try:
            self.api.answer_callback_query(cq["id"], toast)
        except BotAPIError as err:                 # a replayed press is too old to answer; the work is done
            log.warning("could not answer callback %s: %s", cq.get("id"), err)

    def _dispatch_callback(self, cq: dict, uid: int) -> str | None:
        """Act on one callback and return the toast to answer it with."""
        try:
            cb = decode_cb(cq.get("data") or "")
        except ValueError:
            return STALE_BUTTON
        sess = self.store.get(cb.session)
        if sess is None:
            return "That session has expired — send the video again."
        if sess.user_id != uid:
            return "That's someone else's session."
        defn = self._defn(sess)

        if cb.kind == "n":
            return None
        if cb.kind == "t":
            knob = _text_knob(defn, cb.knob_idx)
            if knob is None:
                return STALE_BUTTON
            self._pending_text[sess.chat_id] = (cb.session, cb.knob_idx)
            self.api.send_message(sess.chat_id, f"Send the {knob.label} (up to {knob.max_len} characters):",
                                  reply_markup=_force_reply(knob))
            return None
        if cb.kind == "r":
            return self._switch_recipe(cb, sess, uid)
        if cb.kind == "c":
            try:
                new = recipe.apply_knob(sess.recipe, defn, cb.knob_idx, cb.value_idx)
            except recipe.RecipeError as err:
                return str(err)
            if (new.get("to") != sess.recipe.get("to") and self._clamp_to_clip(new)
                    and new == sess.recipe):
                return CLIP_END           # out ▶ at the last frame: nothing to render, nothing to undo
            if _segment_s(new) > SEGMENT_CAP_S:
                return f"Clips are capped at {SEGMENT_CAP_S:g} s here — move the in-point first."
            if (busy := self._busy(uid)) is not None:
                return busy
            self.store.update(cb.session, new)
            return self._submit(uid, sess.chat_id, self._job(cb.session, self._render_again), quiet=True)
        return self._do_action(cb, sess, uid)

    def _clamp_to_clip(self, inst: dict) -> bool:
        """Pull ``to`` back to the clip's probed end; True when it had to move.

        The engine clamps silently; here the keyboard, caption and sidecar
        must all show the out-point that is really rendered.
        """
        end = self._clip_end(inst)
        if end is None or inst.get("to") is None:
            return False
        if _ms(recipe.parse_time(inst["to"])) <= _ms(end):
            return False
        inst["to"] = recipe.fmt_time(end)
        return True

    def _clip_end(self, inst: dict) -> float | None:
        """The probed duration of the session's input, cached per upload path.

        Filled at ingest; a session that outlived a restart is probed again
        on its first out-point press (the file passed the gate once, so this
        is a local read). None when there is no input or it cannot be read.
        """
        src = inst.get("input")
        if not isinstance(src, str) or not src:
            return None
        end = self._clip_ends.get(src)
        if end is None:
            try:
                end = self.probe_fn(src).duration
            except ffmpeg.FFmpegError as err:
                log.warning("could not re-probe %s for its duration: %s", src, err)
                end = 0.0                 # remembered, so a vanished clip is not probed on every press
            self._clip_ends[src] = end
        return end if end > 0 else None

    def _switch_recipe(self, cb: Callback, sess: Session, uid: int) -> str | None:
        """Restart the session on another recipe, keeping the clip and its trim."""
        target = self.cookbook.get(cb.recipe)
        if target is None:
            return STALE_BUTTON
        if target.name == sess.recipe.get("recipe"):
            return None
        if (busy := self._busy(uid)) is not None:
            return busy
        inst = recipe.defaults(target)
        for key in ("input", "from", "to"):
            if sess.recipe.get(key) is not None:
                inst[key] = sess.recipe[key]
        self._pending_text.pop(sess.chat_id, None)
        self.store.update(cb.session, inst)
        return self._submit(uid, sess.chat_id, self._job(cb.session, self._render_again), quiet=True)

    def _do_action(self, cb: Callback, sess: Session, uid: int) -> str | None:
        chat_id = sess.chat_id
        if cb.action == "grid":
            return GRID_TOAST
        if cb.action == "cli":
            self.api.send_message(chat_id, cli_command(_public(sess.recipe), cookbook=self.cookbook))
            return None
        if (busy := self._busy(uid)) is not None:
            return busy
        if cb.action == "undo":
            if self.store.undo(cb.session) is None:
                return "Nothing to undo."
            return self._submit(uid, chat_id, self._job(cb.session, self._render_again), quiet=True)
        if self.store.quota_hit(uid, self.config.per_day_quota):
            self.api.send_message(
                chat_id, f"You've used today's {self.config.per_day_quota} exports — "
                "the counter resets at midnight UTC. Previews still work.")
            return "Daily export limit reached."
        return self._submit(uid, chat_id, self._job(cb.session, self._render_export), quiet=True)

    # -- render jobs (run on the queue's worker) -----------------------------

    def _submit(self, uid: int, chat_id: int, job: Job, *, quiet: bool = False) -> str | None:
        """Queue ``job``; tell the user the position when they must wait. Returns the toast.

        Callers gate on ``_busy`` first, so a refusal here is a race the
        queue's own contract still allows; it is reported the same way.
        """
        position = self.queue.submit(uid, job)
        if position is None:
            text = self._busy(uid) or QUEUE_FULL
        else:
            self._waiting[uid] = chat_id
            if position > 1:
                text = f"Queued — #{position} in line."
            else:
                text = "Rendering…" if quiet else None
        if text is not None and not quiet:
            self.api.send_message(chat_id, text)
        return text

    def _job(self, token: str, step: Callable[[Session], None]) -> Job:
        """Wrap a render step: load the session, run it, turn any failure into one user message."""
        def run() -> None:
            sess = self.store.get(token)
            if sess is None:
                log.warning("session %s vanished before its render ran", token)
                return
            try:
                step(sess)
            except Exception as exc:
                self._report_failure(sess.chat_id, exc, f"render for session {token}")
        return run

    def _report_failure(self, chat_id: int, exc: Exception, what: str) -> None:
        """One log line and one message for a job that failed.

        While the daemon is shutting down the honest message is that it was
        stopped: a terminal's Ctrl-C reaches the ffmpeg child too, so the
        render dies with it and 'try a different clip' would be wrong advice.
        """
        if self._stopping:
            log.warning("%s was cut short by the shutdown: %s", what, exc)
            self._tell(chat_id, SHUT_DOWN_MID_RENDER)
            return
        log.error("%s failed", what, exc_info=exc)
        self._tell(chat_id, _user_error(exc))

    def _render_preview(self, sess: Session) -> None:
        """First render of a session: cook a proxy and send it with the keyboard."""
        result = self._cook(sess, proxy=True)
        try:
            resp = self.api.send_animation(
                sess.chat_id, result.mp4, caption=self._caption(sess.recipe, result.report),
                reply_markup=self._markup(sess), filename=PREVIEW_NAME)
        finally:
            self._discard(result)
        self.store.set_message_id(sess.token, resp["message_id"])

    def _render_again(self, sess: Session) -> None:
        """Re-cook the proxy after a knob change and swap it into the preview message."""
        if sess.message_id is None:
            self._render_preview(sess)
            return
        result = self._cook(sess, proxy=True)
        try:
            self.api.edit_message_media(
                sess.chat_id, sess.message_id, result.mp4, PREVIEW_NAME,
                caption=self._caption(sess.recipe, result.report), reply_markup=self._markup(sess))
        finally:
            self._discard(result)

    def _render_export(self, sess: Session) -> None:
        """Full cook; send the GIF, ledger it, then send its sidecar.

        The ledger entry is written the moment the GIF is delivered, so a
        sidecar that fails to send costs the user a file, never ``/remix``.
        """
        result = self._cook(sess, proxy=False)
        try:
            gif = result.gif
            if not gif or not os.path.isfile(gif):
                raise RuntimeError(f"cook returned no GIF for session {sess.token}")
            sidecar = (result.report or {}).get("sidecar")
            if not sidecar or not os.path.isfile(sidecar):
                raise RuntimeError(f"cook returned no sidecar for session {sess.token}")
            caption = _export_caption(result.report or {}, os.path.getsize(gif))
            # without the flag Telegram re-types a .gif as an animation and delivers an MP4 transcode
            resp = self.api.send_document(sess.chat_id, gif, os.path.basename(gif), caption=caption,
                                          disable_content_type_detection=True)
            sent = _sent_file(resp)
            if sent and sent.get("file_unique_id"):
                self.store.ledger_put(sent["file_unique_id"], sess.recipe, sess.token, sess.user_id)
            else:
                log.warning("export %s: no file_unique_id in sendDocument response", sess.token)
            try:
                self.api.send_document(sess.chat_id, sidecar, os.path.basename(sidecar),
                                       caption="Recipe sidecar — reply /remix to this GIF to reopen it.")
            except BotAPIError as err:
                log.warning("export %s: sidecar upload failed: %s", sess.token, err)
                self._tell(sess.chat_id, "The recipe sidecar didn't go through, but /remix on that GIF still works.")
        finally:
            self._discard(result)

    def _cook(self, sess: Session, *, proxy: bool) -> CookResult:
        """Run the engine for this session, keeping the 'uploading video…' indicator alive meanwhile.

        The sidecar and CLI line the engine writes name the input by
        ``public_input`` — the file name alone — so the server path, and the
        uploader's id in it, never leave this machine.
        """
        out_dir = os.path.join(self.home, "renders", str(sess.user_id), sess.token)
        os.makedirs(out_dir, exist_ok=True)
        done = threading.Event()
        beat = threading.Thread(target=self._heartbeat, args=(sess.chat_id, done),
                                name="clipwright-heartbeat", daemon=True)
        beat.start()
        try:
            return self.cook_fn(sess.recipe, out_dir=out_dir, proxy=proxy, cookbook=self.cookbook,
                                public_input=_public(sess.recipe).get("input"))
        finally:
            done.set()
            beat.join()

    def _heartbeat(self, chat_id: int, done: threading.Event) -> None:
        """``sendChatAction`` now and every HEARTBEAT_S until ``done`` — Telegram shows one for ~5 s."""
        while True:
            try:
                self.api.send_chat_action(chat_id, "upload_video")
            except BotAPIError as err:
                log.warning("heartbeat for chat %s failed: %s", chat_id, err)
            if done.wait(HEARTBEAT_S):
                return

    @staticmethod
    def _discard(result: CookResult) -> None:
        """Remove a cook's outputs: Telegram has them (or refused them), and the ledger remembers the recipe."""
        for path in (result.gif, result.mp4, (result.report or {}).get("sidecar")):
            if path:
                _unlink(path)

    # -- helpers -------------------------------------------------------------

    def _defn(self, sess: Session) -> recipe.RecipeDef:
        name = sess.recipe.get("recipe")
        if name not in self.cookbook:
            raise recipe.RecipeError(f"session {sess.token} names unknown recipe {name!r}")
        return self.cookbook[name]

    def _markup(self, sess: Session) -> dict:
        return {"inline_keyboard": build_keyboard(self._defn(sess), sess.recipe, sess.token,
                                                  cookbook=self.cookbook)}

    @staticmethod
    def _caption(inst: dict, report: dict | None) -> str:
        """``<recipe> · <from>–<to> [· capped …] [· loop nudged …] [· loop: …]`` — what the engine rendered."""
        report = report or {}
        parts = [str(inst.get("recipe"))]
        start, end = report.get("from", inst.get("from")), report.get("to", inst.get("to"))
        if start is not None and end is not None:
            parts.append(f"{recipe.fmt_time(start)}–{recipe.fmt_time(end)}")
        if report.get("capped"):
            parts.append(f"capped to {SEGMENT_CAP_S:g} s")
        nudge = report.get("loop_nudge_frames")
        if nudge:
            parts.append(f"loop nudged {nudge:+d}f")
        loop = report.get("loop")
        if loop and loop != "seamless":
            parts.append(f"loop: {loop}")   # e.g. "boomerang (degraded)" — never hide a degrade
        return " · ".join(parts) + "\nTweak with the buttons; ⬇ Export when it's right."


def main(argv: list[str] | None = None) -> int:
    """``python3 -m clipwrightd [--home DIR] [--once]``."""
    parser = argparse.ArgumentParser(
        prog="clipwrightd",
        description="Clipwright's Telegram front door: long-polls the Bot API and renders on request.")
    parser.add_argument("--home", help="state directory holding bot.env (default: $CLIPWRIGHT_HOME or ~/.clipwright)")
    parser.add_argument("--once", action="store_true", help="handle one batch of updates, then exit")
    args = parser.parse_args(argv)
    os.umask(0o077)          # daemon.log, uploads/ and renders/ are private to this account

    try:
        cfg = config_mod.load_config(
            os.path.join(args.home, config_mod.ENV_FILE) if args.home else None)
    except config_mod.ConfigError as err:
        print(f"clipwrightd: {err}", file=sys.stderr)
        return 2
    if args.home:
        cfg.home = os.path.abspath(args.home)
    configure_logging(cfg.home)

    api = BotAPI(cfg.token)
    store = Store(os.path.join(cfg.home, STATE_DB))
    render_queue = RenderQueue(depth=cfg.queue_depth)
    daemon = Daemon(cfg, api, store, render_queue, recipe.load_cookbook())
    try:
        api.set_my_commands(COMMANDS)
    except BotAPIError as err:
        if err.error_code in FATAL_API_CODES:      # the token is wrong: say so now, before the pidfile
            log.error("Telegram rejected the bot token (%s) — fix CLIPWRIGHT_BOT_TOKEN in %s",
                      err, os.path.join(cfg.home, config_mod.ENV_FILE))
            store.close()
            return 2
        log.warning("setMyCommands failed: %s", err)

    def on_sigterm(signum: int, frame: object) -> None:
        daemon._stopping = True
        raise KeyboardInterrupt      # `kill`/`systemctl stop` take the Ctrl-C path: finish, tell, save, release

    previous = _install_sigterm(on_sigterm)
    try:
        return daemon.run(once=args.once)
    except DaemonAlreadyRunning as err:
        log.error("%s", err)
        return 1
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        store.close()


def _install_sigterm(handler: Callable[[int, object], None]) -> object | None:
    """Install ``handler`` for SIGTERM and return the previous one; None when this is not the main thread."""
    try:
        return signal.signal(signal.SIGTERM, handler)
    except ValueError:          # signal handlers can only be set from the main thread
        log.warning("not on the main thread: SIGTERM will not shut down cleanly")
        return None


if __name__ == "__main__":
    raise SystemExit(main())
