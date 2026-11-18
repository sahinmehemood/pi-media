"""Test harness helpers.

Loads the plugin modules by file path (bypassing the ``Backend`` package
``__init__``, which needs the full app stack) and installs lightweight
stubs for the streamer's upstream dependencies so the real
``BufferedByteStreamer`` code runs end-to-end against fake Telegram
sessions. Standard library only.
"""

import asyncio
import importlib.util
import logging
import sys
import types
from collections import defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relpath: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_stream_buffer():
    return load_module("stream_buffer_under_test", "Backend/helper/stream_buffer.py")


class FakeGetFile:
    def __init__(self, location=None, offset=0, limit=0):
        self.location = location
        self.offset = offset
        self.limit = limit


class FakeSession:
    """A media session serving slices of ``data`` per a behaviour script.

    behaviour keys: flood_always (bool), flood_once (bool), fail_always
    (bool). ``calls`` counts GetFile sends.
    """

    def __init__(self, data: bytes, behaviour=None):
        self.data = data
        self.behaviour = behaviour or {}
        self.calls = 0
        self.in_flight = 0
        self.max_concurrent = 0

    async def send(self, request):
        self.calls += 1
        self.in_flight += 1
        self.max_concurrent = max(self.max_concurrent, self.in_flight)
        try:
            return await self._send(request)
        finally:
            self.in_flight -= 1

    async def _send(self, request):
        b = self.behaviour
        if b.get("delay"):
            await asyncio.sleep(b["delay"])
        if b.get("fail_always"):
            raise RuntimeError("boom: connection reset")
        if b.get("flood_always"):
            raise RuntimeError("FLOOD_WAIT_999999: wait of 999999 seconds required")
        if b.get("flood_once"):
            b["flood_once"] = False
            raise RuntimeError("FLOOD_WAIT_0: wait of 0 seconds required")
        off, limit = request.offset, request.limit
        if off >= len(self.data):
            return types.SimpleNamespace(bytes=b"")
        return types.SimpleNamespace(bytes=self.data[off:off + limit])


class FakeClient:
    def __init__(self, data: bytes, behaviour=None):
        self.session = FakeSession(data, behaviour)
        self.media_sessions = {}
        self.known_file_id = None


class FakeFileId:
    def __init__(self, media_id=12345):
        self.dc_id = 2
        self.media_id = media_id
        self.local_id = 42
        self.chat_id = -100123


class FakeDB:
    def __init__(self):
        self.logged = []

    async def log_stream_stats(self, entry):
        self.logged.append(dict(entry))


def install_streamer_stubs(sb):
    """Register stub modules so buffered_streamer imports cleanly."""
    # --- pyrogram / fastapi
    pyrogram = types.ModuleType("pyrogram")
    raw = types.ModuleType("pyrogram.raw")
    functions = types.ModuleType("pyrogram.raw.functions")
    upload = types.ModuleType("pyrogram.raw.functions.upload")
    upload.GetFile = FakeGetFile
    functions.upload = upload
    raw.functions = functions
    pyrogram.raw = raw
    file_id_mod = types.ModuleType("pyrogram.file_id")
    file_id_mod.FileId = FakeFileId
    fastapi = types.ModuleType("fastapi")
    fastapi.Request = object
    sys.modules.update({
        "pyrogram": pyrogram,
        "pyrogram.raw": raw,
        "pyrogram.file_id": file_id_mod,
        "fastapi": fastapi,
    })

    # --- Backend package pieces
    backend = types.ModuleType("Backend")
    fake_db = FakeDB()
    backend.db = fake_db

    helper_pkg = types.ModuleType("Backend.helper")
    helper_pkg.stream_buffer = sb

    custom_dl = types.ModuleType("Backend.helper.custom_dl")
    custom_dl.ACTIVE_STREAMS = {}
    custom_dl.RECENT_STREAMS = deque(maxlen=20)

    class FakeByteStreamer:
        """Upstream stand-in: same helpers the adapter relies on."""

        def __init__(self, client, client_index=-1):
            self.client = client
            self.client_index = client_index
            self._file_id_cache = {}
            self.upstream_calls = []

        async def _get_media_session(self, file_id):
            return self.client.session

        @staticmethod
        async def _get_location(file_id):
            return ("loc", file_id.media_id)

        async def prefetch_stream(self, *args, **kwargs):
            self.upstream_calls.append((args, kwargs))

            async def gen():
                yield b"upstream"

            return gen()

    custom_dl.ByteStreamer = FakeByteStreamer

    pyro = types.ModuleType("Backend.helper.pyro")

    async def get_file_ids(client, chat_id, message_id):
        return client.known_file_id

    pyro.get_file_ids = get_file_ids

    logger_mod = types.ModuleType("Backend.logger")
    logger_mod.LOGGER = logging.getLogger("buffer-tests")

    bot_mod = types.ModuleType("Backend.pyrofork.bot")
    bot_mod.client_avg_mbps = {}
    bot_mod.client_failures = {}
    bot_mod.work_loads = defaultdict(int)

    backend_helper = {
        "Backend": backend,
        "Backend.helper": helper_pkg,
        "Backend.helper.stream_buffer": sb,
        "Backend.helper.custom_dl": custom_dl,
        "Backend.helper.pyro": pyro,
        "Backend.logger": logger_mod,
        "Backend.pyrofork.bot": bot_mod,
    }
    sys.modules.update(backend_helper)
    return {
        "db": fake_db,
        "custom_dl": custom_dl,
        "bot": bot_mod,
        "FakeByteStreamer": FakeByteStreamer,
    }


def load_buffered_streamer(sb):
    stubs = install_streamer_stubs(sb)
    mod = load_module("buffered_streamer_under_test", "Backend/helper/buffered_streamer.py")
    return mod, stubs


async def collect(gen):
    out = []
    async for chunk in gen:
        out.append(chunk)
    return b"".join(out)


async def settle():
    """Let consumer finally-blocks and bookkeeping tasks run, then clean up."""
    await asyncio.sleep(0.05)
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def run(coro):
    asyncio.run(coro)
