"""clipwrightd.poll — the getUpdates loop and everything a Telegram update does.

``Daemon`` is the whole bot: it owns the pidfile (``<home>/daemon.pid``,
``fcntl.flock`` — a second poller on the same token makes updates vanish, so
the second one refuses to start), persists the update offset to
``<home>/offset`` after every update it has handled (so a restart replays
at most the one in flight), and rewrites that same file after every
successful ``getUpdates`` — an empty batch keeps the same integer, and a
daemon that has not seen an update yet leaves the file empty — so its mtime
is the last poll that returned. ``clipwrightd.ops health`` reads that mtime;
there is no separate heartbeat file. Each update is dispatched to one
handler. The engine is reached only through ``cook_fn`` / ``probe_fn``, both
injectable so tests run with fakes and never touch ffmpeg or a socket.

Flow, per the plan: a photo, video, or ``/gif`` intention arrives → size
gate → queue and disk-budget gates → *on the render worker*: obtain a
**seed** (download the replied media, fetch a Commons still for text, or
generate a local typecard) → ffprobe gate → a recipe session → a proxy
MP4 sent with the knob keyboard. The poll thread never waits on a
download, a probe or a web fetch, and the user's one queue slot is taken
for the whole ingest. Every button press edits that one message in place;
the top row switches recipes. Export cooks the real GIF, sends it as a
document with its ``.recipe.toml`` sidecar, and ledgers the sent
``file_unique_id`` so ``/remix`` can reopen it later — for the user who
exported it (or the owner): the recipe names that user's private upload.
In a DM anyone not on the allowlist gets silence (logged once). The
allowlist is owner + ``CLIPWRIGHT_FRIEND_IDS`` **plus anyone seen in a
served group** (or verified with ``getChatMember`` against a known
room): once they are in the chat, they can DM the bot the same way
friends do — photo or video without ``/gif``, full foundry. A true
stranger who has never been in a room still gets silence.

Groups and channels are served the same way. A room is not an inbox: only
explicit commands are acted on, and ``/gif`` is the way in — reply to a
photo, video or sentence, or ``/gif <words>``, or a bare ``/gif`` for a tiny
self-aware typecard. Bare videos, chatter, stickers and joins are ignored
without a word (other bots own the commands we do not know, unless one is
addressed ``@us``), but chatter still *remembers* the sender so their DMs
work later. Ignored room text — not a command this bot handles — is also
appended as a short preview to ``<home>/digest-events.jsonl`` for
``clipwrightd.ops digest``, along with session opens, web-seed fallbacks,
and render errors. That file is not a second poller. Everyone in a group or channel the bot is in is trusted (the
owner put it there on purpose — Test2 the same as weir and bencho). Everything the bot sends in a room is a reply — to the ``/gif``
that opened the session (kept as ``origin_message_id``) or to the message
that asked. Buttons and text prompts belong to the user who opened the
session. Senders that are not one person — anonymous admins, unsigned
channel posts, members posting as a channel, Telegram's own service
accounts — share one id and
are not served: ``/gif`` from them gets ``ANON_HINT``, the rest is
dropped. ``CLIPWRIGHT_GROUP_IDS``, when set, names the only rooms the
bot works in; unset, any group or channel it is added to.

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
from clipwright.pipelines.common import SEGMENT_CAP_S, STILL_HOLD_S
from clipwrightd import config as config_mod
from clipwrightd.api import BotAPI, BotAPIError
from clipwrightd.fetch import FetchError, fetch_image
from clipwrightd.keyboards import Callback, build_keyboard, decode_cb
from clipwrightd.queue import RenderQueue
from clipwrightd.session import LedgerEntry, Session, Store

log = logging.getLogger("clipwrightd.poll")

DEFAULT_RECIPE = "gifify"
PHOTO_RECIPE = "ken-burns"
TEXT_RECIPE = "typecard"
POLL_TIMEOUT_S = 50
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 60.0
HEARTBEAT_S = 4.0            # Telegram shows a chat action for ~5 s; keep it alive while a cook runs
SWEEP_INTERVAL_S = 3600.0
ALLOWED_UPDATES = ["message", "callback_query", "channel_post"]
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
GROUP_OFF = "Group rendering is off in this chat."
GROUP_START = (
    "This is Clipwright, the fork Grok Bot runs (clipwright-grok). "
    "Reply /gif to a photo, a video or a sentence in this chat — or /gif "
    "some words — and I'll turn it into a GIF you can tune with buttons. "
    "Bare /gif if you brought nothing. /help for details."
)
ANON_HINT = "I can't tell anonymous admins or channels apart — send /gif as yourself."
NAG_COOLDOWN_S = 60.0        # a guest's hints and refusals in a group: one of each per minute, the rest logged
USERNAME_RETRY_S = 60.0      # how often a failed startup getMe is retried when a /cmd@name needs it
BARE_CAPTIONS = (
    "MISSING ARGUMENT",
    "/gif what, exactly?",
    "this space intentionally left blank",
    "nothing to gif. the void stares back.",
)
# Telegram's stand-in senders: anonymous admins, "send as channel" posters and linked-channel forwards
TELEGRAM_SERVICE_IDS = frozenset({1087968824, 136817688, 777000})
FATAL_API_CODES = (401, 404)   # Unauthorized / Not Found on the bot URL: the token itself is wrong
_EXT_FOR_MIME = {
    "video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm",
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
    "image/webp": "webp", "image/gif": "gif",
}
STILL_MAX_DIM = 8192         # photos are scaled down at cook time; refuse only the absurd

COMMANDS = [
    {"command": "start", "description": "What Grok Bot's Clipwright fork does"},
    {"command": "help", "description": "How to use this Clipwright bot"},
    {"command": "recipes", "description": "List the Clipwright cookbook"},
    {"command": "remix", "description": "Reply to a GIF this bot sent to reopen its knobs"},
    {"command": "gif", "description": "GIF a photo, video, or some words (or reply to one)"},
]

HELP_TEXT = (
    "I'm Clipwright — the GIF foundry Grok Bot runs and keeps alive "
    "(the clipwright-grok fork). "
    "Send a photo or a video, or /gif some words, and I turn it into a looping GIF "
    "you tune with buttons.\n\n"
    "• /gif is the front door: reply to a photo, a video or a sentence, or /gif dog.\n"
    "• Bare /gif with nothing to work with still answers — a tiny 'missing argument' GIF.\n"
    "• A sentence tries Wikimedia Commons for a still, then falls back to a local typecard.\n"
    "• Every button press re-renders the preview in place.\n"
    "• The top row of buttons switches recipes; /recipes describes them.\n"
    "• ✎ buttons ask for text — just reply to the prompt.\n"
    "• ⬇ Export sends the real GIF plus its .recipe.toml.\n"
    "• Reply /remix to any GIF I sent you to reopen its knobs.\n"
    "• ⌘ Show CLI prints the command that reproduces the render.\n\n"
    f"Clips are rendered {SEGMENT_CAP_S:g} s at a time — slide the in/out points to pick the part.\n\n"
    "In groups everyone in the room is trusted: reply /gif to a photo, a video or a sentence "
    "and I'll answer with the preview and its buttons. Only the person who sent /gif can press them. "
    "Once I've seen you in a group, you can DM me too — send a photo or a video, no /gif needed."
)

# ChatMember.status values that mean the user is still in the room.
ROOM_MEMBER_STATUSES = frozenset({"creator", "administrator", "member", "restricted"})

CookFn = Callable[..., CookResult]
ProbeFn = Callable[[str], ffmpeg.Probe]
FetchFn = Callable[[str, str], str]
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


def _photo_of(msg: dict) -> dict | None:
    """The largest photo size, or an image/* document. None when the message has no still."""
    photos = msg.get("photo")
    if isinstance(photos, list):
        sizes = [p for p in photos if isinstance(p, dict) and p.get("file_id")]
        if sizes:
            return max(sizes, key=lambda p: int(p.get("file_size") or 0) or int(p.get("width") or 0))
    doc = msg.get("document")
    if isinstance(doc, dict) and str(doc.get("mime_type", "")).startswith("image/"):
        return doc
    return None


def _seed_media(msg: dict) -> tuple[dict | None, str | None]:
    """``(media, 'photo'|'video')`` for a message that can seed a session, else ``(None, None)``."""
    photo = _photo_of(msg)
    if photo is not None:
        return photo, "photo"
    video = _media_of(msg)
    if video is not None:
        return video, "video"
    return None, None


def _text_of(msg: dict) -> str:
    """Plain text or caption of a message, stripped; empty when there isn't one."""
    for key in ("text", "caption"):
        value = msg.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _command_args(text: str) -> str:
    """``'/gif dog'`` -> ``'dog'``; empty when the command carried no rest."""
    parts = text.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _ext_for(media: dict, kind: str) -> str:
    mime = str(media.get("mime_type", "")).lower()
    if mime in _EXT_FOR_MIME:
        return _EXT_FOR_MIME[mime]
    return "jpg" if kind == "photo" else "mp4"


def _sent_file(resp: dict) -> dict | None:
    """The file object Telegram attached to a sent message, whatever it re-typed it as."""
    for key in ("document", "animation", "video"):
        if isinstance(resp.get(key), dict):
            return resp[key]
    return None


def _command_of(text: str) -> tuple[str, str | None] | None:
    """``"/start@bot arg"`` -> ``("start", "bot")``, ``"/start"`` -> ``("start", None)``; None for non-commands."""
    if not text.startswith("/"):
        return None
    word = text.split(None, 1)[0][1:]
    command, _, target = word.partition("@")
    return command.lower(), (target.lower() or None)


def _chat_kind(chat: dict | None) -> str | None:
    """``"dm"`` for a private chat, ``"group"`` for a group, supergroup or channel, None otherwise."""
    kind = (chat or {}).get("type")
    if kind == "private":
        return "dm"
    if kind in ("group", "supergroup", "channel"):
        return "group"
    return None


def _impersonal(msg: dict) -> bool:
    """True when the sender is not one person: an anonymous admin, an unsigned
    channel post, a bot or a service account.

    Such senders share one ``from.id`` (or have none), so serving them would
    pool their quota, their disk budget and their session ownership. A
    **signed** channel post still has a real ``from``; Telegram always sets
    ``sender_chat`` to the channel, and that alone is not impersonal.
    """
    sender = msg.get("from") or {}
    if bool(sender.get("is_bot")) or sender.get("id") in TELEGRAM_SERVICE_IDS:
        return True
    if (msg.get("chat") or {}).get("type") == "channel":
        return not isinstance(sender.get("id"), int)
    return bool(msg.get("sender_chat"))


def _from_us(msg: dict, username: str | None) -> bool:
    """True when ``msg`` is a reply to a message of ours (a bot's, and by name when we know ours)."""
    sender = (msg.get("reply_to_message") or {}).get("from") or {}
    if not sender.get("is_bot"):
        return False
    return username is None or str(sender.get("username", "")).lower() == username


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
                 probe_fn: ProbeFn | None = None, fetch_fn: FetchFn | None = None) -> None:
        self.config = config
        self.api = api
        self.store = store
        self.queue = queue
        self.cookbook = cookbook
        self.cook_fn: CookFn = cook_fn or cook
        self.probe_fn: ProbeFn = probe_fn or ffmpeg.probe
        self.fetch_fn: FetchFn = fetch_fn or fetch_image
        self.home = config.home
        self.pid_path = os.path.join(self.home, PIDFILE)
        self.offset_path = os.path.join(self.home, OFFSET_FILE)
        self.offset: int | None = None
        self.sleep: Callable[[float], None] = time.sleep
        self.clock: Callable[[], float] = time.monotonic       # for the cooldowns; injectable like sleep
        self._pidfile = None
        self.username: str | None = None                      # from getMe; None until run(), or when it failed
        self._username_retry_at = 0.0                         # clock() before which a failed getMe is not retried
        self._silenced: set[int | None] = set()
        self._silenced_chats: set[int] = set()                # groups off CLIPWRIGHT_GROUP_IDS, logged once each
        self._checked_rooms: dict[int, frozenset[int]] = {}   # uid -> group ids last getChatMember'd against
        for chat_id in config.group_ids:
            store.note_group(chat_id)
        self._nagged: dict[tuple[int, int, str], float] = {}  # (chat_id, user_id, text) -> clock() of the last send
        self._pending_text: dict[tuple[int, int], tuple[str, int]] = {}   # (chat_id, user_id) -> (token, knob_idx)
        self._waiting: dict[int, tuple[int, int | None]] = {}   # user_id -> (chat_id, origin) of their queued render
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

    def note_poll(self) -> None:
        """Refresh ``<home>/offset`` after ``getUpdates`` returns.

        The integer in the file stays the next update id. An empty batch
        rewrites that same integer. A daemon that has not seen an update yet
        leaves the file empty, which ``load_offset`` still reads as None.
        The ops health check treats this file's mtime as the last successful
        poll — the same state the daemon already keeps, not a new metric.
        """
        if self.offset is not None:
            self.save_offset()
            return
        with open(self.offset_path, "a", encoding="utf-8"):
            pass
        os.utime(self.offset_path, None)

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
        log.info("clipwrightd (clipwright-grok) polling from offset %s (home %s)",
                 self.offset, self.home)
        try:
            self.username = self._bot_username()    # inside the try: a Ctrl-C during a hung getMe still exits cleanly
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
                self.note_poll()            # empty batches too: offset mtime is the last poll that returned
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

    def _bot_username(self) -> str | None:
        """``getMe``'s username, so a group's ``/cmd@name`` can be told ours from another bot's.

        None when the call fails: every ``@name`` is then read as another
        bot's, which costs a group the menu-tapped form of our own commands
        (clients send those as ``/gif@name``) — so the next such command
        retries the call, at most once a minute, rather than waiting for a
        restart.
        """
        self._username_retry_at = self.clock() + USERNAME_RETRY_S
        try:
            name = (self.api.get_me() or {}).get("username")
        except BotAPIError as err:
            log.warning("getMe failed (%s); commands addressed @<bot> are treated as another bot's", err)
            return None
        return name.lower() if isinstance(name, str) and name else None

    def _ours(self, target: str | None) -> bool:
        """Whether a command's ``@target`` names this bot, fetching our name again when a startup getMe failed."""
        if target is None:
            return False
        if self.username is None and self.clock() >= self._username_retry_at:
            self.username = self._bot_username()
        return target == self.username

    def _shutdown(self) -> None:
        """Let the running render finish, then tell anyone still in line that theirs was dropped."""
        if self.queue.size:
            log.info("waiting for the running render to finish (Ctrl-C again to abandon it)")
        for uid, _job in self.queue.stop():
            where = self._waiting.get(uid)
            if where is not None:
                self._tell(where[0], "I'm shutting down before your render ran — send that again in a minute.",
                           where[1])

    def handle_update(self, update: dict) -> None:
        """Dispatch one update. Never raises: failures are logged and told to the user."""
        try:
            if isinstance(update.get("callback_query"), dict):
                self._handle_callback(update["callback_query"])
            elif isinstance(update.get("message"), dict):
                self._handle_message(update["message"])
            elif isinstance(update.get("channel_post"), dict):
                self._handle_message(update["channel_post"])
        except Exception:
            log.exception("update %s failed", update.get("update_id"))
            where = self._chat_of(update)
            if where is not None:
                self._tell(where[0], "Something went wrong on my side — try that again.", where[1])

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

    @staticmethod
    def _real_person(user: dict | None) -> int | None:
        """A single human's user id, or None for bots, service accounts, or a malformed ``from``."""
        if not isinstance(user, dict) or user.get("is_bot"):
            return None
        uid = user.get("id")
        if isinstance(uid, int) and uid not in TELEGRAM_SERVICE_IDS:
            return uid
        return None

    def _remember_person(self, user: dict | None) -> None:
        """Add a real person to the room-member allowlist (idempotent)."""
        uid = self._real_person(user)
        if uid is not None:
            self.store.note_member(uid)

    def _remember_joiners(self, msg: dict) -> None:
        """Note everyone in ``new_chat_members`` so they can DM without speaking first."""
        for user in msg.get("new_chat_members") or []:
            self._remember_person(user if isinstance(user, dict) else None)

    def _admit_from_rooms(self, uid: int) -> bool:
        """``getChatMember`` against known served groups; True and notes them when they still belong.

        A miss is remembered against the current room set so a stranger DMing
        in a loop does not hammer Telegram. A newly learned group changes the
        set and the next DM re-checks.
        """
        rooms = frozenset(self.store.list_groups())
        if not rooms:
            return False
        if self._checked_rooms.get(uid) == rooms:
            return False
        self._checked_rooms[uid] = rooms
        for chat_id in sorted(rooms):
            try:
                member = self.api.get_chat_member(chat_id, uid)
            except BotAPIError as err:
                log.debug("getChatMember chat %s user %s: %s", chat_id, uid, err)
                continue
            status = (member or {}).get("status")
            if status in ROOM_MEMBER_STATUSES:
                self.store.note_member(uid)
                log.info("admitted user %s from group %s (%s)", uid, chat_id, status)
                return True
        return False

    def _actor(self, from_user: dict | None, group: bool,
               chat_id: int | None = None) -> tuple[int, bool] | None:
        """Who is served: ``(uid, is_guest)``, or None for a stranger (silence, logged once).

        The allowlist (owner + friends) is served everywhere. In a group
        every other member is trusted too — the owner put the bot in the
        room on purpose — so ``is_guest`` is False; they are also remembered
        so a later DM works like a friend's. In a DM a sender is served if
        they are on the allowlist, already a remembered room member, or
        ``getChatMember`` says they still belong to a known served group.
        Anyone else is a stranger.
        """
        uid = (from_user or {}).get("id")
        if isinstance(uid, int):
            if group:
                if chat_id is not None:
                    self.store.note_group(chat_id)
                self._remember_person(from_user)
                return uid, False
            if uid in self.config.allowed or self.store.is_member(uid):
                return uid, False
            if self._admit_from_rooms(uid):
                return uid, False
        if uid not in self._silenced:
            self._silenced.add(uid)
            log.warning("dropping update from non-allowlisted user %s", uid)
        return None

    def _group_served(self, chat_id: int) -> bool:
        """Whether this group is one the bot works in: any group unless ``group_ids`` names some (logged once)."""
        if not self.config.group_ids or chat_id in self.config.group_ids:
            return True
        if chat_id not in self._silenced_chats:
            self._silenced_chats.add(chat_id)
            log.warning("ignoring group %s: not in CLIPWRIGHT_GROUP_IDS", chat_id)
        return False

    def _nag(self, chat_id: int, uid: int, text: str, origin: int | None, guest: bool) -> None:
        """``_say`` a hint or refusal; a guest hears each one at most once per NAG_COOLDOWN_S in a chat.

        Hints cost a guest nothing, so a member typing ``/gif`` in a loop
        would otherwise have the bot spend the group's send allowance on
        them, and every other member's render with it.
        """
        if guest:
            key, now = (chat_id, uid, text), self.clock()
            last = self._nagged.get(key)
            if last is not None and now - last < NAG_COOLDOWN_S:
                log.debug("not repeating %r to user %s in chat %s", text, uid, chat_id)
                return
            self._nagged[key] = now
        self._say(chat_id, text, origin)

    def _charge_guest(self, uid: int, chat_id: int) -> str | None:
        """Count one guest render; the refusal to send when the group is off or their day is spent.

        Called where the owner's export quota is: after every other gate has
        passed and right before the job is queued, so a refusal never
        charges a render that was not going to happen.
        """
        limit = self.config.guest_per_day_quota
        if limit <= 0:
            return GROUP_OFF
        if self.store.quota_hit(uid, limit):
            return f"You've used today's {limit} renders — try again tomorrow."
        log.info("guest render for user %s in chat %s", uid, chat_id)
        return None

    def _busy(self, uid: int) -> str | None:
        """Why the queue would refuse a job for ``uid`` right now, or None when it would take one."""
        if self.queue.has(uid):
            return STILL_RENDERING
        if not self.queue.can_accept(uid):
            return QUEUE_FULL
        return None

    @staticmethod
    def _chat_of(update: dict) -> tuple[int, int | None] | None:
        """``(chat_id, origin)`` of an update: ``origin`` is the message to reply to in a group, None in a DM."""
        msg = (update.get("message") or update.get("channel_post")
               or (update.get("callback_query") or {}).get("message") or {})
        chat_id = (msg.get("chat") or {}).get("id")
        if chat_id is None:
            return None
        return chat_id, msg.get("message_id") if _chat_kind(msg.get("chat")) == "group" else None

    def _say(self, chat_id: int, text: str, origin: int | None = None,
             reply_markup: dict | None = None) -> dict:
        """send_message, as a reply to ``origin`` when there is one (a group), plain otherwise (a DM)."""
        return self.api.send_message(chat_id, text, reply_markup=reply_markup, reply_to_message_id=origin)

    def _tell(self, chat_id: int, text: str, origin: int | None = None) -> None:
        """``_say`` that swallows its own failure — used only for last-resort notices."""
        try:
            self._say(chat_id, text, origin)
        except BotAPIError as err:
            log.warning("could not message chat %s: %s", chat_id, err)

    # -- messages ------------------------------------------------------------

    def _digest(self, kind: str, chat_id: int | None, user_id: int | None, text: str = "",
                *, session: str | None = None, recipe: str | None = None,
                detail: str | None = None) -> None:
        """Append one morning-digest event. A failure here never changes the reply."""
        try:
            from clipwrightd.ops import record_event
            record_event(self.home, kind=kind, chat_id=chat_id, user_id=user_id, text=text,
                         session=session, recipe=recipe, detail=detail, token=self.config.token)
        except Exception:
            log.warning("could not record digest event (%s)", kind, exc_info=True)

    def _note_chatter(self, chat_id: int, uid: int, msg: dict) -> None:
        """Preview of ignored room text. Commands and prompt answers never reach here."""
        preview = _text_of(msg)
        if preview:
            self._digest("chatter", chat_id, uid, preview)

    def _open_session(self, uid: int, chat_id: int, inst: dict, origin: int | None) -> str:
        """Create a session and note it for the digest. Returns the token."""
        token = self.store.create_session(uid, chat_id, inst, origin_message_id=origin)
        text = recipe.get(inst, "caption.text")
        self._digest("session", chat_id, uid, text if isinstance(text, str) else "",
                     session=token, recipe=str(inst.get("recipe") or ""))
        return token

    def _handle_message(self, msg: dict) -> None:
        kind = _chat_kind(msg.get("chat"))
        if kind is None:
            return
        group = kind == "group"
        chat_id = msg["chat"]["id"]
        if group and not self._group_served(chat_id):
            return
        if group:
            self.store.note_group(chat_id)
            self._remember_joiners(msg)
        origin = msg.get("message_id") if group else None    # in a group, what every answer replies to
        text = msg.get("text")
        parsed = _command_of(text) if isinstance(text, str) else None
        if _impersonal(msg):
            if group and parsed is not None and parsed[0] == "gif" and (parsed[1] is None or self._ours(parsed[1])):
                self._nag(chat_id, (msg.get("from") or {}).get("id") or 0, ANON_HINT, origin, guest=True)
            else:
                log.debug("ignoring a message with no single sender in chat %s", chat_id)
            return
        actor = self._actor(msg.get("from"), group, chat_id)
        if actor is None:
            return
        uid, guest = actor
        if parsed is not None:
            command, target = parsed
            self._handle_command(command, msg, uid, chat_id, target=target, group=group,
                                 origin=origin, guest=guest)
            return
        if group:            # a room, not an inbox: nothing but commands and answers to our own prompts
            if isinstance(text, str) and (chat_id, uid) in self._pending_text and _from_us(msg, self.username):
                self._on_text(text, uid, chat_id, origin=origin, guest=guest)
            else:
                log.debug("ignoring a non-command message from user %s in group %s", uid, chat_id)
                self._note_chatter(chat_id, uid, msg)
            return
        media, kind = _seed_media(msg)
        if media is not None:
            self._on_media(media, uid, chat_id, kind=kind)
        elif isinstance(text, str):
            self._on_text(text, uid, chat_id)
        else:
            self.api.send_message(chat_id, "Send a photo or a video, or /gif some words. /help for how.")

    def _handle_command(self, command: str, msg: dict, uid: int, chat_id: int, *,
                        target: str | None = None, group: bool = False, origin: int | None = None,
                        guest: bool = False) -> None:
        """``target`` is the ``@name`` the command carried; in a group one that is not ours is left alone."""
        ours = self._ours(target)
        if group and target is not None and not ours:
            return
        if command == "gif":
            raw = msg.get("text")
            self._on_gif(msg, uid, chat_id, origin=origin, guest=guest,
                         args=_command_args(raw if isinstance(raw, str) else ""))
        elif command == "start":
            self._nag(chat_id, uid, GROUP_START if group else (
                "Hi! This is Clipwright, the fork Grok Bot runs (clipwright-grok). "
                f"Send a photo or a video (up to {_mb(self.config.max_upload_bytes)}, "
                f"{self.config.max_duration_s:.0f} s), or /gif some words, and I'll turn it "
                "into a looping GIF you can tune with buttons. /help for the details."), origin, guest)
        elif command == "help":
            self._nag(chat_id, uid, HELP_TEXT, origin, guest)
        elif command == "recipes":
            lines = [f"{d.emoji} {name} — {d.blurb}" for name, d in self.cookbook.items()]
            self._nag(chat_id, uid, "Cookbook:\n" + "\n".join(lines), origin, guest)
        elif command == "remix":
            self._on_remix(msg, uid, chat_id, origin=origin, guest=guest)
        elif group and not ours:
            log.debug("ignoring /%s in group %s: not addressed to us", command, chat_id)
        else:
            self._nag(chat_id, uid, f"I don't know /{command}. Try /help.", origin, guest)

    def _on_gif(self, msg: dict, uid: int, chat_id: int, *, origin: int | None = None,
                guest: bool = False, args: str = "") -> None:
        """``/gif`` obtains a seed, then opens the foundry on it.

        Reply media (photo, video, animation, image/video document) wins;
        else the replied-to text; else ``/gif <words>``; else a bare
        typecard. A guest flag is accepted for the call shape but groups
        are trusted, so it does not gate this flow.
        """
        reply = msg.get("reply_to_message") or {}
        media, kind = _seed_media(reply)
        if media is not None:
            intention = _text_of(reply) or args
            self._on_media(media, uid, chat_id, origin=origin, guest=guest,
                           kind=kind or "video", intention=intention)
            return
        intention = _text_of(reply) or args
        if intention:
            self._on_intention(intention, uid, chat_id, origin=origin, guest=guest)
            return
        self._on_bare_gif(uid, chat_id, origin=origin, guest=guest)

    def _on_bare_gif(self, uid: int, chat_id: int, *, origin: int | None = None,
                     guest: bool = False) -> None:
        """Nothing to GIF: a self-aware typecard, not a crash and not silence."""
        caption = BARE_CAPTIONS[uid % len(BARE_CAPTIONS)]
        self._on_intention(caption, uid, chat_id, origin=origin, guest=guest, bare=True)

    def _on_intention(self, text: str, uid: int, chat_id: int, *, origin: int | None = None,
                      guest: bool = False, bare: bool = False) -> None:
        """Web-fetch a still for ``text``, or fall back to a local typecard."""
        if (busy := self._busy(uid)) is not None:
            self._nag(chat_id, uid, busy, origin, guest)
            return
        used = self._user_bytes(uid)
        if used > self.config.max_user_bytes:
            self._nag(
                chat_id, uid, f"Your clips here add up to {_mb(used)}, and I keep at most "
                f"{_mb(self.config.max_user_bytes)} per person. Clips behind your exports stay "
                f"so /remix keeps working; the rest clears once its session has sat idle for "
                f"{self.config.retention_days:g} days.", origin, guest)
            return
        self._pending_text.pop((chat_id, uid), None)
        self._submit(uid, chat_id, self._intention_job(text, uid, chat_id, origin, bare=bare),
                     origin=origin)

    def _on_media(self, media: dict, uid: int, chat_id: int, *, origin: int | None = None,
                  guest: bool = False, kind: str = "video", intention: str = "") -> None:
        """Gate an upload on what Telegram already told us, then hand the ingest to the worker.

        Nothing here waits on the network or on ffprobe: the size, queue and
        disk-budget gates need only the message, and the download + probe
        run as the user's queued job, so a slow or hostile upload costs its
        sender their one slot, not everyone the poll loop.
        """
        cap = self.config.max_upload_bytes
        size = media.get("file_size")
        if isinstance(size, int) and size > cap:
            self._nag(chat_id, uid, f"That's {_mb(size)}; I can take up to {_mb(cap)}. Trim it and resend.",
                      origin, guest)
            return
        if (busy := self._busy(uid)) is not None:
            self._nag(chat_id, uid, busy, origin, guest)
            return
        used = self._user_bytes(uid)
        if used + (size if isinstance(size, int) else 0) > self.config.max_user_bytes:
            self._nag(
                chat_id, uid, f"Your clips here add up to {_mb(used)}, and I keep at most "
                f"{_mb(self.config.max_user_bytes)} per person. Clips behind your exports stay "
                f"so /remix keeps working; the rest clears once its session has sat idle for "
                f"{self.config.retention_days:g} days.", origin, guest)
            return
        self._pending_text.pop((chat_id, uid), None)
        self._submit(uid, chat_id, self._ingest_job(media, uid, chat_id, origin,
                                                    kind=kind, intention=intention), origin=origin)

    def _on_video(self, media: dict, uid: int, chat_id: int, *, origin: int | None = None,
                  guest: bool = False) -> None:
        """Back-compat alias: a video seed."""
        self._on_media(media, uid, chat_id, origin=origin, guest=guest, kind="video")

    def _ingest_job(self, media: dict, uid: int, chat_id: int, origin: int | None = None,
                    *, kind: str = "video", intention: str = "") -> Job:
        """The queued half of an upload: fetch, probe, open the session, render its first preview."""
        def run() -> None:
            try:
                token = self._ingest(media, uid, chat_id, origin, kind=kind, intention=intention)
            except _Rejected as why:
                self._tell(chat_id, str(why), origin)
                return
            except Exception as exc:
                self._report_failure(chat_id, exc, f"upload from user {uid}", origin, user_id=uid)
                return
            self._job(token, self._render_preview)()
        return run

    def _intention_job(self, text: str, uid: int, chat_id: int, origin: int | None = None,
                       *, bare: bool = False) -> Job:
        """Queued half of a text seed: Commons fetch, local typecard fallback, first preview."""
        def run() -> None:
            try:
                token, note = self._ingest_intention(text, uid, chat_id, origin, bare=bare)
            except _Rejected as why:
                self._tell(chat_id, str(why), origin)
                return
            except Exception as exc:
                self._report_failure(chat_id, exc, f"intention from user {uid}", origin, user_id=uid)
                return
            if note:
                self._tell(chat_id, note, origin)
            self._job(token, self._render_preview)()
        return run

    def _ingest(self, media: dict, uid: int, chat_id: int, origin: int | None = None,
                *, kind: str = "video", intention: str = "") -> str:
        """Download and probe one upload; return the token of its new session (``_Rejected`` otherwise)."""
        cap = self.config.max_upload_bytes
        self.api.send_chat_action(chat_id, "upload_photo" if kind == "photo" else "upload_video")
        info = self.api.get_file(media["file_id"])
        ext = _ext_for(media, kind)
        upload_dir = os.path.join(self.home, "uploads", str(uid))
        os.makedirs(upload_dir, exist_ok=True)
        # hex, never token_urlsafe: a name starting with "-" would read as an option in the ⌘ Show CLI line
        dest = os.path.join(upload_dir, f"{secrets.token_hex(6)}.{ext}")
        try:
            self._download(info["file_path"], dest, cap)
            probe = self._probe_checked(dest, still_ok=(kind == "photo"))
        except _Rejected:
            _unlink(dest)
            raise

        name = self._recipe_for_seed(kind, probe)
        inst = recipe.defaults(self.cookbook[name])
        inst["input"] = dest
        self._prime_trim(inst, probe, dest)
        self._apply_caption(inst, name, intention)
        token = self._open_session(uid, chat_id, inst, origin)
        log.info("session %s for user %s: %s %s (%.1fs %dx%d)", token, uid, name, dest,
                 probe.duration, probe.width, probe.height)
        return token

    def _ingest_intention(self, text: str, uid: int, chat_id: int, origin: int | None = None,
                          *, bare: bool = False) -> tuple[str, str | None]:
        """Obtain a seed for ``text``: Commons still, else a typecard. Returns (token, note)."""
        upload_dir = os.path.join(self.home, "uploads", str(uid))
        os.makedirs(upload_dir, exist_ok=True)
        note = None
        src = None
        if not bare:
            self.api.send_chat_action(chat_id, "upload_photo")
            try:
                src = self.fetch_fn(text, upload_dir)
            except FetchError as err:
                log.info("web seed failed for %r: %s", text[:80], err)
                note = f"Couldn't fetch a picture ({err}); made a typecard instead."
                self._digest("seed_fail", chat_id, uid, text, detail=str(err))
            except Exception:
                log.exception("web seed crashed for %r", text[:80])
                note = "Couldn't fetch a picture; made a typecard instead."
                self._digest("seed_fail", chat_id, uid, text, detail="web seed failed")
                src = None
        if src:
            try:
                probe = self._probe_checked(src, still_ok=True)
            except _Rejected as why:
                _unlink(src)
                src = None
                note = f"{why} Made a typecard instead."
                self._digest("seed_fail", chat_id, uid, text, detail=str(why))
            else:
                name = self._recipe_for_seed("photo", probe)
                inst = recipe.defaults(self.cookbook[name])
                inst["input"] = src
                self._prime_trim(inst, probe, src)
                self._apply_caption(inst, name, text)
                token = self._open_session(uid, chat_id, inst, origin)
                log.info("session %s for user %s: web seed %s -> %s", token, uid, text[:40], src)
                return token, note
        name = TEXT_RECIPE if TEXT_RECIPE in self.cookbook else DEFAULT_RECIPE
        inst = recipe.defaults(self.cookbook[name])
        self._apply_caption(inst, name, text)
        token = self._open_session(uid, chat_id, inst, origin)
        log.info("session %s for user %s: typecard %r", token, uid, text[:40])
        return token, note

    def _recipe_for_seed(self, kind: str, probe: ffmpeg.Probe) -> str:
        if (kind == "photo" or ffmpeg.is_still(probe)) and PHOTO_RECIPE in self.cookbook:
            return PHOTO_RECIPE
        return DEFAULT_RECIPE if DEFAULT_RECIPE in self.cookbook else next(iter(self.cookbook))

    def _prime_trim(self, inst: dict, probe: ffmpeg.Probe, path: str) -> None:
        """Set from/to from the probe when the recipe has a trim knob; stills get a hold length."""
        defn = self.cookbook.get(inst.get("recipe"))
        has_trim = defn is not None and any(k.type == "range" for k in defn.knobs)
        if ffmpeg.is_still(probe) or probe.duration <= 0:
            if has_trim:
                self._clip_ends[path] = STILL_HOLD_S
                inst["from"] = recipe.fmt_time(0.0)
                inst["to"] = recipe.fmt_time(STILL_HOLD_S)
            return
        self._clip_ends[path] = probe.duration
        inst["from"] = recipe.fmt_time(0.0)
        inst["to"] = recipe.fmt_time(probe.duration)
        _clamp_segment(inst)

    def _apply_caption(self, inst: dict, recipe_name: str, text: str) -> None:
        """Fill ``caption.text`` when the recipe has that knob and ``text`` is non-empty."""
        if not text:
            return
        defn = self.cookbook.get(recipe_name)
        if defn is None:
            return
        knob = next((k for k in defn.knobs if k.key == "caption.text"), None)
        if knob is None:
            return
        value = text.strip()
        if knob.max_len is not None:
            value = value[:knob.max_len]
        recipe.set_(inst, knob.key, value)

    def _download(self, file_path: str, dest: str, cap: int) -> None:
        try:
            self.api.download_file(file_path, dest, max_bytes=cap)
        except BotAPIError as err:
            if err.error_code == 413:      # the stream ran past the cap; the partial file is gone
                raise _Rejected(f"That file is over {_mb(cap)}. Trim it and resend.") from None
            raise

    def _probe_checked(self, path: str, *, still_ok: bool = False) -> ffmpeg.Probe:
        """Probe an upload and refuse it (``_Rejected``) when it breaks a limit."""
        try:
            probe = self.probe_fn(path)
        except ffmpeg.FFmpegError as err:
            if self._stopping:            # a terminal's Ctrl-C reached ffprobe too: not the clip's fault
                raise _Rejected(SHUT_DOWN_MID_RENDER) from None
            log.warning("probe rejected %s: %s", path, err)
            raise _Rejected("I couldn't read that as a photo or video. Send a jpeg, png, mp4, mov or webm.") from None
        still = ffmpeg.is_still(probe) or (still_ok and probe.duration <= 0)
        if still:
            if max(probe.width, probe.height) > STILL_MAX_DIM:
                raise _Rejected(f"That still is {probe.width}×{probe.height}; I take up to "
                                f"{STILL_MAX_DIM} px on the long side.")
            return probe
        if probe.duration > self.config.max_duration_s:
            raise _Rejected(f"That clip is {probe.duration:.1f} s; I take up to "
                            f"{self.config.max_duration_s:.0f} s. Trim it and resend.")
        if max(probe.width, probe.height) > self.config.max_dim:
            raise _Rejected(f"That clip is {probe.width}×{probe.height}; I take up to "
                            f"{self.config.max_dim} px on the long side. Downscale it and resend.")
        return probe

    def _on_text(self, text: str, uid: int, chat_id: int, *, origin: int | None = None,
                 guest: bool = False) -> None:
        """A text message: the answer to this user's pending prompt, or (in a DM) a nudge towards /help."""
        key = (chat_id, uid)
        pending = self._pending_text.get(key)
        if pending is None:
            self._say(chat_id, "Send a photo or a video, or /gif some words. /help for how.", origin)
            return
        token, knob_idx = pending
        sess = self.store.get(token)
        if sess is None:
            del self._pending_text[key]
            self._say(chat_id, "That session has expired — send a photo or /gif again.", origin)
            return
        defn = self._defn(sess)
        knob = _text_knob(defn, knob_idx)
        if knob is None:                      # the session changed recipe under the prompt
            del self._pending_text[key]
            self._say(chat_id, "That prompt no longer matches the session — use the buttons under the preview.", origin)
            return
        new = copy.deepcopy(sess.recipe)
        recipe.set_(new, knob.key, text.strip())
        problems = recipe.validate(new, defn)
        if problems:
            self._say(chat_id, "; ".join(problems) + ". Try again:", origin, reply_markup=_force_reply(knob))
            return
        if (busy := self._busy(uid)) is not None:   # the prompt stays pending; the retry is taken
            self._say(chat_id, busy, origin, reply_markup=_force_reply(knob))
            return
        if guest and (why := self._charge_guest(uid, chat_id)) is not None:
            del self._pending_text[key]
            self._say(chat_id, why, origin)
            return
        del self._pending_text[key]
        self.store.update(token, new)
        self._submit(uid, chat_id, self._job(token, self._render_again), origin=sess.origin_message_id)

    def _on_remix(self, msg: dict, uid: int, chat_id: int, *, origin: int | None = None,
                  guest: bool = False) -> None:
        target = msg.get("reply_to_message") or {}
        sent = next((target[k] for k in ("animation", "document") if isinstance(target.get(k), dict)), None)
        if sent is None or not sent.get("file_unique_id"):
            self._nag(chat_id, uid, "Reply /remix to a GIF I sent and I'll reopen its knobs.", origin, guest)
            return
        entry = self.store.ledger_entry(sent["file_unique_id"])
        if entry is None:
            self._nag(chat_id, uid, "I don't have a recipe for that file — it wasn't one of my exports.",
                      origin, guest)
            return
        if not self._may_remix(uid, entry):
            log.warning("user %s asked to remix %s, exported by user %s", uid, entry.file_unique_id, entry.user_id)
            self._nag(chat_id, uid, "That export belongs to someone else — only they (or the owner) can remix it.",
                      origin, guest)
            return
        inst = entry.recipe
        src = inst.get("input")
        if isinstance(src, str) and not os.path.isfile(src):
            defn = self.cookbook.get(inst.get("recipe"))
            if defn is None or defn.needs_input:
                self._nag(chat_id, uid, "The clip behind that export is no longer on disk — send it again to start over.",
                          origin, guest)
                return
        if (busy := self._busy(uid)) is not None:
            self._nag(chat_id, uid, busy, origin, guest)
            return
        if guest and (why := self._charge_guest(uid, chat_id)) is not None:
            self._nag(chat_id, uid, why, origin, guest)
            return
        _clamp_segment(inst)
        token = self._open_session(uid, chat_id, inst, origin)
        log.info("remix %s -> session %s for user %s", sent["file_unique_id"], token, uid)
        self._submit(uid, chat_id, self._job(token, self._render_preview), origin=origin)

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
        chat = (cq.get("message") or {}).get("chat") or {}
        kind = _chat_kind(chat)
        if kind is None or (kind == "group" and not self._group_served(chat["id"])) or _impersonal(cq):
            return
        actor = self._actor(cq.get("from"), kind == "group", chat.get("id"))
        if actor is None:
            return
        uid, guest = actor
        try:
            toast = self._dispatch_callback(cq, uid, guest)
        except Exception:
            log.exception("callback %s failed", cq.get("data"))
            toast = "Something went wrong on my side — try that again."
        try:
            self.api.answer_callback_query(cq["id"], toast)
        except BotAPIError as err:                 # a replayed press is too old to answer; the work is done
            log.warning("could not answer callback %s: %s", cq.get("id"), err)

    def _dispatch_callback(self, cq: dict, uid: int, guest: bool = False) -> str | None:
        """Act on one callback and return the toast to answer it with.

        Only the user who opened the session may press its buttons; a
        ``guest`` is charged the guest quota for each render a press starts.
        """
        try:
            cb = decode_cb(cq.get("data") or "")
        except ValueError:
            return STALE_BUTTON
        sess = self.store.get(cb.session)
        if sess is None:
            return "That session has expired — send a photo or /gif again."
        if sess.user_id != uid:
            return "That's someone else's session."
        defn = self._defn(sess)

        if cb.kind == "n":
            return None
        if cb.kind == "t":
            knob = _text_knob(defn, cb.knob_idx)
            if knob is None:
                return STALE_BUTTON
            self._pending_text[(sess.chat_id, sess.user_id)] = (cb.session, cb.knob_idx)
            self._say(sess.chat_id, f"Send the {knob.label} (up to {knob.max_len} characters):",
                      sess.origin_message_id, reply_markup=_force_reply(knob))
            return None
        if cb.kind == "r":
            return self._switch_recipe(cb, sess, uid, guest)
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
            if guest and (why := self._charge_guest(uid, sess.chat_id)) is not None:
                return why
            self.store.update(cb.session, new)
            return self._submit(uid, sess.chat_id, self._job(cb.session, self._render_again), quiet=True,
                                origin=sess.origin_message_id)
        return self._do_action(cb, sess, uid, guest)

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

    def _switch_recipe(self, cb: Callback, sess: Session, uid: int, guest: bool = False) -> str | None:
        """Restart the session on another recipe, keeping the clip and its trim."""
        target = self.cookbook.get(cb.recipe)
        if target is None:
            return STALE_BUTTON
        if target.name == sess.recipe.get("recipe"):
            return None
        if (busy := self._busy(uid)) is not None:
            return busy
        if guest and (why := self._charge_guest(uid, sess.chat_id)) is not None:
            return why
        if target.needs_input:
            src = sess.recipe.get("input")
            if not isinstance(src, str) or not src:
                return "That recipe needs a photo or clip — reply /gif to one, or stay on typecard."
        inst = recipe.defaults(target)
        for key in ("input", "from", "to"):
            if sess.recipe.get(key) is not None:
                inst[key] = sess.recipe[key]
        carried = recipe.get(sess.recipe, "caption.text")
        if isinstance(carried, str) and carried and any(k.key == "caption.text" for k in target.knobs):
            self._apply_caption(inst, target.name, carried)
        self._pending_text.pop((sess.chat_id, sess.user_id), None)
        self.store.update(cb.session, inst)
        return self._submit(uid, sess.chat_id, self._job(cb.session, self._render_again), quiet=True,
                            origin=sess.origin_message_id)

    def _do_action(self, cb: Callback, sess: Session, uid: int, guest: bool = False) -> str | None:
        chat_id = sess.chat_id
        if cb.action == "grid":
            return GRID_TOAST
        if cb.action == "cli":
            self._say(chat_id, cli_command(_public(sess.recipe), cookbook=self.cookbook), sess.origin_message_id)
            return None
        if (busy := self._busy(uid)) is not None:
            return busy
        if cb.action == "undo":
            if not sess.undo:
                return "Nothing to undo."
            if guest and (why := self._charge_guest(uid, chat_id)) is not None:
                return why
            if self.store.undo(cb.session) is None:
                return "Nothing to undo."
            return self._submit(uid, chat_id, self._job(cb.session, self._render_again), quiet=True,
                                origin=sess.origin_message_id)
        if guest:                             # a guest's export is one more render on their day, not an owner export
            if (why := self._charge_guest(uid, chat_id)) is not None:
                return why
        elif self.store.quota_hit(uid, self.config.per_day_quota):
            self._say(chat_id, f"You've used today's {self.config.per_day_quota} exports — "
                      "the counter resets at midnight UTC. Previews still work.", sess.origin_message_id)
            return "Daily export limit reached."
        return self._submit(uid, chat_id, self._job(cb.session, self._render_export), quiet=True,
                            origin=sess.origin_message_id)

    # -- render jobs (run on the queue's worker) -----------------------------

    def _submit(self, uid: int, chat_id: int, job: Job, *, quiet: bool = False,
                origin: int | None = None) -> str | None:
        """Queue ``job``; tell the user the position when they must wait. Returns the toast.

        Callers gate on ``_busy`` first, so a refusal here is a race the
        queue's own contract still allows; it is reported the same way.
        ``origin`` is the group message any of that — and the shutdown
        notice for a job that never ran — replies to.
        """
        position = self.queue.submit(uid, job)
        if position is None:
            text = self._busy(uid) or QUEUE_FULL
        else:
            self._waiting[uid] = (chat_id, origin)
            if position > 1:
                text = f"Queued — #{position} in line."
            else:
                text = "Rendering…" if quiet else None
        if text is not None and not quiet:
            self._say(chat_id, text, origin)
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
                self._report_failure(sess.chat_id, exc, f"render for session {token}",
                                     sess.origin_message_id, user_id=sess.user_id)
        return run

    def _report_failure(self, chat_id: int, exc: Exception, what: str, origin: int | None = None,
                        *, user_id: int | None = None) -> None:
        """One log line and one message for a job that failed.

        While the daemon is shutting down the honest message is that it was
        stopped: a terminal's Ctrl-C reaches the ffmpeg child too, so the
        render dies with it and 'try a different clip' would be wrong advice.
        """
        if self._stopping:
            log.warning("%s was cut short by the shutdown: %s", what, exc)
            self._tell(chat_id, SHUT_DOWN_MID_RENDER, origin)
            return
        log.error("%s failed", what, exc_info=exc)
        self._digest("error", chat_id, user_id, f"{what}: {exc}")
        self._tell(chat_id, _user_error(exc), origin)

    def _render_preview(self, sess: Session) -> None:
        """First render of a session: cook a proxy and send it with the keyboard."""
        result = self._cook(sess, proxy=True)
        try:
            resp = self.api.send_animation(
                sess.chat_id, result.mp4, caption=self._caption(sess.recipe, result.report),
                reply_markup=self._markup(sess), filename=PREVIEW_NAME,
                reply_to_message_id=sess.origin_message_id)
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
                                          disable_content_type_detection=True,
                                          reply_to_message_id=sess.origin_message_id)
            sent = _sent_file(resp)
            if sent and sent.get("file_unique_id"):
                self.store.ledger_put(sent["file_unique_id"], sess.recipe, sess.token, sess.user_id)
            else:
                log.warning("export %s: no file_unique_id in sendDocument response", sess.token)
            try:
                self.api.send_document(sess.chat_id, sidecar, os.path.basename(sidecar),
                                       caption="Recipe sidecar — reply /remix to this GIF to reopen it.",
                                       reply_to_message_id=sess.origin_message_id)
            except BotAPIError as err:
                log.warning("export %s: sidecar upload failed: %s", sess.token, err)
                self._tell(sess.chat_id, "The recipe sidecar didn't go through, but /remix on that GIF still works.",
                           sess.origin_message_id)
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
        line = " · ".join(parts) + "\nTweak with the buttons; ⬇ Export when it's right."
        if inst.get("recipe") == TEXT_RECIPE and not inst.get("input"):
            line += "\nReply /gif to a photo, a video or a sentence — or /gif <words>."
        return line


def main(argv: list[str] | None = None) -> int:
    """``python3 -m clipwrightd [--home DIR] [--once]``."""
    parser = argparse.ArgumentParser(
        prog="clipwrightd",
        description="Grok Bot's Clipwright fork (clipwright-grok): long-polls the Bot API and renders on request.")
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
