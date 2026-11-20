"""Librarian v2 job engine — a leased job table in the same SQLite store.

The pattern every mature queue converges on (SPEC §7): at-least-once
delivery + idempotent handlers. A claim carries a lease; a reaper returns
expired leases to `ready`. There is no state in which work is locked by
something dead — the v1 stale-claims failure is impossible by construction.

Handler outcome protocol: return None for done; raise SnoozeError(seconds)
to reschedule WITHOUT consuming an attempt (provider Retry-After, Telegram
FLOOD_WAIT); raise DeadError(msg) to dead-letter immediately; any other
exception is a retryable failure with exponential backoff, dead-lettered
at max_attempts.
"""

import json

LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 30
BACKOFF_BASE_SECONDS = 30
DEFAULT_MAX_ATTEMPTS = 4


class SnoozeError(Exception):
    def __init__(self, seconds, reason=""):
        super().__init__(reason or f"snooze {seconds}s")
        self.seconds = int(seconds)


class DeadError(Exception):
    pass


def enqueue(store, kind, payload, now, max_attempts=DEFAULT_MAX_ATTEMPTS,
            delay=0):
    with store.tx(immediate=True):
        cur = store.conn.execute(
            "INSERT INTO job (kind, payload, state, max_attempts,"
            " next_run_at, created_at, updated_at) VALUES (?,?, 'ready',"
            " ?,?,?,?)",
            (kind, json.dumps(payload), max_attempts, now + delay, now, now))
        return cur.lastrowid


def claim_next(store, worker_id, now, lease=LEASE_SECONDS):
    """Atomically claim the oldest due job. Returns the job row or None."""
    with store.tx(immediate=True):
        row = store.conn.execute(
            "SELECT * FROM job WHERE state='ready' AND next_run_at<=?"
            " ORDER BY id LIMIT 1", (now,)).fetchone()
        if row is None:
            return None
        store.conn.execute(
            "UPDATE job SET state='running', claimed_by=?,"
            " lease_expires_at=?, heartbeat_at=?, updated_at=? WHERE id=?"
            " AND state='ready'",
            (worker_id, now + lease, now, now, row["id"]))
        if store.conn.execute("SELECT changes()").fetchone()[0] != 1:
            return None  # lost the race; caller may retry
        return store.conn.execute("SELECT * FROM job WHERE id=?",
                                  (row["id"],)).fetchone()


def heartbeat(store, job_id, worker_id, now, lease=LEASE_SECONDS):
    with store.tx():
        store.conn.execute(
            "UPDATE job SET heartbeat_at=?, lease_expires_at=?, updated_at=?"
            " WHERE id=? AND state='running' AND claimed_by=?",
            (now, now + lease, now, job_id, worker_id))


def _finish(store, job_id, state, now, error=None, next_run_at=None):
    with store.tx():
        store.conn.execute(
            "UPDATE job SET state=?, claimed_by=NULL, lease_expires_at=NULL,"
            " heartbeat_at=NULL, last_error=?, next_run_at=?, updated_at=?"
            " WHERE id=?",
            (state, error, next_run_at if next_run_at is not None else now,
             now, job_id))


def complete(store, job_id, now):
    _finish(store, job_id, "done", now)


def snooze(store, job_id, seconds, now, reason=""):
    """Reschedule without consuming an attempt (SPEC §7)."""
    with store.tx():
        store.conn.execute(
            "UPDATE job SET state='snoozed', claimed_by=NULL,"
            " lease_expires_at=NULL, heartbeat_at=NULL, last_error=?,"
            " next_run_at=?, updated_at=? WHERE id=?",
            (reason, now + int(seconds), now, job_id))


def fail(store, job_id, error, now):
    """Retryable failure: attempts+1, backoff, or dead at max_attempts.
    Returns the new state ('ready' for retry-soon is represented by
    'snoozed' with a backoff next_run_at; 'dead' when exhausted)."""
    row = store.conn.execute("SELECT * FROM job WHERE id=?",
                             (job_id,)).fetchone()
    attempts = row["attempts"] + 1
    if attempts >= row["max_attempts"]:
        with store.tx():
            store.conn.execute(
                "UPDATE job SET state='dead', attempts=?, claimed_by=NULL,"
                " lease_expires_at=NULL, heartbeat_at=NULL, last_error=?,"
                " updated_at=? WHERE id=?",
                (attempts, str(error), now, job_id))
        return "dead"
    backoff = BACKOFF_BASE_SECONDS * (4 ** (attempts - 1))
    with store.tx():
        store.conn.execute(
            "UPDATE job SET state='snoozed', attempts=?, claimed_by=NULL,"
            " lease_expires_at=NULL, heartbeat_at=NULL, last_error=?,"
            " next_run_at=?, updated_at=? WHERE id=?",
            (attempts, str(error), now + backoff, now, job_id))
    return "snoozed"


def kill(store, job_id, error, now):
    with store.tx():
        store.conn.execute(
            "UPDATE job SET state='dead', claimed_by=NULL,"
            " lease_expires_at=NULL, heartbeat_at=NULL, last_error=?,"
            " updated_at=? WHERE id=?", (str(error), now, job_id))


def reap_expired(store, now):
    """Return jobs whose lease expired (crashed/stuck worker) to `ready`,
    and wake snoozed jobs whose time has come. Returns jobs recovered."""
    with store.tx(immediate=True):
        cur = store.conn.execute(
            "UPDATE job SET state='ready', claimed_by=NULL,"
            " lease_expires_at=NULL, heartbeat_at=NULL, updated_at=?"
            " WHERE state='running' AND lease_expires_at IS NOT NULL"
            " AND lease_expires_at<?", (now, now))
        recovered = cur.rowcount
        store.conn.execute(
            "UPDATE job SET state='ready', updated_at=? WHERE state='snoozed'"
            " AND next_run_at<=?", (now, now))
        return recovered


def get_job(store, job_id):
    return store.conn.execute("SELECT * FROM job WHERE id=?",
                              (job_id,)).fetchone()


def list_jobs(store, state=None, limit=50):
    if state:
        return store.conn.execute(
            "SELECT * FROM job WHERE state=? ORDER BY id DESC LIMIT ?",
            (state, limit)).fetchall()
    return store.conn.execute(
        "SELECT * FROM job ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def run_once(store, handlers, worker_id, now):
    """Claim one due job and run its handler. Returns True if a job ran.
    Handlers receive (payload: dict, job: row)."""
    reap_expired(store, now)
    job = claim_next(store, worker_id, now)
    if job is None:
        return False
    handler = handlers.get(job["kind"])
    if handler is None:
        kill(store, job["id"], f"no handler for kind {job['kind']}", now)
        return True
    try:
        handler(json.loads(job["payload"]), job)
    except SnoozeError as exc:
        snooze(store, job["id"], exc.seconds, now, str(exc))
    except DeadError as exc:
        kill(store, job["id"], str(exc), now)
    except Exception as exc:  # retryable
        fail(store, job["id"], f"{type(exc).__name__}: {exc}", now)
    else:
        complete(store, job["id"], now)
    return True
