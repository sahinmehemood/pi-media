"""Integration tests that execute the REAL custom_dl.prefetch_stream.

Unlike test_disabled_buffer.py (which mirrors the decision logic), this suite
imports the actual downloader with lightweight stubs and drives
``ByteStreamer.prefetch_stream`` end to end for a single chunk. It exists
because a mutation that removed the disabled-buffer fallback in
custom_dl.py was NOT caught by any mirror-logic test.

Stubs only the network/Telegram edge; the buffer policy, cache wiring,
registry and workload accounting under test are the real implementations.

    python3 tests/test_downloader_integration.py
"""
import asyncio
import importlib.util
import logging
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
MiB = 1024 * 1024
RESULTS = []


class _GetFile:
    """Stand-in for raw.functions.upload.GetFile: accepts any kwargs."""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class FloodWait(Exception):
    def __init__(self, value, message=""):
        self.value = value
        super().__init__(message or f"wait of {value} seconds")


def _bootstrap():
    if "Backend" in sys.modules:
        return
    pkg = types.ModuleType("Backend")
    pkg.__path__ = [str(ROOT / "Backend")]
    sys.modules["Backend"] = pkg

    lg = types.ModuleType("Backend.logger")
    lg.LOGGER = logging.getLogger("dl_integration")
    sys.modules["Backend.logger"] = lg

    hp = types.ModuleType("Backend.helper")
    hp.__path__ = [str(ROOT / "Backend" / "helper")]
    sys.modules["Backend.helper"] = hp
    sys.modules["Backend"].helper = hp

    raw_ns = types.SimpleNamespace(
        functions=types.SimpleNamespace(
            upload=types.SimpleNamespace(GetFile=_GetFile),
            auth=types.SimpleNamespace(ExportAuthorization=type("X", (), {}),
                                       ImportAuthorization=type("Y", (), {})),
        )
    )
    _mod("pyrogram", Client=type("Client", (), {}), raw=raw_ns)
    sys.modules["pyrogram"].__path__ = []
    _mod("pyrogram.errors", AuthBytesInvalid=type("AuthBytesInvalid", (Exception,), {}),
         FloodWait=FloodWait)
    _mod("pyrogram.file_id", FileId=type("FileId", (), {}))
    _mod("pyrogram.session", Auth=type("Auth", (), {}), Session=type("Session", (), {}))
    _mod("fastapi", Request=type("Request", (), {}))
    class _DB:
        @staticmethod
        async def log_stream_stats(_entry):
            return None
    _mod("Backend.db", log_stream_stats=_DB.log_stream_stats)
    _mod("Backend.helper.exceptions", FileNotFound=type("FileNotFound", (Exception,), {}))
    _mod("Backend.helper.pyro", get_file_ids=None)
    _mod("Backend.helper.bounded", BoundedDict=dict)
    _mod("Backend.pyrofork")
    _mod("Backend.pyrofork.bot", client_avg_mbps={}, client_dc_map={},
         client_failures={}, multi_clients={}, work_loads={})

    for name, rel in [("Backend.helper.flood_wait", "Backend/helper/flood_wait.py"),
                      ("Backend.helper.stream_buffer", "Backend/helper/stream_buffer.py"),
                      ("Backend.helper.custom_dl", "Backend/helper/custom_dl.py")]:
        spec = importlib.util.spec_from_file_location(name, ROOT / rel)
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)


_bootstrap()
custom_dl = sys.modules["Backend.helper.custom_dl"]
sb = sys.modules["Backend.helper.stream_buffer"]
ByteStreamer = custom_dl.ByteStreamer
ACTIVE_STREAMS = custom_dl.ACTIVE_STREAMS
work_loads = sys.modules["Backend.pyrofork.bot"].work_loads


class FakeFileId:
    dc_id = 2
    local_id = 42
    chat_id = -100123
    media_id = 987654321   # pyrogram FileId.media_id is an int


class FakeSession:
    """Answers GetFile with one full chunk, then EOF."""

    def __init__(self, chunk_size):
        self.chunk_size = chunk_size
        self.calls = 0

    async def send(self, _msg):
        self.calls += 1
        if self.calls == 1:
            return types.SimpleNamespace(bytes=b"Q" * self.chunk_size)
        return types.SimpleNamespace(bytes=b"")


def _make_streamer(chunk_size=1 * MiB):
    """Build a ByteStreamer without running __init__ (no network tasks)."""
    s = object.__new__(ByteStreamer)
    s.client = types.SimpleNamespace(storage=None, media_sessions={})
    s.client_index = 0
    s._file_id_cache = {}
    s._session_lock = asyncio.Lock()
    s._fake_session = FakeSession(chunk_size)

    async def _get_media_session(_file_id):
        return s._fake_session

    async def _get_location(_file_id):
        return object()

    s._get_media_session = _get_media_session
    s._get_location = _get_location
    return s


async def _drive_one_chunk(streamer, policy_settings, chunk_size=1 * MiB):
    """Run prefetch_stream and consume exactly one chunk, then close."""
    sb._policy = None
    sb._cache = None
    sb.refresh_policy(policy_settings)
    work_loads[0] = 0
    ACTIVE_STREAMS.clear()

    gen = await streamer.prefetch_stream(
        file_id=FakeFileId(),
        client_index=0,
        offset=0,
        first_part_cut=0,
        last_part_cut=chunk_size,
        part_count=1,
        chunk_size=chunk_size,
        prefetch=4,
        parallelism=3,
        stream_id="TESTSTREAM",
        chat_id=-100123,
        message_id=42,
    )

    # The registry entry is visible while the stream is active.
    entry = ACTIVE_STREAMS.get("TESTSTREAM")
    snapshot = None
    if entry is not None:
        snapshot = {
            "prefetch": entry.get("prefetch"),
            "buffer": dict(entry.get("buffer") or {}),
        }

    got = None
    try:
        async for chunk in gen:
            got = chunk
            break
    finally:
        await gen.aclose()

    # Let the finally-block run and the delayed pop settle.
    await asyncio.sleep(0)
    return snapshot, got, dict(work_loads), len(ACTIVE_STREAMS)


def test(name):
    def deco(fn):
        try:
            asyncio.get_event_loop().run_until_complete(fn()) if asyncio.iscoroutinefunction(fn) else fn()
            RESULTS.append((True, name, ""))
        except AssertionError as e:
            RESULTS.append((False, name, str(e)))
        except Exception as e:  # noqa: BLE001
            RESULTS.append((False, name, f"{type(e).__name__}: {e}"))
        return fn
    return deco


@test("disabled buffer: real downloader uses 1 chunk and 1 worker")
def _():
    s = _make_streamer()
    snapshot, got, wl, active = asyncio.get_event_loop().run_until_complete(
        _drive_one_chunk(s, {"buffer_enabled": False, "buffer_profile": "balanced"}))

    assert snapshot is not None, "no registry entry was created"
    assert snapshot["prefetch"] == 1, (
        f"disabled buffer must prefetch 1 chunk, got {snapshot['prefetch']}")
    buf = snapshot["buffer"]
    assert buf["enabled"] is False, f"buffer marked enabled: {buf}"
    assert buf["workers"] == 1, f"disabled buffer must use 1 worker, got {buf['workers']}"
    assert buf["depth_chunks"] == 1, f"depth must be 1, got {buf['depth_chunks']}"


@test("enabled buffer: real downloader applies the profile policy")
def _():
    s = _make_streamer()
    snapshot, got, wl, active = asyncio.get_event_loop().run_until_complete(
        _drive_one_chunk(s, {"buffer_enabled": True, "buffer_profile": "aggressive",
                             "buffer_adaptive": False}))
    assert snapshot is not None, "no registry entry"
    buf = snapshot["buffer"]
    assert buf["enabled"] is True, f"buffer marked disabled: {buf}"
    assert buf["profile"] == "aggressive", buf["profile"]
    # aggressive profile owns these numbers
    assert buf["workers"] == sb.PROFILES["aggressive"]["workers"], buf["workers"]
    assert snapshot["prefetch"] > 1, f"expected read-ahead > 1, got {snapshot['prefetch']}"


@test("real downloader streams bytes and releases workload exactly once")
def _():
    s = _make_streamer()
    snapshot, got, wl, active = asyncio.get_event_loop().run_until_complete(
        _drive_one_chunk(s, {"buffer_enabled": True, "buffer_profile": "balanced",
                             "buffer_adaptive": False}))
    assert got is not None, "generator produced no chunk"
    assert len(got) == 1 * MiB, f"short read: {len(got)}"
    # load_held must be released by the finally block -> back to 0
    assert wl.get(0, 0) == 0, f"work_loads leaked: {wl}"


@test("cached second read of the same chunk is served from RAM")
def _():
    async def scenario():
        chunk_size = 1 * MiB
        s1 = _make_streamer(chunk_size)
        sb._policy = None
        sb._cache = None
        sb.refresh_policy({"buffer_enabled": True, "buffer_profile": "custom",
                           "buffer_cache_mb": 8, "buffer_readahead_mb": 4,
                           "buffer_workers": 2, "buffer_adaptive": False})
        work_loads[0] = 0
        ACTIVE_STREAMS.clear()

        gen = await s1.prefetch_stream(
            file_id=FakeFileId(), client_index=0, offset=0, first_part_cut=0,
            last_part_cut=chunk_size, part_count=1, chunk_size=chunk_size,
            prefetch=2, parallelism=2, stream_id="CACHEME",
            chat_id=-100123, message_id=42)
        first = None
        try:
            async for c in gen:
                first = c
                break
        finally:
            await gen.aclose()
        assert first is not None and len(first) == chunk_size

        cache = sb.get_shared_cache()
        assert cache is not None, "cache not created for enabled buffer"
        # A second stream for the same media/chunk must hit the cache and make
        # no network call at all.
        s2 = _make_streamer(chunk_size)
        calls_before = s2._fake_session.calls
        gen2 = await s2.prefetch_stream(
            file_id=FakeFileId(), client_index=0, offset=0, first_part_cut=0,
            last_part_cut=chunk_size, part_count=1, chunk_size=chunk_size,
            prefetch=2, parallelism=2, stream_id="CACHEHIT",
            chat_id=-100123, message_id=42)
        second = None
        try:
            async for c in gen2:
                second = c
                break
        finally:
            await gen2.aclose()
        assert second == first, "cache returned different bytes"
        assert s2._fake_session.calls == calls_before, (
            "second stream hit Telegram instead of the cache")

    asyncio.get_event_loop().run_until_complete(scenario())


def main():
    passed = sum(1 for ok, _n, _e in RESULTS if ok)
    total = len(RESULTS)
    for ok, name, err in RESULTS:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            print(f"       -> {err}")
    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1
