"""Live-buffer regression tests: mid-stream adaptivity and flood shedding.

Covers the three seams added when the buffer went from "learns between
streams" to "learns during a stream":

  1. _SpeedWindow gating  — mid-stream samples are time-gated so the EWMA
     cannot be hijacked by burst noise; the once-per-stream aggregate path
     (observe) is never gated.
  2. note_speed_sample    — feeds the window WITHOUT touching the
     streams_finished counter (that counts streams, not samples).
  3. shedded_workers      — pure helper behind the producer's live worker
     shedding: only penalty accumulated DURING the stream reduces parallelism,
     and it never returns less than 1 worker.
  4. End-to-end: the fetcher samples link speed at the network edge, and a
     penalty that rises mid-stream actually sheds in-flight workers
     (asserted via measured fetch concurrency).

Run: python3.14 tests/test_buffer_live.py
"""
import asyncio
import importlib.util
import logging
import pathlib
import sys
import time
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
MiB = 1024 * 1024
RESULTS = []


def _check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))


def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


#----- Bootstrap (standalone: stubs the app edges the buffer modules touch) --

def _bootstrap():
    if "Backend" not in sys.modules:
        pkg = types.ModuleType("Backend")
        pkg.__path__ = [str(ROOT / "Backend")]
        sys.modules["Backend"] = pkg

    lg = types.ModuleType("Backend.logger")
    lg.LOGGER = logging.getLogger("buffer_live")
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


class _GetFile:
    """Stand-in for raw.functions.upload.GetFile: accepts any kwargs."""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FloodWait(Exception):
    def __init__(self, value, message=""):
        self.value = value
        super().__init__(message or f"wait of {value} seconds")


class FakeFileId:
    dc_id = 2
    local_id = 42
    chat_id = -100123
    media_id = 987654321


_bootstrap()
fw = sys.modules["Backend.helper.flood_wait"]
sb = sys.modules["Backend.helper.stream_buffer"]
custom_dl = sys.modules["Backend.helper.custom_dl"]


class FakeHandler:
    """Scripted flood penalties: what penalty() returns on each call.

    Call 0 is stream_policy()'s workers build, call 1 is the start_penalty
    capture, call 2+ are the producer's live checks. Returns 1, 1, then 3 —
    i.e. the stream starts healthy and a flood lands mid-stream.
    """

    def __init__(self, script=(1, 1, 3, 3, 3, 3, 3, 3, 3, 3)):
        self._script = list(script)
        self._calls = 0
        self.successes = 0

    def penalty(self, _client):
        i = min(self._calls, len(self._script) - 1)
        self._calls += 1
        return self._script[i]

    def record_success(self, _client):
        self.successes += 1

    async def handle(self, error, _client):
        return types.SimpleNamespace(abort=False, wait_seconds=0.0, reason="fake")


class FakeSession:
    """Full chunks forever; slow enough to overlap and expose concurrency."""

    def __init__(self, chunk_size):
        self.chunk_size = chunk_size
        self.active = 0
        self.max_concurrent = 0

    async def send(self, _msg):
        self.active += 1
        self.max_concurrent = max(self.max_concurrent, self.active)
        try:
            await asyncio.sleep(0.01)
            return types.SimpleNamespace(bytes=b"Q" * self.chunk_size)
        finally:
            self.active -= 1


def _make_streamer(chunk_size=1 * MiB):
    s = object.__new__(custom_dl.ByteStreamer)
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


async def _drive(streamer, part_count, chunk_size=1 * MiB):
    sb._policy = None
    sb._cache = None
    sb.SPEED.__init__()
    custom_dl.work_loads[0] = 0
    custom_dl.ACTIVE_STREAMS.clear()

    gen = await streamer.prefetch_stream(
        file_id=FakeFileId(),
        client_index=0,
        offset=0,
        first_part_cut=0,
        last_part_cut=chunk_size * part_count,
        part_count=part_count,
        chunk_size=chunk_size,
        prefetch=12,
        parallelism=3,
        stream_id="LIVETEST",
        chat_id=-100123,
        message_id=42,
    )
    got = 0
    try:
        async for _chunk in gen:
            got += 1
    finally:
        await gen.aclose()
    await asyncio.sleep(0)
    return got


def _run(coro):
    try:
        loop = asyncio.new_event_loop()
    except Exception:  # pragma: no cover
        loop = asyncio.get_event_loop_policy().new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        #----- Reap leftover background tasks (e.g. the 3 s delayed_pop) so the
        #----- loop closes cleanly instead of warning "Task was destroyed".
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True))
        loop.close()


def _reset_window():
    sb.SPEED.__init__()


#----- 1. Speed window gating ----------------------------------------------

def test_gated_first_sample_accepted():
    _reset_window()
    _check("gated window: first sample accepted", sb.SPEED.observe_gated(5.0) is True)


def test_gated_rapid_sample_rejected():
    _reset_window()
    sb.SPEED.observe_gated(5.0)
    _check("gated window: rapid second sample rejected",
           sb.SPEED.observe_gated(50.0) is False)
    _check("gated window: rejected sample not absorbed", sb.SPEED.samples == 1)


def test_gated_sample_accepted_after_interval():
    _reset_window()
    sb.SPEED.observe_gated(5.0)
    sb.SPEED._last_ts -= sb.SPEED.SAMPLE_INTERVAL_S + 0.01
    accepted = sb.SPEED.observe_gated(50.0)
    _check("gated window: sample accepted after interval", accepted is True)
    _check("gated window: interval sample blended into EWMA",
           5.0 < sb.SPEED.mbps < 50.0, f"mbps={sb.SPEED.mbps:.2f}")


def test_ungated_observe_never_gated():
    _reset_window()
    sb.SPEED.observe(5.0)
    sb.SPEED.observe(50.0)
    _check("aggregate path: both rapid samples accepted", sb.SPEED.samples == 2)


def test_nonpositive_and_garbage_rejected():
    _reset_window()
    for bad in (0.0, -3.0, None, "fast"):
        sb.SPEED.observe_gated(bad)
    _check("window: non-positive/garbage samples rejected", sb.SPEED.samples == 0)


#----- 2. note_speed_sample seam --------------------------------------------

def test_note_speed_sample_does_not_count_streams():
    _reset_window()
    before = sb.METRICS.counters["streams_finished"]
    sb.note_speed_sample(7.5)
    _check("note_speed_sample: does not bump streams_finished",
           sb.METRICS.counters["streams_finished"] == before)


def test_record_stream_speed_counts_streams():
    _reset_window()
    before = sb.METRICS.counters["streams_finished"]
    sb.record_stream_speed(7.5)
    _check("record_stream_speed: bumps streams_finished exactly once",
           sb.METRICS.counters["streams_finished"] == before + 1)


#----- 3. shedded_workers (pure) ---------------------------------------------

def test_shed_ignores_start_penalty():
    _check("shed: stream-start penalty already baked in, ignored",
           sb.shedded_workers(3, 2, 2) == 3)


def test_shed_only_extra_penalty():
    _check("shed: only mid-stream penalty reduces workers",
           sb.shedded_workers(3, 1, 3) == 1)


def test_shed_floors_at_one_worker():
    _check("shed: never below one worker", sb.shedded_workers(3, 0, 99) == 1)


def test_shed_garbage_penalties_safe():
    _check("shed: garbage penalties degrade to parallelism",
           sb.shedded_workers(3, "x", None) == 3)


#----- 4. End-to-end: fetcher sampling + live shedding ------------------------

def test_fetcher_samples_link_speed():
    """The chunk fetcher feeds note_speed_sample from the network edge."""
    calls = []
    original = custom_dl.note_speed_sample
    custom_dl.note_speed_sample = lambda mbps: calls.append(mbps)
    try:
        got = _run(_drive(_make_streamer(), part_count=2))
        _check("fetcher: stream delivered its chunks", got == 2, f"got={got}")
        _check("fetcher: note_speed_sample called from the fetch path",
               len(calls) >= 1, f"calls={len(calls)}")
        _check("fetcher: sample is a positive Mbps number",
               all(isinstance(c, float) and c > 0 for c in calls),
               f"sample={calls[:2]}")
    finally:
        custom_dl.note_speed_sample = original


def test_mid_stream_penalty_sheds_workers():
    """A penalty rising mid-stream caps in-flight fetches at one worker."""
    fake = FakeHandler()
    original = custom_dl.flood_handler
    custom_dl.flood_handler = fake
    # stream_policy() consults sb's flood_handler too — same fake so the
    # worker build and the producer's live checks see one consistent story.
    original_sb = sb.flood_handler
    sb.flood_handler = fake
    try:
        streamer = _make_streamer()
        got = _run(_drive(streamer, part_count=6))
        _check("shed e2e: all chunks delivered despite the shed",
               got == 6, f"got={got}")
        _check("shed e2e: in-flight fetches capped at one worker",
               streamer._fake_session.max_concurrent == 1,
               f"max_concurrent={streamer._fake_session.max_concurrent}")
    finally:
        custom_dl.flood_handler = original
        sb.flood_handler = original_sb


def test_no_penalty_keeps_full_parallelism():
    """Without mid-stream penalty growth the stream uses its full workers."""
    fake = FakeHandler(script=[0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
    original = custom_dl.flood_handler
    original_sb = sb.flood_handler
    custom_dl.flood_handler = fake
    sb.flood_handler = fake
    try:
        streamer = _make_streamer()
        got = _run(_drive(streamer, part_count=6))
        _check("no-shed e2e: all chunks delivered", got == 6, f"got={got}")
        _check("no-shed e2e: full parallelism in flight",
               streamer._fake_session.max_concurrent == 3,
               f"max_concurrent={streamer._fake_session.max_concurrent}")
    finally:
        custom_dl.flood_handler = original
        sb.flood_handler = original_sb


#----- 5. Snapshot still coherent ---------------------------------------------

def test_snapshot_has_live_fields():
    _reset_window()
    sb._policy = None
    sb._cache = None
    data = sb.snapshot()
    for key in ("enabled", "effective_readahead_mb", "effective_workers",
                "measured_mbps", "samples", "flood_penalty", "cache", "counters"):
        _check(f"snapshot: has '{key}'", key in data)
    _check("snapshot: counters include workers_shed",
           "workers_shed" in data.get("counters", {}))


#----- Runner -----------------------------------------------------------------

def main():
    for fn in [test_gated_first_sample_accepted,
               test_gated_rapid_sample_rejected,
               test_gated_sample_accepted_after_interval,
               test_ungated_observe_never_gated,
               test_nonpositive_and_garbage_rejected,
               test_note_speed_sample_does_not_count_streams,
               test_record_stream_speed_counts_streams,
               test_shed_ignores_start_penalty,
               test_shed_only_extra_penalty,
               test_shed_floors_at_one_worker,
               test_shed_garbage_penalties_safe,
               test_fetcher_samples_link_speed,
               test_mid_stream_penalty_sheds_workers,
               test_no_penalty_keeps_full_parallelism,
               test_snapshot_has_live_fields]:
        try:
            fn()
        except Exception as e:
            RESULTS.append((fn.__name__, False, f"exception: {e!r}"))

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f" ({detail})" if detail and not ok else ""))
    print(f"{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
