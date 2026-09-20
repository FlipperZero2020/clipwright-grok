"""clipwrightd.queue — one render worker, per-user concurrency of 1.

Renders block, and the poll loop must never block, so every cook runs on
this queue's single daemon thread. Two caps keep one friend from pegging
the box: a user may have at most one job queued *or* running, and the
whole queue holds at most ``depth`` jobs. ``submit`` reports the 1-based
position in line (1 = running now or next) so the daemon can say "queued,
#3" instead of going quiet, and returns ``None`` when a cap refuses the job.
``can_accept`` answers the same question without enqueueing, so a handler
can check admission *before* it edits a session: the worker only ever frees
slots, so a job that ``can_accept`` said yes to is taken by the ``submit``
that follows on the same thread.

A job is any zero-argument callable. It runs inside ``try/except``: a
failing render is logged and the worker moves on, so one bad clip never
takes the daemon's only worker with it. ``inline=True`` runs each job in
the caller's thread the moment it is submitted — no thread at all — which
is how tests drive the daemon deterministically.

``stop`` lets the running job finish (renders are bounded by ffmpeg's own
timeout) and hands back the jobs that were still waiting, so the daemon
can tell their users instead of dropping them silently.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable

log = logging.getLogger("clipwrightd.queue")

Job = Callable[[], None]
_POLL_S = 0.1


class RenderQueue:
    """A depth-capped FIFO of render jobs with one worker and one job per user."""

    def __init__(self, depth: int = 8, *, inline: bool = False) -> None:
        if depth < 1:
            raise ValueError(f"depth must be >= 1, got {depth}")
        self.depth = depth
        self.inline = inline
        self._jobs: queue.Queue[tuple[int, Job]] = queue.Queue()
        self._lock = threading.Lock()
        self._active: set[int] = set()
        self._size = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if not inline:
            self._thread = threading.Thread(target=self._work, name="clipwright-render", daemon=True)
            self._thread.start()

    # -- public ------------------------------------------------------------

    @property
    def size(self) -> int:
        """Jobs queued or running right now."""
        with self._lock:
            return self._size

    def has(self, user_id: int) -> bool:
        """True while ``user_id`` has a job queued or running."""
        with self._lock:
            return user_id in self._active

    def can_accept(self, user_id: int) -> bool:
        """Whether ``submit(user_id, ...)`` would be taken right now."""
        with self._lock:
            return user_id not in self._active and self._size < self.depth

    def submit(self, user_id: int, job: Job) -> int | None:
        """Enqueue ``job`` for ``user_id``; return its 1-based position or None.

        None means refused: the user already has a job in the queue, or the
        queue is at ``depth``. The caller decides what to tell the user.
        """
        with self._lock:
            if user_id in self._active or self._size >= self.depth:
                return None
            self._active.add(user_id)
            self._size += 1
            position = self._size
        if self.inline:
            self._run(user_id, job)
            return position
        self._jobs.put((user_id, job))
        return position

    def stop(self, timeout: float | None = None) -> list[tuple[int, Job]]:
        """Let the running job finish, stop the worker, and return the jobs that never ran.

        ``timeout`` bounds the wait for the running job; ``None`` waits for
        it (a render is bounded by ffmpeg's own timeout, so this ends). When
        the worker is still busy after a finite timeout the waiting jobs are
        left alone — it would race the worker to drain them — and ``[]`` is
        returned.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                return []
        dropped: list[tuple[int, Job]] = []
        while True:
            try:
                dropped.append(self._jobs.get_nowait())
            except queue.Empty:
                break
        with self._lock:
            for user_id, _ in dropped:
                self._active.discard(user_id)
                self._size -= 1
        return dropped

    # -- worker ------------------------------------------------------------

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                user_id, job = self._jobs.get(timeout=_POLL_S)
            except queue.Empty:
                continue
            self._run(user_id, job)

    def _run(self, user_id: int, job: Job) -> None:
        try:
            job()
        except Exception:
            log.exception("render job for user %s failed", user_id)
        finally:
            with self._lock:
                self._active.discard(user_id)
                self._size -= 1
