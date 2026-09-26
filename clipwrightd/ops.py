"""Supervise Grok Bot's Clipwright daemon from a ``live-src`` checkout.

``$CLIPWRIGHT_HOME`` (default ``~/.clipwright``) holds ``bot.env``, the
pidfile, the update offset, the log, and ``live-src/`` — a git checkout of
this fork. The daemon runs from that checkout, so a code change is a
checkout plus a restart, not a reread of ``poll.py``.

``deploy REF`` fetches one remote, checks the ref out detached, restarts,
and prints status. ``live-src``'s ``origin`` must be this fork
(``clipwright-grok``). A remote whose URL still names ``CLIPWRIGHT_PLAN``
is refused unless ``--force-remote``; name that remote ``upstream`` if you
keep it. When a ``clipwright-grok`` URL is configured next to ``origin``,
deploy fetches the fork remote and says so on the status line.
``$CLIPWRIGHT_OPS_REMOTE`` or ``--remote`` picks a remote by name.
``restart`` is pidfile-aware:
it sends SIGTERM only to a pid that currently holds the flock on
``daemon.pid``, and it will not start a second poller while that lock is
held. A live pid that does not hold the lock is left alone (it may have
been recycled). ``health`` exits non-zero when the pidfile is missing, the
recorded pid is dead, the pid does not hold the lock, or ``getUpdates`` has
not returned within ``--max-age`` seconds.

The only progress signal is state the daemon already writes. ``daemon.pid``
is the flock. ``<home>/offset`` stores the next update id, and the daemon
rewrites it after every successful ``getUpdates`` (the integer does not
change when the batch was empty) so the file's mtime is the last poll that
returned. A process that has just started is allowed one ``--max-age``
window to finish its first poll. Nothing in here shells out: git and the
daemon are argv lists.

``digest`` does not poll Telegram. It reads that same pidfile and offset,
the open sessions and recent ledger rows in ``state.db``, and the short
ring of events the poller appends to ``digest-events.jsonl`` (ignored room
text, session opens, web-seed fallbacks, render errors). It prints a
summary and writes ``digest-latest.json``.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time

from clipwrightd import config as config_mod
from clipwrightd.api import redact
from clipwrightd.poll import LOG_FILE, OFFSET_FILE, PIDFILE, STATE_DB
from clipwrightd.session import Store

LIVE_SRC = "live-src"
DAEMON_OUT = "daemon.out"
FORK_REPO = "clipwright-grok"
PLAN_REPO = "CLIPWRIGHT_PLAN"
OPS_REMOTE_ENV = "CLIPWRIGHT_OPS_REMOTE"
# Long poll waits 50s and errors back off to 60s. Ten minutes is "stuck".
DEFAULT_MAX_AGE_S = 600.0
# A render is allowed to finish (the daemon's SIGTERM path). ffmpeg's own
# timeout is the backstop; this is how long restart waits before refusing
# to start a second poller.
STOP_TIMEOUT_S = 300.0
START_WAIT_S = 15.0
_URL_USERINFO = re.compile(r"://[^/\s@]+@")
# Bot tokens look like ``123456789:AAH...``. Scrub that shape even when the
# exact token is not in hand, so a chatter line cannot carry one into the digest.
_TOKEN_SHAPE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")

EVENTS_FILE = "digest-events.jsonl"
DIGEST_LATEST = "digest-latest.json"
PREVIEW_CHARS = 160
RETENTION_S = 48 * 3600
MAX_EVENT_BYTES = 256 * 1024
_EVENTS_LOCK = threading.Lock()


class OpsError(RuntimeError):
    """The operator asked for something the checkout or the pidfile cannot do."""


def home_dir(explicit: str | None = None) -> str:
    """``--home``, else ``$CLIPWRIGHT_HOME``, else ``~/.clipwright``."""
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    return config_mod.home_dir()


def live_src_dir(home: str) -> str:
    return os.path.join(home, LIVE_SRC)


def pidfile_path(home: str) -> str:
    return os.path.join(home, PIDFILE)


def _redact(text: str) -> str:
    """Drop URL userinfo so a credentialed git remote cannot land in a status line."""
    return _URL_USERINFO.sub("://", text or "")


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(cwd: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """One git argv. ``OpsError`` on a failing command when ``check`` is set."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=_git_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as err:
        raise OpsError(f"could not run git: {err}") from None
    if check and proc.returncode != 0:
        detail = _redact((proc.stderr or proc.stdout or "").strip())
        raise OpsError(f"git {' '.join(args)} failed ({proc.returncode})"
                       + (f": {detail}" if detail else ""))
    return proc


def _is_checkout(path: str) -> bool:
    """True for a normal repo or a linked worktree (``.git`` is a file there)."""
    git_path = os.path.join(path, ".git")
    return os.path.isdir(git_path) or os.path.isfile(git_path)


def _remote_names(src: str) -> list[str]:
    proc = _git(src, "remote", check=False)
    if proc.returncode != 0:
        return []
    return [name for name in proc.stdout.split() if name]


def _remote_url(src: str, name: str) -> str:
    proc = _git(src, "remote", "get-url", name, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def repo_name(url: str) -> str:
    """Last path segment of a remote URL, without a trailing ``.git``.

    ``ssh://`` and ``https://`` URLs use the path. ``git@host:org/repo.git``
    uses the part after the colon. Userinfo is stripped first so a token in
    the URL cannot become the repo name.
    """
    cleaned = _redact(url).strip().rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    if "://" not in cleaned and ":" in cleaned:
        cleaned = cleaned.split(":", 1)[1]
    return cleaned.rstrip("/").split("/")[-1]


def _is_fork_url(url: str) -> bool:
    """True when the URL's repo name is this fork (``clipwright-grok``)."""
    return repo_name(url).lower() == FORK_REPO


def _is_plan_url(url: str) -> bool:
    """True when the URL's repo name is the upstream plan repo."""
    return repo_name(url).lower() == PLAN_REPO.lower()


def choose_remote(src: str, explicit: str | None = None) -> str | None:
    """The remote ``deploy`` should fetch. None when the checkout has no remotes.

    ``explicit`` (``--remote``) wins, then ``$CLIPWRIGHT_OPS_REMOTE``.
    Otherwise a remote whose URL names ``clipwright-grok`` wins over
    ``origin``, including when ``origin`` still points at ``CLIPWRIGHT_PLAN``.
    If several fork remotes exist, ``origin`` is used when it is one of them;
    otherwise the name that sorts first. With no fork URL, ``origin`` is used.
    """
    names = _remote_names(src)
    chosen = (explicit or "").strip() or (os.environ.get(OPS_REMOTE_ENV) or "").strip() or None
    if chosen:
        if chosen not in names:
            have = ", ".join(names) or "none"
            raise OpsError(f"remote {chosen!r} is not in {src} (have: {have})")
        return chosen
    forks = [name for name in names if _is_fork_url(_remote_url(src, name))]
    if forks:
        return "origin" if "origin" in forks else sorted(forks)[0]
    if "origin" in names:
        return "origin"
    return None


def _guard_plan_remote(src: str, remote: str, *, force_remote: bool) -> str:
    """Refuse a ``CLIPWRIGHT_PLAN`` URL unless ``force_remote``. Return the redacted URL."""
    url = _redact(_remote_url(src, remote))
    if _is_plan_url(url) and not force_remote:
        raise OpsError(
            f"{remote} points at {url} ({PLAN_REPO}). "
            f"live-src's deploy remote must be this fork ({FORK_REPO}). "
            f"Name the plan repo 'upstream' if you keep it, and point origin "
            f"(or another remote whose URL ends with {FORK_REPO}) at this fork. "
            f"Re-run with --force-remote to deploy {remote} anyway."
        )
    return url


def remote_line(src: str, explicit: str | None = None) -> str:
    """One status line: which remote deploy will fetch, and its URL."""
    if not _is_checkout(src):
        return "remote: none"
    try:
        remote = choose_remote(src, explicit)
    except OpsError as err:
        return f"remote: {err}"
    if remote is None:
        return "remote: none"
    url = _redact(_remote_url(src, remote))
    note = ""
    names = _remote_names(src)
    if _is_plan_url(url):
        note = (f" ({PLAN_REPO} — deploy refuses this unless --force-remote; "
                "name it upstream and point a remote at clipwright-grok)")
    elif remote != "origin" and "origin" in names:
        note = " (used instead of origin)"
    return f"remote: {remote} {url}{note}".rstrip()


def _resolve_ref(src: str, ref: str, remote: str | None) -> str:
    """The commit ``deploy`` should check out.

    A branch name prefers ``<remote>/<ref>`` after fetch, so a stale local
    branch does not win. A tag or a raw commit resolves on the second try.
    """
    candidates = [ref]
    if remote:
        candidates.insert(0, f"{remote}/{ref}")
    for candidate in candidates:
        proc = _git(src, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}", check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    tried = " and ".join(candidates)
    raise OpsError(f"unknown git ref {ref!r} in {src} (tried {tried})")


def sync_live_src(home: str, ref: str, *, force: bool = False,
                  remote: str | None = None, force_remote: bool = False) -> str:
    """Check ``ref`` out detached in ``<home>/live-src``. Return the commit.

    Fetches the remote from :func:`choose_remote` (a ``clipwright-grok`` URL
    beats ``origin``). A ``CLIPWRIGHT_PLAN`` URL is refused unless
    ``force_remote``. A dirty worktree is refused unless ``force``, which
    discards changes to tracked files (``checkout --force``). Untracked
    files, including a ``bot.env`` someone left inside the checkout, are
    left in place — secrets belong in ``home``, not in ``live-src``.
    """
    src = live_src_dir(home)
    if not _is_checkout(src):
        raise OpsError(
            f"{src} is not a git checkout. Clone this fork there once "
            f"(git clone <clipwright-grok remote> {src}). "
            f"origin must be {FORK_REPO}; name {PLAN_REPO} 'upstream' if you keep it. "
            "The live @username is your bot's; this directory is only the code."
        )
    chosen = choose_remote(src, remote)
    if chosen:
        _guard_plan_remote(src, chosen, force_remote=force_remote)
        _git(src, "fetch", "--prune", chosen)
    commit = _resolve_ref(src, ref, chosen)
    dirty = _git(src, "status", "--porcelain").stdout.strip()
    if dirty and not force:
        raise OpsError(
            f"{src} has uncommitted changes; re-run with --force to discard "
            f"tracked edits and check out {ref}"
        )
    checkout = ["checkout", "--detach", commit]
    if force:
        checkout = ["checkout", "--force", "--detach", commit]
    _git(src, *checkout)
    return commit


def read_pid(home: str) -> int | None:
    """The integer in ``daemon.pid``, or None when the file is missing or not a pid."""
    try:
        with open(pidfile_path(home), encoding="utf-8") as fh:
            text = fh.read().strip()
        pid = int(text)
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def pid_alive(pid: int) -> bool:
    """True when ``pid`` is a live process. A recycled pid still counts as alive."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lock_held(path: str) -> bool:
    """True when some process holds the exclusive flock on ``path``.

    Opening the file ourselves and failing to take ``LOCK_EX|LOCK_NB`` is
    the check. A missing file is not held. Acquiring the lock means nobody
    else has it; it is released before this returns.
    """
    if not os.path.isfile(path):
        return False
    try:
        fh = open(path, "r+", encoding="utf-8")
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False
    finally:
        fh.close()


def read_offset(home: str) -> int | None:
    """The next update id stored in ``offset``, or None when it is absent or empty."""
    try:
        with open(os.path.join(home, OFFSET_FILE), encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def offset_mtime(home: str) -> float | None:
    path = os.path.join(home, OFFSET_FILE)
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def process_started_at(pid: int, pidfile: str | None = None) -> float | None:
    """Wall-clock start of ``pid``, from ``/proc`` or else the pidfile's mtime.

    ``/proc/<pid>/stat`` field 22 is start ticks since boot. When that is
    unreadable, the pidfile mtime is the fallback: the daemon truncates
    ``daemon.pid`` when it takes the lock, so that mtime is the start of
    the process that wrote it.
    """
    started = _proc_start(pid)
    if started is not None:
        return started
    if pidfile and os.path.isfile(pidfile):
        try:
            return os.stat(pidfile).st_mtime
        except OSError:
            return None
    return None


def _proc_start(pid: int) -> float | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            text = fh.read()
        after = text.rsplit(")", 1)[1].split()
        start_ticks = int(after[19])          # field 22; field 3 is the first token after comm
        hz = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        btime = None
        with open("/proc/stat", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("btime "):
                    btime = float(line.split()[1])
                    break
        if btime is None or hz <= 0:
            return None
        return btime + start_ticks / hz
    except (OSError, ValueError, IndexError, KeyError):
        return None


def _poll_age(now: float, mtime: float | None, started: float | None) -> float | None:
    """Seconds since the last successful ``getUpdates`` for this process.

    An offset file older than the process is the previous daemon's. Until
    this process rewrites it, the age is how long the new process has been
    up — the startup grace.
    """
    if mtime is None and started is None:
        return None
    if mtime is None or (started is not None and mtime < started - 1.0):
        if started is None:
            return None
        return max(0.0, now - started)
    return max(0.0, now - mtime)


class Health:
    """One verdict from :func:`assess`. ``ok`` is what ``health`` exits on."""

    def __init__(self, ok: bool, summary: str, *, pid: int | None = None,
                 locked: bool = False, offset: int | None = None,
                 poll_age_s: float | None = None, started_at: float | None = None) -> None:
        self.ok = ok
        self.summary = summary
        self.pid = pid
        self.locked = locked
        self.offset = offset
        self.poll_age_s = poll_age_s
        self.started_at = started_at

    def __repr__(self) -> str:
        return f"Health(ok={self.ok}, summary={self.summary!r})"


def assess(home: str, *, max_age: float = DEFAULT_MAX_AGE_S, now: float | None = None,
           started_at: float | None = None, use_proc_start: bool = True) -> Health:
    """Pidfile liveness plus how long since ``getUpdates`` last returned.

    ``started_at``, when passed, is used instead of ``/proc`` (tests).
    ``use_proc_start=False`` with ``started_at is None`` leaves the start
    time unknown so the offset mtime stands on its own.
    """
    path = pidfile_path(home)
    now = time.time() if now is None else now
    if not os.path.isfile(path):
        return Health(False, f"down: no pidfile at {path}")
    pid = read_pid(home)
    if pid is None:
        return Health(False, f"stale: {path} has no pid")
    if not pid_alive(pid):
        return Health(False, f"stale: pid {pid} in {path} is not running", pid=pid)
    if not lock_held(path):
        return Health(False, f"stale: pid {pid} does not hold {path}", pid=pid)

    if started_at is None and use_proc_start:
        started_at = process_started_at(pid, path)
    mtime = offset_mtime(home)
    offset = read_offset(home)
    age = _poll_age(now, mtime, started_at)
    common = dict(pid=pid, locked=True, offset=offset, poll_age_s=age, started_at=started_at)
    if age is None:
        return Health(False, f"stalled: pid {pid} holds {path}, but getUpdates progress is unknown",
                      **common)
    if age > max_age:
        return Health(
            False,
            f"stalled: pid {pid} holds {path}, but getUpdates has not returned "
            f"for {age:.0f}s (limit {max_age:.0f}s)",
            **common,
        )
    off = f"offset {offset}" if offset is not None else "no update offset yet"
    return Health(True, f"ok: pid {pid} holds {path}; {off}; getUpdates {age:.0f}s ago", **common)


def stop_daemon(home: str, *, timeout: float = STOP_TIMEOUT_S) -> str:
    """SIGTERM the process holding ``daemon.pid`` and wait until the lock is free.

    A missing pidfile, a dead pid, or a live pid that does not hold the
    flock is already stopped. The live-but-unlocked case is not signalled.
    Raises ``OpsError`` when the holder does not exit in time — the caller
    must not start a second poller on top of it.
    """
    path = pidfile_path(home)
    if not os.path.isfile(path):
        return f"not running: no pidfile at {path}"
    pid = read_pid(home)
    if pid is None:
        return f"stopped: {path} has no pid"
    if not pid_alive(pid):
        return f"stopped: pid {pid} in {path} is not running"
    if not lock_held(path):
        return f"stopped: pid {pid} does not hold {path}; not signalling it"
    try:
        os.kill(pid, signal.SIGTERM)   # the daemon finishes the in-flight render on this
    except ProcessLookupError:
        return f"stopped: pid {pid} exited before SIGTERM"
    except PermissionError as err:
        raise OpsError(f"could not signal pid {pid}: {err}") from None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid) or not lock_held(path):
            return f"stopped: pid {pid} released {path}"
        time.sleep(0.1)
    raise OpsError(
        f"pid {pid} still holds {path} after {timeout:.0f}s — not starting another poller. "
        "A render in flight is allowed to finish; wait and retry, or stop that pid yourself."
    )


def start_daemon(home: str, src: str | None = None, *, popen=None) -> int:
    """Start ``python -m clipwrightd`` from ``live-src``. Return the child pid.

    Refuses to start while the pidfile lock is held. stdout and stderr go to
    ``<home>/daemon.out`` (truncated for this start). The child is in its
    own session so it outlives this command. ``PYTHONPATH`` prefers the
    checkout over an older install.
    """
    src = src or live_src_dir(home)
    if not os.path.isdir(src):
        raise OpsError(
            f"live-src is not a checkout: {src}. "
            f"Clone this repo there once (git clone <your remote> {src})."
        )
    path = pidfile_path(home)
    if lock_held(path):
        raise OpsError(f"clipwrightd already holds {path}; stop it before starting another")
    os.makedirs(home, exist_ok=True)
    env = os.environ.copy()
    env["CLIPWRIGHT_HOME"] = home
    previous = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src if not previous else src + os.pathsep + previous
    launcher = popen or subprocess.Popen
    out_path = os.path.join(home, DAEMON_OUT)
    # 0600 even when the operator's umask is looser: startup stderr can
    # mention paths next to the token file, and daemon.log is private too.
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(out_path, 0o600)
    out = os.fdopen(fd, "w", encoding="utf-8")
    try:
        try:
            proc = launcher(
                [sys.executable, "-m", "clipwrightd", "--home", home],
                cwd=src,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as err:
            raise OpsError(f"could not start clipwrightd: {err}") from None
    finally:
        out.close()
    return proc.pid


def wait_until_running(home: str, *, timeout: float = START_WAIT_S,
                       max_age: float = DEFAULT_MAX_AGE_S) -> Health:
    """Poll :func:`assess` until the pidfile is held or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    last = assess(home, max_age=max_age)
    while True:
        if last.locked and last.pid and pid_alive(last.pid):
            return last
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)
        last = assess(home, max_age=max_age)
    if last.ok:
        return last
    log_path = os.path.join(home, LOG_FILE)
    out_path = os.path.join(home, DAEMON_OUT)
    note = f"{last.summary} — daemon did not hold the pidfile within {timeout:.0f}s; see {log_path} and {out_path}"
    return Health(False, note, pid=last.pid, locked=last.locked, offset=last.offset,
                  poll_age_s=last.poll_age_s, started_at=last.started_at)


def restart(home: str, *, stop_timeout: float = STOP_TIMEOUT_S,
            start_timeout: float = START_WAIT_S, max_age: float = DEFAULT_MAX_AGE_S) -> Health:
    """Stop whatever holds the pidfile, then start the ``live-src`` checkout."""
    stop_daemon(home, timeout=stop_timeout)
    start_daemon(home, live_src_dir(home))
    return wait_until_running(home, timeout=start_timeout, max_age=max_age)


def deploy(home: str, ref: str, *, force: bool = False, max_age: float = DEFAULT_MAX_AGE_S,
           remote: str | None = None, force_remote: bool = False) -> tuple[str, Health]:
    """Sync ``live-src`` to ``ref`` and restart. Returns ``(commit, health)``."""
    commit = sync_live_src(home, ref, force=force, remote=remote, force_remote=force_remote)
    return commit, restart(home, max_age=max_age)


def _head_line(src: str) -> str:
    if not _is_checkout(src):
        return f"live-src: {src} (not a checkout)"
    proc = _git(src, "log", "-1", "--format=%h %s", check=False)
    if proc.returncode != 0:
        return f"live-src: {src} (no commits)"
    return f"live-src: {src} @ {_redact(proc.stdout.strip())}"


def format_status(home: str, *, max_age: float = DEFAULT_MAX_AGE_S,
                  remote: str | None = None) -> str:
    """Home, the live-src commit, the deploy remote, and the health summary.

    Does not change exit status. ``remote`` is ``--remote`` when deploy
    passed one; otherwise ``$CLIPWRIGHT_OPS_REMOTE`` or the automatic choice.
    """
    health = assess(home, max_age=max_age)
    src = live_src_dir(home)
    return "\n".join([f"home: {home}", _head_line(src), remote_line(src, remote), health.summary])


def _file_age(path: str, now: float) -> float | None:
    try:
        return max(0.0, now - os.stat(path).st_mtime)
    except OSError:
        return None


def scrub_text(text: str, token: str | None = None) -> str:
    """Remove a bot token from ``text``. The exact token, then the token shape."""
    if token:
        text = redact(token, text)
    return _TOKEN_SHAPE.sub("<token>", text)


def preview_text(text: str, *, token: str | None = None, limit: int = PREVIEW_CHARS) -> str:
    """One line, token-scrubbed, at most ``limit`` characters."""
    collapsed = " ".join(scrub_text(str(text), token).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def events_path(home: str) -> str:
    return os.path.join(home, EVENTS_FILE)


def _dumps_event(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"))


def _encoded_size(events: list[dict]) -> int:
    return sum(len(_dumps_event(event).encode("utf-8")) + 1 for event in events)


def _parse_events(text: str) -> tuple[list[dict], bool]:
    """Decode JSONL. The bool is True when a non-empty line was not an event."""
    events: list[dict] = []
    skipped = False
    for line in text.splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            skipped = True
            continue
        if isinstance(event, dict) and isinstance(event.get("kind"), str):
            events.append(event)
        else:
            skipped = True
    return events, skipped


def _stored_event(event: dict) -> dict:
    """Fields the ring keeps. A stuffed ``file_id`` or message blob does not survive a rewrite."""
    out = {key: event[key] for key in ("ts", "chat_id", "user_id", "kind", "text_preview",
                                       "session", "recipe", "detail") if key in event}
    for key in ("text_preview", "detail"):
        if isinstance(out.get(key), str):
            out[key] = preview_text(out[key])
    return out


def _retain(events: list[dict], now: float) -> list[dict]:
    """Drop events older than 48h, then the oldest lines, until the ring fits."""
    cutoff = now - RETENTION_S
    kept = [_stored_event(event) for event in events
            if isinstance(event.get("ts"), (int, float)) and not isinstance(event.get("ts"), bool)
            and event["ts"] >= cutoff]
    while len(kept) > 1 and _encoded_size(kept) > MAX_EVENT_BYTES:
        kept.pop(0)
    return kept


def _rewrite_events(fh, events: list[dict]) -> None:
    fh.seek(0)
    fh.truncate()
    for event in events:
        fh.write(_dumps_event(event) + "\n")
    fh.flush()


def record_event(home: str, *, kind: str, chat_id: int | None, user_id: int | None,
                 text: str = "", ts: float | None = None, session: str | None = None,
                 recipe: str | None = None, detail: str | None = None,
                 token: str | None = None) -> dict:
    """Append one compact digest event and prune the ring.

    The stored object is ``{ts, chat_id, user_id, kind, text_preview}`` plus
    optional ``session``, ``recipe``, and ``detail``. No media, no message
    JSON, no bot token.
    """
    now = time.time()
    event: dict = {
        "ts": now if ts is None else float(ts),
        "chat_id": chat_id if isinstance(chat_id, int) and not isinstance(chat_id, bool) else None,
        "user_id": user_id if isinstance(user_id, int) and not isinstance(user_id, bool) else None,
        "kind": str(kind)[:32],
        "text_preview": preview_text(text, token=token),
    }
    if session:
        event["session"] = str(session)[:16]
    if recipe:
        event["recipe"] = str(recipe)[:64]
    if detail:
        event["detail"] = preview_text(detail, token=token)
    os.makedirs(home, mode=0o700, exist_ok=True)
    path = events_path(home)
    with _EVENTS_LOCK:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fh = os.fdopen(fd, "r+", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            parsed, _skipped = _parse_events(fh.read())
            _rewrite_events(fh, _retain(parsed + [event], now))
        finally:
            fh.close()
    return event


def load_events(home: str, *, since_ts: float | None = None, prune: bool = False,
                now: float | None = None) -> list[dict]:
    """Events in ``digest-events.jsonl``. ``prune`` rewrites the 48h / size cap."""
    path = events_path(home)
    now = time.time() if now is None else now
    if not os.path.isfile(path):
        return []
    with _EVENTS_LOCK:
        try:
            fh = open(path, "r+", encoding="utf-8")
        except OSError:
            fh = None
        if fh is None:
            try:
                with open(path, encoding="utf-8") as readable:
                    parsed, _skipped = _parse_events(readable.read())
            except OSError:
                return []
            kept = _retain(parsed, now)
        else:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                parsed, skipped = _parse_events(fh.read())
                kept = _retain(parsed, now)
                if prune and (skipped or kept != parsed):
                    _rewrite_events(fh, kept)
            finally:
                fh.close()
    if since_ts is None:
        return kept
    return [event for event in kept
            if isinstance(event.get("ts"), (int, float)) and event["ts"] >= since_ts]


def _caption_text(inst: dict) -> str | None:
    caption = inst.get("caption")
    if not isinstance(caption, dict):
        return None
    text = caption.get("text")
    if isinstance(text, str) and text.strip():
        return preview_text(text)
    return None


def _session_view(sess) -> dict:
    return {
        "token": sess.token,
        "user_id": sess.user_id,
        "chat_id": sess.chat_id,
        "recipe": sess.recipe.get("recipe"),
        "text": _caption_text(sess.recipe),
        "created_at": sess.created_at,
        "updated_at": sess.updated_at,
    }


def _export_view(entry) -> dict:
    return {
        "file_unique_id": entry.file_unique_id,
        "user_id": entry.user_id,
        "token": entry.token,
        "recipe": entry.recipe.get("recipe"),
        "text": _caption_text(entry.recipe),
        "created_at": entry.created_at,
    }


def _health_view(home: str, now: float) -> dict:
    """Pidfile and offset age from the files ``health`` already reads. No poll."""
    health = assess(home, now=now)
    return {
        "ok": health.ok,
        "summary": health.summary,
        "pid": health.pid,
        "locked": health.locked,
        "offset": read_offset(home),
        "pidfile_age_s": _file_age(pidfile_path(home), now),
        "offset_age_s": _file_age(os.path.join(home, OFFSET_FILE), now),
        "poll_age_s": health.poll_age_s,
    }


def _public_event(event: dict) -> dict:
    """The fields a digest may carry. Extra keys (message blobs, file ids) are dropped."""
    out = {key: event[key] for key in ("ts", "chat_id", "user_id", "kind", "text_preview",
                                       "session", "recipe", "detail") if key in event}
    for key in ("text_preview", "detail"):
        if isinstance(out.get(key), str):
            out[key] = preview_text(out[key])
    return out


def build_digest(home: str, *, since_hours: float = 16.0, now: float | None = None) -> dict:
    """Structured overnight signal. Does not read ``bot.env`` or call Telegram."""
    now = time.time() if now is None else now
    since_ts = now - since_hours * 3600.0
    sessions: list[dict] = []
    exports: list[dict] = []
    db_path = os.path.join(home, STATE_DB)
    if os.path.isfile(db_path):
        store = Store(db_path)
        try:
            sessions = [_session_view(sess) for sess in store.list_sessions()]
            exports = [_export_view(entry) for entry in store.ledger_since(since_ts)]
        finally:
            store.close()
    events = [_public_event(event) for event in load_events(home, since_ts=since_ts, prune=True, now=now)]
    return {
        "generated_at": now,
        "since_hours": since_hours,
        "since_ts": since_ts,
        "health": _health_view(home, now),
        "open_sessions": sessions,
        "exports": exports,
        "errors": [event for event in events if event.get("kind") in ("error", "seed_fail")],
        "chatter": [event for event in events if event.get("kind") == "chatter"],
        "events": events,
    }


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "none"
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def format_digest(report: dict) -> str:
    """The human summary ``digest`` prints. The JSON file is the full record."""
    health = report["health"]
    lines = [f"clipwright morning digest — last {report['since_hours']:g}h", health["summary"]]
    offset = health.get("offset")
    lines.append(
        "pidfile age " + _age(health.get("pidfile_age_s"))
        + "; offset age " + _age(health.get("offset_age_s"))
        + (f" (offset {offset})" if offset is not None else "")
    )
    sessions = report["open_sessions"]
    lines.append("")
    lines.append(f"open sessions: {len(sessions)}")
    generated = report["generated_at"]
    for sess in sessions:
        text = f" {sess['text']!r}" if sess.get("text") else ""
        updated = sess.get("updated_at")
        ago = _age(None if updated is None else generated - updated)
        lines.append(
            f"  {sess['token']} user {sess['user_id']} chat {sess['chat_id']} "
            f"{sess.get('recipe')}{text} updated {ago} ago"
        )
    exports = report["exports"]
    lines.append(f"exports: {len(exports)}")
    for entry in exports:
        text = f" {entry['text']!r}" if entry.get("text") else ""
        lines.append(
            f"  {entry.get('recipe')} user {entry.get('user_id')} "
            f"token {entry.get('token')}{text}"
        )
    errors = report["errors"]
    lines.append(f"errors: {len(errors)}")
    for event in errors:
        detail = event.get("detail") or event.get("text_preview") or ""
        lines.append(
            f"  {event.get('kind')} user {event.get('user_id')} "
            f"chat {event.get('chat_id')} {detail}"
        )
    chatter = report["chatter"]
    chats = {event.get("chat_id") for event in chatter}
    lines.append(f"chatter: {len(chatter)} in {len(chats)} chat(s)")
    shown = chatter[-40:]
    if len(chatter) > len(shown):
        lines.append(f"  … {len(chatter) - len(shown)} older in {DIGEST_LATEST}")
    for event in shown:
        lines.append(
            f"  chat {event.get('chat_id')} user {event.get('user_id')}: "
            f"{event.get('text_preview')}"
        )
    return "\n".join(lines)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _write_private(path: str, text: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.chmod(tmp, 0o600)
        _write_all(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _append_jsonl(path: str, obj: dict) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    line = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.chmod(path, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        _write_all(fd, line)
    finally:
        os.close(fd)


def write_digest(home: str, report: dict, *, json_path: str | None = None) -> str:
    """Write ``digest-latest.json``. Optionally append one JSONL line to ``json_path``."""
    latest = os.path.join(home, DIGEST_LATEST)
    _write_private(latest, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if json_path and os.path.abspath(json_path) != os.path.abspath(latest):
        _append_jsonl(json_path, report)
    return latest


def _cmd_digest(home: str, *, since_hours: float, json_path: str | None) -> int:
    try:
        report = build_digest(home, since_hours=since_hours)
        print(format_digest(report))
        write_digest(home, report, json_path=json_path)
    except (OSError, sqlite3.Error) as err:
        raise OpsError(f"could not write the digest: {err}") from None
    return 0


def _running(health: Health) -> bool:
    return bool(health.locked and health.pid and pid_alive(health.pid) and health.ok)


def main(argv: list[str] | None = None) -> int:
    """``python3 -m clipwrightd.ops deploy|restart|status|health|digest``."""
    parser = argparse.ArgumentParser(
        prog="clipwrightd.ops",
        description="Deploy and supervise Grok Bot's Clipwright daemon from $CLIPWRIGHT_HOME/live-src.",
    )
    parser.add_argument("--home", metavar="DIR",
                        help="state directory (default: $CLIPWRIGHT_HOME or ~/.clipwright)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_max_age(target: argparse.ArgumentParser) -> None:
        target.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_S, metavar="SECONDS",
                            help=f"seconds since the last successful getUpdates before health fails "
                                 f"(default {DEFAULT_MAX_AGE_S:g})")

    deploy_p = sub.add_parser("deploy", help="check out REF in live-src, restart clipwrightd, print status")
    deploy_p.add_argument("ref", help="git ref to check out (branch, tag, or commit)")
    deploy_p.add_argument("--force", action="store_true",
                          help="discard uncommitted tracked changes in live-src")
    deploy_p.add_argument("--remote", default=None, metavar="NAME",
                          help=f"git remote to fetch (default: ${OPS_REMOTE_ENV}, else a "
                               f"{FORK_REPO} URL, else origin)")
    deploy_p.add_argument("--force-remote", action="store_true",
                          help=f"deploy even if that remote's repo name is {PLAN_REPO}")
    add_max_age(deploy_p)

    restart_p = sub.add_parser("restart", help="pidfile-aware restart of the current live-src checkout")
    add_max_age(restart_p)

    status_p = sub.add_parser("status", help="print pidfile, lock, and getUpdates age (always exits 0)")
    add_max_age(status_p)

    health_p = sub.add_parser("health", help="exit 1 when the pidfile is stale or getUpdates has stalled")
    add_max_age(health_p)

    digest_p = sub.add_parser(
        "digest",
        help="print overnight signal and write digest-latest.json (does not poll Telegram)",
    )
    digest_p.add_argument("--since", type=float, default=16.0, metavar="HOURS",
                          help="hours of history to include (default 16)")
    digest_p.add_argument("--json", metavar="PATH",
                          help="also append this digest as one JSON line to PATH")

    args = parser.parse_args(argv)
    if getattr(args, "max_age", None) is not None and args.max_age <= 0:
        print("clipwrightd ops: --max-age must be greater than 0", file=sys.stderr)
        return 2
    home = home_dir(args.home)
    try:
        if args.cmd == "digest":
            if not math.isfinite(args.since) or args.since < 0:
                print("clipwrightd ops: --since must be a number of hours >= 0", file=sys.stderr)
                return 2
            return _cmd_digest(home, since_hours=args.since, json_path=args.json)
        if args.cmd == "health":
            health = assess(home, max_age=args.max_age)
            print(health.summary)
            return 0 if health.ok else 1
        if args.cmd == "status":
            print(format_status(home, max_age=args.max_age))
            return 0
        if args.cmd == "restart":
            health = restart(home, max_age=args.max_age)
            print(format_status(home, max_age=args.max_age))
            return 0 if _running(health) else 1
        if args.cmd == "deploy":
            commit, health = deploy(home, args.ref, force=args.force, max_age=args.max_age,
                                    remote=args.remote, force_remote=args.force_remote)
            print(f"checked out {commit}")
            print(format_status(home, max_age=args.max_age, remote=args.remote))
            return 0 if _running(health) else 1
    except OpsError as err:
        print(f"clipwrightd ops: {err}", file=sys.stderr)
        return 2
    print(f"clipwrightd ops: unknown command {args.cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
