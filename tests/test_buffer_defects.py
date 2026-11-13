"""Regression tests for the four defects found in the buffer integration.

Each test pins the BEHAVIOUR that was broken, so a future edit that
reintroduces the defect goes red. None of these assert on implementation
details (no monkey-patching of internals, no line numbers).

  F1  record_stream_speed was never called -> adaptive depth was inert
  F2  the chunk fetcher bypassed the central FloodWait handler
      (no max_wait cap, no worker shedding)
  F3  depth_chunks had no upper bound -> thousands of queue slots
  F4  the settings API silently coerced 3.9 -> 3 and True -> 1

    python3 tests/test_buffer_defects.py
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
    lg.LOGGER = logging.getLogger("defects")
    sys.modules["Backend.logger"] = lg

    hp = types.ModuleType("Backend.helper")
    hp.__path__ = [str(ROOT / "Backend" / "helper")]
    sys.modules["Backend.helper"] = hp
    sys.modules["Backend"].helper = hp

    class _GetFile:
        def __init__(self, **kw):
            self.__dict__.update(kw)

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
sb = sys.modules["Backend.helper.stream_buffer"]
fw = sys.modules["Backend.helper.flood_wait"]


def _reset():
    sb._policy = None
    sb._cache = None
    sb.SPEED.__init__()
    fw.flood_handler._penalties.clear()
    fw.flood_handler._flood_counts.clear()
    fw.flood_handler._last_success.clear()



def _run(coro):
    """Run a coroutine on a fresh loop.

    asyncio.get_event_loop() raises on Python 3.12+ when no loop is running in
    the main thread (and is deprecated outright in 3.14), which is why this is
    not simply asyncio.run: tests may be invoked from an environment that has
    already closed its loop.
    """
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

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


# ===================== F1: adaptive depth is live =====================

@test("F1: recorded speed moves the adaptive multiplier")
def _():
    _reset()
    sb.refresh_policy({"buffer_profile": "balanced", "buffer_adaptive": True})
    before = sb.SPEED.samples
    sb.record_stream_speed(0.5)      # 0.5 MB/s = slow
    sb.record_stream_speed(0.4)
    sb.record_stream_speed(0.6)
    assert sb.SPEED.samples > before, "record_stream_speed did not record samples"
    assert sb.SPEED.multiplier() > 1.0, (
        f"slow link should deepen read-ahead, multiplier={sb.SPEED.multiplier()}")


@test("F1: a fast link relaxes read-ahead below the configured depth")
def _():
    _reset()
    sb.refresh_policy({"buffer_profile": "balanced", "buffer_adaptive": True})
    for _i in range(10):
        sb.record_stream_speed(60.0)     # 60 MB/s = fast
    assert sb.SPEED.multiplier() < 1.0, (
        f"fast link should relax read-ahead, multiplier={sb.SPEED.multiplier()}")


@test("F1: non-positive samples are ignored, not fed into the EWMA")
def _():
    _reset()
    sb.SPEED.__init__()
    sb.record_stream_speed(0)
    sb.record_stream_speed(-5)
    sb.record_stream_speed(None)
    assert sb.SPEED.samples == 0, "invalid samples must not be recorded"


@test("F1: adaptive off keeps the configured depth regardless of samples")
def _():
    _reset()
    sb.refresh_policy({"buffer_profile": "balanced", "buffer_adaptive": False,
                       "buffer_readahead_mb": 12, "buffer_workers": 3})
    for _i in range(10):
        sb.record_stream_speed(0.1)
    p = sb.stream_policy(client_index=0)
    assert p.readahead_bytes == 12 * MiB, (
        f"adaptive disabled but depth changed: {p.readahead_bytes/MiB} MiB")


# ===================== F2: flood handling =====================

@test("F2: a long flood aborts instead of blocking for hours")
def _():
    _reset()
    handler = fw.FloodWaitHandler()
    handler.max_wait = 300.0
    decision = _run(
        handler.handle(FloodWait(72000), 0))          # 20 hours
    assert decision.abort is True, "a 20h flood must abort, not block"
    assert decision.wait_seconds == 0.0, "an aborted flood must not wait"


@test("F2: a short flood waits and is retried")
def _():
    _reset()
    handler = fw.FloodWaitHandler()
    handler.max_wait = 300.0
    decision = _run(
        handler.handle(FloodWait(5), 0))
    assert decision.abort is False, "a 5s flood should be retried"
    assert decision.wait_seconds >= 5.0, f"should wait at least 5s, got {decision.wait_seconds}"


@test("F2: exceeding max_retries aborts")
def _():
    _reset()
    handler = fw.FloodWaitHandler(max_retries=2, max_wait=300.0)
    for i in range(4):
        d = _run(handler.handle(FloodWait(5), 0))
    assert d.abort is True, "repeated floods must eventually abort"


@test("F2: a non-flood error is NOT treated as a flood")
def _():
    _reset()
    # This is the gate the chunk fetcher relies on: _parse_wait must return
    # None for unrelated errors so they go to backoff, not the flood handler.
    class OtherError(Exception):
        pass
    assert fw.FloodWaitHandler._parse_wait(OtherError("FILE_REFERENCE expired")) is None
    assert fw.FloodWaitHandler._parse_wait(OtherError("connection reset")) is None
    assert fw.FloodWaitHandler._parse_wait(FloodWait(30)) == 30.0


# ===================== F3: bounded prefetch =====================

@test("F3: depth_chunks is capped no matter how small the chunk")
def _():
    _reset()
    sb.refresh_policy({"buffer_profile": "aggressive", "buffer_adaptive": True,
                       "buffer_readahead_mb": 24, "buffer_workers": 5})
    for _i in range(50):
        sb.record_stream_speed(0.1)         # slow -> multiplier 2.0
    p = sb.stream_policy(client_index=0)
    for tiny in (64 * 1024, 16 * 1024, 1024):
        d = p.depth_chunks(tiny)
        assert 1 <= d <= sb.MAX_PREFETCH_CHUNKS, (
            f"chunk_size={tiny} produced depth {d} outside [1, {sb.MAX_PREFETCH_CHUNKS}]")


@test("F3: the cap does not change depth for a real 1 MiB chunk")
def _():
    _reset()
    sb.refresh_policy({"buffer_profile": "balanced", "buffer_adaptive": False,
                       "buffer_readahead_mb": 12, "buffer_workers": 3})
    p = sb.stream_policy(client_index=0)
    assert p.depth_chunks(1 * MiB) == 12, (
        f"production chunk size must keep its configured depth, got {p.depth_chunks(1*MiB)}")


@test("F3: a non-positive chunk size degrades to a single slot")
def _():
    _reset()
    p = sb.build_policy({"buffer_profile": "balanced"})
    assert p.depth_chunks(0) == 1
    assert p.depth_chunks(-5) == 1


# ===================== F4: settings validation =====================

class _HTTPException(Exception):
    def __init__(self, status_code=400, detail=""):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _load_coercer():
    """Extract _coerce_bounded_int from api_routes.py without importing the app.

    api_routes pulls in FastAPI, motor, the whole route graph and the DB, none
    of which exist in this harness. The function under test is pure, so we lift
    its source with ast and exec it against a stub HTTPException. This keeps the
    test honest (real code, real behaviour) instead of skipping.
    """
    import ast as _ast
    path = ROOT / "Backend/fastapi/routes/api_routes.py"
    tree = _ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, _ast.FunctionDef) and node.name == "_coerce_bounded_int":
            src = _ast.get_source_segment(path.read_text(), node)
            ns = {"HTTPException": _HTTPException}
            exec(src, ns)                       # noqa: S102 - test-only, pure fn
            return ns["_coerce_bounded_int"]
    return None


@test("F4: the settings API coerces valid integer forms")
def _():
    f = _load_coercer()
    assert f is not None, "_coerce_bounded_int not found in api_routes.py"
    # workers range is 1..8, so 6 is in-range; 99 must clamp.
    assert f(6, 1, 8, "k") == 6
    assert f(6.0, 1, 8, "k") == 6
    assert f("6", 1, 8, "k") == 6
    assert f(" 7 ", 1, 8, "k") == 7
    assert f(48, 0, 512, "k") == 48
    assert f(99, 1, 8, "k") == 8, "out-of-range should clamp"
    assert f(-5, 1, 8, "k") == 1, "below-range should clamp"


@test("F4: bools and fractional numbers are rejected, not silently coerced")
def _():
    f = _load_coercer()
    assert f is not None, "_coerce_bounded_int not found in api_routes.py"
    # bool is a subclass of int, so True must be rejected explicitly.
    for bad in (True, False, 3.9, 12.5, "abc", None, [], {}, ""):
        try:
            got = f(bad, 1, 8, "k")
        except _HTTPException:
            continue
        except Exception as e:
            raise AssertionError(f"{bad!r} raised {type(e).__name__}, expected HTTPException")
        raise AssertionError(f"{bad!r} was silently accepted as {got!r}")


def main():
    passed = sum(1 for ok, _n, _e in RESULTS if ok)
    total = len(RESULTS)
    for ok, name, err in RESULTS:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            print(f"       -> {err}")
    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1


# ===================== F1: the CALL SITE is wired =====================

def _ensure_custom_dl():
    """Load the real downloader module (stubbing only its external deps)."""
    _bootstrap()
    if "Backend.helper.custom_dl" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "Backend.helper.custom_dl", ROOT / "Backend/helper/custom_dl.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
# The tests above prove record_stream_speed WORKS. They cannot prove the
# downloader CALLS it — which is exactly how the original defect shipped.
# This test drives the real prefetch_stream and asserts SPEED actually moved.

class _FakeFileId:
    dc_id = 2
    local_id = 42
    chat_id = -100123
    media_id = 987654321


class _FakeSession:
    def __init__(self, chunk_size):
        self.chunk_size = chunk_size
        self.calls = 0

    async def send(self, _msg):
        self.calls += 1
        if self.calls == 1:
            return types.SimpleNamespace(bytes=b"Q" * self.chunk_size)
        return types.SimpleNamespace(bytes=b"")


def _make_streamer(chunk_size=1 * MiB):
    custom_dl = sys.modules["Backend.helper.custom_dl"]
    s = object.__new__(custom_dl.ByteStreamer)
    s.client = types.SimpleNamespace(storage=None, media_sessions={})
    s.client_index = 0
    s._file_id_cache = {}
    s._session_lock = asyncio.Lock()
    s._fake_session = _FakeSession(chunk_size)

    async def _get_media_session(_fid):
        return s._fake_session

    async def _get_location(_fid):
        return object()

    s._get_media_session = _get_media_session
    s._get_location = _get_location
    return s


@test("F1: the downloader actually records stream speed (call site wired)")
def _():
    _ensure_custom_dl()
    custom_dl = sys.modules["Backend.helper.custom_dl"]
    _reset()
    sb.SPEED.__init__()

    async def scenario():
        streamer = _make_streamer()
        sb._policy = None
        sb._cache = None
        sb.refresh_policy({"buffer_enabled": True, "buffer_profile": "balanced",
                           "buffer_adaptive": False})
        custom_dl.work_loads[0] = 0
        custom_dl.ACTIVE_STREAMS.clear()

        gen = await streamer.prefetch_stream(
            file_id=_FakeFileId(), client_index=0, offset=0,
            first_part_cut=0, last_part_cut=1 * MiB, part_count=1,
            chunk_size=1 * MiB, prefetch=2, parallelism=2,
            stream_id="SPEEDPROBE", chat_id=-100123, message_id=42)
        try:
            async for _chunk in gen:
                break
        finally:
            await gen.aclose()
        # let the finally block (and its record_stream_speed) run
        await asyncio.sleep(0.05)

    _run(scenario())

    assert sb.SPEED.samples > 0, (
        "prefetch_stream finished a stream but never called record_stream_speed "
        "— adaptive depth is inert again (this is the F1 defect)")


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
