"""clipwrightd.session — sessions, undo stack, ledger, quota, tokens, threads."""

import os
import re
import sqlite3
import stat
import threading

import pytest

from clipwrightd import session as session_mod
from clipwrightd.session import UNDO_DEPTH, Store, utc_day

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{6}$")
RECIPE = {"recipe": "caption-loop", "input": "clip.mp4", "caption": {"text": "hi", "size": 64}}


def _variant(i: int) -> dict:
    return {"recipe": "caption-loop", "input": "clip.mp4", "caption": {"text": f"v{i}", "size": 64}}


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "state" / "state.db"))
    yield s
    s.close()


# -- open / schema ----------------------------------------------------------

def test_open_creates_parent_dir_schema_and_wal(tmp_path):
    path = str(tmp_path / "nested" / "dir" / "state.db")
    store = Store(path)
    store.close()
    conn = sqlite3.connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "ledger", "quota"} <= tables
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    pk = [r[1] for r in conn.execute("PRAGMA table_info(quota)") if r[5]]
    assert pk == ["user_id", "day"]
    conn.close()


def test_store_dir_and_files_are_private_whatever_the_umask(tmp_path):
    """Friends' recipes and server paths live here: 0700 dir, 0600 db and WAL sidecars."""
    old_umask = os.umask(0o022)
    try:
        path = str(tmp_path / "state" / "state.db")
        store = Store(path)
        store.create_session(1, 10, RECIPE)          # a write, so -wal/-shm exist
        try:
            modes = {name: stat.S_IMODE(os.stat(os.path.join(tmp_path, "state", name)).st_mode)
                     for name in os.listdir(tmp_path / "state")}
        finally:
            store.close()
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(os.stat(tmp_path / "state").st_mode) == 0o700
    assert set(modes) >= {"state.db", "state.db-wal", "state.db-shm"}
    assert modes == {name: 0o600 for name in modes}


def test_reopen_keeps_state(tmp_path):
    path = str(tmp_path / "state.db")
    first = Store(path)
    token = first.create_session(1, 10, RECIPE)
    first.ledger_put("fid", RECIPE, token)
    first.close()
    second = Store(path)
    assert second.get(token).recipe == RECIPE
    assert second.ledger_get("fid") == RECIPE
    second.close()


# -- sessions ---------------------------------------------------------------

def test_create_get_update_undo_sequence(store):
    token = store.create_session(user_id=42, chat_id=-100, recipe=RECIPE)
    assert TOKEN_RE.match(token)

    s = store.get(token)
    assert s.token == token
    assert (s.user_id, s.chat_id, s.message_id) == (42, -100, None)
    assert s.recipe == RECIPE and s.recipe is not RECIPE
    assert s.undo == []
    assert s.created_at == s.updated_at > 0

    store.update(token, _variant(1), message_id=777)
    store.update(token, _variant(2))
    s = store.get(token)
    assert s.recipe == _variant(2)
    assert s.message_id == 777
    assert s.undo == [RECIPE, _variant(1)]
    assert s.updated_at >= s.created_at

    assert store.undo(token) == _variant(1)
    assert store.get(token).recipe == _variant(1)
    assert store.undo(token) == RECIPE
    assert store.get(token).recipe == RECIPE
    assert store.undo(token) is None
    assert store.get(token).undo == []


def test_undo_cap(store):
    token = store.create_session(1, 1, _variant(0))
    for i in range(1, UNDO_DEPTH + 6):
        store.update(token, _variant(i))
    assert len(store.get(token).undo) == UNDO_DEPTH
    # Newest first on the way back out; the oldest five fell off the bottom.
    restored = [store.undo(token) for _ in range(UNDO_DEPTH)]
    assert restored == [_variant(i) for i in range(UNDO_DEPTH + 4, 4, -1)]
    assert store.undo(token) is None
    assert store.get(token).recipe == _variant(5)


def test_update_with_unchanged_recipe_only_sets_message_id(store):
    token = store.create_session(1, 1, RECIPE)
    store.update(token, dict(RECIPE), message_id=5)
    s = store.get(token)
    assert s.message_id == 5 and s.undo == []
    assert store.undo(token) is None


def test_returned_recipe_is_a_copy(store):
    token = store.create_session(1, 1, RECIPE)
    store.get(token).recipe["caption"]["text"] = "mutated"
    assert store.get(token).recipe == RECIPE


def test_unknown_token(store):
    assert store.get("nopeee") is None
    assert store.undo("nopeee") is None
    with pytest.raises(KeyError):
        store.update("nopeee", RECIPE)


# -- tokens -----------------------------------------------------------------

def test_tokens_unique_and_urlsafe(store):
    tokens = [store.create_session(1, 1, RECIPE) for _ in range(200)]
    assert len(set(tokens)) == 200
    assert all(TOKEN_RE.match(t) for t in tokens)


def test_token_collision_is_retried(store, monkeypatch):
    seq = iter(["AAAAAA", "AAAAAA", "AAAAAA", "BBBBBB"])
    monkeypatch.setattr(session_mod, "_new_token", lambda: next(seq))
    assert store.create_session(1, 1, RECIPE) == "AAAAAA"
    assert store.create_session(1, 1, RECIPE) == "BBBBBB"
    assert store.get("AAAAAA") is not None and store.get("BBBBBB") is not None


def test_set_message_id_leaves_recipe_and_undo_alone(store):
    token = store.create_session(1, 1, RECIPE)
    store.update(token, _variant(1))
    store.set_message_id(token, 42)               # a worker recording where the preview landed
    s = store.get(token)
    assert s.message_id == 42 and s.recipe == _variant(1) and s.undo == [RECIPE]
    with pytest.raises(KeyError):
        store.set_message_id("nopeee", 1)


# -- expiry and references ------------------------------------------------------

def test_expire_sessions_drops_only_the_idle(store):
    old = store.create_session(1, 10, RECIPE)
    young = store.create_session(2, 20, _variant(1))
    later = store.get(young).updated_at
    expired = store.expire_sessions(cutoff=later)       # strictly before: ``young`` survives
    assert [s.token for s in expired] == [old]
    assert expired[0].recipe == RECIPE and expired[0].chat_id == 10
    assert store.get(old) is None and store.get(young) is not None
    assert store.session_tokens() == {young}
    assert store.expire_sessions(cutoff=later) == []


def test_referenced_inputs_covers_sessions_and_ledger(store):
    assert store.referenced_inputs() == set()
    a = store.create_session(1, 1, {"recipe": "gifify", "input": "/up/1/a.mp4"})
    store.create_session(1, 1, {"recipe": "gifify", "input": "/up/1/b.mp4"})
    store.create_session(1, 1, {"recipe": "gifify"})            # no input: nothing to keep
    store.ledger_put("fid", {"recipe": "gifify", "input": "/up/1/exported.mp4"}, a, user_id=1)
    assert store.referenced_inputs() == {"/up/1/a.mp4", "/up/1/b.mp4", "/up/1/exported.mp4"}
    store.expire_sessions(cutoff=store.get(a).updated_at + 1)
    assert store.referenced_inputs() == {"/up/1/exported.mp4"}  # ledger entries never expire


# -- ledger -----------------------------------------------------------------

def test_ledger_put_get(store):
    assert store.ledger_get("missing") is None and store.ledger_entry("missing") is None
    token = store.create_session(1, 1, RECIPE)
    store.ledger_put("AgADfile1", RECIPE, token, user_id=1)
    assert store.ledger_get("AgADfile1") == RECIPE
    entry = store.ledger_entry("AgADfile1")
    assert (entry.file_unique_id, entry.recipe, entry.token, entry.user_id) == ("AgADfile1", RECIPE, token, 1)
    assert entry.created_at > 0
    store.ledger_put("AgADfile1", _variant(9), user_id=2)
    assert store.ledger_get("AgADfile1") == _variant(9)
    assert store.ledger_entry("AgADfile1").user_id == 2 and store.ledger_entry("AgADfile1").token is None
    store.ledger_put("AgADfile2", _variant(2))
    assert store.ledger_get("AgADfile2") == _variant(2) and store.ledger_entry("AgADfile2").user_id is None


def test_ledger_gains_the_user_column_on_an_older_file(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE ledger (file_unique_id TEXT PRIMARY KEY, recipe_json TEXT NOT NULL,
                             token TEXT, created_at REAL NOT NULL);
        INSERT INTO ledger VALUES ('legacy', '{"recipe": "gifify"}', 'AAAAAA', 1.0);
    """)
    conn.commit()
    conn.close()
    store = Store(path)
    try:
        entry = store.ledger_entry("legacy")
        assert entry.user_id is None and entry.token == "AAAAAA" and entry.recipe == {"recipe": "gifify"}
        store.ledger_put("fresh", RECIPE, user_id=7)
        assert store.ledger_entry("fresh").user_id == 7
    finally:
        store.close()
    reopened = Store(path)                        # the migration is idempotent
    assert reopened.ledger_entry("fresh").user_id == 7
    reopened.close()


# -- quota ------------------------------------------------------------------

def test_quota_crossing(store):
    hits = [store.quota_hit(7, per_day=3) for _ in range(5)]
    assert hits == [False, False, False, True, True]
    assert store.quota_hit(8, per_day=3) is False  # other user untouched


def test_quota_buckets_by_utc_day(store):
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", utc_day())
    assert utc_day(0) == "1970-01-01"
    assert utc_day(86399.9) == "1970-01-01" and utc_day(86400) == "1970-01-02"
    for _ in range(3):
        store.quota_hit(7, per_day=2, day="2026-09-11")
    assert store.quota_hit(7, per_day=2, day="2026-09-12") is False


# -- threads ----------------------------------------------------------------

def test_writes_from_second_thread(store):
    result: dict = {}

    def worker():
        try:
            token = store.create_session(3, 30, RECIPE)
            store.update(token, _variant(1), message_id=9)
            store.ledger_put("fid-thread", _variant(1), token, user_id=3)
            result["token"] = token
        except Exception as exc:  # threads swallow exceptions; surface via assert
            result["error"] = exc

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=10)
    assert "error" not in result, result.get("error")
    s = store.get(result["token"])
    assert s.recipe == _variant(1) and s.message_id == 9 and s.user_id == 3
    assert store.ledger_get("fid-thread") == _variant(1)
    assert store.ledger_entry("fid-thread").user_id == 3


def test_concurrent_quota_increments_are_atomic(store):
    per_thread, n_threads = 50, 4

    def hammer():
        for _ in range(per_thread):
            store.quota_hit(5, per_day=10_000)

    threads = [threading.Thread(target=hammer) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    # The next hit is count 201: exactly per_thread * n_threads landed.
    assert store.quota_hit(5, per_day=per_thread * n_threads) is True
    assert store.quota_hit(5, per_day=per_thread * n_threads + 1) is True
    assert store.quota_hit(5, per_day=per_thread * n_threads + 3) is False
