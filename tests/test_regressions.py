"""Regression tests for bugs fixed during the engineering mission.

Tests pure logic functions in isolation (no Telegram bot / MongoDB needed).
Run:  python3 tests/test_regressions.py
"""

#------------------------------------------------------------------------------
# Range header parsing (from stream_routes.py) — pure function, no deps
#------------------------------------------------------------------------------

def parse_range_header(range_header: str, file_size: int):
    if not range_header:
        return 0, file_size - 1
    try:
        value = range_header.replace("bytes=", "").strip()
        start_str, end_str = value.split("-")
        if start_str == "":
            length = int(end_str)
            start = file_size - length
            end = file_size - 1
        elif end_str == "":
            start = int(start_str)
            end = file_size - 1
        else:
            start = int(start_str)
            end = int(end_str)
    except Exception:
        raise ValueError("Invalid Range header")
    if start < 0:
        start = 0
    if end >= file_size:
        end = file_size - 1
    if end < start:
        raise ValueError("Requested Range Not Satisfiable")
    return start, end


def test_parse_range_header_basic():
    start, end = parse_range_header("bytes=0-99", 1000)
    assert start == 0 and end == 99
    start, end = parse_range_header("bytes=500-", 1000)
    assert start == 500 and end == 999
    start, end = parse_range_header("bytes=-500", 1000)
    assert start == 500 and end == 999


def test_parse_range_header_invalid():
    try:
        parse_range_header("bytes=abc-def", 1000)
        assert False, "should have raised"
    except ValueError:
        pass
    try:
        parse_range_header("bytes=1000-2000", 100)
        assert False, "should have raised"
    except ValueError:
        pass


def test_parse_range_header_none():
    start, end = parse_range_header("", 1000)
    assert start == 0 and end == 999


#------------------------------------------------------------------------------
# Stremio ID parsing (from stremio_routes.py) — pure function, no deps
#------------------------------------------------------------------------------

def _parse_stremio_id(id: str):
    parts = id.split(":")
    is_kitsu = parts and parts[0].lower() == "kitsu"
    imdb_id = None
    kitsu_id = None
    season_num = None
    episode_num = None
    absolute_episode = None

    if is_kitsu:
        try:
            kitsu_id = int(parts[1]) if len(parts) > 1 else None
        except (TypeError, ValueError):
            kitsu_id = None
        if len(parts) == 3:
            try:
                absolute_episode = int(parts[2])
            except (TypeError, ValueError):
                absolute_episode = None
            episode_num = absolute_episode
        elif len(parts) >= 4:
            try:
                season_num = int(parts[2]) if parts[2] not in ("", "null", "None") else None
            except (TypeError, ValueError):
                season_num = None
            try:
                episode_num = int(parts[3]) if parts[3] not in ("", "null", "None") else None
            except (TypeError, ValueError):
                episode_num = None
            if season_num is None and episode_num is not None:
                absolute_episode = episode_num
    else:
        imdb_id = parts[0] if parts else id
        try:
            season_num = int(parts[1]) if len(parts) > 1 and parts[1] not in ("", "null", "None") else None
        except (TypeError, ValueError):
            season_num = None
        try:
            episode_num = int(parts[2]) if len(parts) > 2 and parts[2] not in ("", "null", "None") else None
        except (TypeError, ValueError):
            episode_num = None

    return {
        "imdb_id": imdb_id,
        "kitsu_id": kitsu_id,
        "season_num": season_num,
        "episode_num": episode_num,
        "absolute_episode": absolute_episode,
        "is_kitsu": is_kitsu,
    }


def test_parse_stremio_id_imdb():
    r = _parse_stremio_id("tt1234567")
    assert r["imdb_id"] == "tt1234567"
    assert r["season_num"] is None
    assert r["episode_num"] is None
    assert r["is_kitsu"] is False


def test_parse_stremio_id_imdb_season_episode():
    r = _parse_stremio_id("tt1234567:1:3")
    assert r["imdb_id"] == "tt1234567"
    assert r["season_num"] == 1
    assert r["episode_num"] == 3


def test_parse_stremio_id_kitsu():
    r = _parse_stremio_id("kitsu:42:100")
    assert r["is_kitsu"] is True
    assert r["kitsu_id"] == 42
    assert r["absolute_episode"] == 100
    assert r["episode_num"] == 100


def test_parse_stremio_id_kitsu_season_ep():
    r = _parse_stremio_id("kitsu:42:1:5")
    assert r["is_kitsu"] is True
    assert r["kitsu_id"] == 42
    assert r["season_num"] == 1
    assert r["episode_num"] == 5


#------------------------------------------------------------------------------
# Size parsing (from stremio_routes.py) — pure function, no deps
#------------------------------------------------------------------------------

import re


def parse_size_to_bytes(size_str: str) -> int:
    if not size_str:
        return 0
    match = re.match(r"([\d.]+)\s*([A-Za-z]+)", size_str.strip())
    if not match:
        return 0
    value, unit = float(match.group(1)), match.group(2).upper()
    multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return int(value * multipliers.get(unit, 1))


def test_parse_size_to_bytes():
    assert parse_size_to_bytes("0") == 0
    assert parse_size_to_bytes("") == 0
    assert parse_size_to_bytes("1.5GB") == int(1.5 * 1024**3)
    assert parse_size_to_bytes("720MB") == int(720 * 1024**2)
    assert parse_size_to_bytes("100KB") == 100 * 1024
    assert parse_size_to_bytes("5B") == 5


#------------------------------------------------------------------------------
# Stream buffer adaptive policy (from stream_buffer.py) — load with stubs
#------------------------------------------------------------------------------

import importlib.util
import pathlib
import sys
import types
import logging

ROOT = pathlib.Path(__file__).resolve().parents[1]
MiB = 1024 * 1024


def _load_stream_buffer():
    if "Backend.logger" not in sys.modules:
        m = types.ModuleType("Backend.logger")
        m.LOGGER = logging.getLogger("sb_test")
        sys.modules["Backend.logger"] = m
    spec = importlib.util.spec_from_file_location(
        "sb_under_test", ROOT / "Backend" / "helper" / "stream_buffer.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_stream_buffer_adaptive_deepen():
    sb = _load_stream_buffer()
    sb.SPEED.__init__()
    sb._flood_penalty = 0
    sb._cache = None
    sb.METRICS.__init__()
    sb._policy = sb.build_policy({})
    sb.SPEED.observe(1.0)
    sb.SPEED.observe(1.0)
    assert sb.SPEED.multiplier() == 2.0
    eff = sb.stream_policy()
    assert eff.readahead_bytes == 2 * sb._policy.readahead_bytes


def test_stream_buffer_flood_penalty_recovers():
    sb = _load_stream_buffer()
    sb.SPEED.__init__()
    sb._flood_penalty = 0
    sb._cache = None
    sb.METRICS.__init__()
    sb._policy = sb.build_policy({})
    base = sb.build_policy({})
    assert sb.stream_policy().workers == base.workers
    sb.note_flood_wait()
    assert sb.stream_policy().workers == base.workers - 1
    for _ in range(sb.FLOOD_PENALTY_MAX):
        sb.note_flood_wait()
    assert sb.stream_policy().workers == sb.MIN_WORKERS
    for _ in range(sb.FLOOD_PENALTY_MAX):
        sb.record_stream_speed(10.0)
    assert sb.stream_policy().workers == base.workers


#------------------------------------------------------------------------------
# Runner
#------------------------------------------------------------------------------

def _main():
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failures = 0
    for name, test in tests:
        try:
            test()
            print(f"PASS {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
