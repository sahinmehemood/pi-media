"""Production-shaped test harness for Librarian v2 (SPEC §13).

The v1 suite stayed green through three production-only bugs because its
fakes were more generous than production: identical collection objects per
call, stub modules carrying names the real modules do not, accessors that
exist only in the stub. This harness exists so v2 tests cannot make those
mistakes:

- Mongo-shaped handles are FRESH objects on every call (shared data,
  distinct identity) — code that relies on `is` fails here, as it should.
- SQLite is the real schema.sql on a real temp file with WAL on.
- Telegram is a recording fake with scriptable failures (flood waits,
  identity drift) instead of a happy-path stub.
- Providers are scripted stubs with an outage mode and a call log.

Stdlib only, like the rest of the suite.
"""

import sqlite3
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_SQL = REPO_ROOT / "Backend" / "librarian_v2" / "schema.sql"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# --------------------------------------------------------------------------
# SQLite store fixture — real schema, real file, WAL on.
# --------------------------------------------------------------------------

class StoreFixture:
    """A temp-file SQLite database with the production schema applied."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="libv2-")
        self.path = str(Path(self._tmp.name) / "librarian_v2.sqlite3")
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
        self.conn.commit()

    def journal_mode(self) -> str:
        return self.conn.execute("PRAGMA journal_mode").fetchone()[0]

    def close(self):
        self.conn.close()
        self._tmp.cleanup()


# --------------------------------------------------------------------------
# Mongo-shaped fake — fresh handle per call, shared underlying data.
# --------------------------------------------------------------------------

class FakeCollection:
    """Minimal motor-shaped collection over a shared list of dicts.

    Every instance is a distinct object; two handles to the same
    collection are never `is`-identical, exactly like production.
    """

    def __init__(self, data):
        self._data = data

    def insert_one(self, doc):
        self._data.append(dict(doc))
        return doc

    def find_one(self, filt):
        for doc in self._data:
            if all(doc.get(k) == v for k, v in filt.items()):
                return doc
        return None

    def find(self, filt=None):
        filt = filt or {}
        return [d for d in self._data
                if all(d.get(k) == v for k, v in filt.items())]

    def count(self, filt=None):
        return len(self.find(filt))


class FakeMongo:
    """`collection(name)` returns a NEW FakeCollection every call."""

    def __init__(self):
        self._stores = {}

    def collection(self, name):
        return FakeCollection(self._stores.setdefault(name, []))


# --------------------------------------------------------------------------
# Telegram fake — records edits; scriptable failures and identity drift.
# --------------------------------------------------------------------------

class FloodWait(Exception):
    def __init__(self, retry_after):
        super().__init__(f"FLOOD_WAIT {retry_after}")
        self.retry_after = retry_after


class FakeTelegram:
    def __init__(self):
        self.captions = {}          # (chat_id, msg_id) -> current caption
        self.edits = []             # [(chat_id, msg_id, new_caption)]
        self._failures = []         # exceptions to raise, in order

    def seed(self, chat_id, msg_id, caption):
        self.captions[(chat_id, msg_id)] = caption

    def get_caption(self, chat_id, msg_id):
        return self.captions.get((chat_id, msg_id))

    def drift(self, chat_id, msg_id, new_caption):
        """Simulate someone else editing the caption behind our back."""
        self.captions[(chat_id, msg_id)] = new_caption

    def fail_next(self, exc):
        self._failures.append(exc)

    def flood_next(self, retry_after):
        self._failures.append(FloodWait(retry_after))

    def edit_caption(self, chat_id, msg_id, new_caption):
        if self._failures:
            raise self._failures.pop(0)
        self.captions[(chat_id, msg_id)] = new_caption
        self.edits.append((chat_id, msg_id, new_caption))
        return True


# --------------------------------------------------------------------------
# Provider stub — scripted results, outage mode, call log.
# --------------------------------------------------------------------------

class ProviderDown(Exception):
    pass


class ProviderStub:
    def __init__(self):
        self._results = {}          # (provider, query) -> list of dicts
        self._outages = set()       # provider names currently down
        self.calls = []             # [(provider, query)]

    def add_result(self, provider, query, results):
        self._results[(provider, query)] = results

    def set_outage(self, provider, down=True):
        if down:
            self._outages.add(provider)
        else:
            self._outages.discard(provider)

    def search(self, provider, query):
        self.calls.append((provider, query))
        if provider in self._outages:
            raise ProviderDown(provider)
        return list(self._results.get((provider, query), []))


def now() -> int:
    return int(time.time())
