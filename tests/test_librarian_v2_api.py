"""Librarian v2 Phase 3 API tests: token auth logic, provider-candidate
mapping, and the bot route flow (open -> plan -> report -> kill, file
registration, undo) driven through the real route functions with a
harness-backed runtime. No fastapi/aiohttp needed: guards are patched
to pass, auth logic is tested directly."""

import asyncio
import importlib
import re
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "librarian_v2"))
import harness  # noqa: E402

_pkg = types.ModuleType("libv2pkg")
_pkg.__path__ = [str(ROOT / "Backend" / "librarian_v2")]
sys.modules["libv2pkg"] = _pkg
store_mod = importlib.import_module("libv2pkg.store")
identify = importlib.import_module("libv2pkg.identify")
orders_mod = importlib.import_module("libv2pkg.orders")
api = importlib.import_module("libv2pkg.api")

T0 = 1_700_000_000
ANI_TG = -1004395609351
REAL_TOKEN_OK = api.token_ok  # captured before any test patches it


class FakeRequest:
    def __init__(self, payload=None, query=None, headers=None):
        self._payload = payload or {}
        self.query_params = query or {}
        self.headers = headers or {}

    async def json(self):
        return self._payload


def _parse_caption(caption):
    m = re.search(r"S(\d+)E(\d+)", caption or "")
    return {"season": int(m.group(1)), "episode": int(m.group(2))} \
        if m else None


class _Providers:
    def __init__(self):
        self.by_id = {}

    def search(self, provider, query, kind, year):
        return []

    def lookup_by_id(self, provider, pid, kind):
        return self.by_id.get((provider, pid))

    def episodes(self, provider, pid, kind):
        return []


def _setup_runtime():
    path = str(Path(tempfile.mkdtemp(prefix="libv2api-")) / "t.sqlite3")
    store = store_mod.open_store(path)
    tg = harness.FakeTelegram()
    chan = store.upsert_channel(ANI_TG, "Ani", now=T0)
    sid = store.upsert_series("Steins;Gate", kind="tv", year=2011,
                              provider="tmdb", provider_id="7",
                              imdb_id="tt1910272", now=T0)
    store.replace_episode_list(sid, [
        {"season_number": 1, "episode_number": i, "absolute_number": i,
         "title": f"E{i}", "air_date": None} for i in (1, 2)], T0)
    expected = {}

    def verify(file_row, new_caption):
        return tg.get_caption(file_row["chat_id"], file_row["msg_id"]) == \
            expected.get((file_row["chat_id"], file_row["msg_id"]))

    ident = identify.Identifier(store, _Providers())
    svc = orders_mod.OrderService(store, ident, tg.edit_caption,
                                  tg.get_caption, verify, _parse_caption,
                                  dry_run=False)
    rt = api.Runtime()
    rt._store = store
    rt._service = svc
    api._RUNTIME = rt
    api.token_ok = lambda request: True
    api.enabled = lambda: True
    defaults = {"librarian_v2_daily_budget": 5000,
                "librarian_v2_auto_floor": 0.95,
                "librarian_v2_approval_floor": 0.80,
                "librarian_v2_dry_run": False}
    api._setting = lambda name, default=None: defaults.get(name, default)
    return rt, store, tg, chan, sid, expected


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_token_ok_logic():
    api.token_ok = REAL_TOKEN_OK
    settings = {"librarian_api_token": "sekret"}
    orig = api._setting
    api._setting = lambda name, default=None: settings.get(name, default)
    try:
        good = FakeRequest(headers={"x-librarian-token": "sekret"})
        bad = FakeRequest(headers={"x-librarian-token": "nope"})
        none = FakeRequest(headers={})
        assert api.token_ok(good) is True
        assert api.token_ok(bad) is False
        assert api.token_ok(none) is False
    finally:
        api._setting = orig


def test_candidate_mapping_shape():
    c = api._cand("tmdb", 41498, "Strong World", 2009, "tt1485763",
                  ["Wan Pisu"])
    assert c["provider_id"] == "41498" and c["imdb_id"] == "tt1485763"
    assert c["also_known_as"] == ["Wan Pisu"]


def test_route_flow_open_plan_report_kill():
    rt, store, tg, chan, sid, expected = _setup_runtime()
    fid = store.upsert_media_file(
        ANI_TG, 101, T0, original_caption="sg raw", current_caption="sg raw",
        source_channel_id=chan, location_status="located")
    tg.seed(ANI_TG, 101, "sg raw")
    out = _run(api.v2_orders_open_api(FakeRequest(
        {"scope": "series", "series_id": sid})))
    oid = out["order_id"]
    caption = "Steins;Gate - S01E01 [tt1910272]"
    expected[(ANI_TG, 101)] = caption
    out = _run(api.v2_orders_plan_api(FakeRequest({
        "order_id": oid,
        "items": [{"media_file_id": fid, "series_id": sid,
                   "bits": {"season": 1, "episode": 1},
                   "new_caption": caption, "base_score": 0.95,
                   "breakdown": {}}]})))
    assert out["summary"]["auto"] == 1
    out = _run(api.v2_orders_report_api(FakeRequest(query={"order_id": oid})))
    assert out["progress"]["planned"] == 1
    assert out["progress"]["applied"] == 0
    out = _run(api.v2_orders_kill_api(FakeRequest({"order_id": oid})))
    assert out["ok"] and store.get_order(oid)["state"] == "killed"


def test_files_register_and_unidentified():
    rt, store, tg, chan, sid, expected = _setup_runtime()
    out = _run(api.v2_files_register_api(FakeRequest({
        "chat_id": ANI_TG, "msg_id": 555, "caption": "mystery file",
        "stem": "mystery", "channel_name": "Ani"})))
    fid = out["file_id"]
    out = _run(api.v2_unidentified_api(FakeRequest(query={})))
    ids = [f["id"] for f in out["files"]]
    assert fid in ids


def test_status_route():
    rt, store, tg, chan, sid, expected = _setup_runtime()
    out = _run(api.v2_status_api(FakeRequest()))
    assert out["ok"] and "series" in out["totals"]
