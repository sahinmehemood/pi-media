"""Librarian v2 Phase 1 tests: store (domain + counters) and the leased
job engine, incl. the chaos test (SPEC §14 Phase 1 gate).

Modules are loaded by file path: store.py/jobs.py are stdlib-only by
design, and importing the real `Backend` package would pull the app's
heavy dependencies — the tests must exercise the real files, not stubs.
"""

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "librarian_v2"))
import harness  # noqa: E402


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


store_mod = _load("libv2_store", "Backend/librarian_v2/store.py")
jobs_mod = _load("libv2_jobs", "Backend/librarian_v2/jobs.py")

T0 = 1_700_000_000


def _store():
    path = str(Path(tempfile.mkdtemp(prefix="libv2core-")) / "test.sqlite3")
    return store_mod.open_store(path)


def _series_with_episodes(store, n=3):
    sid = store.upsert_series("Perfect World", kind="donghua", year=2021,
                              provider="tmdb", provider_id="999", now=T0)
    store.replace_episode_list(sid, [
        {"season_number": 1, "episode_number": i + 1,
         "absolute_number": i + 1, "title": f"Ep {i + 1}", "air_date": None}
        for i in range(n)], T0)
    return sid


def _file(store, msg):
    return store.upsert_media_file(-1001, msg, T0,
                                   original_caption=f"cap {msg}",
                                   current_caption=f"cap {msg}",
                                   location_status="located")


# ---------------------------------------------------------------- store ---

def test_counters_follow_assignments():
    store = _store()
    sid = _series_with_episodes(store, 3)
    eps = store.list_episodes(sid)
    assert len(eps) == 3
    row = store.get_series(sid)
    assert (row["episode_count_cached"], row["file_count_cached"],
            row["missing_count_cached"]) == (3, 0, 3)
    f1, f2 = _file(store, 11), _file(store, 12)
    store.assign_file(f1, sid, eps[0]["id"], True, "test", 0.97, T0)
    store.assign_file(f2, sid, eps[1]["id"], True, "test", 0.97, T0)
    row = store.get_series(sid)
    assert (row["file_count_cached"], row["missing_count_cached"]) == (2, 1)
    store.unassign_file(f2, T0)
    row = store.get_series(sid)
    assert (row["file_count_cached"], row["missing_count_cached"]) == (1, 2)
    assert store.count_unidentified() == 1


def test_plan_hash_stable_and_tamper_evident():
    store = _store()
    sid = _series_with_episodes(store, 2)
    eps = store.list_episodes(sid)
    oid = store.create_order("series", T0, series_id=sid)
    f_a, f_b = _file(store, 31), _file(store, 32)
    items = [{"media_file_id": f_a, "episode_id": eps[0]["id"],
              "new_caption": "A"},
             {"media_file_id": f_b, "episode_id": eps[1]["id"],
              "new_caption": "B"}]
    h1 = store_mod.Store.plan_hash(items)
    h2 = store_mod.Store.plan_hash(list(reversed(items)))
    assert h1 == h2  # order of submission does not change the artefact
    changed = [dict(items[0], new_caption="A2"), items[1]]
    assert store_mod.Store.plan_hash(changed) != h1
    pid = store.create_plan(oid, [
        {**it, "evidence_score": 0.97, "score_breakdown": {}, "tier": "auto"}
        for it in items], T0)
    assert store.get_plan(pid)["plan_hash"] == h1
    assert len(store.list_plan_items(pid)) == 2


def test_store_usable_from_worker_thread():
    # Regression (staging proof, 2026-11-20): the store is opened on the
    # app's thread and used from the job worker thread — sqlite3's
    # default thread check crashed exactly that in production shape.
    import threading
    store = _store()
    errors = []

    def work():
        try:
            store.upsert_channel(-1001, "Ani", now=T0)
            store.upsert_series("Threaded", kind="tv", now=T0)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=work)
    t.start()
    t.join(timeout=10)
    assert not errors, errors
    assert store.list_series()


def test_revisions_roundtrip():
    store = _store()
    f1 = _file(store, 21)
    rid = store.add_revision(f1, "before", "after", "bot", None, T0)
    latest = store.latest_revision_for_file(f1)
    assert latest["id"] == rid and latest["after_caption"] == "after"
    store.mark_revision_reverted(rid, T0 + 10)
    assert store.latest_revision_for_file(f1) is None


def test_provider_cache_negative_and_expiry():
    store = _store()
    store.cache_put("tmdb:search:pw", [], 100, T0, negative=True)
    hit = store.cache_get("tmdb:search:pw", T0 + 50)
    assert hit == {"payload": [], "negative": True}
    assert store.cache_get("tmdb:search:pw", T0 + 101) is None
    store.cache_put("tmdb:series:1", {"id": 1}, 100, T0)
    assert store.cache_get("tmdb:series:1", T0 + 1)["negative"] is False


# ----------------------------------------------------------------- jobs ---

def test_jobs_claim_lease_and_heartbeat():
    store = _store()
    jid = jobs_mod.enqueue(store, "identify_series", {"x": 1}, T0)
    job = jobs_mod.claim_next(store, "w1", T0)
    assert job["id"] == jid and job["state"] == "running"
    assert job["lease_expires_at"] == T0 + jobs_mod.LEASE_SECONDS
    assert jobs_mod.claim_next(store, "w2", T0) is None  # leased out
    jobs_mod.heartbeat(store, jid, "w1", T0 + 60)
    row = jobs_mod.get_job(store, jid)
    assert row["lease_expires_at"] == T0 + 60 + jobs_mod.LEASE_SECONDS
    jobs_mod.complete(store, jid, T0 + 61)
    assert jobs_mod.get_job(store, jid)["state"] == "done"


def test_jobs_snooze_keeps_attempts_and_wakes():
    store = _store()
    jid = jobs_mod.enqueue(store, "identify_series", {}, T0)

    def sleepy(payload, job):
        raise jobs_mod.SnoozeError(600, "provider down")

    assert jobs_mod.run_once(store, {"identify_series": sleepy}, "w1", T0)
    row = jobs_mod.get_job(store, jid)
    assert row["state"] == "snoozed" and row["attempts"] == 0
    # Not due yet: run_once finds nothing.
    assert not jobs_mod.run_once(store, {"identify_series": sleepy},
                                 "w1", T0 + 599)
    done = []
    assert jobs_mod.run_once(
        store, {"identify_series": lambda p, j: done.append(1)}, "w1",
        T0 + 600)
    assert done == [1]
    assert jobs_mod.get_job(store, jid)["state"] == "done"


def test_jobs_retry_backoff_then_dead():
    store = _store()
    jid = jobs_mod.enqueue(store, "apply_caption", {}, T0, max_attempts=2)

    def boom(payload, job):
        raise ValueError("edit failed")

    handlers = {"apply_caption": boom}
    assert jobs_mod.run_once(store, handlers, "w1", T0)
    row = jobs_mod.get_job(store, jid)
    assert row["state"] == "snoozed" and row["attempts"] == 1
    assert jobs_mod.run_once(store, handlers, "w1", T0 + 10_000)
    row = jobs_mod.get_job(store, jid)
    assert row["state"] == "dead" and row["attempts"] == 2
    assert "edit failed" in row["last_error"]


class _Crash(BaseException):
    """Simulates the worker process dying mid-job (not an Exception)."""


def test_jobs_chaos_crash_recovers_via_lease():
    store = _store()
    jid = jobs_mod.enqueue(store, "apply_caption",
                           {"file": 7, "effect_key": "fix-7"}, T0)
    applied = set()

    def crashing(payload, job):
        raise _Crash()

    def healthy(payload, job):
        applied.add(payload["effect_key"])  # idempotent effect

    try:
        jobs_mod.run_once(store, {"apply_caption": crashing}, "w1", T0)
    except _Crash:
        pass
    else:
        raise AssertionError("crash did not propagate")
    row = jobs_mod.get_job(store, jid)
    assert row["state"] == "running"  # worker died holding the lease
    # Before lease expiry nobody else can take it.
    assert not jobs_mod.run_once(store, {"apply_caption": healthy}, "w2",
                                 T0 + jobs_mod.LEASE_SECONDS - 1)
    # After expiry the reaper recovers it; the healthy worker completes it.
    assert jobs_mod.run_once(store, {"apply_caption": healthy}, "w2",
                             T0 + jobs_mod.LEASE_SECONDS + 1)
    assert jobs_mod.get_job(store, jid)["state"] == "done"
    assert applied == {"fix-7"}  # effect applied exactly once
