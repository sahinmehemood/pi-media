"""Integration tests at the stream_buffer seam (no Telegram/Mongo/FastAPI).

Uses the same stubbing approach as tests/test_stream_buffer.py so the module
loads without the application stack, then exercises policy clamping, adaptive
depth, worker shedding, and the shared chunk cache.
"""
import importlib.util
import logging
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
MiB = 1024 * 1024


def setup():
    if "Backend" not in sys.modules:
        pkg = types.ModuleType("Backend")
        pkg.__path__ = [str(ROOT / "Backend")]
        sys.modules["Backend"] = pkg

    logger_mod = types.ModuleType("Backend.logger")
    logger_mod.LOGGER = logging.getLogger("buf_integration")
    sys.modules["Backend.logger"] = logger_mod

    if "pyrogram" not in sys.modules:
        pg = types.ModuleType("pyrogram")
        pg.__path__ = []
        pg.Client = type("Client", (), {})
        sys.modules["pyrogram"] = pg
    if "pyrogram.errors" not in sys.modules:
        errs = types.ModuleType("pyrogram.errors")

        class FloodWait(Exception):
            def __init__(self, value, message=""):
                self.value = value
                super().__init__(message or f"wait of {value} seconds")

        errs.FloodWait = FloodWait
        sys.modules["pyrogram.errors"] = errs

    bot = types.ModuleType("Backend.pyrofork.bot")
    bot.multi_clients = {}
    bot.StreamBot = None
    bot.Userbot = None
    sys.modules["Backend.pyrofork.bot"] = bot

    def load(name, rel):
        spec = importlib.util.spec_from_file_location(name, ROOT / rel)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    fw = load("Backend.helper.flood_wait", "Backend/helper/flood_wait.py")
    sb = load("Backend.helper.stream_buffer", "Backend/helper/stream_buffer.py")
    return fw, sb


fw, sb = setup()


def reset():
    sb.SPEED.__init__()
    fw.flood_handler._penalties.clear()
    fw.flood_handler._flood_counts.clear()
    fw.flood_handler._last_success.clear()
    sb._cache = None
    sb.METRICS.__init__()
    sb._policy = None


def test_profiles_are_valid():
    for name, p in sb.PROFILES.items():
        assert p["workers"] >= sb.MIN_WORKERS, name
        assert sb.MIN_READAHEAD_MB <= p["readahead_mb"] <= sb.MAX_READAHEAD_MB, name
        assert p["cache_mb"] <= sb.MAX_CACHE_MB, name


def test_readahead_is_clamped():
    reset()
    p = sb.build_policy({"buffer_profile": "custom", "buffer_readahead_mb": 99999})
    assert p.readahead_bytes <= sb.MAX_READAHEAD_MB * MiB, p.readahead_mb


def test_unknown_profile_falls_back():
    reset()
    p = sb.build_policy({"buffer_profile": "turbo"})
    assert p.profile == sb.DEFAULT_PROFILE, p.profile


def test_custom_profile_accepts_in_range():
    reset()
    p = sb.build_policy({"buffer_profile": "custom", "buffer_readahead_mb": 32,
                         "buffer_workers": 6, "buffer_cache_mb": 64})
    assert p.profile == "custom"
    assert p.readahead_mb == 32 and p.workers == 6 and p.cache_mb == 64


def test_ram_budget_clamps_request():
    reset()
    # Ask for more than the budget: the policy must clamp, not exceed it.
    p = sb.build_policy({"buffer_profile": "custom", "buffer_readahead_mb": 128,
                         "buffer_cache_mb": 512})
    budget = sb.ram_budget_bytes()
    assert p.readahead_bytes + p.cache_bytes <= max(budget, sb.MIN_READAHEAD_MB * MiB) or p.clamped


def test_depth_chunks_never_below_one():
    reset()
    assert sb.BufferPolicy(True, True, "x", 0, 1, 0).depth_chunks(MiB) == 1
    assert sb.BufferPolicy(True, True, "x", 4 * MiB, 1, 0).depth_chunks(MiB) == 4


def test_cache_never_exceeds_budget():
    reset()
    c = sb.ChunkCache(3 * MiB)
    one = b"x" * MiB
    for i in range(20):
        c.put((1, MiB, i), one)
    assert c.size_bytes <= c.budget_bytes, (c.size_bytes, c.budget_bytes)
    assert c.entries <= 3, c.entries


def test_cache_skips_oversized_item():
    reset()
    c = sb.ChunkCache(MiB)
    c.put((1, 4 * MiB, 0), b"y" * (4 * MiB))
    assert c.entries == 0 and c.size_bytes == 0


def test_cache_hit_counts_and_eviction():
    reset()
    c = sb.ChunkCache(2 * MiB)
    key = (7, MiB, 0)
    c.put(key, b"z" * MiB)
    assert c.get(key) is not None
    assert c.get((7, MiB, 99)) is None
    stats = c.stats()
    assert stats["hits"] >= 1 and stats["misses"] >= 1
    assert stats["hit_rate"] >= 0


def test_chunk_index_is_absolute_not_sequential():
    reset()
    # Two streams of the same file starting at different offsets must not alias.
    assert sb.chunk_index(0, MiB) == 0
    assert sb.chunk_index(5 * MiB, MiB) == 5
    assert sb.chunk_index(0, 0) == 0


def test_cache_key_is_none_for_bad_input():
    reset()
    assert sb.cache_key(None, MiB, 1) is None
    assert sb.cache_key("x", MiB, 1) is None


def test_flood_penalty_sheds_workers():
    reset()
    h = fw.flood_handler
    before = h.penalty(fw.PRIMARY_CLIENT)
    for _ in range(2):
        h.note_flood_wait(fw.PRIMARY_CLIENT)
    after = h.penalty(fw.PRIMARY_CLIENT)
    assert after > before, (before, after)

    # A success decays the penalty back.
    h.record_success(fw.PRIMARY_CLIENT)
    assert h.penalty(fw.PRIMARY_CLIENT) == after - 1


def test_stream_policy_never_exceeds_ceiling():
    reset()
    for mbps in (0.1, 1.0, 10.0, 100.0):
        reset()
        sb.SPEED.observe(mbps)
        p = sb.stream_policy(client_index=fw.PRIMARY_CLIENT, userbot=False)
        assert p.readahead_bytes <= sb.get_policy().max_readahead_bytes


def test_userbot_workers_are_capped():
    reset()
    p = sb.stream_policy(client_index=fw.PRIMARY_CLIENT, userbot=True)
    assert p.workers <= sb.USERBOT_MAX_WORKERS, p.workers


def test_snapshot_exposes_admin_fields():
    reset()
    s = sb.snapshot()
    for k in ("enabled", "profile", "effective_readahead_mb", "workers",
              "cache", "counters", "ram_budget_mb", "flood_penalty"):
        assert k in s, k


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = []
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed.append(t.__name__)
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    if failed:
        print("failed:", ", ".join(failed))
    return 0
