"""clipwrightd.ops — live-src sync, pidfile-aware restart, and health.

Git runs against a local origin. The daemon is not started for real: process
control is a holder script or a fake ``Popen``. No network, no token.
"""

from __future__ import annotations

import fcntl
import os
import stat
import subprocess
import sys
import textwrap
import time

import pytest

from clipwrightd.ops import (
    Health,
    OpsError,
    assess,
    lock_held,
    main,
    process_started_at,
    read_offset,
    remote_line,
    repo_name,
    start_daemon,
    stop_daemon,
    sync_live_src,
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "ops",
    "GIT_AUTHOR_EMAIL": "ops@example.com",
    "GIT_COMMITTER_NAME": "ops",
    "GIT_COMMITTER_EMAIL": "ops@example.com",
}


def _git(cwd: str, *args: str, env: dict | None = None) -> str:
    merged = os.environ.copy()
    merged.update(_GIT_ENV)
    if env:
        merged.update(env)
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=merged,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _origin(tmp_path) -> str:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(str(origin), "init", "-b", "main")
    (origin / "README").write_text("one\n")
    _git(str(origin), "add", "README")
    _git(str(origin), "commit", "-m", "one")
    return str(origin)


def _clone(home, origin: str) -> str:
    src = home / "live-src"
    _git(str(home), "clone", origin, str(src))
    return str(src)


def _hold(home) -> subprocess.Popen:
    """A child that holds the pidfile lock until it is signalled."""
    script = home / "hold.py"
    script.write_text(textwrap.dedent("""\
        import fcntl, os, sys, time
        path = sys.argv[1]
        fh = open(path, "a+", encoding="utf-8")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\\n")
        fh.flush()
        time.sleep(60)
    """))
    proc = subprocess.Popen([sys.executable, str(script), str(home / "daemon.pid")])
    deadline = time.monotonic() + 5
    path = home / "daemon.pid"
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text().strip().isdigit() and lock_held(str(path)):
            return proc
        time.sleep(0.05)
    proc.kill()
    raise AssertionError("holder never took the pidfile lock")


def _release(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _commit_repo(path, text: str, message: str) -> str:
    path.mkdir()
    _git(str(path), "init", "-b", "main")
    (path / "README").write_text(text)
    _git(str(path), "add", "README")
    _git(str(path), "commit", "-m", message)
    return str(path)


@pytest.fixture
def home(tmp_path, monkeypatch):
    for key, value in _GIT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("CLIPWRIGHT_OPS_REMOTE", raising=False)
    directory = tmp_path / "state"
    directory.mkdir()
    return directory


def test_sync_prefers_origin_and_checks_out_a_raw_commit(home, tmp_path):
    origin = _origin(tmp_path)
    first = _git(origin, "rev-parse", "HEAD")
    src = _clone(home, origin)
    (tmp_path / "origin" / "README").write_text("two\n")
    _git(origin, "commit", "-am", "two")
    second = _git(origin, "rev-parse", "HEAD")

    got = sync_live_src(str(home), "main")
    assert got == second
    assert _git(src, "rev-parse", "HEAD") == second

    again = sync_live_src(str(home), first)
    assert again == first
    assert _git(src, "rev-parse", "HEAD") == first


def test_sync_refuses_a_dirty_tree_unless_forced(home, tmp_path):
    origin = _origin(tmp_path)
    src = _clone(home, origin)
    (home / "live-src" / "README").write_text("dirty\n")
    with pytest.raises(OpsError, match="uncommitted"):
        sync_live_src(str(home), "main")
    assert (home / "live-src" / "README").read_text() == "dirty\n"

    commit = sync_live_src(str(home), "main", force=True)
    assert commit == _git(src, "rev-parse", "HEAD")
    assert (home / "live-src" / "README").read_text() == "one\n"


def test_sync_leaves_an_untracked_env_file_alone(home, tmp_path):
    origin = _origin(tmp_path)
    _clone(home, origin)
    secret = home / "live-src" / "bot.env"
    secret.write_text("CLIPWRIGHT_BOT_TOKEN=should-not-be-committed\n")
    sync_live_src(str(home), "main", force=True)
    assert secret.read_text().startswith("CLIPWRIGHT_BOT_TOKEN=")


def test_sync_missing_checkout_names_the_directory(home):
    with pytest.raises(OpsError, match="not a git checkout"):
        sync_live_src(str(home), "main")


def test_process_start_is_recent_for_this_process():
    """``/proc`` field 22 is the real start, not a made-up heartbeat."""
    started = process_started_at(os.getpid())
    assert started is not None
    age = time.time() - started
    assert 0 <= age < 86_400


def test_daemon_out_is_private(home):
    src = home / "live-src"
    src.mkdir()

    class Fake:
        pid = 1

        def __init__(self, argv, **kwargs):
            pass

    start_daemon(str(home), str(src), popen=Fake)
    assert stat.S_IMODE((home / "daemon.out").stat().st_mode) == 0o600


def test_health_missing_pidfile(home):
    verdict = assess(str(home))
    assert verdict.ok is False and verdict.summary.startswith("down:")
    assert main(["--home", str(home), "health"]) == 1
    assert main(["--home", str(home), "status"]) == 0


def test_health_dead_pid_is_stale(home):
    (home / "daemon.pid").write_text("2147483646\n")
    verdict = assess(str(home))
    assert verdict.ok is False
    assert "not running" in verdict.summary


def test_health_live_pid_without_the_lock_is_not_signalled(home):
    """A recycled pid must not be treated as the daemon, and must not be killed."""
    (home / "daemon.pid").write_text(f"{os.getpid()}\n")
    verdict = assess(str(home))
    assert verdict.ok is False and "does not hold" in verdict.summary
    message = stop_daemon(str(home))
    assert "not signalling" in message
    assert os.getpid() > 0          # this process is still the one running the test


def test_health_startup_grace_and_stale_poll(home):
    path = home / "daemon.pid"
    fh = open(path, "w", encoding="utf-8")
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    try:
        offset = home / "offset"
        offset.write_text("42\n")
        now = time.time()
        old = now - 10_000
        os.utime(offset, (old, old))

        fresh = assess(str(home), now=now, started_at=now - 5, use_proc_start=False)
        assert fresh.ok is True
        assert fresh.offset == 42
        assert fresh.poll_age_s is not None and fresh.poll_age_s < 30

        stalled = assess(str(home), now=now, started_at=old, use_proc_start=False, max_age=600)
        assert stalled.ok is False and stalled.summary.startswith("stalled:")

        os.utime(offset, (now - 10, now - 10))
        progressed = assess(str(home), now=now, started_at=old, use_proc_start=False, max_age=600)
        assert progressed.ok is True
        assert "offset 42" in progressed.summary
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def test_health_unknown_progress_when_start_and_offset_are_both_missing(home):
    path = home / "daemon.pid"
    fh = open(path, "w", encoding="utf-8")
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    try:
        verdict = assess(str(home), started_at=None, use_proc_start=False)
        assert verdict.ok is False and "unknown" in verdict.summary
        assert read_offset(str(home)) is None
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def test_stop_sigterms_the_pidfile_holder(home):
    proc = _hold(home)
    try:
        message = stop_daemon(str(home), timeout=5)
        assert "released" in message or "not running" in message or "exited" in message
        proc.wait(timeout=5)
    finally:
        _release(proc)
    assert not lock_held(str(home / "daemon.pid"))


def test_start_daemon_argv_uses_the_checkout_and_refuses_a_held_lock(home, monkeypatch):
    src = home / "live-src"
    src.mkdir()
    spawned: list[dict] = []

    class Fake:
        pid = 4242

        def __init__(self, argv, **kwargs):
            spawned.append({"argv": argv, "kwargs": kwargs})

    pid = start_daemon(str(home), str(src), popen=Fake)
    assert pid == 4242
    spec = spawned[0]
    assert spec["argv"] == [sys.executable, "-m", "clipwrightd", "--home", str(home)]
    assert spec["kwargs"]["cwd"] == str(src)
    assert spec["kwargs"]["env"]["CLIPWRIGHT_HOME"] == str(home)
    assert spec["kwargs"]["env"]["PYTHONPATH"].split(os.pathsep)[0] == str(src)
    assert spec["kwargs"]["start_new_session"] is True
    assert spec["kwargs"]["stdin"] == subprocess.DEVNULL
    out = home / "daemon.out"
    assert out.is_file()

    proc = _hold(home)
    try:
        with pytest.raises(OpsError, match="already holds"):
            start_daemon(str(home), str(src), popen=Fake)
    finally:
        _release(proc)


def test_deploy_command_syncs_then_restarts(home, tmp_path, monkeypatch, capsys):
    origin = _origin(tmp_path)
    _clone(home, origin)
    calls: list[str] = []

    def fake_restart(state, **kwargs):
        calls.append(state)
        return Health(True, "ok: pid 7 holds pidfile; offset 1; getUpdates 0s ago",
                      pid=os.getpid(), locked=True)

    monkeypatch.setattr("clipwrightd.ops.restart", fake_restart)
    # assess() inside format_status would see no real daemon; stub it too
    monkeypatch.setattr(
        "clipwrightd.ops.assess",
        lambda state, **kwargs: Health(True, "ok: pid 7 holds the pidfile; getUpdates 0s ago",
                                       pid=7, locked=True),
    )
    monkeypatch.setattr("clipwrightd.ops.pid_alive", lambda pid: True)

    assert main(["--home", str(home), "deploy", "main"]) == 0
    assert calls == [str(home)]
    out = capsys.readouterr().out
    assert "checked out" in out and "live-src:" in out and out.strip().splitlines()[-1].startswith("ok:")


def test_deploy_dirty_tree_exits_2_without_restarting(home, tmp_path, monkeypatch, capsys):
    origin = _origin(tmp_path)
    _clone(home, origin)
    (home / "live-src" / "README").write_text("dirty\n")
    monkeypatch.setattr("clipwrightd.ops.restart", lambda *a, **k: pytest.fail("restarted a dirty tree"))
    assert main(["--home", str(home), "deploy", "main"]) == 2
    assert "uncommitted" in capsys.readouterr().err


def test_restart_command_reports_a_daemon_that_never_took_the_lock(home, monkeypatch, capsys):
    src = home / "live-src"
    src.mkdir()
    monkeypatch.setattr("clipwrightd.ops.stop_daemon", lambda *a, **k: "stopped")
    monkeypatch.setattr("clipwrightd.ops.start_daemon", lambda *a, **k: 1)
    monkeypatch.setattr(
        "clipwrightd.ops.wait_until_running",
        lambda *a, **k: Health(False, "down: no pidfile"),
    )
    monkeypatch.setattr(
        "clipwrightd.ops.assess",
        lambda *a, **k: Health(False, "down: no pidfile"),
    )
    assert main(["--home", str(home), "restart"]) == 1
    err = capsys.readouterr()
    assert "down: no pidfile" in err.out


def test_status_redacts_userinfo_in_a_commit_subject(home, tmp_path, capsys):
    origin = _origin(tmp_path)
    _git(origin, "commit", "--allow-empty", "-m", "mirror https://user:secret@example.test/clipwright")
    _clone(home, origin)
    sync_live_src(str(home), "main")
    assert main(["--home", str(home), "status"]) == 0
    text = capsys.readouterr().out
    assert "secret" not in text
    assert "https://example.test/clipwright" in text


def test_max_age_must_be_positive(capsys):
    assert main(["health", "--max-age", "0"]) == 2
    assert "--max-age" in capsys.readouterr().err


def test_repo_name_strips_git_suffix_and_userinfo():
    assert repo_name("https://github.com/FlipperZero2020/clipwright-grok.git") == "clipwright-grok"
    assert repo_name("https://github.com/FlipperZero2020/clipwright-grok") == "clipwright-grok"
    assert repo_name("git@github.com:FlipperZero2020/CLIPWRIGHT_PLAN.git") == "CLIPWRIGHT_PLAN"
    assert repo_name("ssh://git@github.com/org/clipwright-grok") == "clipwright-grok"
    assert repo_name("https://user:secret@github.com/org/clipwright-grok.git") == "clipwright-grok"


def test_plan_origin_is_refused_before_checkout(home, tmp_path):
    """The production failure: origin is CLIPWRIGHT_PLAN, deploy must not detach there."""
    plan = _commit_repo(tmp_path / "CLIPWRIGHT_PLAN", "old\n", "old")
    src = _clone(home, plan)
    (tmp_path / "CLIPWRIGHT_PLAN" / "README").write_text("plan-main\n")
    _git(plan, "commit", "-am", "plan moves")
    plan_head = _git(plan, "rev-parse", "HEAD")
    cloned = _git(src, "rev-parse", "HEAD")
    assert cloned != plan_head
    with pytest.raises(OpsError, match="CLIPWRIGHT_PLAN") as err:
        sync_live_src(str(home), "main")
    assert "upstream" in str(err.value) and "clipwright-grok" in str(err.value)
    assert _git(src, "rev-parse", "HEAD") == cloned
    assert (home / "live-src" / "README").read_text() == "old\n"
    # A URL that only looks like the plan repo is the same refusal, before fetch.
    _git(src, "remote", "set-url", "origin", "https://github.com/example/CLIPWRIGHT_PLAN.git")
    with pytest.raises(OpsError, match="CLIPWRIGHT_PLAN"):
        sync_live_src(str(home), "main")
    assert _git(src, "rev-parse", "HEAD") == cloned
    _git(src, "remote", "set-url", "origin", plan)
    assert sync_live_src(str(home), "main", force_remote=True) == plan_head
    assert (home / "live-src" / "README").read_text() == "plan-main\n"


def test_fork_remote_is_used_instead_of_plan_origin(home, tmp_path):
    plan = _commit_repo(tmp_path / "CLIPWRIGHT_PLAN", "plan\n", "plan")
    fork = _commit_repo(tmp_path / "clipwright-grok", "fork\n", "fork")
    src = _clone(home, plan)
    _git(src, "remote", "add", "grok", fork)
    fork_head = _git(fork, "rev-parse", "HEAD")
    assert sync_live_src(str(home), "main") == fork_head
    assert (home / "live-src" / "README").read_text() == "fork\n"
    line = remote_line(src)
    assert line.startswith("remote: grok ")
    assert "clipwright-grok" in line and "used instead of origin" in line


def test_deploy_refuses_plan_origin_and_status_names_it(home, tmp_path, monkeypatch, capsys):
    plan = _commit_repo(tmp_path / "CLIPWRIGHT_PLAN", "plan\n", "plan")
    _clone(home, plan)
    monkeypatch.setattr("clipwrightd.ops.restart", lambda *a, **k: pytest.fail("restarted the plan repo"))
    assert main(["--home", str(home), "deploy", "main"]) == 2
    assert "CLIPWRIGHT_PLAN" in capsys.readouterr().err
    assert main(["--home", str(home), "status"]) == 0
    status = capsys.readouterr().out
    assert "remote: origin" in status and "CLIPWRIGHT_PLAN" in status
    assert "--force-remote" in status


def test_remote_flag_beats_env_and_fork_preference(home, tmp_path, monkeypatch, capsys):
    fork = _commit_repo(tmp_path / "clipwright-grok", "fork\n", "fork")
    other = _commit_repo(tmp_path / "other-repo", "other\n", "other")
    src = _clone(home, fork)
    _git(src, "remote", "add", "other", other)
    monkeypatch.setattr(
        "clipwrightd.ops.restart",
        lambda *a, **k: Health(True, "ok: pid 7 holds pidfile; getUpdates 0s ago",
                               pid=os.getpid(), locked=True),
    )
    monkeypatch.setattr("clipwrightd.ops.pid_alive", lambda pid: True)
    monkeypatch.setattr(
        "clipwrightd.ops.assess",
        lambda *a, **k: Health(True, "ok: pid 7 holds the pidfile; getUpdates 0s ago",
                               pid=7, locked=True),
    )
    monkeypatch.setenv("CLIPWRIGHT_OPS_REMOTE", "other")
    assert main(["--home", str(home), "deploy", "--remote", "origin", "main"]) == 0
    assert (home / "live-src" / "README").read_text() == "fork\n"
    out = capsys.readouterr().out
    assert "remote: origin" in out and "clipwright-grok" in out

    assert main(["--home", str(home), "deploy", "main"]) == 0
    assert (home / "live-src" / "README").read_text() == "other\n"
    assert "remote: other" in capsys.readouterr().out


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "deploy" in capsys.readouterr().out
