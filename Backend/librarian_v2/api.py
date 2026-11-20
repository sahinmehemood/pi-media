"""Librarian v2 API + runtime wiring (SPEC §8).

Route functions follow the app's existing style (plain async functions
taking Request, registered in fastapi/main.py by the seam patch) and
the existing bot auth: the `x-librarian-token` header against the
Settings token. Everything heavy is lazy: the runtime (SQLite store,
identifier, order service) is built on first use, and ALL app-loop
coroutines (provider searches, Bot API edits, pyrogram reads, Mongo
verifies) are scheduled onto the app's event loop from the job worker
thread via run_coroutine_threadsafe — the v2 engine itself stays
synchronous and testable.

Bindings (all verified against the real tree):
  editor   — Telegram Bot API editMessageCaption as the Media Manager
             bot (v1's _bot_api path); FLOOD_WAIT -> SnoozeError via an
             exception carrying retry_after.
  reader   — StreamBot pyrogram get_messages caption (live read);
             on any failure, falls back to the store's known caption.
  verify   — db.get_media_ids_by_part must return the IDs named in the
             new caption's ID link (v1's _verify_indexed pattern).
  parser   — v1 name_bits() season/episode extraction.
  search   — merged search_any_candidates (TMDB+Cinemeta) PLUS direct
             TVDB search and Kitsu search_anime, so dual-named titles
             (romaji/western/native) are covered by the providers that
             index synonyms.
  episodes — TVDB _iter_series_episodes; Cinemeta meta videos; TMDB
             best-effort via its client; otherwise [] (the series then
             relies on bot-supplied research until its list lands).
"""

import asyncio
import json
import re
import threading
import time

from . import identify as identify_mod
from . import jobs as jobs_mod
from . import orders as orders_mod
from . import store as store_mod

DEFAULT_DB_PATH = "librarian_v2.sqlite3"
_ID_LINK_RE = re.compile(r"imdb\.com/title/(tt\d+)|themoviedb\.org/"
                         r"(?:tv|movie)/(\d+)")


class FloodWait(Exception):
    def __init__(self, retry_after):
        super().__init__(f"flood wait {retry_after}s")
        self.retry_after = retry_after


def _settings():
    from Backend.helper.settings_manager import SettingsManager
    return SettingsManager.current()


def _setting(name, default):
    return getattr(_settings(), name, default)


def token_ok(request) -> bool:
    token = _setting("librarian_api_token", "")
    return bool(token) and request.headers.get("x-librarian-token") == token


def enabled() -> bool:
    return bool(_setting("librarian_v2_enabled", False))


# ---------------------------------------------------------------------------
# Provider adapter (async app functions behind identify's sync protocol)
# ---------------------------------------------------------------------------

def _cand(provider, provider_id, title, year, imdb_id, aka=None):
    return {"provider": provider,
            "provider_id": str(provider_id) if provider_id else None,
            "title": title or "", "year": year, "imdb_id": imdb_id,
            "also_known_as": aka or []}


class AppProviders:
    def __init__(self, run_coro):
        self._run = run_coro          # sync bridge onto the app loop
        self._merged_cache = {}

    # -- search ------------------------------------------------------------
    def search(self, provider, query, kind, year):
        if provider in ("tmdb", "cinemeta"):
            return self._merged(provider, query, year)
        if provider == "tvdb":
            return self._run(self._tvdb_search(query, year))
        if provider == "kitsu":
            return self._run(self._kitsu_search(query, kind))
        return []

    def _merged(self, provider, query, year):
        key = (query, year)
        if key not in self._merged_cache:
            from Backend.helper.metadata.entry import search_any_candidates
            try:
                res = self._run(search_any_candidates(query, year))
            except Exception:
                raise identify_mod.ProviderDown(provider)
            buckets = {}
            for c in res or []:
                src = c.get("source")
                prov = "cinemeta" if src == "imdb" else (src or "tmdb")
                y = c.get("year")
                buckets.setdefault(prov, []).append(_cand(
                    prov, c.get("tmdb_id") or c.get("selected_id"),
                    c.get("title"),
                    int(y) if str(y or "").isdigit() else None,
                    c.get("imdb_id")))
            self._merged_cache[key] = buckets
        return list(self._merged_cache[key].get(provider, []))

    async def _tvdb_search(self, query, year):
        from Backend.helper.metadata.providers import tvdb
        try:
            hit = await tvdb.search(query, year)
        except Exception:
            raise identify_mod.ProviderDown("tvdb")
        if not hit:
            return []
        return [_cand("tvdb", hit.get("tvdb_id") or hit.get("id"),
                      hit.get("name"), hit.get("year"),
                      hit.get("imdb_id"),
                      hit.get("aliases") or [])]

    async def _kitsu_search(self, query, kind):
        from Backend.helper.metadata.providers import kitsu
        try:
            hit = await kitsu.search_anime(query, movie=(kind == "movie"))
        except Exception:
            raise identify_mod.ProviderDown("kitsu")
        if not hit:
            return []
        attrs = hit.get("attributes") or hit
        titles = attrs.get("titles") or {}
        aka = [t for t in (titles.get("en"), titles.get("ja_jp"),
                           attrs.get("canonicalTitle")) if t]
        return [_cand("kitsu", hit.get("id"), attrs.get("canonicalTitle"),
                      (attrs.get("startDate") or "")[:4] or None,
                      None, aka)]

    # -- lookup --------------------------------------------------------------
    def lookup_by_id(self, provider, provider_id, kind):
        from Backend.helper.metadata.entry import (
            fetch_selected_movie_metadata, fetch_selected_tv_metadata)
        fn = fetch_selected_movie_metadata if kind == "movie" \
            else fetch_selected_tv_metadata
        try:
            data = self._run(fn(str(provider_id)))
        except Exception:
            raise identify_mod.ProviderDown(provider)
        if not data:
            return None
        return _cand("tmdb", data.get("tmdb_id") or provider_id,
                     data.get("title"), data.get("release_year"),
                     data.get("imdb_id"))

    # -- episodes --------------------------------------------------------------
    def episodes(self, provider, provider_id, kind):
        try:
            if provider == "tvdb":
                return self._run(self._tvdb_episodes(int(provider_id)))
            if provider == "cinemeta":
                return self._run(self._cinemeta_episodes(provider_id, kind))
        except Exception:
            return []
        return []

    async def _tvdb_episodes(self, tvdb_id):
        from Backend.helper.metadata.providers import tvdb
        eps = await tvdb._iter_series_episodes(tvdb_id)
        out = []
        for e in eps or []:
            out.append({
                "season_number": e.get("seasonNumber"),
                "episode_number": e.get("number"),
                "absolute_number": e.get("absoluteNumber"),
                "title": e.get("name"), "air_date": e.get("aired")})
        return [e for e in out if e["season_number"] is not None
                and e["episode_number"] is not None]

    async def _cinemeta_episodes(self, imdb_id, kind):
        from Backend.helper.metadata.providers import cinemeta
        data = await cinemeta.get_detail(imdb_id=imdb_id,
                                         media_type="tvSeries")
        out = []
        for v in (data or {}).get("videos") or []:
            out.append({
                "season_number": v.get("season"),
                "episode_number": v.get("episode"),
                "absolute_number": None,
                "title": v.get("name") or v.get("title"),
                "air_date": (v.get("released") or "")[:10] or None})
        return [e for e in out if e["season_number"] is not None
                and e["episode_number"] is not None]


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

class Runtime:
    def __init__(self):
        self._loop = None
        self._store = None
        self._service = None
        self._worker = None
        self._pace_lock = threading.Lock()
        self._pace_last = {}
        self._pace_global = 0.0

    # -- construction ---------------------------------------------------------
    def bind_loop(self):
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop

    def run_coro(self, coro, timeout=90):
        loop = self._loop or self.bind_loop()
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    @property
    def store(self):
        if self._store is None:
            import os
            path = _setting("librarian_v2_db_path", DEFAULT_DB_PATH)
            if not os.path.isabs(path):
                path = os.path.join(os.getcwd(), path)
            self._store = store_mod.open_store(path)
        return self._store

    @property
    def service(self):
        if self._service is None:
            identifier = identify_mod.Identifier(
                self.store, AppProviders(self.run_coro))
            self._service = orders_mod.OrderService(
                self.store, identifier, self._edit, self._read,
                self._verify, self._parse, pace=self._pace,
                specials_policy=_setting("librarian_v2_specials_policy",
                                         "guard"),
                dry_run=bool(_setting("librarian_v2_dry_run", True)))
        return self._service

    # -- bindings ---------------------------------------------------------------
    async def _bot_api(self, method, params):
        import aiohttp
        token = _setting("hermes_bot_token", "")
        if not token:
            return {"ok": False, "description": "No Hermes bot token."}
        url = f"https://api.telegram.org/bot{token}/{method}"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                    url, json=params,
                    timeout=aiohttp.ClientTimeout(total=30)) as resp:
                return await resp.json()

    def _edit(self, chat_id, msg_id, new_caption):
        res = self.run_coro(self._bot_api("editMessageCaption", {
            "chat_id": chat_id, "message_id": msg_id,
            "caption": new_caption, "parse_mode": "HTML"}))
        if res.get("ok"):
            return
        params = (res.get("parameters") or {})
        if res.get("error_code") == 429 or "retry_after" in params:
            raise FloodWait(int(params.get("retry_after") or 30))
        raise RuntimeError(f"edit failed: {res.get('description')}")

    def _read(self, chat_id, msg_id):
        async def _get():
            from Backend.pyrofork.bot import StreamBot
            msg = await StreamBot.get_messages(chat_id, msg_id)
            return (msg.caption if msg else None) or ""
        try:
            live = self.run_coro(_get(), timeout=30)
            if live:
                return live
        except Exception:
            pass
        f = self.store.conn.execute(
            "SELECT current_caption FROM media_file WHERE chat_id=?"
            " AND msg_id=?", (chat_id, msg_id)).fetchone()
        return f["current_caption"] if f else ""

    def _verify(self, file_row, new_caption):
        m = _ID_LINK_RE.search(new_caption or "")
        want_imdb = m.group(1) if m else None
        want_tmdb = int(m.group(2)) if m and m.group(2) else None

        async def _check():
            from Backend import db
            for _ in range(10):
                try:
                    got = await db.get_media_ids_by_part(
                        file_row["chat_id"], file_row["msg_id"])
                except Exception:
                    got = None
                if got:
                    imdb_id, tmdb_id = got[0], got[1]
                    if want_tmdb:
                        return int(tmdb_id or 0) == want_tmdb
                    if want_imdb:
                        return imdb_id == want_imdb
                    return True
                await asyncio.sleep(1.0)
            return False
        try:
            return bool(self.run_coro(_check(), timeout=30))
        except Exception:
            return False

    @staticmethod
    def _parse(caption):
        from Backend.helper.librarian import name_bits
        bits = name_bits(caption or "", "")
        if bits.get("season") is None or bits.get("episode") is None:
            return None
        return {"season": bits["season"], "episode": bits["episode"]}

    def _pace(self, channel_id):
        with self._pace_lock:
            now = time.monotonic()
            wait = max(0.0, self._pace_last.get(channel_id, 0) + 1.0 - now,
                       self._pace_global + 0.34 - now)
            stamp = now + wait
            self._pace_last[channel_id] = stamp
            self._pace_global = stamp
        if wait:
            time.sleep(wait)

    # -- job driving ------------------------------------------------------------
    def tick(self):
        """Called by the app's ticker seam; starts the worker if there is
        due work and no worker is running."""
        self.bind_loop()
        if self._worker and self._worker.is_alive():
            return
        if not self.store.due_jobs(int(time.time())):
            jobs_mod.reap(self.store, int(time.time()))
            if not self.store.due_jobs(int(time.time())):
                return
        self._worker = threading.Thread(target=self._drain, daemon=True,
                                        name="librarian-v2-worker")
        self._worker.start()

    def _drain(self):
        handlers = {"apply_caption": self.service.make_apply_handler()}
        while jobs_mod.run_once(self.store, handlers, "v2-api",
                                int(time.time())):
            for oid in self._applying_orders():
                self.service.maybe_finish_order(oid, int(time.time()))

    def _applying_orders(self):
        rows = self.store.conn.execute(
            "SELECT id FROM work_order WHERE state='applying'").fetchall()
        return [r["id"] for r in rows]


_RUNTIME = Runtime()


def get_runtime() -> Runtime:
    return _RUNTIME


# ---------------------------------------------------------------------------
# Bot-token route functions (registered in main.py by the seam patch)
# ---------------------------------------------------------------------------

def _guard(request):
    # HTTPException imported lazily so this module stays importable (and
    # unit-testable) outside the app's dependency environment.
    if not token_ok(request):
        from fastapi import HTTPException
        raise HTTPException(status_code=401,
                            detail="Invalid Librarian API token.")
    if not enabled():
        from fastapi import HTTPException
        raise HTTPException(status_code=403,
                            detail="Librarian v2 is disabled.")


def _resolution_out(res):
    return {"series_id": res.series_id, "method": res.method,
            "score": res.score, "status": res.status,
            "breakdown": res.breakdown}


async def v2_status_api(request):
    _guard(request)
    rt = get_runtime()
    rt.bind_loop()
    totals = rt.store.totals()
    return {"ok": True, "totals": totals,
            "dry_run": bool(_setting("librarian_v2_dry_run", True))}


async def v2_series_list_api(request):
    _guard(request)
    rt = get_runtime()
    kind = request.query_params.get("kind")
    rows = rt.store.list_series(kind=kind or None)
    return {"ok": True, "series": rows}


async def v2_series_resolve_api(request):
    _guard(request)
    rt = get_runtime()
    rt.bind_loop()
    payload = await request.json()
    ident = rt.service.identifier
    now = int(time.time())
    proposal = payload.get("proposal")
    if proposal:
        res = ident.verify_proposal(proposal,
                                    kind=payload.get("kind") or "tv",
                                    year=payload.get("year"), now=now)
    else:
        res = ident.resolve(payload.get("title") or payload.get("stem")
                            or "", kind=payload.get("kind") or "tv",
                            year=payload.get("year"), now=now)
    return {"ok": True, "resolution": _resolution_out(res)}


async def v2_unidentified_api(request):
    _guard(request)
    rt = get_runtime()
    channel = request.query_params.get("channel")
    chan_id = None
    if channel:
        row = rt.store.conn.execute(
            "SELECT id FROM source_channel WHERE telegram_chat_id=?",
            (int(channel),)).fetchone()
        chan_id = row["id"] if row else -1
    rows = rt.store.list_unidentified(channel_id=chan_id, limit=50)
    return {"ok": True, "files": rows}


async def v2_files_register_api(request):
    _guard(request)
    rt = get_runtime()
    payload = await request.json()
    now = int(time.time())
    chan = rt.store.upsert_channel(int(payload["chat_id"]),
                                   payload.get("channel_name") or "",
                                   now=now)
    fid = rt.store.upsert_media_file(
        int(payload["chat_id"]), int(payload["msg_id"]), now,
        file_unique_id=payload.get("file_unique_id"),
        original_caption=payload.get("caption") or "",
        current_caption=payload.get("caption") or "",
        parsed_title_raw=payload.get("stem"),
        parsed_season=payload.get("season"),
        parsed_episode=payload.get("episode"),
        source_channel_id=chan, location_status="located")
    return {"ok": True, "file_id": fid}


async def v2_orders_open_api(request):
    _guard(request)
    rt = get_runtime()
    payload = await request.json()
    now = int(time.time())
    oid = rt.service.open_order(
        payload.get("scope") or "series", now,
        series_id=payload.get("series_id"),
        channel_id=payload.get("channel_id"),
        max_files=int(payload.get("max_files") or 500),
        budget_applies=int(payload.get("budget") or
                           _setting("librarian_v2_daily_budget", 5000)),
        auto_floor=float(_setting("librarian_v2_auto_floor", 0.95)),
        approval_floor=float(_setting("librarian_v2_approval_floor", 0.80)),
        opened_by="bot")
    return {"ok": True, "order_id": oid}


async def v2_orders_plan_api(request):
    _guard(request)
    rt = get_runtime()
    payload = await request.json()
    plan_id, summary = rt.service.build_plan(
        int(payload["order_id"]), payload.get("items") or [],
        int(time.time()))
    return {"ok": True, "plan_id": plan_id, "summary": summary}


async def v2_orders_apply_api(request):
    _guard(request)
    rt = get_runtime()
    rt.bind_loop()
    payload = await request.json()
    queued = rt.service.apply_plan(int(payload["order_id"]),
                                   int(time.time()))
    rt.tick()
    return {"ok": True, "queued": queued}


async def v2_orders_report_api(request):
    _guard(request)
    rt = get_runtime()
    oid = int(request.query_params.get("order_id"))
    return {"ok": True, "progress": rt.service.progress(oid)}


async def v2_orders_kill_api(request):
    _guard(request)
    rt = get_runtime()
    payload = await request.json()
    rt.store.set_order_state(int(payload["order_id"]), "killed",
                             int(time.time()))
    return {"ok": True}


async def v2_undo_api(request):
    _guard(request)
    rt = get_runtime()
    payload = await request.json()
    now = int(time.time())
    if payload.get("order_id"):
        return {"ok": True, "undone": rt.service.undo_order(
            int(payload["order_id"]), now)}
    return {"ok": True, "undone": rt.service.undo_file(
        int(payload["file_id"]), now)}


async def v2_approve_api(request):
    """Yan-only approval — registered behind the admin session auth in
    main.py, NOT behind the bot token."""
    rt = get_runtime()
    payload = await request.json()
    rt.service.approve_plan(int(payload["order_id"]),
                            int(payload["plan_id"]), int(time.time()))
    return {"ok": True}
