"""Automations engine tests: schedule maths, config merge/persist,
and run recording — against fake collections, with the job executor
stubbed (no Telegram, no real librarian work)."""

import asyncio
import logging
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_module

_MOD = None


def load_mod():
    global _MOD
    if _MOD is not None:
        return _MOD
    backend = sys.modules.get("Backend")
    if backend is None:
        backend = types.ModuleType("Backend")
        sys.modules["Backend"] = backend
    if not hasattr(backend, "LOGGER"):
        backend.LOGGER = logging.getLogger("automations-tests")
    _MOD = load_module("automations_under_test",
                       "Backend/helper/automations.py")
    return _MOD


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _matches(doc, query):
    return all(doc.get(k) == v for k, v in (query or {}).items())


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, field, direction=1):
        self._docs = sorted(self._docs, key=lambda d: d.get(field) or 0,
                            reverse=(direction < 0))
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d
        return gen()


class _FakeColl:
    def __init__(self):
        self.docs = {}

    async def find_one(self, query):
        for d in self.docs.values():
            if _matches(d, query):
                return dict(d)
        return None

    async def update_one(self, query, update, upsert=False):
        key = query.get("_id")
        if key in self.docs:
            self.docs[key].update(update.get("$set") or {})
        elif upsert:
            doc = dict(query)
            doc.update(update.get("$set") or {})
            self.docs[key] = doc

    async def insert_one(self, doc):
        doc = dict(doc)
        doc.setdefault("_id", f"run-{len(self.docs) + 1}")
        self.docs[doc["_id"]] = doc

    def find(self, query):
        return _Cursor([dict(d) for d in self.docs.values()
                        if _matches(d, query)])


def _wire(mod):
    cfg, runs = _FakeColl(), _FakeColl()
    saved = (mod._cfg_coll, mod._runs_coll)
    mod._cfg_coll = lambda: cfg
    mod._runs_coll = lambda: runs
    return cfg, runs, saved


def _unwire(mod, saved):
    mod._cfg_coll, mod._runs_coll = saved


def test_next_run_math():
    mod = load_mod()
    base = {"enabled": True, "schedule": "daily", "anchor": 1000.0,
            "last_run": 0.0}
    assert mod.next_run_ts(base, now=2000.0) == 1000.0 + 86400
    base["last_run"] = 5000.0
    assert mod.next_run_ts(base, now=6000.0) == 5000.0 + 86400
    # Due in the past -> still returned (coalesced run on next tick).
    assert mod.next_run_ts(base, now=10 ** 9) == 5000.0 + 86400
    assert mod.next_run_ts({**base, "enabled": False}, now=0.0) is None
    assert mod.next_run_ts({**base, "schedule": "off"}, now=0.0) is None
    fresh = {"enabled": True, "schedule": "hourly", "anchor": 0.0,
             "last_run": 0.0}
    assert mod.next_run_ts(fresh, now=100.0) == 100.0 + 3600


def test_config_defaults_and_persist():
    mod = load_mod()
    cfg, runs, saved = _wire(mod)
    try:
        c = run(mod.get_config("stats"))
        assert c["enabled"] is True and c["schedule"] == "hourly"
        c = run(mod.get_config("harvest"))
        assert c["enabled"] is False and c["schedule"] == "off"
        out = run(mod.set_config("harvest", enabled=True, schedule="daily"))
        assert out["ok"] is True
        c = run(mod.get_config("harvest"))
        assert c["enabled"] is True and c["schedule"] == "daily"
        assert c["anchor"] > 0  # schedule change re-anchors
        bad = run(mod.set_config("harvest", schedule="fortnightly"))
        assert bad["ok"] is False
        bad = run(mod.set_config("nope", enabled=True))
        assert bad["ok"] is False
        # A corrupt stored schedule is sanitised back to off.
        cfg.docs["audit"] = {"_id": "audit", "enabled": True,
                             "schedule": "whenever"}
        assert run(mod.get_config("audit"))["schedule"] == "off"
    finally:
        _unwire(mod, saved)


def test_run_job_records_history():
    mod = load_mod()
    cfg, runs, saved = _wire(mod)
    orig_execute = mod._execute

    async def fake_execute(d):
        return "ok", "did the thing"

    mod._execute = fake_execute
    try:
        out = run(mod.run_job("harvest", "manual"))
        assert out["ok"] is True and out["status"] == "ok"
        history = run(mod.list_runs("harvest"))
        assert len(history) == 1
        assert history[0]["trigger"] == "manual"
        assert history[0]["summary"] == "did the thing"
        c = run(mod.get_config("harvest"))
        assert c["last_status"] == "ok" and c["last_run"] > 0
        view = run(mod.jobs_view())
        ids = [j["id"] for j in view]
        assert ids == ["harvest", "audit", "stats", "preidentify", "scan_all"]
        assert run(mod.run_job("nope"))["ok"] is False
    finally:
        mod._execute = orig_execute
        _unwire(mod, saved)
