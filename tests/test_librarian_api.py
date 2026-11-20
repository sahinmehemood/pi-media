"""Phase 3 tests — Librarian bot API / CLI / page (DESIGN §9/§11).

Loads the REAL ``librarian_routes``, ``librarian_store``,
``api_routes`` settings functions, ``backup`` and ``librarian_cli``
by file path (the repo harness pattern) against stub modules whose
shapes mirror the real tree — every stubbed name was verified
against the real module it stands in for:

- ``fastapi`` — ``APIRouter`` recording route registrations (method,
  path, handler, dependencies) with the decorator semantics the
  router uses; ``Depends``/``Query`` markers; ``HTTPException``
  (status_code/detail, as in starlette); ``responses.JSONResponse`` /
  ``HTMLResponse`` carrying status + content; ``templating.
  Jinja2Templates`` whose ``TemplateResponse`` records (template,
  context) — the recorded context is the page handler's assertion
  surface (the real template render is §10.5's separate test).
- ``Backend`` package stubs: ``.db`` = the fake database (motor-shaped
  handles: ``dbs["tracking"]`` + ``dbs["storage_1"]``);
  ``.helper.settings_manager`` = ``FakeSettingsManager`` whose
  ``current()`` returns a settings object carrying the real property
  names/formulas for the librarian keys (copied from
  settings_manager.py:210-246) and whose ``update()`` records its
  calls and flips the fake snapshot, exactly like the real
  ``SettingsManager.update`` minus network title-sync (the fake
  ``StreamBot`` is None, so the real sync would resolve nothing);
  ``.pyrofork.bot`` (``Userbot``/``StreamBot``/``multi_clients``,
  scriptable per test); ``.fastapi.security.credentials`` —
  ``require_auth`` mirroring credentials.py:43 (401 unless the fake
  request session is authenticated); ``.fastapi.themes``
  (``get_theme``/``get_all_themes``/``theme_key`` returning the real
  shapes template_routes.py consumes); ``.helper.scan_manager`` —
  a ``FakeScanManager`` whose ``state`` is the real ``_blank_state``
  shape and whose ``get_status()`` returns the real key set.
- ``Backend.helper.librarian_gateway`` is the REAL gateway module
  (loaded by the Phase 2 suite); delegation tests monkeypatch its
  ``lookup``/``series``/``crossid`` attributes with scriptable fakes
  exposing the real signatures (librarian_gateway.py:687, 896,
  1067) — the attribute the router calls, at the boundary it calls
  it. Provider fakes that raise where real code returns None are
  the Phase 2 failure class and are not used here.
- Fake Mongo — the Phase 1 ``FakeCollection`` pattern extended with
  the §7 surface: ``find`` cursors with ``sort``/``limit``/
  ``to_list``, ``count_documents``, ``insert_one``, ``delete_one``,
  range operators (``$gte``/``$lt``), ``$or``, ``$regex``,
  ``$elemMatch``, dotted-field equality traversing lists, and
  unique-index enforcement raising the stubbed
  ``DuplicateKeyError``. Fresh handles per call, real semantics —
  a fake more generous than motor is a failed fake (AGENTS.md).
- ``bson`` — an ``ObjectId`` class with the real validation
  (24-hex), equality, and ``str()`` round-trip.
- Fake Telegram clients for ``probe``: async ``get_messages``
  scriptable to return a media message (``video``/``document``
  carrying ``file_id``, ``file_name``, ``file_size``, ``mime_type``
  — the pyrogram attribute names), return ``None``, return an
  empty message, raise, or sleep past the timeout.
- The real ``export_config``/``import_config`` (backup.py) and the
  real ``get_settings_api``/``update_settings_api`` (api_routes.py)
  are loaded by path with the stub inventory above — the redaction
  tests exercise production code, never a reimplementation.
"""

import copy
import hashlib
import json
import re
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_module, run  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------
# bson.ObjectId (real validation semantics)
# ------------------------------------------------------------------

class ObjectId:
    _counter = [0]

    def __init__(self, value=None):
        if value is None:
            ObjectId._counter[0] += 1
            value = "%024x" % (0x5eed000000000000000000 + ObjectId._counter[0])
        value = str(value)
        if len(value) != 24 or any(c not in "0123456789abcdefABCDEF" for c in value):
            raise ValueError(f"'{value}' is not a valid ObjectId")
        self._hex = value.lower()

    def __str__(self):
        return self._hex

    def __repr__(self):
        return f"ObjectId('{self._hex}')"

    def __eq__(self, other):
        return isinstance(other, ObjectId) and other._hex == self._hex

    def __hash__(self):
        return hash(self._hex)


class DuplicateKeyError(Exception):
    pass


# ------------------------------------------------------------------
# Fake Mongo (motor surface, real semantics for the used operations)
# ------------------------------------------------------------------

def _dotted_values(doc, path):
    """All values reachable at a dotted path, traversing lists."""
    parts = path.split(".")
    cur = [doc]
    for part in parts:
        nxt = []
        for item in cur:
            if isinstance(item, list):
                for el in item:
                    if isinstance(el, dict) and part in el:
                        nxt.append(el[part])
            elif isinstance(item, dict) and part in item:
                nxt.append(item[part])
        cur = nxt
    return cur


def _dotted_get(doc, path):
    vals = _dotted_values(doc, path)
    return (vals[0], True) if vals else (None, False)


def _dotted_set(doc, path, value):
    parts = path.split(".")
    cur = doc
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _value_matches(value, cond):
    if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
        for op, arg in cond.items():
            if op == "$gte":
                if not (value is not None and value >= arg):
                    return False
            elif op == "$lt":
                if not (value is not None and value < arg):
                    return False
            elif op == "$regex":
                if not isinstance(value, str):
                    return False
                opts = cond.get("$options", "")
                flags = re.IGNORECASE if "i" in opts else 0
                if not re.search(arg, value, flags):
                    return False
            elif op == "$options":
                continue
            elif op == "$elemMatch":
                if not isinstance(value, list):
                    return False
                if not any(isinstance(el, dict) and _matches(el, arg) for el in value):
                    return False
            else:
                return False
        return True
    return value == cond


def _matches(doc, filt):
    for key, cond in (filt or {}).items():
        if key == "$or":
            if not any(_matches(doc, sub) for sub in cond):
                return False
        elif key == "$and":
            if not all(_matches(doc, sub) for sub in cond):
                return False
        else:
            vals = _dotted_values(doc, key)
            if not vals:
                if cond is None:
                    continue
                return False
            if not any(_value_matches(v, cond) for v in vals):
                return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs
        self._limit = None

    def sort(self, spec):
        if isinstance(spec, str):
            spec = [(spec, 1)]
        for key, direction in reversed(list(spec)):
            self._docs.sort(key=lambda d: (_dotted_get(d, key)[0] is None,
                                           _dotted_get(d, key)[0]),
                            reverse=(direction < 0))
        return self

    def limit(self, n):
        self._limit = n
        return self

    async def to_list(self, length=None):
        docs = self._docs
        if self._limit is not None:
            docs = docs[: self._limit]
        if length is not None:
            docs = docs[:length]
        return copy.deepcopy(docs)

    def __aiter__(self):
        self._iter = iter(copy.deepcopy(self._docs))
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class FakeCollection:
    def __init__(self, name):
        self.name = name
        self.docs = []
        self.indexes = []
        self.update_calls = []  # (filter, update, upsert) — assertion surface
        self.raise_on_update = False

    async def create_index(self, keys, unique=False, sparse=False, expireAfterSeconds=None):
        self.indexes.append((list(keys), unique, sparse, expireAfterSeconds))
        return "idx"

    def _check_unique(self, candidate, ignore=None):
        for keys, unique, _sparse, _ttl in self.indexes:
            if not unique:
                continue
            paths = [k for k, _ in keys]
            vals = tuple(_dotted_get(candidate, p)[0] for p in paths)
            if any(v is None for v in vals):
                continue
            for doc in self.docs:
                if doc is ignore:
                    continue
                if tuple(_dotted_get(doc, p)[0] for p in paths) == vals:
                    raise DuplicateKeyError(f"E11000 duplicate key on {paths}")

    def find(self, filt=None, projection=None):
        return FakeCursor([copy.deepcopy(d) for d in self.docs if _matches(d, filt or {})])

    async def find_one(self, filt, projection=None):
        for doc in self.docs:
            if _matches(doc, filt):
                return copy.deepcopy(doc)
        return None

    async def count_documents(self, filt):
        return sum(1 for d in self.docs if _matches(d, filt))

    async def insert_one(self, doc):
        new_doc = copy.deepcopy(doc)
        if "_id" not in new_doc:
            new_doc["_id"] = ObjectId()
        self._check_unique(new_doc)
        self.docs.append(new_doc)
        return types.SimpleNamespace(inserted_id=new_doc["_id"])

    async def insert_many(self, docs):
        for d in docs:
            await self.insert_one(d)
        return types.SimpleNamespace(inserted_ids=[])

    async def delete_one(self, filt):
        for i, doc in enumerate(self.docs):
            if _matches(doc, filt):
                del self.docs[i]
                return types.SimpleNamespace(deleted_count=1)
        return types.SimpleNamespace(deleted_count=0)

    async def delete_many(self, filt):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not _matches(d, filt)]
        return types.SimpleNamespace(deleted_count=before - len(self.docs))

    async def update_one(self, filt, update, upsert=False):
        self.update_calls.append((copy.deepcopy(filt), copy.deepcopy(update), upsert))
        if self.raise_on_update:
            raise RuntimeError("fake update failure")
        for doc in self.docs:
            if _matches(doc, filt):
                for k, v in update.get("$set", {}).items():
                    _dotted_set(doc, k, copy.deepcopy(v))
                return types.SimpleNamespace(matched_count=1, upserted_id=None)
        if not upsert:
            return types.SimpleNamespace(matched_count=0, upserted_id=None)
        new_doc = {}
        for k, v in filt.items():
            if not k.startswith("$"):
                _dotted_set(new_doc, k, copy.deepcopy(v))
        for k, v in update.get("$setOnInsert", {}).items():
            _dotted_set(new_doc, k, copy.deepcopy(v))
        for k, v in update.get("$set", {}).items():
            _dotted_set(new_doc, k, copy.deepcopy(v))
        if "_id" not in new_doc:
            new_doc["_id"] = ObjectId()
        self._check_unique(new_doc)
        self.docs.append(new_doc)
        return types.SimpleNamespace(matched_count=0, upserted_id=new_doc["_id"])


class FakeDBHandle:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, FakeCollection(name))


class FakeDatabase:
    """Mirrors Backend.helper.database.Database's used surface."""

    def __init__(self):
        self.dbs = {"tracking": FakeDBHandle(), "storage_1": FakeDBHandle()}
        self.current_db_index = 1
        self.saved_settings = []

    def get_database_list(self):
        return []

    async def save_settings(self, data):
        self.saved_settings.append(copy.deepcopy(data))


fake_db = FakeDatabase()


# ------------------------------------------------------------------
# Fake settings (property formulas copied from settings_manager.py)
# ------------------------------------------------------------------

_SETTINGS_DEFAULTS = {
    "librarian_capture_enabled": False,
    "librarian_enabled": False,
    "librarian_mode": "off",
    "librarian_test_mode": True,
    "librarian_writes_enabled": False,
    "librarian_edit_budget_per_day": 10,
    "librarian_channels": [],
    "librarian_channel_types": {},
    "librarian_api_token_hash": "",
    # Phase 4 writer keys (settings_manager.py _DEFAULTS values).
    "librarian_writer_token": "",
    "librarian_writer_token_fingerprint": "",
    "librarian_writer_bot_id": 0,
    "librarian_writer_bot_username": "",
    "admin_password": "",
    "session_secret": "",
    "channel_titles": {},
    "auth_channels": [], "global_search_channels": [],
    "manual_channels": [], "anime_channels": [],
    "announcement_channel": "", "skip_channel": "", "dl_channel_id": "",
}


class FakeSettings:
    def __init__(self, data=None):
        merged = copy.deepcopy(_SETTINGS_DEFAULTS)
        merged.update({k: copy.deepcopy(v) for k, v in (data or {}).items() if k != "_id"})
        self._d = merged

    @property
    def librarian_capture_enabled(self):
        return bool(self._d.get("librarian_capture_enabled", False))

    @property
    def librarian_enabled(self):
        return bool(self._d.get("librarian_enabled", False))

    @property
    def librarian_mode(self):
        return str(self._d.get("librarian_mode") or "off").strip().lower()

    @property
    def librarian_test_mode(self):
        return bool(self._d.get("librarian_test_mode", True))

    @property
    def librarian_writes_enabled(self):
        return bool(self._d.get("librarian_writes_enabled", False))

    @property
    def librarian_edit_budget_per_day(self):
        try:
            return max(0, int(self._d.get("librarian_edit_budget_per_day", 10)))
        except (TypeError, ValueError):
            return 10

    @property
    def librarian_channels(self):
        return list(self._d.get("librarian_channels") or [])

    @property
    def librarian_channel_types(self):
        return dict(self._d.get("librarian_channel_types") or {})

    @property
    def librarian_api_token_hash(self):
        return str(self._d.get("librarian_api_token_hash") or "")

    # Phase 4 writer accessors (formulas copied from settings_manager.py).
    @property
    def librarian_writer_token(self):
        return str(self._d.get("librarian_writer_token") or "")

    @property
    def librarian_writer_token_fingerprint(self):
        return str(self._d.get("librarian_writer_token_fingerprint") or "")

    @property
    def librarian_writer_bot_id(self):
        try:
            return int(self._d.get("librarian_writer_bot_id") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def librarian_writer_bot_username(self):
        return str(self._d.get("librarian_writer_bot_username") or "")

    @property
    def channel_titles(self):
        raw = self._d.get("channel_titles") or {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items() if k and v}

    @property
    def auth_channels(self):
        return list(self._d.get("auth_channels") or [])

    @property
    def global_search_channels(self):
        return list(self._d.get("global_search_channels") or [])

    @property
    def manual_channels(self):
        return list(self._d.get("manual_channels") or [])

    @property
    def anime_channels(self):
        return list(self._d.get("anime_channels") or [])

    @property
    def announcement_channel(self):
        return str(self._d.get("announcement_channel") or "").strip()

    @property
    def skip_channel(self):
        return str(self._d.get("skip_channel") or "").strip()

    @property
    def dl_channel_id(self):
        raw = str(self._d.get("dl_channel_id") or "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def to_dict(self):
        return copy.deepcopy(self._d)


class FakeSettingsManager:
    _current = FakeSettings()
    update_calls = []  # list of payload dicts reaching update()

    @classmethod
    def current(cls):
        return cls._current

    @classmethod
    async def update(cls, db, new_values):
        cls.update_calls.append(copy.deepcopy(new_values))
        merged = cls._current.to_dict()
        merged.update(copy.deepcopy(new_values))
        cls._current = FakeSettings(merged)
        return {}

    @staticmethod
    def _all_channel_ids(data):
        ids = set()
        for key in ("auth_channels", "global_search_channels", "manual_channels", "anime_channels"):
            for c in (data.get(key) or []):
                c = str(c).strip()
                if c:
                    ids.add(c)
        for key in ("announcement_channel", "skip_channel", "dl_channel_id"):
            v = str(data.get(key) or "").strip()
            if v:
                ids.add(v)
        return ids

    @classmethod
    async def _sync_channel_titles(cls, data):
        # No clients are connected in the fake (StreamBot is None), so
        # the real sync resolves nothing; existing titles are filtered.
        active = cls._all_channel_ids(data)
        existing = data.get("channel_titles") or {}
        if not isinstance(existing, dict):
            existing = {}
        titles = {str(k): str(v) for k, v in existing.items() if str(k) in active and v}
        data["channel_titles"] = titles
        return titles


# ------------------------------------------------------------------
# Fake ScanManager (state shape = scan_manager._blank_state)
# ------------------------------------------------------------------

class FakeScanManager:
    def __init__(self):
        self.state = self._blank()

    @staticmethod
    def _blank():
        return {
            "status": "idle", "mode": "scan", "selected_channels": [],
            "pending": [], "current_channel": None, "current_channel_name": "",
            "current_id": 0, "current_target_id": 0, "cursors": {},
            "counters": {"total_found": 0, "processed": 0, "indexed": 0,
                         "skipped_dup": 0, "skipped_meta": 0, "skipped_nonvid": 0,
                         "subtitles_added": 0, "subtitles_skipped": 0, "errors": 0},
            "started_at": 0.0, "updated_at": 0.0, "finished_at": 0.0, "error": None,
        }

    def get_status(self):
        s = self.state
        return {
            "status": s["status"], "mode": s["mode"],
            "is_running": s["status"] == "running",
            "resumable": s["status"] in ("paused", "cancelled") and bool(s["pending"]),
            "selected_channels": list(s["selected_channels"]),
            "pending": list(s["pending"]),
            "current_channel": s["current_channel"],
            "current_channel_name": s["current_channel_name"],
            "current_id": int(s.get("current_id", 0) or 0),
            "current_target_id": int(s.get("current_target_id", 0) or 0),
            "progress": 0, "has_progress": False,
            "counters": dict(s["counters"]),
            "elapsed": "0s", "elapsed_seconds": 0, "error": s["error"],
        }


fake_scan_manager = FakeScanManager()


# ------------------------------------------------------------------
# fastapi stub
# ------------------------------------------------------------------

class HTTPException(Exception):
    def __init__(self, status_code, detail=None, headers=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _Depends:
    def __init__(self, dependency=None):
        self.dependency = dependency


class _Response:
    def __init__(self, content=None, status_code=200, media_type=None):
        self.content = content
        self.body = content
        self.status_code = status_code
        self.media_type = media_type


class JSONResponse(_Response):
    pass


class HTMLResponse(_Response):
    pass


class FakeAPIRouter:
    def __init__(self, *args, **kwargs):
        self.routes = []  # (method, path, handler, dependencies)
        self.on_startup = list(kwargs.get("on_startup") or [])

    def _add(self, method, path, dependencies=None):
        def deco(fn):
            self.routes.append((method, path, fn, list(dependencies or [])))
            return fn
        return deco

    def get(self, path, dependencies=None, **kw):
        return self._add("GET", path, dependencies)

    def post(self, path, dependencies=None, **kw):
        return self._add("POST", path, dependencies)

    def delete(self, path, dependencies=None, **kw):
        return self._add("DELETE", path, dependencies)

    def route_map(self):
        return {(m, p): fn for m, p, fn, _ in self.routes}


class FakeTemplates:
    def __init__(self, directory=None):
        self.directory = directory
        self.calls = []  # (template_name, context)

    def TemplateResponse(self, name, context=None, *args, **kwargs):
        self.calls.append((name, context))
        resp = HTMLResponse(content=f"<template:{name}>", status_code=200)
        resp.template_name = name
        resp.context = context
        return resp


class FakeRequest:
    """Mirrors the starlette Request surface the code under test uses:
    headers, query_params, session, cookies, and async json()."""

    def __init__(self, headers=None, query=None, body=None, session=None,
                 cookies=None, raw_body=None):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.query_params = dict(query or {})
        self._body = body
        self._raw_body = raw_body
        self.session = dict(session or {})
        self.cookies = dict(cookies or {})

    async def json(self):
        if self._raw_body is not None:
            return json.loads(self._raw_body)  # raises on malformed JSON, like starlette
        return copy.deepcopy(self._body)


# ------------------------------------------------------------------
# Permissive stubs for api_routes' wide import surface. Only the
# names the tested functions actually call are given real behaviour
# (in their dedicated stubs above/below); the rest exist so the real
# module loads — they are never called by the tested functions.
# ------------------------------------------------------------------

class _Dummy:
    def __init__(self, *a, **kw):
        pass

    def __call__(self, *a, **kw):
        return _Dummy()

    def __getattr__(self, name):
        return _Dummy()


def _permissive(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    mod.__getattr__ = lambda attr: _Dummy
    return mod


def _install_stubs():
    import logging

    bson_mod = types.ModuleType("bson")
    bson_mod.ObjectId = ObjectId

    pymongo = types.ModuleType("pymongo")
    pymongo.ASCENDING = 1
    pm_errors = types.ModuleType("pymongo.errors")
    pm_errors.DuplicateKeyError = DuplicateKeyError
    pymongo.errors = pm_errors

    fastapi = types.ModuleType("fastapi")
    fastapi.APIRouter = FakeAPIRouter
    fastapi.Depends = lambda dependency=None: _Depends(dependency)
    fastapi.Query = lambda default=None, **kw: default
    fastapi.Request = FakeRequest
    fastapi.HTTPException = HTTPException
    fastapi.Form = lambda default=None, **kw: default
    fa_responses = types.ModuleType("fastapi.responses")
    fa_responses.JSONResponse = JSONResponse
    fa_responses.HTMLResponse = HTMLResponse
    fa_responses.FileResponse = _Dummy
    fa_responses.StreamingResponse = _Dummy
    fa_responses.RedirectResponse = _Dummy
    fa_templating = types.ModuleType("fastapi.templating")
    fa_templating.Jinja2Templates = FakeTemplates
    fastapi.responses = fa_responses
    fastapi.templating = fa_templating

    pyrogram = types.ModuleType("pyrogram")
    pg_enums = _permissive("pyrogram.enums")
    pg_errors = types.ModuleType("pyrogram.errors")

    class FloodWait(Exception):
        def __init__(self, value=0):
            super().__init__(f"FloodWait {value}")
            self.value = value

    pg_errors.FloodWait = FloodWait
    pg_types = _permissive("pyrogram.types")
    pyrogram.enums = pg_enums
    pyrogram.errors = pg_errors
    pyrogram.types = pg_types
    pyrogram.Client = object

    backend = types.ModuleType("Backend")
    backend.__version__ = "test"
    backend.StartTime = 0.0
    backend.db = fake_db
    logger_mod = types.ModuleType("Backend.logger")
    logger_mod.LOGGER = logging.getLogger("librarian-api-tests")

    helper = types.ModuleType("Backend.helper")
    backend_pkg_fastapi = types.ModuleType("Backend.fastapi")
    security_pkg = types.ModuleType("Backend.fastapi.security")
    credentials = types.ModuleType("Backend.fastapi.security.credentials")

    async def require_auth(request):
        # credentials.py:43 — 401 unless the session is authenticated.
        if not request.session.get("authenticated"):
            raise HTTPException(status_code=401, detail="Not authenticated")
        return True

    def get_current_user(request):
        if request.session.get("authenticated"):
            return request.session.get("username", "admin")
        return None

    credentials.require_auth = require_auth
    credentials.get_current_user = get_current_user
    credentials.is_authenticated = lambda request: bool(request.session.get("authenticated"))
    security_pkg.credentials = credentials

    themes = types.ModuleType("Backend.fastapi.themes")
    themes.get_theme = lambda name="paper": {"name": name, "is_dark": False, "colors": {}}
    themes.get_all_themes = lambda: {"paper": {"name": "Paper"}}
    themes.theme_key = lambda request: "paper"
    backend_pkg_fastapi.themes = themes
    backend_pkg_fastapi.security = security_pkg

    settings_manager = types.ModuleType("Backend.helper.settings_manager")
    settings_manager.SettingsManager = FakeSettingsManager
    settings_manager.Settings = FakeSettings

    scan_manager_mod = types.ModuleType("Backend.helper.scan_manager")
    scan_manager_mod.scan_manager = fake_scan_manager
    scan_manager_mod.dbcheck_manager = FakeScanManager()
    scan_manager_mod.duplicate_manager = FakeScanManager()

    bot_mod = types.ModuleType("Backend.pyrofork.bot")
    bot_mod.StreamBot = None
    bot_mod.Userbot = None
    bot_mod.multi_clients = {}
    bot_mod.client_avg_mbps = {}
    bot_mod.client_dc_map = {}
    bot_mod.client_failures = {}
    bot_mod.work_loads = {}

    passwords = types.ModuleType("Backend.helper.passwords")
    passwords.hash_password = lambda pw: f"hashed:{pw}"
    passwords.verify_password = lambda pw, hashed: hashed == f"hashed:{pw}"

    stream_buffer = types.ModuleType("Backend.helper.stream_buffer")
    stream_buffer.RANGES = {
        "buffer_workers": (1, 32), "buffer_readahead_mb": (8, 1024),
        "buffer_cache_mb": (16, 2048), "buffer_stall_timeout": (10, 600),
        "buffer_disk_cache_mb": (0, 8192),
    }
    prewarm = types.ModuleType("Backend.helper.prewarm")
    prewarm.RANGES = {
        "prewarm_mb_per_file": (16, 2048), "prewarm_episodes_ahead": (0, 10),
        "prewarm_trigger_pct": (10, 95), "prewarm_daily_budget_mb": (0, 65536),
    }

    split_files = types.ModuleType("Backend.helper.split_files")
    _TRAILING_NUMERIC = re.compile(
        r"(?i)\.(mkv|mp4|avi|ts|m4v|mov|wmv|webm|flv|m2ts|mpg|mpeg|zip)\.(\d{2,3})(?=$|\D)")

    def _strip_part_suffix(filename):
        if not filename:
            return filename
        name = filename.strip()
        m = _TRAILING_NUMERIC.search(name)
        if not m:
            return filename
        return (name[: m.start()] + "." + m.group(1)) if m.group(1) else (name[: m.start()] + name[m.end():])

    split_files.strip_part_suffix = _strip_part_suffix

    # Store import surface (Phase 1 suite's faithful reductions).
    metadata_pkg = types.ModuleType("Backend.helper.metadata")
    md_common = types.ModuleType("Backend.helper.metadata.common")

    def _caption_with_links(message):
        urls = [e.url for e in (getattr(message, "caption_entities", None) or []) if getattr(e, "url", None)]
        return "\n".join([getattr(message, "caption", None) or "", *urls])

    md_common.caption_with_links = _caption_with_links
    md_parse = types.ModuleType("Backend.helper.metadata.parse")
    _RES_RE = re.compile(r"(?i)\b(2160|1440|1080|720|576|480|360|240)p\b")
    _SE_RE2 = re.compile(r"(?i)\bS\d{1,2}E\d{1,3}\b|\b\d{1,2}x\d{2,3}\b")
    _YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
    _CODEC_RE = re.compile(
        r"(?i)\b(x264|x265|h\.?264|h\.?265|hevc|avc|web-dl|webrip|bluray|bdrip|hdrip|aac|ac3|dts|flac|mp3|10bit|hdr|dv)\b")

    def _parse_media_name(name):
        base = name or ""
        quality = None
        m = _RES_RE.search(base)
        if m:
            quality = m.group(0).lower()
        title = re.sub(r"\[[^\]]*\]", " ", base)
        title = _SE_RE2.sub(" ", title)
        title = _RES_RE.sub(" ", title)
        title = _CODEC_RE.sub(" ", title)
        title = _YEAR_RE.sub(" ", title)
        title = re.sub(r"[._\-]+", " ", title)
        title = re.sub(r"\s+", " ", title).strip()
        return {"title": title or None, "year": None, "season": None,
                "episode": None, "quality": quality, "excess": None}

    md_parse.parse_media_name = _parse_media_name
    metadata_pkg.common = md_common
    metadata_pkg.parse = md_parse
    metadata_pkg.__getattr__ = lambda attr: _Dummy
    pyro_helper = types.ModuleType("Backend.helper.pyro")

    def _readable_size(size_in_bytes):
        size_in_bytes = int(size_in_bytes) if str(size_in_bytes).isdigit() else 0
        if not size_in_bytes:
            return "0B"
        index, units = 0, ["B", "KB", "MB", "GB", "TB", "PB"]
        while size_in_bytes >= 1024 and index < len(units) - 1:
            size_in_bytes /= 1024
            index += 1
        return f"{size_in_bytes:.2f}{units[index]}" if index > 0 else f"{size_in_bytes:.0f}B"

    def _clean_filename(filename):
        if not filename:
            return "unknown_file"
        filename = re.sub(r"[^\x20-\x7E]", " ", filename)
        filename = re.sub(r"\s+", " ", filename).strip().replace(" .", ".")
        return filename if filename else "unknown_file"

    pyro_helper.clean_filename = _clean_filename
    pyro_helper.get_readable_file_size = _readable_size
    subtitles = types.ModuleType("Backend.helper.subtitles")
    subtitles.is_subtitle_file = lambda n: bool(n) and n.lower().strip().endswith(
        (".srt", ".vtt", ".ass", ".ssa", ".sub"))
    subtitles.__getattr__ = lambda attr: _Dummy

    mods = {
        "bson": bson_mod,
        "pymongo": pymongo, "pymongo.errors": pm_errors,
        "fastapi": fastapi, "fastapi.responses": fa_responses,
        "fastapi.templating": fa_templating,
        "pyrogram": pyrogram, "pyrogram.enums": pg_enums,
        "pyrogram.errors": pg_errors, "pyrogram.types": pg_types,
        "Backend": backend, "Backend.logger": logger_mod,
        "Backend.helper": helper,
        "Backend.fastapi": backend_pkg_fastapi,
        "Backend.fastapi.security": security_pkg,
        "Backend.fastapi.security.credentials": credentials,
        "Backend.fastapi.themes": themes,
        "Backend.helper.settings_manager": settings_manager,
        "Backend.helper.scan_manager": scan_manager_mod,
        "Backend.pyrofork.bot": bot_mod,
        "Backend.helper.passwords": passwords,
        "Backend.helper.stream_buffer": stream_buffer,
        "Backend.helper.prewarm": prewarm,
        "Backend.helper.split_files": split_files,
        "Backend.helper.metadata.common": md_common,
        "Backend.helper.metadata.parse": md_parse,
        "Backend.helper.pyro": pyro_helper,
        "Backend.helper.subtitles": subtitles,
    }
    # Permissive modules for the rest of api_routes' import surface.
    for name in (
        "Backend.fastapi.routes.stream_routes",
        "Backend.fastapi.routes.stremio_routes",
        "Backend.helper.analytics", "Backend.helper.auto_catalog",
        "Backend.helper.custom_dl", "Backend.helper.encrypt",
        "Backend.helper.health", "Backend.helper.manual_add",
        "Backend.helper.own_bots",
        "Backend.helper.requests_manager",
        "Backend.helper.session_auth",
        "Backend.helper.announcer", "Backend.helper.stream_formatter",
        "Backend.pyrofork", "Backend.fastapi.routes",
    ):
        mods[name] = _permissive(name)
    pyro_helper.get_readable_time = lambda s: f"{s}s"
    pyro_helper.resolve_video_thumb_url = lambda *a, **kw: None
    mods["Backend.helper.stream_formatter"].PRESETS = {}
    mods["Backend.helper.metadata"] = metadata_pkg
    helper.metadata = metadata_pkg
    sys.modules.update(mods)
    backend.helper = helper
    backend.fastapi = backend_pkg_fastapi
    helper.settings_manager = settings_manager
    helper.scan_manager = scan_manager_mod
    # `import Backend.pyrofork.bot as botmod` resolves through the
    # parent package's attributes, so bind the real stub there too
    # (a permissive parent __getattr__ would shadow the submodule).
    sys.modules["Backend.pyrofork"].bot = bot_mod
    return {"bot": bot_mod, "credentials": credentials, "themes": themes}


STUBS = _install_stubs()

# Real modules under test (backup first: api_routes imports it).
backup = load_module("Backend.helper.backup", "Backend/helper/backup.py")
sys.modules["Backend.helper"].backup = backup
api_routes = load_module("api_routes_under_test", "Backend/fastapi/routes/api_routes.py")
store = load_module("Backend.helper.librarian_store", "Backend/helper/librarian_store.py")

HASH_SENTINEL = "deadbeef" * 8  # a known non-empty hash value


def _reset_api():
    fake_db.dbs["tracking"] = FakeDBHandle()
    fake_db.dbs["storage_1"] = FakeDBHandle()
    fake_db.saved_settings = []
    FakeSettingsManager._current = FakeSettings()
    FakeSettingsManager.update_calls = []
    fake_scan_manager.state = FakeScanManager._blank()
    STUBS["bot"].Userbot = None
    STUBS["bot"].StreamBot = None
    STUBS["bot"].multi_clients = {}
    if hasattr(store, "_indexes_ready"):
        store._indexes_ready = False


def _set_settings(**kw):
    data = FakeSettingsManager._current.to_dict()
    data.update(kw)
    FakeSettingsManager._current = FakeSettings(data)
    return FakeSettingsManager._current


# ------------------------------------------------------------------
# §10.3 Security tests R1–R5 (Step 1)
# ------------------------------------------------------------------

def test_R1_settings_response_redacts_hash():
    _reset_api()
    _set_settings(librarian_api_token_hash=HASH_SENTINEL)

    async def go():
        result = await api_routes.get_settings_api()
        blob = json.dumps(result)
        assert HASH_SENTINEL not in blob
        assert result["settings"]["librarian_api_token_hash"] == ""
        assert result["settings"]["librarian_api_token_hash_set"] is True

    run(go())


def test_R2_backup_export_redacts_hash():
    _reset_api()
    _set_settings(librarian_api_token_hash=HASH_SENTINEL)

    async def go():
        result = await backup.export_config()
        blob = json.dumps(result)
        assert HASH_SENTINEL not in blob
        assert "librarian_api_token_hash" not in result["settings"]

    run(go())


def test_R3_backup_restore_cannot_set_hash():
    _reset_api()
    _set_settings(librarian_api_token_hash="originalhash")

    async def go():
        payload = {"app": "telegram-stremio",
                   "settings": {"librarian_api_token_hash": HASH_SENTINEL,
                                "librarian_mode": "ask"}}
        await backup.import_config(payload)
        assert FakeSettingsManager.update_calls, "update() should have been called"
        for call in FakeSettingsManager.update_calls:
            assert "librarian_api_token_hash" not in call
        assert FakeSettingsManager.current().librarian_api_token_hash == "originalhash"

    run(go())


def test_R4_settings_payload_cannot_set_hash():
    _reset_api()

    async def go():
        # (a) non-empty hash -> 400, update() never called
        try:
            await api_routes.update_settings_api({"librarian_api_token_hash": HASH_SENTINEL})
            raise AssertionError("expected HTTPException 400")
        except HTTPException as e:
            assert e.status_code == 400
        assert FakeSettingsManager.update_calls == []
        # (b) empty hash -> save proceeds, key deleted before update()
        await api_routes.update_settings_api({"librarian_api_token_hash": "  "})
        assert FakeSettingsManager.update_calls
        assert "librarian_api_token_hash" not in FakeSettingsManager.update_calls[-1]
        # (c) last-seen in payload -> deleted before update()
        FakeSettingsManager.update_calls = []
        await api_routes.update_settings_api({"librarian_agent_last_seen": "2026-01-01"})
        assert "librarian_agent_last_seen" not in FakeSettingsManager.update_calls[-1]

    run(go())


# ------------------------------------------------------------------
# Phase 4 Workstream B — B-3 regression (plan §3.3 fallback placement:
# the real update_settings_api needs this suite's stub inventory).
# A settings payload carrying plausible db-name keys goes through the
# REAL update_settings_api (and is really merged by SettingsManager.
# update — the fake records it), yet a subsequently constructed REAL
# Database resolves its name exactly as before: no settings path can
# reach db_name, self.db_name, or os.environ (security B-3).
# ------------------------------------------------------------------

_real_database_cls = None


def _load_real_database():
    global _real_database_cls
    if _real_database_cls is not None:
        return _real_database_cls

    def _ensure(name):
        mod = sys.modules.get(name)
        if mod is None:
            mod = types.ModuleType(name)
            sys.modules[name] = mod
        return mod

    # Only the names database.py imports that this suite's inventory
    # does not already provide, with the real shapes.
    motor_asyncio = _ensure("motor.motor_asyncio")
    if not hasattr(motor_asyncio, "AsyncIOMotorClient"):
        motor_asyncio.AsyncIOMotorClient = type("AsyncIOMotorClient", (), {})
    if not hasattr(motor_asyncio, "AsyncIOMotorDatabase"):
        motor_asyncio.AsyncIOMotorDatabase = type("AsyncIOMotorDatabase", (), {})
    _ensure("motor").motor_asyncio = motor_asyncio

    pydantic = _ensure("pydantic")
    if not hasattr(pydantic, "ValidationError"):
        pydantic.ValidationError = type("ValidationError", (Exception,), {})

    pymongo = sys.modules["pymongo"]
    if not hasattr(pymongo, "DESCENDING"):
        pymongo.DESCENDING = -1

    config = _ensure("Backend.config")
    if not hasattr(config, "Telegram"):
        config.Telegram = type("Telegram", (), {})
    if len(getattr(config.Telegram, "DATABASE", []) or []) < 2:
        config.Telegram.DATABASE = [
            "mongodb://user:pass@host1.example/b3-tests",
            "mongodb://user:pass@host2.example/b3-tests",
        ]

    if "Backend.helper.bounded" not in sys.modules:
        bounded = load_module("Backend.helper.bounded",
                              "Backend/helper/bounded.py")
        sys.modules["Backend.helper"].bounded = bounded

    modal = _ensure("Backend.helper.modal")
    for name in ("Episode", "MovieSchema", "QualityDetail",
                 "QualityPart", "Season", "TVShowSchema"):
        if not hasattr(modal, name):
            setattr(modal, name, type(name, (), {}))
    sys.modules["Backend.helper"].modal = modal

    task_manager = _ensure("Backend.helper.task_manager")
    if not hasattr(task_manager, "delete_message"):
        async def _delete_message(*args, **kwargs):
            return None
        task_manager.delete_message = _delete_message
    sys.modules["Backend.helper"].task_manager = task_manager

    mod = load_module("database_b3_under_test", "Backend/helper/database.py")
    _real_database_cls = mod.Database
    return _real_database_cls


def test_B3_settings_payload_cannot_reach_db_name():
    _reset_api()
    Database = _load_real_database()
    payload = {"db_name": "otherdb", "TS_DB_NAME": "otherdb",
               "database_name": "otherdb"}
    sentinel = object()
    old_env = os.environ.get("TS_DB_NAME", sentinel)

    async def submit():
        await api_routes.update_settings_api(dict(payload))

    try:
        # Env in force: a staging name. The payload must not move it.
        os.environ["TS_DB_NAME"] = "dbFyvio_staging"
        assert Database().db_name == "dbFyvio_staging"
        run(submit())
        # The payload really went through the real update path and was
        # merged into the settings document (SettingsManager.update
        # merges arbitrary keys) — it simply has no path to Database.
        assert FakeSettingsManager.update_calls, "update() should have been called"
        merged = FakeSettingsManager.update_calls[-1]
        for key in payload:
            assert merged.get(key) == "otherdb", (key, merged)
        assert os.environ["TS_DB_NAME"] == "dbFyvio_staging"
        assert Database().db_name == "dbFyvio_staging"

        # Env unset: the payload must not move the default either.
        del os.environ["TS_DB_NAME"]
        FakeSettingsManager.update_calls = []
        run(submit())
        assert FakeSettingsManager.update_calls
        assert "TS_DB_NAME" not in os.environ
        assert Database().db_name == "dbFyvio"
    finally:
        if old_env is sentinel:
            os.environ.pop("TS_DB_NAME", None)
        else:
            os.environ["TS_DB_NAME"] = old_env


def test_R4_librarian_validation_table():
    _reset_api()

    async def expect_400(payload):
        FakeSettingsManager.update_calls = []
        try:
            await api_routes.update_settings_api(payload)
            raise AssertionError(f"expected 400 for {payload}")
        except HTTPException as e:
            assert e.status_code == 400, (payload, e.status_code)
        assert FakeSettingsManager.update_calls == []

    async def go():
        await expect_400({"librarian_mode": "sometimes"})
        await expect_400({"librarian_edit_budget_per_day": "10"})
        await expect_400({"librarian_edit_budget_per_day": True})
        await expect_400({"librarian_channels": ["12345"]})
        await expect_400({"librarian_channels": "notalist"})
        await expect_400({"librarian_channel_types": {"-10012345678": "western"}})
        await expect_400({"librarian_channel_types": ["tv"]})
        # Valid payload round-trips through update() in shape.
        FakeSettingsManager.update_calls = []
        await api_routes.update_settings_api({
            "librarian_enabled": 1, "librarian_mode": "ASK",
            "librarian_edit_budget_per_day": 25,
            "librarian_channels": ["-10012345678"],
            "librarian_channel_types": {"-10012345678": "anime"},
        })
        sent = FakeSettingsManager.update_calls[-1]
        assert sent["librarian_enabled"] is True
        assert sent["librarian_mode"] == "ask"
        assert sent["librarian_edit_budget_per_day"] == 25
        assert sent["librarian_channels"] == ["-10012345678"]
        assert sent["librarian_channel_types"] == {"-10012345678": "anime"}

    run(go())


def test_R5_templates_never_output_hash():
    settings_html = (ROOT / "Backend/fastapi/templates/settings.html").read_text(encoding="utf-8")
    assert not re.search(r"\{\{[^}]*librarian_api_token_hash", settings_html)
    lib_page = ROOT / "Backend/fastapi/templates/librarian.html"
    if lib_page.exists():
        text = lib_page.read_text(encoding="utf-8")
        assert "librarian_api_token_hash" not in text
        assert not re.search(r"\{\{[^}]*librarian_api_token_hash", text)


# ------------------------------------------------------------------
# §10.2 Store extension tests (Step 2)
# ------------------------------------------------------------------

def _ledger_row(chat_id=-100111, msg_id=1, stem="some show", status="waiting", **kw):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row = {
        "source_locator": {"chat_id": chat_id, "msg_id": msg_id},
        "current_locator": {"chat_id": chat_id, "msg_id": msg_id},
        "skip_locator": None, "file_unique_id": "u1", "file_name": "f.mkv",
        "size": 100, "caption": "cap", "origin": "live-fail", "stem": stem,
        "special_flag": False,
        "identity": {"quality": "1080p", "norm_name": stem, "size": "100B"},
        "status": status, "park_reason": None, "escalation_reason": None,
        "chat_id": chat_id, "created_at": now, "updated_at": now,
    }
    row.update(kw)
    return row


def _seed_ledger(rows):
    fake_db.dbs["tracking"]["reject_ledger"].docs.extend(copy.deepcopy(rows))


def _fix_row(**kw):
    row = {
        "request_id": "req-1", "batch_id": None, "stem": "some show",
        "chat_id": -100111,
        "source_locator": {"chat_id": -100111, "msg_id": 1},
        "current_locator": {"chat_id": -100111, "msg_id": 1},
        "action": "edit", "state": "fixed", "index_confirmed": True,
        "dry_run": False, "budget_counted": True, "error": None,
        "old_caption": "old", "new_caption": "new",
        "expected_ids": {"tmdb": 1}, "series_record_id": None,
        "created_at": datetime.now(timezone.utc),
    }
    row.update(kw)
    return row


def test_store_counts_by_status():
    _reset_api()
    _seed_ledger([_ledger_row(msg_id=1), _ledger_row(msg_id=2),
                  _ledger_row(msg_id=3, status="parked"),
                  _ledger_row(msg_id=4, status="escalated"),
                  _ledger_row(msg_id=5, status="fixed")])

    async def go():
        counts = await store.count_by_status()
        assert counts == {"waiting": 2, "parked": 1, "escalated": 1, "fixed": 1}

    run(go())


def test_store_budget_today_ist_boundary():
    _reset_api()
    # IST = UTC+5:30. Today 23:59 IST = 18:29 UTC today; tomorrow
    # 00:01 IST = 18:31 UTC today. Build from the store's own bounds.
    start, end = store._ist_day_bounds_utc(datetime.now(timezone.utc))
    inside = start + timedelta(hours=1)
    before = start - timedelta(minutes=1)
    fixes = fake_db.dbs["tracking"]["librarian_fixes"]
    fixes.docs.extend([
        _fix_row(request_id="a", created_at=inside),
        _fix_row(request_id="b", created_at=before),
        _fix_row(request_id="c", created_at=inside, budget_counted=False),
        _fix_row(request_id="d", created_at=end - timedelta(seconds=1)),
    ])

    async def go():
        assert await store.count_budget_used_today() == 2
        total, fixed = await store.count_operations_today()
        assert total == 3 and fixed == 3

    run(go())


def test_store_waiting_sort_filter_limit():
    _reset_api()
    _seed_ledger([
        _ledger_row(chat_id=-100222, msg_id=1, stem="beta"),
        _ledger_row(chat_id=-100111, msg_id=9, stem="beta"),
        _ledger_row(chat_id=-100111, msg_id=3, stem="beta"),
        _ledger_row(chat_id=-100111, msg_id=5, stem="alpha"),
        _ledger_row(chat_id=-100111, msg_id=7, stem="alpha", status="parked"),
    ])

    async def go():
        rows = await store.list_waiting()
        keys = [(r["chat_id"], r["stem"], r["source_locator"]["msg_id"]) for r in rows]
        assert keys == [(-100222, "beta", 1), (-100111, "alpha", 5),
                        (-100111, "beta", 3), (-100111, "beta", 9)]
        rows = await store.list_waiting(channel_id=-100111, stem="beta")
        assert [r["source_locator"]["msg_id"] for r in rows] == [3, 9]
        rows = await store.list_waiting(limit=2)
        assert len(rows) == 2
        assert len(await store.list_waiting_all()) == 4

    run(go())


def test_store_transitions_legal_and_illegal():
    _reset_api()
    _seed_ledger([_ledger_row(msg_id=1), _ledger_row(msg_id=2, status="fixed")])

    async def go():
        await store.ensure_indexes()
        row, cur = await store.transition_ledger(-100111, 1, "park", "held")
        assert cur == "waiting" and row["status"] == "parked" and row["park_reason"] == "held"
        row, cur = await store.transition_ledger(-100111, 1, "escalate", "ghost?")
        assert row["status"] == "escalated" and row["escalation_reason"] == "ghost?"
        row, cur = await store.transition_ledger(-100111, 1, "unpark")
        assert row["status"] == "waiting" and row["park_reason"] is None
        assert row["escalation_reason"] is None
        # Illegal: park a fixed row — no write, stored doc byte-identical.
        before = copy.deepcopy(fake_db.dbs["tracking"]["reject_ledger"].docs)
        row, cur = await store.transition_ledger(-100111, 2, "park", "nope")
        assert cur == "fixed" and row["status"] == "fixed"
        assert fake_db.dbs["tracking"]["reject_ledger"].docs == before
        # Illegal: unpark a waiting row; escalate a fixed row.
        row, cur = await store.transition_ledger(-100111, 1, "unpark")
        assert cur == "waiting"
        row, cur = await store.transition_ledger(-100111, 2, "escalate", "x")
        assert cur == "fixed"
        # Missing row.
        row, cur = await store.transition_ledger(-100111, 999, "park", "x")
        assert row is None and cur is None

    run(go())


def test_store_record_upsert_and_lookups():
    _reset_api()
    rec = {
        "canonical_key": "tmdb:42", "ids": {"tmdb": 42, "imdb": "tt0000042"},
        "titles": ["Fixture Show"], "media_type": "tv",
        "numbering": {"source": "tmdb", "order": "aired", "offset": None, "note": ""},
        "status": "verified", "related_entries": [],
    }

    async def go():
        await store.ensure_indexes()
        stored = await store.upsert_record(rec)
        assert stored["created_at"] is not None and stored["updated_at"] is not None
        first_created = stored["created_at"]
        by_id = await store.get_record_by_id_field("tmdb", 42)
        assert by_id["canonical_key"] == "tmdb:42"
        by_imdb = await store.get_record_by_id_field("imdb", "tt0000042")
        assert by_imdb["canonical_key"] == "tmdb:42"
        by_key = await store.get_record_by_canonical_key("tmdb:42")
        assert by_key["_id"] == stored["_id"]
        rec2 = dict(rec, titles=["Fixture Show", "Fixture Show Alt"])
        stored2 = await store.upsert_record(rec2)
        assert stored2["created_at"] == first_created
        assert stored2["titles"] == ["Fixture Show", "Fixture Show Alt"]
        assert len(fake_db.dbs["tracking"]["series_records"].docs) == 1
        assert (await store.list_records())[0]["canonical_key"] == "tmdb:42"

    run(go())


def test_store_alias_roundtrip_unique_pair():
    _reset_api()
    rid = ObjectId()

    async def go():
        await store.ensure_indexes()
        await store.set_alias(-100111, "some show", rid)
        got = await store.get_alias(-100111, "some show")
        assert got["series_record_id"] == rid
        rid2 = ObjectId()
        await store.set_alias(-100111, "some show", rid2)  # repoint, never duplicate
        assert len(fake_db.dbs["tracking"]["alias_map"].docs) == 1
        assert (await store.get_alias(-100111, "some show"))["series_record_id"] == rid2
        await store.set_alias(-100222, "other stem", rid)
        assert len(await store.list_aliases()) == 2
        assert len(await store.list_aliases(channel_id=-100111)) == 1
        assert await store.forget_alias(-100111, "some show") is True
        assert await store.forget_alias(-100111, "some show") is False
        assert await store.get_alias(-100111, "some show") is None

    run(go())


def test_store_history_filters_and_order():
    _reset_api()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fixes = fake_db.dbs["tracking"]["librarian_fixes"]
    fixes.docs.extend([
        _fix_row(request_id="a", created_at=t0 + timedelta(hours=1), stem="alpha"),
        _fix_row(request_id="b", created_at=t0 + timedelta(hours=3), stem="beta", batch_id="batch-1"),
        _fix_row(request_id="c", created_at=t0 + timedelta(hours=2), stem="alpha", batch_id="batch-1"),
    ])

    async def go():
        rows = await store.list_history()
        assert [r["request_id"] for r in rows] == ["b", "c", "a"]
        rows = await store.list_history(stem="alpha")
        assert [r["request_id"] for r in rows] == ["c", "a"]
        rows = await store.list_history(batch_id="batch-1")
        assert [r["request_id"] for r in rows] == ["b", "c"]
        rows = await store.list_history(limit=1)
        assert len(rows) == 1

    run(go())


# ------------------------------------------------------------------
# Router tests (Steps 3+): auth, last-seen, status/mode, stubs
# ------------------------------------------------------------------

routes = load_module("Backend.fastapi.librarian_routes",
                     "Backend/fastapi/librarian_routes.py")
ROUTE_MAP = routes.router.route_map()

TEST_TOKEN = "test-token-0123456789abcdef"
TEST_HASH = hashlib.sha256(TEST_TOKEN.encode("utf-8")).hexdigest()


def _authed_req(body=None, query=None, raw_body=None, token=TEST_TOKEN, headers=None):
    hdrs = dict(headers or {})
    if token is not None:
        hdrs["Authorization"] = f"Bearer {token}"
    return FakeRequest(headers=hdrs, query=query, body=body, raw_body=raw_body)


def _enable(token_hash=TEST_HASH, **kw):
    base = {"librarian_enabled": True, "librarian_api_token_hash": token_hash}
    base.update(kw)
    return _set_settings(**base)


def _call(method, path, request):
    import asyncio

    handler = ROUTE_MAP[(method, path)]
    return asyncio.run(handler(request))


def _settings_coll():
    return fake_db.dbs["tracking"]["settings"]


def test_R7_auth_dependency():
    _reset_api()
    _enable()
    # No header -> 401
    resp = _call("GET", "/api/librarian/status", _authed_req(token=None))
    assert resp.status_code == 401 and resp.content == {"error": "missing or invalid token"}
    # Non-Bearer -> 401
    resp = _call("GET", "/api/librarian/status",
                 FakeRequest(headers={"Authorization": "Basic abc"}))
    assert resp.status_code == 401
    # Wrong token -> 401
    resp = _call("GET", "/api/librarian/status", _authed_req(token="wrong-token"))
    assert resp.status_code == 401
    # Empty stored hash -> 401 for every token
    _set_settings(librarian_api_token_hash="")
    resp = _call("GET", "/api/librarian/status", _authed_req())
    assert resp.status_code == 401
    # Correct token -> passes
    _set_settings(librarian_api_token_hash=TEST_HASH)
    resp = _call("GET", "/api/librarian/status", _authed_req())
    assert resp.status_code == 200
    # Disabled + correct token -> 403 for park, passes for status/mode
    _set_settings(librarian_enabled=False)
    resp = _call("GET", "/api/librarian/status", _authed_req())
    assert resp.status_code == 200
    resp = _call("GET", "/api/librarian/mode", _authed_req())
    assert resp.status_code == 200
    resp = _call("POST", "/api/librarian/fix",
                 _authed_req(body={"request_id": "u-1"}))
    assert resp.status_code == 403
    assert resp.content == {"state": "disabled", "error": "librarian is disabled"}
    # 401 before 403: disabled + wrong token -> 401
    resp = _call("POST", "/api/librarian/fix",
                 _authed_req(body={"request_id": "u-1"}, token="wrong"))
    assert resp.status_code == 401


def test_R7_compare_digest_used():
    _reset_api()
    _enable()
    calls = []
    original = routes.hmac.compare_digest

    def recording(a, b):
        calls.append((a, b))
        return original(a, b)

    routes.hmac.compare_digest = recording
    try:
        resp = _call("GET", "/api/librarian/status", _authed_req())
        assert resp.status_code == 200
    finally:
        routes.hmac.compare_digest = original
    assert calls and calls[0] == (TEST_HASH, TEST_HASH)


def test_last_seen_stamp_mechanics():
    _reset_api()
    _enable()
    resp = _call("GET", "/api/librarian/status", _authed_req())
    assert resp.status_code == 200
    calls = _settings_coll().update_calls
    assert len(calls) == 1
    filt, update, upsert = calls[0]
    assert filt == {"_id": "app_settings"}
    assert set(update.keys()) == {"$set"}
    assert set(update["$set"].keys()) == {"librarian_agent_last_seen"}
    ts = update["$set"]["librarian_agent_last_seen"]
    assert isinstance(ts, datetime) and ts.tzinfo is not None
    # No SettingsManager.update() from any bot-API request.
    assert FakeSettingsManager.update_calls == []
    # Unauthenticated request stamps nothing.
    _settings_coll().update_calls = []
    resp = _call("GET", "/api/librarian/status", _authed_req(token="wrong"))
    assert resp.status_code == 401
    assert _settings_coll().update_calls == []
    # A stamp failure does not fail the request.
    _settings_coll().raise_on_update = True
    resp = _call("GET", "/api/librarian/status", _authed_req())
    assert resp.status_code == 200


def test_status_and_mode_shapes():
    _reset_api()
    _enable(librarian_mode="ask", librarian_edit_budget_per_day=10,
            librarian_channels=["-100111"],
            librarian_channel_types={"-100111": "tv"})
    _seed_ledger([_ledger_row(msg_id=1), _ledger_row(msg_id=2, status="parked"),
                  _ledger_row(msg_id=3, status="fixed")])
    resp = _call("GET", "/api/librarian/status", _authed_req())
    assert resp.status_code == 200
    body = resp.content
    assert set(body.keys()) == {"enabled", "mode", "test_mode", "writes_enabled",
                                "budget", "channels", "channel_types", "counts",
                                "userbot", "bot"}
    assert body["enabled"] is True and body["mode"] == "ask"
    assert body["test_mode"] is True and body["writes_enabled"] is False
    assert body["budget"] == {"limit": 10, "used_today": 0, "remaining": 10}
    assert body["channels"] == ["-100111"]
    assert body["channel_types"] == {"-100111": "tv"}
    assert body["counts"] == {"waiting": 1, "parked": 1, "escalated": 0, "fixed": 1}
    assert body["userbot"] == "absent" and body["bot"] == "absent"
    # userbot flips with the fake botmod
    STUBS["bot"].Userbot = object()
    resp = _call("GET", "/api/librarian/status", _authed_req())
    assert resp.content["userbot"] == "present"
    # mode: same switch fields minus counts/userbot/bot
    resp = _call("GET", "/api/librarian/mode", _authed_req())
    assert set(resp.content.keys()) == {"enabled", "mode", "test_mode",
                                        "writes_enabled", "budget", "channels",
                                        "channel_types"}
    # budget math floors at 0
    _set_settings(librarian_edit_budget_per_day=1)
    start, end = store._ist_day_bounds_utc(datetime.now(timezone.utc))
    fake_db.dbs["tracking"]["librarian_fixes"].docs.extend([
        _fix_row(request_id="x1", created_at=start + timedelta(minutes=1)),
        _fix_row(request_id="x2", created_at=start + timedelta(minutes=2)),
    ])
    resp = _call("GET", "/api/librarian/status", _authed_req())
    assert resp.content["budget"] == {"limit": 1, "used_today": 2, "remaining": 0}


def test_stub_routes_not_implemented():
    _reset_api()
    _enable()
    # Phase 4 (§4.5.3): undo keeps the stub verbatim; fix and
    # fix-batch are real from Phase 4 and are covered by
    # tests/test_librarian_engine.py instead. Scan was the second
    # stub until Phase 5 Workstream D (§5.2) made it real — in this
    # harness's default settings (test mode on) it now answers the
    # pinned test-mode refusal; its full route coverage lives in
    # tests/test_librarian_scan.py.
    resp = _call("POST", "/api/librarian/undo", _authed_req(body={"request_id": "u-1"}))
    assert resp.status_code == 200
    assert resp.content == {"state": "not_implemented"}
    resp = _call("POST", "/api/librarian/undo", _authed_req(raw_body="{not json"))
    assert resp.status_code == 400
    resp = _call("POST", "/api/librarian/scan", _authed_req(body={}))
    assert resp.status_code == 200
    assert resp.content["state"] == "refused_precondition"
    assert "test mode" in resp.content["error"]
    # Nothing was written anywhere.
    assert fake_db.dbs["tracking"]["reject_ledger"].docs == []
    assert fake_db.dbs["tracking"]["librarian_fixes"].docs == []


# ------------------------------------------------------------------
# Step 4 tests: reads, gateway delegation, scan-status
# ------------------------------------------------------------------

sys.modules["Backend.fastapi.routes.api_routes"] = api_routes

# Scriptable fake gateway (real signatures: gateway.py:687/896/1067).
GW = {"calls": [], "mode": "ok", "result": {"legs": {}, "candidates": []}}


def _gw_reset():
    GW["calls"] = []
    GW["mode"] = "ok"
    GW["result"] = {"legs": {"tmdb": {"state": "ok"}}, "candidates": [{"id": 1}]}


async def _gw_lookup(name, media_type):
    GW["calls"].append(("lookup", name, media_type))
    if GW["mode"] == "value_error":
        raise ValueError("unknown media_type: 'bogus'")
    return GW["result"]


async def _gw_series(ref):
    GW["calls"].append(("series", ref))
    if GW["mode"] == "value_error":
        raise ValueError(f"malformed ref: {ref!r}")
    return GW["result"]


async def _gw_crossid(ref):
    GW["calls"].append(("crossid", ref))
    if GW["mode"] == "value_error":
        raise ValueError(f"malformed ref: {ref!r}")
    return GW["result"]


_gw_mod = types.ModuleType("Backend.helper.librarian_gateway")
_gw_mod.lookup = _gw_lookup
_gw_mod.series = _gw_series
_gw_mod.crossid = _gw_crossid
sys.modules["Backend.helper.librarian_gateway"] = _gw_mod
sys.modules["Backend.helper"].librarian_gateway = _gw_mod


def _seed_storage():
    # Production-shaped seeds (E-4): the seasons/qualities below are
    # byte-equal to the .dict() output of the real models in
    # Backend/helper/modal.py — Season(season_number=…, episodes=[
    # Episode(episode_number=…, title=…, telegram=[QualityDetail(
    # quality=…, id=…, name=…, size=…, parts=[QualityPart(
    # part_number=…, chat_id=…, msg_id=…, size_bytes=…)])])]) —
    # the shape database.py's insert_media actually stores. (The
    # pre-E-4 seeds used invented "season"/"episode" keys, which
    # masked the routes' wrong-key reads.)
    storage = fake_db.dbs["storage_1"]
    storage["movie"].docs.append({
        "title": "Fixture Film", "title_english": None, "original_title": None,
        "release_year": 2020, "imdb_id": "tt0000001", "tmdb_id": 101,
        "kitsu_id": 55,
        "telegram": [{"quality": "1080p", "id": "enc:111:55",
                      "name": "Fixture Film 2020 1080p.mkv",
                      "size": "1.00GB", "group_key": None,
                      "parts": [{"part_number": 1, "chat_id": 111,
                                 "msg_id": 55, "size_bytes": 1073741824}]}],
    })
    storage["tv"].docs.append({
        "title": "Fixture Show", "title_english": None, "original_title": None,
        "release_year": 2021, "imdb_id": "tt0000002", "tmdb_id": 202,
        "kitsu_id": None,
        "seasons": [{"season_number": 1, "episodes": [
            {"episode_number": 2, "title": "Ep Title",
             "episode_backdrop": None, "overview": None, "released": None,
             "absolute_episode": None, "telegram": [
                 {"quality": "720p", "id": "enc:111:56",
                  "name": "Fixture Show S01E02 720p.mkv",
                  "size": "700.00MB", "group_key": None,
                  "parts": [{"part_number": 1, "chat_id": 111,
                             "msg_id": 56, "size_bytes": 734003200}]}]}]}],
    })


def test_waiting_ungrouped_and_grouped():
    _reset_api()
    _enable()
    _seed_ledger([
        _ledger_row(chat_id=-100111, msg_id=55, stem="fixture film",
                    file_name="Fixture Film 2020 1080p.mkv"),
        _ledger_row(chat_id=-100111, msg_id=56, stem="fixture show",
                    file_name="Fixture Show S01E02 720p.mkv"),
        _ledger_row(chat_id=-100111, msg_id=57, stem="fixture show",
                    file_name="Fixture Show S01E03 720p.mkv"),
        _ledger_row(chat_id=-100222, msg_id=58, stem="fixture show",
                    file_name="Fixture Show 1x04.mkv"),
    ])
    resp = _call("GET", "/api/librarian/waiting", _authed_req())
    assert resp.status_code == 200
    assert resp.content["count"] == 4
    keys = [i["key"] for i in resp.content["items"]]
    assert keys == ["-100222:58", "-100111:55", "-100111:56", "-100111:57"]
    item = resp.content["items"][1]
    assert item["stem"] == "fixture film" and item["status"] == "waiting"
    assert item["source_locator"] == {"chat_id": -100111, "msg_id": 55}
    # Grouped, no aliases yet.
    resp = _call("GET", "/api/librarian/waiting", _authed_req(query={"grouped": "true"}))
    groups = resp.content["groups"]
    assert resp.content["count"] == 4 and len(groups) == 3
    show_group = [g for g in groups if g["stem"] == "fixture show" and g["channel_id"] == "-100111"][0]
    assert show_group["count"] == 2
    assert show_group["episode_tokens"] == ["S01E02", "S01E03"]
    assert show_group["tokens_label"] == "unverified — from file names"
    assert show_group["record"] is None
    # Alias upgrade: same record in two channels -> two groups, both upgraded.

    async def seed_alias():
        await store.ensure_indexes()
        rec = await store.upsert_record({
            "canonical_key": "tmdb:202", "ids": {"tmdb": 202},
            "titles": ["Fixture Show"], "media_type": "tv",
            "numbering": {"source": "tmdb", "order": "aired", "offset": None, "note": ""},
            "status": "verified", "related_entries": []})
        await store.set_alias(-100111, "fixture show", rec["_id"])
        await store.set_alias(-100222, "fixture show", rec["_id"])

    run(seed_alias())
    resp = _call("GET", "/api/librarian/waiting", _authed_req(query={"grouped": "true"}))
    upgraded = [g for g in resp.content["groups"] if g["stem"] == "fixture show"]
    assert len(upgraded) == 2
    for g in upgraded:
        assert g["record"] == {"canonical_key": "tmdb:202",
                                "canonical_title": "Fixture Show",
                                "status": "verified"}


def test_find_and_file_reads():
    _reset_api()
    _enable()
    _seed_storage()
    _seed_ledger([_ledger_row(chat_id=-100111, msg_id=55, stem="fixture film")])
    resp = _call("GET", "/api/librarian/find", _authed_req(query={"text": "Fixture Film"}))
    assert resp.status_code == 200
    results = resp.content["results"]
    assert len(results) == 1
    entry = results[0]
    assert entry["title"] == "Fixture Film" and entry["year"] == 2020
    assert entry["media_type"] == "movie" and entry["db_index"] == 1
    assert entry["ids"] == {"imdb": "tt0000001", "tmdb": 101, "kitsu": 55}
    assert entry["qualities"] == [{"quality": "1080p",
                                   "name": "Fixture Film 2020 1080p.mkv",
                                   "size": "1.00GB",
                                   "parts": [{"chat_id": 111, "msg_id": 55}]}]
    resp = _call("GET", "/api/librarian/find", _authed_req(query={"text": "fixture"}))
    tv = [r for r in resp.content["results"] if r["media_type"] == "tv"][0]
    assert tv["seasons"][0]["season"] == 1
    assert tv["seasons"][0]["episodes"][0]["episode"] == 2
    assert tv["seasons"][0]["episodes"][0]["qualities"][0]["name"] == "Fixture Show S01E02 720p.mkv"
    # file: indexed + in ledger
    resp = _call("GET", "/api/librarian/file",
                 _authed_req(query={"chat": "-100111", "msg": "55"}))
    body = resp.content
    assert body["ledger"]["key"] == "-100111:55"
    assert body["indexed"]["media_type"] == "movie"
    assert body["indexed"]["quality"] == "1080p"
    assert body["indexed"]["name"] == "Fixture Film 2020 1080p.mkv"
    assert body["indexed"]["ids"] == {"imdb": "tt0000001", "tmdb": 101}
    # file: indexed TV placement, never rejected -> ledger null
    resp = _call("GET", "/api/librarian/file",
                 _authed_req(query={"chat": "-100111", "msg": "56"}))
    body = resp.content
    assert body["ledger"] is None
    assert body["indexed"]["season"] == 1 and body["indexed"]["episode"] == 2
    assert body["indexed"]["quality"] == "720p"
    # file: unindexed, not in ledger
    resp = _call("GET", "/api/librarian/file",
                 _authed_req(query={"chat": "-100111", "msg": "999"}))
    assert resp.content == {"ledger": None, "indexed": None}
    # file: bad params -> 400
    resp = _call("GET", "/api/librarian/file",
                 _authed_req(query={"chat": "abc", "msg": "1"}))
    assert resp.status_code == 400


def test_file_readback_absolute_filings():
    """Phase 5 B-3 (§3.4; DISP-4 item 3): _find_indexed's TV branch
    surfaces the stored Episode's absolute_episode as `absolute` —
    without it an absolute filing cannot be read back via `file`.
    An episode without one reads back absolute: None; the movie
    branch carries no absolute field."""
    _reset_api()
    _enable()
    _seed_storage()
    storage = fake_db.dbs["storage_1"]
    storage["tv"].docs.append({
        "title": "Absolute Show", "title_english": None,
        "original_title": None, "release_year": 2022,
        "imdb_id": "tt0000003", "tmdb_id": 303, "kitsu_id": None,
        "seasons": [{"season_number": 3, "episodes": [
            {"episode_number": 2, "title": "Absolute Ep Title",
             "episode_backdrop": None, "overview": None, "released": None,
             "absolute_episode": 30, "telegram": [
                 {"quality": "1080p", "id": "enc:111:60",
                  "name": "Absolute Show 030 1080p.mkv",
                  "size": "1.00GB", "group_key": None,
                  "parts": [{"part_number": 1, "chat_id": 111,
                             "msg_id": 60, "size_bytes": 1073741824}]}]}]}]})
    # Absolute filing: absolute is the stored number.
    resp = _call("GET", "/api/librarian/file",
                 _authed_req(query={"chat": "-100111", "msg": "60"}))
    body = resp.content
    assert body["indexed"]["media_type"] == "tv"
    assert body["indexed"]["season"] == 3 and body["indexed"]["episode"] == 2
    assert body["indexed"]["absolute"] == 30
    # Ordinary TV filing (seeded msg 56): absolute is None.
    resp = _call("GET", "/api/librarian/file",
                 _authed_req(query={"chat": "-100111", "msg": "56"}))
    assert resp.content["indexed"]["absolute"] is None
    # Movie branch: unchanged shape, no absolute field.
    resp = _call("GET", "/api/librarian/file",
                 _authed_req(query={"chat": "-100111", "msg": "55"}))
    assert resp.content["indexed"]["media_type"] == "movie"
    assert "absolute" not in resp.content["indexed"]


class FakeTgClient:
    def __init__(self, mode):
        self.mode = mode
        self.calls = []

    async def get_messages(self, chat_id, msg_id):
        self.calls.append((chat_id, msg_id))
        if self.mode == "found":
            media = types.SimpleNamespace(
                file_id="FILEID1", file_name="Some File.mkv",
                file_size=1234, mime_type="video/x-matroska")
            return types.SimpleNamespace(video=media, document=None, text=None)
        if self.mode == "none":
            return None
        if self.mode == "empty":
            return types.SimpleNamespace(video=None, document=None, text=None)
        if self.mode == "raise":
            raise RuntimeError("client exploded")
        if self.mode == "slow":
            import asyncio as _a
            await _a.sleep(5)
            return None
        raise AssertionError(self.mode)


def test_probe_outcomes():
    _reset_api()
    _enable()
    STUBS["bot"].Userbot = FakeTgClient("found")
    STUBS["bot"].StreamBot = FakeTgClient("none")
    resp = _call("GET", "/api/librarian/probe",
                 _authed_req(query={"chat": "-100111", "msg": "55"}))
    body = resp.content
    assert body["userbot"] == {"state": "found", "file_id": "FILEID1",
                               "media": {"file_name": "Some File.mkv", "size": 1234,
                                         "mime_type": "video/x-matroska"}}
    assert body["bot"] == {"state": "missing"}
    # Empty message -> missing; raise -> error; both clients consulted.
    STUBS["bot"].Userbot = FakeTgClient("empty")
    STUBS["bot"].StreamBot = FakeTgClient("raise")
    resp = _call("GET", "/api/librarian/probe",
                 _authed_req(query={"chat": "-100111", "msg": "55"}))
    assert resp.content["userbot"] == {"state": "missing"}
    assert resp.content["bot"]["state"] == "error"
    assert "client exploded" in resp.content["bot"]["error"]
    # Absent clients -> per-client error, never a crash.
    STUBS["bot"].Userbot = None
    STUBS["bot"].StreamBot = None
    resp = _call("GET", "/api/librarian/probe",
                 _authed_req(query={"chat": "-100111", "msg": "55"}))
    assert resp.content["userbot"] == {"state": "error", "error": "userbot not connected"}
    assert resp.content["bot"] == {"state": "error", "error": "bot client not connected"}
    # Timeout -> error (shortened test timeout).
    original = routes.PROBE_TIMEOUT
    routes.PROBE_TIMEOUT = 0.05
    try:
        STUBS["bot"].Userbot = FakeTgClient("slow")
        STUBS["bot"].StreamBot = FakeTgClient("none")
        resp = _call("GET", "/api/librarian/probe",
                     _authed_req(query={"chat": "-100111", "msg": "55"}))
        assert resp.content["userbot"]["state"] == "error"
        assert resp.content["bot"] == {"state": "missing"}
    finally:
        routes.PROBE_TIMEOUT = original


def test_history_route():
    _reset_api()
    _enable()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fake_db.dbs["tracking"]["librarian_fixes"].docs.extend([
        _fix_row(request_id="a", created_at=t0 + timedelta(hours=1)),
        _fix_row(request_id="b", created_at=t0 + timedelta(hours=2), batch_id="batch-9"),
    ])
    resp = _call("GET", "/api/librarian/history", _authed_req())
    assert resp.content["count"] == 2
    assert [i["request_id"] for i in resp.content["items"]] == ["b", "a"]
    item = resp.content["items"][0]
    assert item["ledger_key"] == "-100111:1" and item["batch_id"] == "batch-9"
    resp = _call("GET", "/api/librarian/history", _authed_req(query={"batch": "batch-9"}))
    assert resp.content["count"] == 1


def test_gateway_delegation():
    _reset_api()
    _enable()
    _gw_reset()
    resp = _call("POST", "/api/librarian/lookup",
                 _authed_req(body={"name": "Fixture", "media_type": "tv"}))
    assert resp.status_code == 200 and resp.content == GW["result"]
    assert GW["calls"] == [("lookup", "Fixture", "tv")]
    # media_type omitted -> "unknown" passed to the gateway
    _gw_reset()
    resp = _call("POST", "/api/librarian/lookup", _authed_req(body={"name": "Fixture"}))
    assert GW["calls"] == [("lookup", "Fixture", "unknown")]
    # bad media_type -> 400 without calling the gateway
    _gw_reset()
    resp = _call("POST", "/api/librarian/lookup",
                 _authed_req(body={"name": "Fixture", "media_type": "western"}))
    assert resp.status_code == 400 and GW["calls"] == []
    # gateway ValueError -> 400
    GW["mode"] = "value_error"
    resp = _call("POST", "/api/librarian/series", _authed_req(body={"ref": "bogus"}))
    assert resp.status_code == 400
    resp = _call("POST", "/api/librarian/crossid", _authed_req(body={"ref": "bogus"}))
    assert resp.status_code == 400
    GW["mode"] = "ok"
    resp = _call("POST", "/api/librarian/series", _authed_req(body={"ref": "tmdb:202"}))
    assert resp.status_code == 200 and resp.content == GW["result"]
    resp = _call("POST", "/api/librarian/crossid", _authed_req(body={"ref": "tmdb:202"}))
    assert resp.status_code == 200 and resp.content == GW["result"]
    # missing ref -> 400
    resp = _call("POST", "/api/librarian/series", _authed_req(body={}))
    assert resp.status_code == 400


def test_scan_status_route():
    _reset_api()
    _enable()
    resp = _call("GET", "/api/librarian/scan-status", _authed_req())
    assert resp.content == {"state": "never_run"}
    fake_scan_manager.state["status"] = "completed"
    fake_scan_manager.state["started_at"] = 123.0
    resp = _call("GET", "/api/librarian/scan-status", _authed_req())
    assert resp.content["state"] == "completed"
    assert resp.content["scan"]["status"] == "completed"


# ------------------------------------------------------------------
# Step 5 tests: transitions, records, aliases, R6 token action
# ------------------------------------------------------------------

def test_transitions_via_routes():
    _reset_api()
    _enable()
    _seed_ledger([
        _ledger_row(msg_id=1),
        _ledger_row(msg_id=2, status="fixed"),
        _ledger_row(msg_id=3, status="parked"),
    ])
    # Happy paths
    resp = _call("POST", "/api/librarian/park",
                 _authed_req(body={"key": "-100111:1", "reason": "held for review"}))
    assert resp.status_code == 200
    assert resp.content == {"state": "parked", "key": "-100111:1", "status": "parked"}
    stored = fake_db.dbs["tracking"]["reject_ledger"].docs[0]
    assert stored["park_reason"] == "held for review"
    resp = _call("POST", "/api/librarian/unpark", _authed_req(body={"key": "-100111:1"}))
    assert resp.content == {"state": "waiting", "key": "-100111:1", "status": "waiting"}
    resp = _call("POST", "/api/librarian/escalate",
                 _authed_req(body={"key": "-100111:3", "reason": "ghost suspected"}))
    assert resp.content["state"] == "escalated"
    resp = _call("POST", "/api/librarian/unpark", _authed_req(body={"key": "-100111:3"}))
    assert resp.content["state"] == "waiting"
    # Wrong status -> 200 refused_wrong_status naming the current status
    for path, body, current in (
        ("/api/librarian/park", {"key": "-100111:2", "reason": "x"}, "fixed"),
        ("/api/librarian/unpark", {"key": "-100111:1"}, "waiting"),
        ("/api/librarian/escalate", {"key": "-100111:2", "reason": "x"}, "fixed"),
    ):
        resp = _call("POST", path, _authed_req(body=body))
        assert resp.status_code == 200, path
        assert resp.content["state"] == "refused_wrong_status", path
        assert resp.content["status"] == current, path
        assert current in resp.content["error"], path
    # Empty reason -> 400; bad key -> 400; unknown key -> 404
    resp = _call("POST", "/api/librarian/park",
                 _authed_req(body={"key": "-100111:1", "reason": "  "}))
    assert resp.status_code == 400
    resp = _call("POST", "/api/librarian/park",
                 _authed_req(body={"key": "nonsense", "reason": "x"}))
    assert resp.status_code == 400
    resp = _call("POST", "/api/librarian/park",
                 _authed_req(body={"key": "-100111:999", "reason": "x"}))
    assert resp.status_code == 404


_RECORD_PAYLOAD = {
    "canonical_key": "tmdb:202",
    "ids": {"tmdb": 202, "imdb": "tt0000002"},
    "titles": ["Fixture Show", "Fixture Show Alt"],
    "media_type": "tv",
    "numbering": {"source": "tmdb", "order": "aired", "offset": None, "note": "proof"},
    "status": "verified",
    "related_entries": [],
}


def test_record_set_validation_table():
    _reset_api()
    _enable()

    def expect_400(mutate):
        payload = copy.deepcopy(_RECORD_PAYLOAD)
        mutate(payload)
        resp = _call("POST", "/api/librarian/record-set", _authed_req(body=payload))
        assert resp.status_code == 400, (payload, resp.content)
        assert resp.content.get("error"), payload

    expect_400(lambda p: p.update({"_id": "a" * 24}))
    expect_400(lambda p: p.update({"created_at": "2026-01-01"}))
    expect_400(lambda p: p.pop("canonical_key"))
    expect_400(lambda p: p.update({"canonical_key": "  "}))
    expect_400(lambda p: p.update({"ids": {}}))
    expect_400(lambda p: p.update({"ids": {"tmdb": "202"}}))
    expect_400(lambda p: p.update({"ids": {"tmdb": True}}))
    expect_400(lambda p: p.update({"ids": {"imdb": 42}}))
    expect_400(lambda p: p.update({"ids": {"bogus": 1}}))
    expect_400(lambda p: p.update({"titles": []}))
    expect_400(lambda p: p.update({"media_type": "western"}))
    expect_400(lambda p: p["numbering"].update({"source": "kitsu"}))
    expect_400(lambda p: p["numbering"].update({"order": "random"}))
    expect_400(lambda p: p["numbering"].update({"offset": "1"}))
    expect_400(lambda p: p["numbering"].pop("note"))
    expect_400(lambda p: p.update({"status": "maybe"}))
    expect_400(lambda p: p.update({"related_entries": ["not-hex"]}))
    assert fake_db.dbs["tracking"]["series_records"].docs == []


def test_record_roundtrip_and_aliases():
    _reset_api()
    _enable()
    resp = _call("POST", "/api/librarian/record-set",
                 _authed_req(body=copy.deepcopy(_RECORD_PAYLOAD)))
    assert resp.status_code == 200 and resp.content["found"] is True
    created = resp.content["record"]
    assert created["canonical_key"] == "tmdb:202"
    assert created["titles"] == ["Fixture Show", "Fixture Show Alt"]

    def get(**body):
        r = _call("POST", "/api/librarian/record-get", _authed_req(body=body))
        assert r.status_code == 200
        return r.content

    by_id = get(ref="tmdb:202")
    by_key = get(ref="tmdb:202")
    assert by_id["found"] is True and by_key["found"] is True
    assert by_id["record"] == created and by_key["record"] == created
    by_imdb = get(ref="imdb:tt0000002")
    assert by_imdb["record"] == created
    assert get(ref="tmdb:999") == {"found": False}
    # Alias round-trip
    resp = _call("POST", "/api/librarian/alias-set",
                 _authed_req(body={"channel": "-100111", "stem": "fixture show",
                                   "record": "tmdb:202"}))
    assert resp.status_code == 200
    assert resp.content["canonical_key"] == "tmdb:202"
    assert resp.content["channel"] == "-100111"
    by_alias = get(channel="-100111", stem="fixture show")
    assert by_alias["record"] == created
    resp = _call("GET", "/api/librarian/aliases", _authed_req())
    assert resp.content["aliases"] == [{
        "channel": "-100111", "stem": "fixture show",
        "record_id": created["_id"], "canonical_key": "tmdb:202"}]
    resp = _call("GET", "/api/librarian/aliases",
                 _authed_req(query={"channel": "-100222"}))
    assert resp.content["aliases"] == []
    # alias-set with unknown record -> 404
    resp = _call("POST", "/api/librarian/alias-set",
                 _authed_req(body={"channel": "-100111", "stem": "x",
                                   "record": "tmdb:999"}))
    assert resp.status_code == 404
    # forget -> gone; forget again -> forgotten false
    resp = _call("POST", "/api/librarian/alias-forget",
                 _authed_req(body={"channel": "-100111", "stem": "fixture show"}))
    assert resp.content["forgotten"] is True
    resp = _call("GET", "/api/librarian/aliases", _authed_req())
    assert resp.content["aliases"] == []
    resp = _call("POST", "/api/librarian/alias-forget",
                 _authed_req(body={"channel": "-100111", "stem": "fixture show"}))
    assert resp.content == {"forgotten": False, "channel": "-100111",
                            "stem": "fixture show"}


def test_record_get_canonical_key_fallback():
    # Deviation (b) regression: _resolve_record tries the provider-id
    # lookup first and falls back to canonical_key. The round-trip
    # test above cannot catch a broken fallback because its
    # canonical_key ("tmdb:202") IS its provider id. Here the
    # canonical_key's provider prefix is ABSENT from ids, so only
    # the fallback can find the record.
    _reset_api()
    _enable()
    payload = {
        "canonical_key": "tvdb:999001",
        "ids": {"tmdb": 555001},
        "titles": ["Fallback Show"],
        "media_type": "tv",
        "numbering": {"source": "tvdb", "order": "aired",
                      "offset": None, "note": "fallback proof"},
        "status": "verified",
        "related_entries": [],
    }
    resp = _call("POST", "/api/librarian/record-set",
                 _authed_req(body=copy.deepcopy(payload)))
    assert resp.status_code == 200 and resp.content["found"] is True
    created = resp.content["record"]
    assert created["canonical_key"] == "tvdb:999001"
    assert created["ids"] == {"tmdb": 555001}

    def get(**body):
        r = _call("POST", "/api/librarian/record-get", _authed_req(body=body))
        assert r.status_code == 200
        return r.content

    # Provider-id lookup still works for the id that IS present.
    by_tmdb = get(ref="tmdb:555001")
    assert by_tmdb["found"] is True and by_tmdb["record"] == created
    # Fallback firing: no record has ids.tvdb == 999001, so a
    # provider-id lookup alone must miss; only canonical_key finds it.
    by_key = get(ref="tvdb:999001")
    assert by_key["found"] is True and by_key["record"] == created
    # Negative controls: nonexistent provider id AND nonexistent key.
    assert get(ref="tvdb:999002") == {"found": False}
    assert get(ref="tmdb:999999") == {"found": False}
    # Alias path: alias-set via the canonical key (also goes through
    # _resolve_record's fallback), then record-get by channel+stem
    # resolves to the same field-identical record.
    resp = _call("POST", "/api/librarian/alias-set",
                 _authed_req(body={"channel": "-100111", "stem": "fallback show",
                                   "record": "tvdb:999001"}))
    assert resp.status_code == 200
    assert resp.content["canonical_key"] == "tvdb:999001"
    by_alias = get(channel="-100111", stem="fallback show")
    assert by_alias["found"] is True and by_alias["record"] == created


def test_R6_token_action():
    _reset_api()
    _enable(token_hash="")
    # The route is bound to the session dependency (require_auth).
    handler = ROUTE_MAP[("POST", "/admin/librarian/token/generate")]
    defaults = handler.__defaults__ or ()
    deps = [d for d in defaults if isinstance(d, _Depends)]
    assert deps and deps[0].dependency is STUBS["credentials"].require_auth
    # Unauthenticated session -> the dependency raises 401.
    try:
        run(STUBS["credentials"].require_auth(FakeRequest(session={})))
        raise AssertionError("expected 401 from require_auth")
    except HTTPException as e:
        assert e.status_code == 401
    # Authenticated -> 200, plaintext once, only the hash persists.
    import asyncio as _a
    resp = _a.run(handler(FakeRequest(session={"authenticated": True})))
    assert resp.status_code == 200
    token1 = resp.content["token"]
    assert token1 and isinstance(token1, str)
    hash1 = hashlib.sha256(token1.encode("utf-8")).hexdigest()
    assert FakeSettingsManager.update_calls == [{"librarian_api_token_hash": hash1}]
    assert FakeSettingsManager.current().librarian_api_token_hash == hash1
    # The plaintext appears in no other response.
    _set_settings(librarian_enabled=True)
    status_resp = _call("GET", "/api/librarian/status", _authed_req(token=token1))
    assert status_resp.status_code == 200
    assert token1 not in json.dumps(status_resp.content)

    async def surfaces():
        settings_resp = await api_routes.get_settings_api()
        assert token1 not in json.dumps(settings_resp)
        export = await backup.export_config()
        assert token1 not in json.dumps(export)
        assert hash1 not in json.dumps(export)

    run(surfaces())
    # Rotation: old rejected, new accepted, only the new hash persists.
    resp = _a.run(handler(FakeRequest(session={"authenticated": True})))
    token2 = resp.content["token"]
    assert token2 != token1
    hash2 = hashlib.sha256(token2.encode("utf-8")).hexdigest()
    assert FakeSettingsManager.current().librarian_api_token_hash == hash2
    assert _call("GET", "/api/librarian/status", _authed_req(token=token1)).status_code == 401
    assert _call("GET", "/api/librarian/status", _authed_req(token=token2)).status_code == 200


# ------------------------------------------------------------------
# Step 6 tests: page handler + operator actions (§6.8, §6.10)
# ------------------------------------------------------------------

def _page_ctx(query=None):
    handler = ROUTE_MAP[("GET", "/admin/librarian")]
    import asyncio as _a
    req = FakeRequest(query=query or {}, session={"authenticated": True})
    resp = _a.run(handler(req))
    return resp.context


def test_page_handler_context():
    _reset_api()
    _enable(librarian_channels=["-100111"],
            librarian_channel_types={"-100111": "tv"})
    _set_settings(channel_titles={"-100111": "Fixture Channel"})
    _seed_ledger([
        _ledger_row(msg_id=1, stem="alpha show", file_name="Alpha Show S01E01.mkv"),
        _ledger_row(msg_id=2, stem="alpha show", file_name="Alpha Show S01E02.mkv"),
        _ledger_row(msg_id=3, stem="beta show", file_name="Beta Show S01E01.mkv"),
        _ledger_row(msg_id=4, status="escalated", stem="gamma",
                    file_name="Gamma.mkv", escalation_reason="ghost suspected"),
        _ledger_row(msg_id=5, status="parked", stem="delta"),
    ])
    # Stamp lives in the settings DOCUMENT; the snapshot must not win.
    stamp = datetime(2026, 11, 20, 4, 0, tzinfo=timezone.utc)  # 09:30 IST
    fake_db.dbs["tracking"]["settings"].docs.append(
        {"_id": "app_settings", "librarian_agent_last_seen": stamp})
    ctx = _page_ctx()
    assert ctx["counts"] == {"waiting": 3, "parked": 1, "escalated": 1, "fixed": 0}
    assert ctx["ops_today_total"] == 0 and ctx["ops_today_fixed"] == 0
    assert ctx["agent_last_seen"] == "20 Nov 2026, 09:30 IST"
    assert ctx["librarian_enabled"] is True
    # Groups sorted count-desc then stem; record-less groups carry the stem.
    assert [g["stem"] for g in ctx["groups"]] == ["alpha show", "beta show"]
    g = ctx["groups"][0]
    assert g["count"] == 2 and g["channel_name"] == "Fixture Channel"
    assert g["episode_tokens"] == ["S01E01", "S01E02"]
    assert g["files"][0]["key"] == "-100111:1"
    assert ctx["page"] == 1 and ctx["total_pages"] == 1
    # Needs Yan: reason verbatim, channel name resolved.
    assert ctx["escalated"] == [{
        "key": "-100111:4", "file_name": "Gamma.mkv", "channel_id": "-100111",
        "channel_name": "Fixture Channel", "escalation_reason": "ghost suspected"}]
    assert ctx["channels"] == [{"channel_id": "-100111",
                                 "channel_name": "Fixture Channel", "type": "tv"}]
    assert ctx["records"] == [] and ctx["aliases"] == []
    # No stamp in the document -> agent line omitted (None).
    fake_db.dbs["tracking"]["settings"].docs.clear()
    ctx = _page_ctx()
    assert ctx["agent_last_seen"] is None
    # Disabled -> page still renders, librarian_enabled False.
    _set_settings(librarian_enabled=False)
    ctx = _page_ctx()
    assert ctx["librarian_enabled"] is False
    assert ctx["counts"]["waiting"] == 3


def test_page_pagination_whole_groups():
    _reset_api()
    _enable()
    rows = []
    msg = 0
    for stem, n in (("g-one", 30), ("g-two", 30), ("g-three", 10)):
        for _ in range(n):
            msg += 1
            rows.append(_ledger_row(msg_id=msg, stem=stem,
                                    file_name=f"{stem} E{msg}.mkv"))
    _seed_ledger(rows)
    ctx = _page_ctx()
    assert ctx["total_pages"] == 2
    page1_files = sum(g["count"] for g in ctx["groups"])
    assert page1_files == 30  # whole groups only: 30, next group would exceed 50
    ctx2 = _page_ctx(query={"page": "2"})
    assert sum(g["count"] for g in ctx2["groups"]) == 40
    assert ctx2["page"] == 2


def test_page_action_operator():
    _reset_api()
    _enable()
    _seed_ledger([_ledger_row(msg_id=1), _ledger_row(msg_id=2, status="fixed")])
    handler = ROUTE_MAP[("POST", "/admin/librarian/action")]
    import asyncio as _a

    def act(body):
        req = FakeRequest(body=body, session={"authenticated": True})
        return _a.run(handler(req))

    resp = act({"action": "park", "key": "-100111:1", "reason": "held"})
    assert resp.status_code == 200 and resp.content["state"] == "parked"
    resp = act({"action": "park", "key": "-100111:2", "reason": "held"})
    assert resp.content["state"] == "refused_wrong_status"
    assert resp.content["status"] == "fixed"
    resp = act({"action": "bogus", "key": "-100111:1"})
    assert resp.status_code == 400
    _set_settings(librarian_enabled=False)
    resp = act({"action": "unpark", "key": "-100111:1"})
    assert resp.status_code == 403


def test_page_size_measurement():
    """§10.5: seeded 50-file page. The suite env has no jinja2, so the
    real-template render measurement is recorded from the frontend
    engineer's seeded render (content block 25,286 bytes < 51,200;
    total 79,692 bytes — base.html alone renders 53,046 bytes with an
    empty content block, so the total is over the §13 gate on the
    total basis; a tech-lead ruling on the measurement basis is
    pending and the total is NOT hard-asserted here). In-suite, this
    test asserts the structural bound the handler controls: the
    first page carries at most 50 files, and — when jinja2 is
    available — renders the real template and asserts the content
    block size."""
    _reset_api()
    _enable()
    rows = []
    for i in range(1, 51):
        stem = ("stem-alpha", "stem-beta", "stem-gamma")[(i - 1) % 3]
        rows.append(_ledger_row(msg_id=i, stem=stem,
                                file_name=f"Seeded Show S01E{i:02d} 1080p.mkv"))
    _seed_ledger(rows)
    _seed_ledger([_ledger_row(msg_id=90, status="escalated", stem="esc",
                              file_name="Esc.mkv", escalation_reason="check me"),
                  _ledger_row(msg_id=91, status="escalated", stem="esc",
                              file_name="Esc2.mkv", escalation_reason="check me too")])
    ctx = _page_ctx()
    total_files = sum(g["count"] for g in ctx["groups"])
    assert total_files <= 50, total_files
    try:
        import jinja2  # noqa: F401
    except ImportError:
        print("NOTE §10.5: jinja2 unavailable in suite env — real-template "
              "render measurement deferred to the §11 staging exercise; "
              "frontend-recorded content block = 25,286 bytes (< 51,200), "
              "total = 79,692 bytes (not asserted, ruling pending).")
        return
    # jinja2 available: render the real librarian.html content block.
    import jinja2
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(
        str(ROOT / "Backend/fastapi/templates")))
    env.loader = jinja2.DictLoader({
        "base.html": "{% block content %}{% endblock %}",
        "librarian.html": (ROOT / "Backend/fastapi/templates/librarian.html").read_text(encoding="utf-8"),
    })
    template = env.get_template("librarian.html")
    html = template.render(**ctx)
    size = len(html.encode("utf-8"))
    print(f"§10.5 content-block render: {size} bytes")
    assert size < 51200, size


# ------------------------------------------------------------------
# Step 7 tests: CLI (§10.6) + static (§10.7)
# ------------------------------------------------------------------

import io
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout

cli = load_module("librarian_cli_under_test", "tools/librarian_cli.py")


def _run_cli(argv, http_result=None, http_error=None, token="tok"):
    calls = []

    def fake_http(method, path, payload=None):
        calls.append((method, path, payload))
        if http_error is not None:
            raise http_error
        return http_result

    original_http = cli._http
    cli._http = fake_http
    old_token = os.environ.get("LIBRARIAN_API_TOKEN")
    if token is None:
        os.environ.pop("LIBRARIAN_API_TOKEN", None)
    else:
        os.environ["LIBRARIAN_API_TOKEN"] = token
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
    finally:
        cli._http = original_http
        if old_token is None:
            os.environ.pop("LIBRARIAN_API_TOKEN", None)
        else:
            os.environ["LIBRARIAN_API_TOKEN"] = old_token
    return code, out.getvalue(), err.getvalue(), calls


def test_cli_command_map():
    cases = [
        (["status"], ("GET", "/api/librarian/status", None)),
        (["mode"], ("GET", "/api/librarian/mode", None)),
        (["scan-status"], ("GET", "/api/librarian/scan-status", None)),
        (["waiting"], ("GET", "/api/librarian/waiting", None)),
        (["waiting", "--grouped", "--channel", "-100111", "--limit", "5"],
         ("GET", "/api/librarian/waiting?channel=-100111&limit=5&grouped=true", None)),
        (["find", "Fixture Film"],
         ("GET", "/api/librarian/find?text=Fixture+Film", None)),
        (["file", "-100111", "55"],
         ("GET", "/api/librarian/file?chat=-100111&msg=55", None)),
        (["probe", "-100111", "55"],
         ("GET", "/api/librarian/probe?chat=-100111&msg=55", None)),
        (["history", "--today", "--stem", "some show"],
         ("GET", "/api/librarian/history?stem=some+show&today=true", None)),
        (["lookup", "Fixture"], ("POST", "/api/librarian/lookup", {"name": "Fixture"})),
        (["lookup", "Fixture", "--type", "tv"],
         ("POST", "/api/librarian/lookup", {"name": "Fixture", "media_type": "tv"})),
        (["series", "tmdb:202"], ("POST", "/api/librarian/series", {"ref": "tmdb:202"})),
        (["crossid", "tmdb:202"], ("POST", "/api/librarian/crossid", {"ref": "tmdb:202"})),
        (["record-get", "tmdb:202"],
         ("POST", "/api/librarian/record-get", {"ref": "tmdb:202"})),
        (["record-get", "--channel", "-100111", "--stem", "some show"],
         ("POST", "/api/librarian/record-get",
          {"channel": "-100111", "stem": "some show"})),
        (["aliases"], ("GET", "/api/librarian/aliases", None)),
        (["aliases", "--channel", "-100111"],
         ("GET", "/api/librarian/aliases?channel=-100111", None)),
        (["alias-set", "-100111", "some show", "tmdb:202"],
         ("POST", "/api/librarian/alias-set",
          {"channel": "-100111", "stem": "some show", "record": "tmdb:202"})),
        (["alias-forget", "-100111", "some show"],
         ("POST", "/api/librarian/alias-forget",
          {"channel": "-100111", "stem": "some show"})),
        (["park", "-100111:55", "--reason", "held"],
         ("POST", "/api/librarian/park", {"key": "-100111:55", "reason": "held"})),
        (["unpark", "-100111:55"],
         ("POST", "/api/librarian/unpark", {"key": "-100111:55"})),
        (["escalate", "-100111:55", "--reason", "ghost"],
         ("POST", "/api/librarian/escalate", {"key": "-100111:55", "reason": "ghost"})),
        (["undo", "-100111", "55"],
         ("POST", "/api/librarian/undo", {"chat_id": -100111, "msg_id": 55})),
        (["undo", "-100111", "55", "--request-id", "u-1"],
         ("POST", "/api/librarian/undo",
          {"chat_id": -100111, "msg_id": 55, "request_id": "u-1"})),
        (["undo", "-100111", "55", "--target-request-id", "fix-1"],
         ("POST", "/api/librarian/undo",
          {"chat_id": -100111, "msg_id": 55, "target_request_id": "fix-1"})),
        (["undo", "-100111", "55", "--request-id", "u-1",
          "--target-request-id", "fix-1"],
         ("POST", "/api/librarian/undo",
          {"chat_id": -100111, "msg_id": 55, "request_id": "u-1",
           "target_request_id": "fix-1"})),
        (["scan"], ("POST", "/api/librarian/scan", {})),
        (["scan", "--channel", "-100111"],
         ("POST", "/api/librarian/scan", {"channel": "-100111"})),
    ]
    for argv, expected in cases:
        code, out, err, calls = _run_cli(argv, http_result=(200, "{}"))
        assert code == 0, (argv, code, err)
        assert calls == [expected], (argv, calls, expected)


def test_cli_undo_target_request_id_parsing():
    # Phase 5 C-2 (plan §4.3, DISP-8, OQ-1 (a)): --request-id is the
    # undo's OWN idempotency key; --target-request-id optionally
    # names the fix to undo. Absent keys are omitted from the
    # payload, exactly as the pre-C-2 dispatch omitted request_id.
    # Flag order does not matter (both orders dispatch identically).
    code, out, err, calls = _run_cli(
        ["undo", "-100111", "55", "--target-request-id", "fix-1",
         "--request-id", "u-1"], http_result=(200, "{}"))
    assert code == 0, (code, err)
    assert calls == [("POST", "/api/librarian/undo",
                      {"chat_id": -100111, "msg_id": 55,
                       "request_id": "u-1", "target_request_id": "fix-1"})]
    # The flag requires a value: a trailing --target-request-id is a
    # local usage error (exit 3) and no request is dispatched.
    code, out, err, calls = _run_cli(
        ["undo", "-100111", "55", "--target-request-id"],
        http_result=(200, "{}"))
    assert code == 3 and calls == [] and "--target-request-id" in err
    # The usage line carries the new flag in its pinned form.
    assert ("undo <chat> <msg> [--request-id <uuid>] "
            "[--target-request-id <uuid>]") in cli.USAGE


def test_cli_exit_codes_and_verbatim_output():
    body = '{"state": "ok", "note": "line1\nline2 \"quoted\" <b>html</b>"}'
    code, out, err, _ = _run_cli(["status"], http_result=(200, body))
    assert code == 0 and out == body  # byte-identical, never re-serialised
    for status in (401, 403, 400, 404):
        code, out, err, _ = _run_cli(["status"], http_result=(status, body))
        assert code == 2 and out == body, status
    code, out, err, _ = _run_cli(["status"], http_error=cli.TransportError("boom"))
    assert code == 3 and out == "" and "boom" in err
    code, out, err, calls = _run_cli(["status"], token=None)
    assert code == 3 and calls == [] and "LIBRARIAN_API_TOKEN is not set" in err
    code, out, err, _ = _run_cli(["bogus-command"], http_result=(200, "{}"))
    assert code == 3 and out == ""


def test_cli_json_file_byte_exact():
    payload = {
        "request_id": "u-1", "chat_id": -100111, "msg_id": 55,
        "expected_caption": "Old <b>caption</b>\nwith \"quotes\"",
        "new_caption": "New <b>caption</b>\nsecond line",
        "expected_ids": {"tmdb": 202},
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as fh:
        json.dump(payload, fh)
        path = fh.name
    try:
        code, out, err, calls = _run_cli(["fix", "--json-file", path],
                                         http_result=(200, '{"state": "not_implemented"}'))
        assert code == 0
        assert calls == [("POST", "/api/librarian/fix", payload)]
        code, out, err, calls = _run_cli(["record-set", "--json-file", path],
                                         http_result=(200, "{}"))
        assert calls == [("POST", "/api/librarian/record-set", payload)]
    finally:
        os.unlink(path)
    # stdin form
    original_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    try:
        code, out, err, calls = _run_cli(["fix-batch", "--json-file", "-"],
                                         http_result=(200, "{}"))
        assert calls == [("POST", "/api/librarian/fix-batch", payload)]
    finally:
        sys.stdin = original_stdin


def test_static_phase3_registrations():
    reapply = (ROOT / "tools/reapply.sh").read_text(encoding="utf-8")
    for rel in (
        "Backend/fastapi/librarian_routes.py",
        "Backend/fastapi/templates/librarian.html",
        "tools/librarian_cli.py",
        "tests/test_librarian_api.py",
        "tools/media-manager-profile/SOUL.md",
        "tools/media-manager-profile/AGENTS.md",
        "tools/media-manager-profile/MEMORY.md",
        "tools/media-manager-profile/INSTALL.md",
        "tools/media-manager-profile/skills/librarian-toolbox/SKILL.md",
        "tools/media-manager-profile/skills/provider-research/SKILL.md",
        "tools/media-manager-profile/skills/episode-verification/SKILL.md",
        "tools/media-manager-profile/skills/anime-donghua-numbering/SKILL.md",
        "tools/media-manager-profile/skills/caption-writing/SKILL.md",
        "tools/media-manager-profile/skills/series-records/SKILL.md",
        "tools/media-manager-profile/skills/failure-recovery/SKILL.md",
        "tools/media-manager-profile/skills/scope-and-reporting/SKILL.md",
        "tools/media-manager-profile/skills/recognition-and-context/SKILL.md",
    ):
        assert rel in reapply, rel
        assert (ROOT / rel).exists(), rel
    run_all_text = (ROOT / "tests/run_all.py").read_text(encoding="utf-8")
    assert '"test_librarian_api"' in run_all_text
    manifest = (ROOT / "tools/seam_manifest.txt").read_text(encoding="utf-8")
    assert "Backend/helper/backup.py" in manifest.splitlines()
    toolbox = (ROOT / "tools/media-manager-profile/skills/librarian-toolbox/SKILL.md").read_text(encoding="utf-8")
    assert "## Argument encodings (pinned in Phase 3)" in toolbox
    for form in ("fix --json-file", "fix-batch --json-file",
                 "record-set --json-file", "record-get", "alias-set"):
        assert form in toolbox, form


def test_no_series_names_in_phase3_production_files():
    fixture_titles = ("Harbour Lights", "The Last Ferry",
                      "Star Blossom Academy", "River of Clouds",
                      "Fixture Film", "Fixture Show")
    for rel in ("Backend/fastapi/librarian_routes.py",
                "Backend/helper/librarian_store.py",
                "tools/librarian_cli.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for title in fixture_titles:
            assert title not in text, (rel, title)
