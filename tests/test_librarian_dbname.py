"""Phase 4 Workstream B tests — the ``TS_DB_NAME`` env override
(DESIGN §13 Phase 4 staging mechanic; security pre-review B-1…B-7).

Loads the REAL ``Backend/helper/database.py`` by file path (the repo
harness pattern) and exercises ``Database`` construction only — no
``connect()`` — against stub modules whose shapes mirror the real
tree:

- ``Backend.config`` — ``Telegram.DATABASE`` carries two dummy URIs
  so the ≥ 2 check in ``Database.__init__`` passes and name
  resolution is what is exercised (plan §3.3).
- ``Backend.helper.bounded`` — the REAL module, loaded by path
  (it is import-free; ``database.py`` instantiates ``BoundedDict``
  at module level).
- ``motor`` / ``pydantic`` / ``pymongo`` / ``bson`` — only the names
  ``database.py`` imports, with the real shapes (``ObjectId`` with
  the real 24-hex validation, copied from the Phase 3 suite).
- ``Backend.helper.modal`` / ``.encrypt`` / ``.task_manager`` /
  ``.settings_manager`` / ``Backend.logger`` — the exact attribute
  names ``database.py`` binds at import; none is called during
  construction.

The B-3 settings-payload regression lives in
``tests/test_librarian_api.py`` (plan §3.3's named fallback: the
real ``update_settings_api`` needs that suite's full stub
inventory, which is impractical to duplicate here).
"""

import logging
import os
import sys
import types
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_module  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

ENV_KEY = "TS_DB_NAME"
_DUMMY_URIS = [
    "mongodb://user:pass@host1.example/dbname-tests",
    "mongodb://user:pass@host2.example/dbname-tests",
]


# ------------------------------------------------------------------
# bson.ObjectId (real validation semantics, as in the Phase 3 suite)
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


# ------------------------------------------------------------------
# Stub installation (additive: never clobbers a sibling suite's stub;
# only fills the names database.py binds that are not yet present)
# ------------------------------------------------------------------

def _ensure_module(name):
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    return mod


def _install_stubs():
    # motor — database.py binds AsyncIOMotorClient/Database names in
    # annotations; construction never instantiates a client.
    motor = _ensure_module("motor")
    motor_asyncio = _ensure_module("motor.motor_asyncio")
    if not hasattr(motor_asyncio, "AsyncIOMotorClient"):
        motor_asyncio.AsyncIOMotorClient = type("AsyncIOMotorClient", (), {})
    if not hasattr(motor_asyncio, "AsyncIOMotorDatabase"):
        motor_asyncio.AsyncIOMotorDatabase = type("AsyncIOMotorDatabase", (), {})
    motor.motor_asyncio = motor_asyncio

    # pydantic — only ValidationError is imported.
    pydantic = _ensure_module("pydantic")
    if not hasattr(pydantic, "ValidationError"):
        pydantic.ValidationError = type("ValidationError", (Exception,), {})

    # pymongo — ASCENDING/DESCENDING constants only.
    pymongo = _ensure_module("pymongo")
    if not hasattr(pymongo, "ASCENDING"):
        pymongo.ASCENDING = 1
    if not hasattr(pymongo, "DESCENDING"):
        pymongo.DESCENDING = -1

    # bson — ObjectId only (a sibling suite's faithful stub is fine).
    bson_mod = _ensure_module("bson")
    if not hasattr(bson_mod, "ObjectId"):
        bson_mod.ObjectId = ObjectId

    # Backend package + logger.
    backend = _ensure_module("Backend")
    logger_mod = _ensure_module("Backend.logger")
    if not hasattr(logger_mod, "LOGGER"):
        logger_mod.LOGGER = logging.getLogger("librarian-dbname-tests")
    backend.logger = logger_mod

    # Backend.config — Telegram.DATABASE with two dummy URIs (§3.3).
    config = _ensure_module("Backend.config")
    telegram = getattr(config, "Telegram", None)
    if telegram is None:
        telegram = type("Telegram", (), {})
        config.Telegram = telegram
    if len(getattr(telegram, "DATABASE", []) or []) < 2:
        telegram.DATABASE = list(_DUMMY_URIS)
    backend.config = config

    helper = _ensure_module("Backend.helper")
    backend.helper = helper

    # Backend.helper.bounded — the REAL module (import-free).
    if "Backend.helper.bounded" not in sys.modules:
        bounded = load_module("Backend.helper.bounded",
                              "Backend/helper/bounded.py")
        helper.bounded = bounded

    # Backend.helper.encrypt — the two codec names database.py binds.
    encrypt = _ensure_module("Backend.helper.encrypt")
    if not hasattr(encrypt, "encode_string"):
        encrypt.encode_string = lambda s: s
    if not hasattr(encrypt, "decode_string"):
        encrypt.decode_string = lambda s: s
    helper.encrypt = encrypt

    # Backend.helper.modal — the schema names used in annotations.
    modal = _ensure_module("Backend.helper.modal")
    for name in ("Episode", "MovieSchema", "QualityDetail",
                 "QualityPart", "Season", "TVShowSchema"):
        if not hasattr(modal, name):
            setattr(modal, name, type(name, (), {}))
    helper.modal = modal

    # Backend.helper.settings_manager — SettingsManager is only
    # called from Database methods, never during construction.
    sm = _ensure_module("Backend.helper.settings_manager")
    if not hasattr(sm, "SettingsManager"):
        class _SettingsManager:
            @classmethod
            def current(cls):
                raise RuntimeError("not used by dbname tests")
        sm.SettingsManager = _SettingsManager
    helper.settings_manager = sm

    # Backend.helper.task_manager — delete_message, unused here.
    tm = _ensure_module("Backend.helper.task_manager")
    if not hasattr(tm, "delete_message"):
        async def _delete_message(*args, **kwargs):
            return None
        tm.delete_message = _delete_message
    helper.task_manager = tm


_install_stubs()
dbmod = load_module("database_dbname_under_test", "Backend/helper/database.py")
Database = dbmod.Database


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

@contextmanager
def _env(value):
    """Set TS_DB_NAME to ``value`` (None = unset); restore after."""
    sentinel = object()
    old = os.environ.get(ENV_KEY, sentinel)
    if value is None:
        os.environ.pop(ENV_KEY, None)
    else:
        os.environ[ENV_KEY] = value
    try:
        yield
    finally:
        if old is sentinel:
            os.environ.pop(ENV_KEY, None)
        else:
            os.environ[ENV_KEY] = old


def _expect_value_error(fn, needle=None):
    try:
        fn()
    except ValueError as e:
        if needle is not None:
            assert needle in str(e), (needle, str(e))
        return
    raise AssertionError("expected ValueError")


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@contextmanager
def _capture_db_log():
    logger = dbmod.LOGGER
    handler = _LogCapture()
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield handler.messages
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


# ------------------------------------------------------------------
# B-1 precedence: explicit > env > default
# ------------------------------------------------------------------

def test_b1_default_when_env_unset():
    with _env(None):
        assert Database().db_name == "dbFyvio"


def test_b1_env_used_when_set():
    with _env("dbFyvio_staging"):
        assert Database().db_name == "dbFyvio_staging"


def test_b1_explicit_beats_env():
    with _env("dbFyvio_staging"):
        assert Database("dbFyvio").db_name == "dbFyvio"
        assert Database(db_name="dbFyvio_p4gate").db_name == "dbFyvio_p4gate"


def test_b1_explicit_beats_default():
    with _env(None):
        assert Database("dbFyvio_staging").db_name == "dbFyvio_staging"


def test_b1_shim_pattern_explicit_staging_accepted():
    # The Phase 3 shim's exact call shape keeps working (plan §3.1).
    with _env(None):
        assert Database(db_name="dbFyvio_staging").db_name == "dbFyvio_staging"


# ------------------------------------------------------------------
# B-1: empty / whitespace-only env = unset; env value is trimmed
# ------------------------------------------------------------------

def test_b1_empty_env_is_unset():
    with _env(""):
        assert Database().db_name == "dbFyvio"


def test_b1_whitespace_env_is_unset():
    with _env("   \t  "):
        assert Database().db_name == "dbFyvio"


def test_b1_env_value_is_trimmed():
    with _env("  dbFyvio_staging  "):
        assert Database().db_name == "dbFyvio_staging"


# ------------------------------------------------------------------
# B-5 namespace allowlist: exactly dbFyvio or dbFyvio_[a-z0-9_]+
# ------------------------------------------------------------------

def test_b5_env_exact_default_accepted():
    with _env("dbFyvio"):
        assert Database().db_name == "dbFyvio"


def test_b5_env_staging_names_accepted():
    for name in ("dbFyvio_staging", "dbFyvio_p4gate", "dbFyvio_a1_b2"):
        with _env(name):
            assert Database().db_name == name


def test_b5_env_out_of_namespace_raises():
    for name in ("dbFyvio2", "otherdb", "dbFyvio_Staging",
                 "dbFyvio-staging", "dbFyvio_", "DBFYVIO_STAGING"):
        with _env(name):
            _expect_value_error(lambda: Database(), needle="TS_DB_NAME")


# ------------------------------------------------------------------
# B-4 malformed env values fail closed, per forbidden-character class
# ------------------------------------------------------------------

def test_b4_env_malformed_raises():
    bad = [
        "dbFyvio/staging",      # slash
        "dbFyvio\\staging",     # backslash
        "dbFyvio.staging",      # dot
        'dbFyvio"staging',      # quote
        "dbFyvio$staging",      # dollar
        "dbFyvio*staging",      # star
        "dbFyvio<staging",      # angle bracket
        "dbFyvio>staging",
        "dbFyvio:staging",      # colon
        "dbFyvio|staging",      # pipe
        "dbFyvio?staging",      # question mark
        "dbFyvio staging",      # interior space
        "dbFyvio\tstaging",     # interior tab
        "x" * 65,               # over the 64-char cap
    ]
    for value in bad:
        with _env(value):
            _expect_value_error(lambda: Database(), needle="TS_DB_NAME")


def test_b4_malformed_env_never_falls_back():
    # Fail closed: no instance is produced at all (B-4's prohibition
    # on silently landing on the live dbFyvio).
    with _env("dbFyvio staging"):
        try:
            Database()
        except ValueError:
            pass
        else:
            raise AssertionError("malformed env must raise, not fall back")


# ------------------------------------------------------------------
# Explicit arguments: shape regex applies, namespace allowlist does
# not (plan §3.1 — explicit values come from code, not an operator)
# ------------------------------------------------------------------

def test_b4_explicit_out_of_namespace_accepted():
    with _env(None):
        assert Database("otherdb").db_name == "otherdb"


def test_b4_explicit_malformed_raises():
    with _env(None):
        _expect_value_error(lambda: Database("bad name"))
        _expect_value_error(lambda: Database("bad/name"))
        _expect_value_error(lambda: Database(""))


# ------------------------------------------------------------------
# B-6: one startup INFO line naming the effective db + its source
# ------------------------------------------------------------------

def test_b6_startup_log_line_default():
    with _env(None), _capture_db_log() as messages:
        Database()
    assert messages == ["Database name: dbFyvio (source: default)"], messages


def test_b6_startup_log_line_env():
    with _env("dbFyvio_staging"), _capture_db_log() as messages:
        Database()
    assert messages == ["Database name: dbFyvio_staging (source: env)"], messages


def test_b6_startup_log_line_explicit():
    with _env("dbFyvio_staging"), _capture_db_log() as messages:
        Database("dbFyvio_p4gate")
    assert messages == ["Database name: dbFyvio_p4gate (source: explicit)"], messages


def test_b6_log_line_never_carries_a_uri():
    with _env("dbFyvio_staging"), _capture_db_log() as messages:
        Database()
    for message in messages:
        assert "mongodb" not in message and "://" not in message, message


# ------------------------------------------------------------------
# B-6a: get_database_list() surfaces the effective name
# ------------------------------------------------------------------

def test_b6a_database_list_carries_effective_name():
    with _env("dbFyvio_staging"):
        db = Database()
    entries = db.get_database_list()
    assert entries and all(e["db_name"] == "dbFyvio_staging" for e in entries)
