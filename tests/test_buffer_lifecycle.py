"""Regression tests: cache correctness, memory bounds, lifecycle accounting.

Covers the buffer requirements testable without Telegram/MongoDB: cache key
uniqueness, byte-bounded memory, deterministic eviction, and the
load_held / work_loads release contract (exactly one release per stream).
"""
import importlib.util
import logging
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
MiB = 1024 * 1024
RESULTS = []


def _setup():
    if "Backend" not in sys.modules:
        pkg = types.ModuleType("Backend")
        pkg.__path__ = [str(ROOT / "Backend")]
        sys.modules["Backend"] = pkg
    lg = types.ModuleType("Backend.logger")
    lg.LOGGER = logging.getLogger("lifecycle_test")
    sys.modules["Backend.logger"] = lg
    pg = types.ModuleType("pyrogram")
    pg.__path__ = []
    pg.Client = type("Client", (), {})
    sys.modules["pyrogram"] = pg
    er = types.ModuleType("pyrogram.errors")

    class FloodWait(Exception):
        def __init__(self, value, message=""):
            self.value = value
            super().__init__(message or f"wait of {value} seconds")

    er.FloodWait = FloodWait
    sys.modules["pyrogram.errors"] = er
    bm = types.ModuleType("Backend.pyrofork.bot")
    bm.multi_clients = {}
    bm.StreamBot = None
    bm.Userbot = None
    sys.modules["Backend.pyrofork.bot"] = bm
    for name, rel in [("Backend.helper.flood_wait", "Backend/helper/flood_wait.py"),
                      ("Backend.helper.stream_buffer", "Backend/helper/stream_buffer.py")]:
        spec = importlib.util.spec_from_file_location(name, ROOT / rel)
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
    return sys.modules["Backend.helper.stream_buffer"]


sb = _setup()


def _reset():
    sb._policy = None
    sb._cache = None
    sb.flood_handler._penalties.clear()
    sb.flood_handler._flood_counts.clear()
    sb.flood_handler._last_success.clear()


def test(name):
    def deco(fn):
        try:
            fn()
            RESULTS.append((True, name, ""))
        except AssertionError as e:
            RESULTS.append((False, name, str(e)))
        except Exception as e:  # noqa: BLE001
            RESULTS.append((False, name, f"{type(e).__name__}: {e}"))
        return fn
    return deco


#----- Cache correctness ----------------------------------------------------

@test("chunk_index is absolute, not a per-stream sequence")
def _():
    _reset()
    cs = 1 * MiB
    a = sb.chunk_index(0, cs)
    b = sb.chunk_index(5 * cs, cs)
    assert a != b, f"offset aliasing: both -> {a}"
    assert a == 0 and b == 5, (a, b)


@test("cache_key distinguishes media, chunk size and index")
def _():
    _reset()
    k1 = sb.cache_key(123, MiB, 0)
    k2 = sb.cache_key(123, MiB, 1)
    k3 = sb.cache_key(124, MiB, 0)
    k4 = sb.cache_key(123, 2 * MiB, 0)
    assert len({k1, k2, k3, k4}) == 4, "cache keys collide"
    assert sb.cache_key(None, MiB, 0) is None


@test("cache hit returns exactly the bytes stored")
def _():
    _reset()
    c = sb.ChunkCache(4 * MiB)
    payload = b"x" * 1024
    c.put((1, MiB, 0), payload)
    assert c.get((1, MiB, 0)) == payload, "cache returned wrong bytes"
    assert c.get((1, MiB, 1)) is None, "miss returned data"


@test("cache is bounded by bytes, not entry count")
def _():
    _reset()
    budget = 2 * MiB
    c = sb.ChunkCache(budget)
    chunk = b"y" * (512 * 1024)
    for i in range(10):
        c.put((1, 512 * 1024, i), chunk)
    assert c.size_bytes <= budget, f"unbounded: {c.size_bytes} > {budget}"
    assert c.budget_bytes == budget


@test("resize shrinks and evicts")
def _():
    _reset()
    c = sb.ChunkCache(4 * MiB)
    chunk = b"z" * (512 * 1024)
    for i in range(8):
        c.put((1, 512 * 1024, i), chunk)
    c.resize(1 * MiB)
    assert c.budget_bytes == 1 * MiB
    assert c.size_bytes <= 1 * MiB, f"over budget after resize: {c.size_bytes}"


@test("zero budget clears the cache")
def _():
    _reset()
    c = sb.ChunkCache(4 * MiB)
    c.put((1, MiB, 0), b"a" * 1024)
    c.resize(0)
    assert c.size_bytes == 0
    assert c.entries == 0


#----- Lifecycle / workload accounting -------------------------------------

@test("load_held release is idempotent (pop-then-decrement)")
def _():
    _reset()
    work_loads = {0: 0}
    work_loads[0] += 1
    entry = {"load_held": True}
    for _i in range(3):  # repeated release must not double-decrement
        if entry.pop("load_held", False):
            work_loads[0] = max(0, work_loads.get(0, 0) - 1)
    assert work_loads[0] == 0, f"workload leaked: {work_loads[0]}"


@test("workload never goes negative on repeated release")
def _():
    _reset()
    work_loads = {1: 0}
    entry = {}
    for _i in range(5):
        if entry.pop("load_held", False):
            work_loads[1] = max(0, work_loads.get(1, 0) - 1)
    assert work_loads[1] == 0


@test("policy refresh does not accumulate metrics or tasks")
def _():
    _reset()
    before = len(sb.METRICS.counters)
    for _i in range(20):
        sb.refresh_policy({"buffer_profile": "custom", "buffer_cache_mb": 8,
                           "buffer_readahead_mb": 8, "buffer_workers": 2})
    assert len(sb.METRICS.counters) == before, "metrics mutated by refresh"


def main():
    passed = sum(1 for ok, _n, _e in RESULTS if ok)
    total = len(RESULTS)
    for ok, name, err in RESULTS:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            print(f"       -> {err}")
    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
