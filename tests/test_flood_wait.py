"""Unit tests for Backend/helper/flood_wait.py.

Runs without the application stack: stubs ``Backend.logger``,
``pyrogram.errors.FloodWait``, and ``Backend.pyrofork.bot`` so the module
can be imported standalone.

    python3 tests/test_flood_wait.py     # plain asserts
    pytest tests/test_flood_wait.py      # same tests, if pytest is around
"""

import asyncio
import importlib.util
import logging
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]


#----- Stub the minimal dependency graph ----------------------------------

def _setup():
    # Backend package + logger
    if "Backend" not in sys.modules:
        pkg = types.ModuleType("Backend")
        pkg.__path__ = [str(ROOT / "Backend")]
        sys.modules["Backend"] = pkg

    logger_mod = types.ModuleType("Backend.logger")
    logger_mod.LOGGER = logging.getLogger("flood_wait_test")
    sys.modules["Backend.logger"] = logger_mod

    # pyrogram + pyrogram.errors.FloodWait
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

    # Backend.pyrofork.bot (used by client_index_of)
    bot_mod = types.ModuleType("Backend.pyrofork.bot")
    bot_mod.multi_clients = {}
    bot_mod.StreamBot = None
    bot_mod.Userbot = None
    sys.modules["Backend.pyrofork.bot"] = bot_mod

    # Load flood_wait under its real import path
    spec = importlib.util.spec_from_file_location(
        "Backend.helper.flood_wait",
        ROOT / "Backend" / "helper" / "flood_wait.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fw = _setup()
handler = fw.flood_handler
PRIMARY_CLIENT = fw.PRIMARY_CLIENT
FloodWaitDecision = fw.FloodWaitDecision


def _reset():
    handler._penalties.clear()
    handler._flood_counts.clear()
    handler._last_success.clear()


def _flood(value=5):
    return fw.FloodWait(value, f"wait of {value} seconds")


def _run_handle(err, client):
    return asyncio.run(handler.handle(err, client))


#----- handle() ------------------------------------------------------------

def test_handle_parses_wait_from_value_attribute():
    _reset()
    decision = _run_handle(_flood(30), PRIMARY_CLIENT)
    assert decision.abort is False
    assert 30.0 <= decision.wait_seconds <= 31.0


def test_handle_parses_wait_from_error_string():
    _reset()
    err = Exception("Some upstream error wait of 60 second")
    decision = _run_handle(err, PRIMARY_CLIENT)
    assert decision.abort is False
    assert 60.0 <= decision.wait_seconds <= 61.0


def test_handle_aborts_on_unrecognized_error():
    _reset()
    decision = _run_handle(ValueError("not a flood"), PRIMARY_CLIENT)
    assert decision.abort is True
    assert decision.wait_seconds == 0.0


def test_handle_aborts_after_max_retries():
    _reset()
    for _ in range(handler.max_retries + 1):
        decision = _run_handle(_flood(5), PRIMARY_CLIENT)
    assert decision.abort is True


def test_handle_tracks_consecutive_floods_per_client():
    _reset()
    _run_handle(_flood(5), PRIMARY_CLIENT)
    _run_handle(_flood(5), PRIMARY_CLIENT)
    assert handler._flood_counts[PRIMARY_CLIENT] == 2


#----- record_success / penalty -------------------------------------------

def test_record_success_decays_penalty():
    _reset()
    _run_handle(_flood(5), PRIMARY_CLIENT)
    assert handler.penalty(PRIMARY_CLIENT) == 1
    handler.record_success(PRIMARY_CLIENT)
    assert handler.penalty(PRIMARY_CLIENT) == 0


def test_record_success_clears_flood_count():
    _reset()
    _run_handle(_flood(5), PRIMARY_CLIENT)
    assert handler._flood_counts[PRIMARY_CLIENT] == 1
    handler.record_success(PRIMARY_CLIENT)
    assert PRIMARY_CLIENT not in handler._flood_counts


#----- client_index_of -----------------------------------------------------

def test_client_index_of_int_passthrough():
    assert handler.client_index_of(5) == 5
    assert handler.client_index_of(-1) == -1


def test_client_index_of_unknown_object_returns_primary():
    class FakeClient:
        pass

    assert handler.client_index_of(FakeClient()) == PRIMARY_CLIENT


#----- note_flood_wait -----------------------------------------------------

def test_note_flood_wait_increments_penalty():
    _reset()
    handler.note_flood_wait(PRIMARY_CLIENT)
    assert handler.penalty(PRIMARY_CLIENT) == 1


#----- FloodWaitDecision ---------------------------------------------------

def test_flood_wait_decision_fields():
    d = FloodWaitDecision(abort=True, wait_seconds=0.0, reason="boom")
    assert d.abort is True
    assert d.wait_seconds == 0.0
    assert d.reason == "boom"


def main():
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failures = 0
    for name, test in tests:
        try:
            test()
            print(f"PASS {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001 - test harness
            failures += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())


#----- max_wait cap (added in test-3) ----------------------------------------

def test_handle_aborts_when_wait_exceeds_max_wait():
    _reset()
    decision = _run_handle(_flood(3600), PRIMARY_CLIENT)
    assert decision.abort is True
    assert decision.wait_seconds == 0.0
    assert "max_wait" in decision.reason


def test_max_wait_is_configurable():
    _reset()
    h = fw.FloodWaitHandler(max_wait=1.0)
    d = asyncio.run(h.handle(_flood(30), PRIMARY_CLIENT))
    assert d.abort is True


#----- run_with_flood_retry helper (added in test-3) --------------------------

def test_retry_helper_returns_result_and_records_success():
    _reset()
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _flood(5)
        return "ok"

    out = asyncio.run(fw.run_with_flood_retry(PRIMARY_CLIENT, flaky, what="test"))
    assert out == "ok"
    assert calls["n"] == 2
    assert handler.penalty(PRIMARY_CLIENT) == 0  # success decayed the penalty


def test_retry_helper_aborts_after_max_attempts():
    _reset()

    async def always_floods():
        raise _flood(5)

    try:
        asyncio.run(fw.run_with_flood_retry(PRIMARY_CLIENT, always_floods, attempts=3, what="test"))
        raised = False
    except Exception as e:
        raised = True
        assert type(e).__name__ == "FloodWait"
    assert raised is True
    assert handler._flood_counts[PRIMARY_CLIENT] == 3


def test_retry_helper_propagates_non_flood_errors_immediately():
    _reset()
    calls = {"n": 0}

    async def bad():
        calls["n"] += 1
        raise ValueError("boom")

    try:
        asyncio.run(fw.run_with_flood_retry(PRIMARY_CLIENT, bad, what="test"))
        raised = False
    except ValueError:
        raised = True
    assert raised is True
    assert calls["n"] == 1  # no retry on non-flood errors
