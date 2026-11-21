"""Phase 2 tests — Librarian provider gateway (DESIGN §7/§6).

Loads the real ``librarian_gateway``, the real ``librarian_store``,
the real AniList client, and the real TVDB/Kitsu seam functions by
file path (the repo harness pattern) against stub modules whose
shapes mirror the real tree — every stubbed name was verified
against the real module it stands in for:

- Gateway provider fakes for ``Backend.helper.metadata.providers.
  {tmdb, tvdb, cinemeta, kitsu, anilist}`` expose exactly the public
  functions the gateway calls, with the real signatures verified in
  the Phase 2 plan §1/§3/§4: ``tmdb.raw_search`` returns a list of
  attribute objects (never dicts — the fake uses SimpleNamespace
  with ``.id``/``.name``/``.title``/``.original_*`` and date objects
  carrying ``.year``, mirroring themoviedb's schemas; ``.external_ids``
  is a namespace), ``tmdb.details`` returns an object or None,
  ``tmdb.get_tmdb_client().season(tv_id, n).details()`` mirrors the
  verified themoviedb 1.0.2 surface (Season with ``.episodes``);
  ``tvdb.search_candidates`` is the §4.3 seam contract (all rows,
  ``tvdb_id`` normalised, ``tvdb_id_raw`` retained, raises the
  module's own ``TvdbTransportError``); ``kitsu.search_candidates``
  is the §4.4 seam contract over ``_kitsu_search`` (raises
  ``KitsuTransportError``); ``cinemeta.search_candidates`` is the
  §18-D14 seam contract over ``_fetch_json`` (same row shape as
  ``search_title_multi`` — ``{id, type, title, year, poster}`` —
  raises the module's own ``CinemetaTransportError`` when the fetch
  produced no well-formed body); ``cinemeta.search_title_multi``
  also returns that row shape and, in production, can neither raise
  nor return a non-list (its ``_fetch_json`` swallows every
  non-200 / exception to None and the search converts that to
  ``[]``), so the fake's ``search_title_multi`` has no raise mode —
  an outage is scripted only on the seam, whose production failure
  shape IS the raise; ``cached_detail`` returns a dict or None;
  ``anilist.search_media``/``media_by_id`` mirror the new client's
  surface, raising ``AnilistTransportError`` with ``.status`` /
  ``.retry_after``. Every fake is scriptable per test
  (mode in {ok, empty, raise, malformed, slow, huge} per provider),
  with the per-function production-shape limits above.
- The REAL ``tvdb.search_candidates``, ``kitsu.search_candidates``
  and ``cinemeta.search_candidates`` are additionally loaded by
  file path against a fake ``httpx`` transport, so the seam code
  itself (status-aware request, ID normalisation,
  raise-on-failure) is under test, not just the gateway-side fakes.
- The REAL AniList client is loaded by file path against the same
  fake ``httpx`` transport for its unit tests.
- The REAL TMDB provider (``tmdb.py``) is loaded by file path
  against a fake ``themoviedb`` client whose ``search().tv()`` /
  ``search().movies()`` return recorded-shape stand-ins for
  themoviedb's ``TVs`` / ``Movies`` collections (see
  ``_TmdbResultCollection`` below — the library is not importable
  in the test environment, so the shape was recorded from the
  themoviedb 1.0.2 / 2.0.2 wheels: a dataclass holding the items
  in ``.results``, iterable/indexable/sized/bool-tested through
  that list, but NOT itself a ``list``). The gateway's own TMDB
  fake keeps returning plain lists — that IS the real
  ``raw_search`` contract once the provider coerces the collection
  at its boundary; the collection shape is exercised against the
  real provider, including through the gateway leg with the real
  provider wired into ``sys.modules``.
- ``Backend.helper.metadata.common.year_from_str`` — copied from
  common.py (first 4-digit run); ``cached_call`` mirrors
  common.py's (store dict, key, ns, producer) semantics;
  ``caption_with_links`` copied verbatim (the store needs it).
- ``Backend.helper.metadata.parse.parse_media_name``,
  ``Backend.helper.pyro`` (``clean_filename`` /
  ``get_readable_file_size``), ``Backend.helper.split_files.
  strip_part_suffix``, ``Backend.helper.subtitles.is_subtitle_file``
  — the same faithful reductions as the Phase 1 suite (the store's
  import surface).
- ``Backend.helper.settings_manager.SettingsManager`` — classmethod
  ``current()`` returning an object with the property names the code
  reads (``tvdb_api`` for the real TVDB seam).
- ``pymongo`` — only ``ASCENDING`` and ``errors.DuplicateKeyError``.
- The fake tracking DB implements the motor surface the store and
  gateway use (``create_index``, ``update_one`` with
  ``$set``/``$setOnInsert``/upsert, ``find_one``) with real Mongo
  semantics for those operations, including unique-index
  enforcement — the same fake as the Phase 1 suite.
- Fixtures are recorded-payload JSON files under
  ``tests/fixtures/gateway/``, hand-built to the verified in-tree
  field usage (Phase 2 plan §1) and the providers' API docs. The
  four cases are fixture-only invented titles; the suite never
  touches the network.
"""

import asyncio
import copy
import json
import re
import sys
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_module, run  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "gateway"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ------------------------------------------------------------------
# Scriptable provider script (shared by the gateway fakes)
# ------------------------------------------------------------------

class _Script:
    def __init__(self):
        self.modes = {}
        self.calls = []
        self.in_flight = 0
        self.max_in_flight = 0

    def reset(self):
        self.modes = {}
        self.calls = []
        self.in_flight = 0
        self.max_in_flight = 0

    def mode(self, provider):
        return self.modes.get(provider, "ok")

    def count(self, provider, fn=None):
        return sum(1 for p, f in self.calls
                   if p == provider and (fn is None or f == fn))


SCRIPT = _Script()


async def _track(provider, fn):
    SCRIPT.calls.append((provider, fn))
    SCRIPT.in_flight += 1
    SCRIPT.max_in_flight = max(SCRIPT.max_in_flight, SCRIPT.in_flight)
    await asyncio.sleep(0.01)


def _untrack():
    SCRIPT.in_flight -= 1


# ------------------------------------------------------------------
# Faithful small stubs (store import surface, as in Phase 1)
# ------------------------------------------------------------------

class DuplicateKeyError(Exception):
    pass


def _get_readable_file_size(size_in_bytes):
    size_in_bytes = int(size_in_bytes) if str(size_in_bytes).isdigit() else 0
    if not size_in_bytes:
        return "0B"
    index, SIZE_UNITS = 0, ["B", "KB", "MB", "GB", "TB", "PB"]
    while size_in_bytes >= 1024 and index < len(SIZE_UNITS) - 1:
        size_in_bytes /= 1024
        index += 1
    return f"{size_in_bytes:.2f}{SIZE_UNITS[index]}" if index > 0 else f"{size_in_bytes:.0f}B"


def _clean_filename(filename):
    if not filename:
        return "unknown_file"
    filename = re.sub(r"[^\x20-\x7E]", " ", filename)
    return re.sub(r"\s+", " ", filename).strip() or "unknown_file"


def _caption_with_links(message):
    urls = [e.url for e in (getattr(message, "caption_entities", None) or [])
            if getattr(e, "url", None)]
    return "\n".join([getattr(message, "caption", None) or "", *urls])


def _year_from_str(year_val) -> int:
    # Copied from Backend/helper/metadata/common.py.
    if not year_val:
        return 0
    m = re.search(r"(\d{4})", str(year_val))
    return int(m.group(1)) if m else 0


def _build_query_variants(title: str, year=None) -> list:
    # Copied from Backend/helper/metadata/common.py.
    variants = [title]
    if year:
        variants.append(f"{title} {year}")
    stripped = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", title)).strip()
    if stripped and stripped.lower() != title.lower():
        variants.append(stripped)
        if year:
            variants.append(f"{stripped} {year}")
    no_article = re.sub(r"^\b(the|a|an)\b\s+", "", title, flags=re.IGNORECASE).strip()
    if no_article and no_article.lower() != title.lower():
        variants.append(no_article)
    seen: set = set()
    ordered = []
    for v in variants:
        key = v.lower()
        if v and key not in seen:
            seen.add(key)
            ordered.append(v)
    return ordered


async def _cached_call(store, key, ns, producer):
    # Mirrors common.py cached_call's observable semantics.
    if key in store:
        return store[key]
    result = await producer()
    store[key] = result
    return result


def _parse_media_name(name):
    return {"title": name or None, "year": None, "season": None,
            "episode": None, "quality": None, "excess": None}


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


def _is_subtitle_file(name):
    return bool(name) and name.lower().strip().endswith((".srt", ".vtt", ".ass", ".ssa", ".sub"))


class FakeSettings:
    def __init__(self):
        self.librarian_capture_enabled = True
        self.tvdb_api = "test-tvdb-key"
        self.tmdb_api = ""


class FakeSettingsManager:
    _current = FakeSettings()

    @classmethod
    def current(cls):
        return cls._current


# ------------------------------------------------------------------
# Fake Mongo (motor surface, real semantics for the used operations)
# ------------------------------------------------------------------

def _dotted_get(doc, path):
    cur = doc
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None, False
        cur = cur[part]
    return cur, True


def _dotted_set(doc, path, value):
    parts = path.split(".")
    cur = doc
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


class FakeCollection:
    def __init__(self, name):
        self.name = name
        self.docs = []
        self.indexes = []

    async def create_index(self, keys, unique=False, sparse=False, expireAfterSeconds=None):
        self.indexes.append((list(keys), unique, sparse, expireAfterSeconds))
        return "idx"

    def _matches(self, doc, filt):
        return all(_dotted_get(doc, k)[0] == v for k, v in filt.items())

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

    async def update_one(self, filt, update, upsert=False):
        for doc in self.docs:
            if self._matches(doc, filt):
                for k, v in update.get("$set", {}).items():
                    _dotted_set(doc, k, copy.deepcopy(v))
                return types.SimpleNamespace(matched_count=1, upserted_id=None)
        if not upsert:
            return types.SimpleNamespace(matched_count=0, upserted_id=None)
        new_doc = {}
        for k, v in filt.items():
            _dotted_set(new_doc, k, copy.deepcopy(v))
        for k, v in update.get("$setOnInsert", {}).items():
            _dotted_set(new_doc, k, copy.deepcopy(v))
        for k, v in update.get("$set", {}).items():
            _dotted_set(new_doc, k, copy.deepcopy(v))
        self._check_unique(new_doc)
        self.docs.append(new_doc)
        return types.SimpleNamespace(matched_count=0, upserted_id=id(new_doc))

    async def find_one(self, filt):
        for doc in self.docs:
            if self._matches(doc, filt):
                return copy.deepcopy(doc)
        return None


class FakeTracking:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, FakeCollection(name))


class FakeDatabase:
    def __init__(self):
        self.dbs = {"tracking": FakeTracking()}


fake_db = FakeDatabase()


# ------------------------------------------------------------------
# Fake httpx transport (for the real AniList client + real seams)
# ------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeAsyncClient:
    """httpx.AsyncClient stand-in: class-level ``transport`` callable
    (method, url, payload) -> FakeResponse (or raises)."""

    transport = None

    def __init__(self, timeout=None, follow_redirects=None, headers=None):
        self.is_closed = False

    async def post(self, url, json=None, params=None):
        if type(self).transport is None:
            raise RuntimeError("no fake transport installed")
        return type(self).transport("POST", url, json if json is not None else params)

    async def get(self, url, params=None, headers=None):
        if type(self).transport is None:
            raise RuntimeError("no fake transport installed")
        return type(self).transport("GET", url, params)


# ------------------------------------------------------------------
# Gateway provider fakes (mirror the real public surfaces)
# ------------------------------------------------------------------

def _date_ns(value):
    if not value:
        return None
    return types.SimpleNamespace(year=int(str(value)[:4]))


def _tmdb_item_ns(row, media_type):
    ns = types.SimpleNamespace(id=row["id"], overview=row.get("overview", ""),
                               poster_path=row.get("poster_path"))
    if media_type == "movie":
        ns.title = row.get("title", "")
        ns.original_title = row.get("original_title", "")
        ns.release_date = _date_ns(row.get("release_date"))
    else:
        ns.name = row.get("name", "")
        ns.original_name = row.get("original_name", "")
        ns.first_air_date = _date_ns(row.get("first_air_date"))
    return ns


def _tmdb_details_ns(fixture_name):
    data = _fixture(fixture_name)
    ext = data.get("external_ids") or {}
    ns = types.SimpleNamespace(
        id=data["id"],
        external_ids=types.SimpleNamespace(
            imdb_id=ext.get("imdb_id"), tvdb_id=ext.get("tvdb_id")),
        seasons=[types.SimpleNamespace(season_number=s["season_number"],
                                       episode_count=s.get("episode_count"))
                 for s in data.get("seasons", [])],
    )
    return ns


_TMDB_SEARCH = {
    ("tv", "Harbour Lights"): "tmdb_search_tv_western.json",
    ("movie", "The Last Ferry"): "tmdb_search_movie_movie.json",
    ("tv", "Star Blossom Academy"): "tmdb_search_tv_anime.json",
    ("tv", "River of Clouds"): "tmdb_search_tv_donghua.json",
}
_TMDB_DETAILS = {
    ("tv", 1001): "tmdb_details_tv_western.json",
    ("movie", 3001): "tmdb_details_movie_movie.json",
    ("tv", 9001): "tmdb_details_tv_anime.json",
    ("tv", 9002): "tmdb_details_tv_donghua.json",
}
_TMDB_SEASONS = {
    (1001, 0): "tmdb_season0_western.json",
    (1001, 1): "tmdb_season_western.json",
    (9001, 1): "tmdb_season_anime.json",
}


def _make_tmdb_fake():
    mod = types.ModuleType("Backend.helper.metadata.providers.tmdb")

    async def raw_search(title, media_type, year):
        await _track("tmdb", "raw_search")
        try:
            mode = SCRIPT.mode("tmdb")
            if mode == "raise":
                raise RuntimeError("tmdb transport boom")
            if mode == "malformed":
                return {"not": "a list"}
            if mode == "slow":
                await asyncio.sleep(0.4)
            if mode == "empty":
                return []
            fixture = _TMDB_SEARCH.get((media_type, title))
            if fixture is None:
                return []
            return [_tmdb_item_ns(r, media_type) for r in _fixture(fixture)["results"]]
        finally:
            _untrack()

    async def details(media_type, item_id):
        await _track("tmdb", "details")
        try:
            if SCRIPT.mode("tmdb") == "raise":
                raise RuntimeError("tmdb transport boom")
            fixture = _TMDB_DETAILS.get((media_type, item_id))
            if fixture is None:
                return None
            return _tmdb_details_ns(fixture)
        finally:
            _untrack()

    class _SeasonResource:
        def __init__(self, tv_id, season_number):
            self.tv_id = tv_id
            self.season_number = season_number

        async def details(self):
            fixture = _TMDB_SEASONS.get((self.tv_id, self.season_number))
            if fixture is None:
                return types.SimpleNamespace(episodes=[])
            data = _fixture(fixture)
            episodes = []
            for ep in data["episodes"]:
                episodes.append(types.SimpleNamespace(
                    season_number=ep["season_number"],
                    episode_number=ep["episode_number"],
                    name=ep.get("name"),
                    air_date=date.fromisoformat(ep["air_date"]) if ep.get("air_date") else None,
                ))
            return types.SimpleNamespace(
                season_number=data["season_number"], episodes=episodes)

    class _Client:
        def season(self, tv_id, season_number):
            return _SeasonResource(tv_id, season_number)

    def get_tmdb_client():
        return _Client()

    mod.raw_search = raw_search
    mod.details = details
    mod.get_tmdb_client = get_tmdb_client
    return mod


def _normalise_search_id(value):
    # The seam's normalisation (tvdb.py): int passthrough, digit
    # string, or <non-digit prefix>-<digits> slug -> trailing int.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
        head, sep, tail = text.rpartition("-")
        if sep and tail.isdigit() and not head.isdigit():
            return int(tail)
    return None


_TVDB_SEARCH = {
    ("series", "Harbour Lights"): "tvdb_search_series_western.json",
    ("movie", "The Last Ferry"): "tvdb_search_movie_movie.json",
    ("series", "Star Blossom Academy"): "tvdb_search_series_anime.json",
    ("series", "River of Clouds"): "tvdb_search_series_donghua.json",
}
_TVDB_EXTENDED = {
    2001: "tvdb_series_extended_western.json",
    8001: "tvdb_series_extended_anime.json",
    2007: "tvdb_series_extended_multiseason.json",
}
_TVDB_EPISODES = {
    (2001, "default"): "tvdb_episodes_default_western.json",
    (2001, "absolute"): "tvdb_episodes_absolute_western.json",
    (8001, "default"): "tvdb_episodes_default_anime.json",
    (8001, "absolute"): "tvdb_episodes_absolute_anime.json",
    (2007, "default"): "tvdb_episodes_default_multiseason.json",
    (2007, "absolute"): "tvdb_episodes_absolute_multiseason.json",
}


def _make_tvdb_fake():
    mod = types.ModuleType("Backend.helper.metadata.providers.tvdb")

    class TvdbTransportError(Exception):
        pass

    async def search_candidates(title, year=None, entity="series"):
        await _track("tvdb", "search_candidates")
        try:
            mode = SCRIPT.mode("tvdb")
            if mode == "raise":
                raise TvdbTransportError("tvdb down")
            if mode == "malformed":
                return {"not": "a list"}
            if mode == "slow":
                await asyncio.sleep(0.4)
            if mode == "empty":
                return []
            fixture = _TVDB_SEARCH.get((entity, title))
            if fixture is None:
                return []
            out = []
            for row in copy.deepcopy(_fixture(fixture)["data"]):
                raw_id = row.get("tvdb_id") or row.get("id")
                row["tvdb_id_raw"] = raw_id
                row["tvdb_id"] = _normalise_search_id(raw_id)
                out.append(row)
            return out
        finally:
            _untrack()

    async def series_extended(tvdb_id):
        await _track("tvdb", "series_extended")
        try:
            if SCRIPT.mode("tvdb") == "raise":
                raise RuntimeError("tvdb transport boom")
            fixture = _TVDB_EXTENDED.get(tvdb_id)
            return copy.deepcopy(_fixture(fixture)["data"]) if fixture else None
        finally:
            _untrack()

    async def movie_extended(tvdb_id):
        await _track("tvdb", "movie_extended")
        try:
            if tvdb_id == 4001:
                return {"id": 4001, "name": "The Last Ferry",
                        "remoteIds": [{"sourceName": "IMDb", "id": "tt3000001"},
                                      {"sourceName": "TheMovieDB", "id": 3001}]}
            return None
        finally:
            _untrack()

    async def _iter_series_episodes(tvdb_id, order="default"):
        await _track("tvdb", "_iter_series_episodes")
        try:
            if SCRIPT.mode("tvdb") == "raise":
                raise RuntimeError("tvdb transport boom")
            if SCRIPT.mode("tvdb") == "huge":
                return [{"seasonNumber": 1, "number": i + 1,
                         "name": "Episode " + "y" * 140 + f" {i}",
                         "aired": "2020-01-01"} for i in range(3000)]
            fixture = _TVDB_EPISODES.get((tvdb_id, order))
            return copy.deepcopy(_fixture(fixture)["episodes"]) if fixture else []
        finally:
            _untrack()

    mod.TvdbTransportError = TvdbTransportError
    mod.search_candidates = search_candidates
    mod.series_extended = series_extended
    mod.movie_extended = movie_extended
    mod._iter_series_episodes = _iter_series_episodes
    return mod


_CINEMETA_SEARCH = {
    ("tv", "Harbour Lights"): "cinemeta_search_series_western.json",
    ("movie", "The Last Ferry"): "cinemeta_search_movie_movie.json",
    ("tv", "Star Blossom Academy"): "cinemeta_search_series_anime.json",
}


def _make_cinemeta_fake():
    mod = types.ModuleType("Backend.helper.metadata.providers.cinemeta")

    class CinemetaTransportError(Exception):
        pass

    # Production-shaped ONLY (D1): the real search_title_multi()
    # never raises and never returns a non-list — _fetch_json()
    # swallows every non-200/exception to None and the search
    # converts that to []; it therefore has no "raise" mode here.
    # The §18-D14 seam search_candidates() calls _fetch_json()
    # directly and raises CinemetaTransportError when the fetch
    # produced no well-formed body, so an outage is scripted on the
    # seam as mode "raise" (that exact exception type) and a
    # genuine no-match as mode "empty" ([]); neither function ever
    # returns a non-list. cached_detail() returns a dict or None.
    async def search_title_multi(query, type, limit=8):
        await _track("cinemeta", "search_title_multi")
        try:
            if SCRIPT.mode("cinemeta") == "empty":
                return []
            fixture = _CINEMETA_SEARCH.get((type, query))
            return copy.deepcopy(_fixture(fixture)) if fixture else []
        finally:
            _untrack()

    async def search_candidates(query, type, limit=8):
        await _track("cinemeta", "search_candidates")
        try:
            mode = SCRIPT.mode("cinemeta")
            if mode == "raise":
                raise CinemetaTransportError("cinemeta down")
            if mode == "empty":
                return []
            fixture = _CINEMETA_SEARCH.get((type, query))
            rows = copy.deepcopy(_fixture(fixture)) if fixture else []
            return rows[:limit]
        finally:
            _untrack()

    async def cached_detail(imdb_id, media_type):
        await _track("cinemeta", "cached_detail")
        try:
            if imdb_id == "tt1000001":
                return copy.deepcopy(_fixture("cinemeta_detail_western.json"))
            return None
        finally:
            _untrack()

    mod.CinemetaTransportError = CinemetaTransportError
    mod.search_title_multi = search_title_multi
    mod.search_candidates = search_candidates
    mod.cached_detail = cached_detail
    return mod


_KITSU_SEARCH = {
    "Star Blossom Academy": "kitsu_search_anime.json",
    "River of Clouds": "kitsu_search_donghua.json",
    "The Last Ferry": "kitsu_search_movie.json",
}
_ANIZIP_DOCS = {
    5001: "anizip_anime.json",
    5002: "anizip_donghua.json",
    5003: "anizip_movie.json",
    5004: "anizip_skip.json",
    9999: "anizip_large.json",
}


def _make_kitsu_fake():
    mod = types.ModuleType("Backend.helper.metadata.providers.kitsu")

    class KitsuTransportError(Exception):
        pass

    async def search_candidates(query, subtype=None):
        await _track("kitsu", "search_candidates")
        try:
            mode = SCRIPT.mode("kitsu")
            if mode == "raise":
                raise KitsuTransportError("kitsu down")
            if mode == "malformed":
                return {"not": "a list"}
            if mode == "empty":
                return []
            fixture = _KITSU_SEARCH.get(query)
            return copy.deepcopy(_fixture(fixture)["data"]) if fixture else []
        finally:
            _untrack()

    async def get_anizip_mappings(kitsu_id):
        await _track("anizip", "get_anizip_mappings")
        try:
            mode = SCRIPT.mode("anizip")
            if mode == "raise":
                raise RuntimeError("anizip transport boom")
            if mode == "none":
                return None
            fixture = _ANIZIP_DOCS.get(kitsu_id)
            return copy.deepcopy(_fixture(fixture)) if fixture else None
        finally:
            _untrack()

    mod.KitsuTransportError = KitsuTransportError
    mod.search_candidates = search_candidates
    mod.get_anizip_mappings = get_anizip_mappings
    return mod


_ANILIST_SEARCH = {
    "Star Blossom Academy": "anilist_search_anime.json",
    "River of Clouds": "anilist_search_donghua.json",
    "The Last Ferry": "anilist_search_movie.json",
}
_ANILIST_BY_ID = {6001: "anilist_media_by_id.json"}


def _make_anilist_fake():
    mod = types.ModuleType("Backend.helper.metadata.providers.anilist")

    class AnilistTransportError(Exception):
        def __init__(self, message, status=None, retry_after=None):
            super().__init__(message)
            self.status = status
            self.retry_after = retry_after

    async def search_media(name, *, format=None, country=None, media_kind="ANIME"):
        await _track("anilist", "search_media")
        SCRIPT.calls.append(("anilist_args", (format, country, media_kind)))
        try:
            mode = SCRIPT.mode("anilist")
            if mode == "raise":
                raise AnilistTransportError("anilist down", status=503)
            if mode == "raise429":
                raise AnilistTransportError("anilist rate limited",
                                            status=429, retry_after=17)
            if mode == "malformed":
                return {"not": "a list"}
            if mode == "empty":
                return []
            fixture = _ANILIST_SEARCH.get(name)
            return copy.deepcopy(_fixture(fixture)) if fixture else []
        finally:
            _untrack()

    async def media_by_id(anilist_id):
        await _track("anilist", "media_by_id")
        try:
            if SCRIPT.mode("anilist") == "raise":
                raise AnilistTransportError("anilist down", status=503)
            fixture = _ANILIST_BY_ID.get(anilist_id)
            if fixture:
                return copy.deepcopy(_fixture(fixture))
            for search_fixture in _ANILIST_SEARCH.values():
                for entry in _fixture(search_fixture):
                    if entry["id"] == anilist_id:
                        return copy.deepcopy(entry)
            return None
        finally:
            _untrack()

    mod.AnilistTransportError = AnilistTransportError
    mod.search_media = search_media
    mod.media_by_id = media_by_id
    return mod


def _make_episode_maps_fake():
    mod = types.ModuleType("Backend.helper.metadata.episode_maps")

    async def resolve_absolute_episode(absolute, *, anidb_id=None,
                                       anilist_id=None, mal_id=None):
        await _track("episode_maps", "resolve_absolute_episode")
        try:
            return {"season_number": 1, "episode_number": int(absolute),
                    "source": "anime-lists"}
        finally:
            _untrack()

    async def absolute_from_imdb_episode(imdb_id, season, episode, *,
                                         title=None, videos=None):
        return {"absolute_episode": int(episode), "imdb_id": imdb_id,
                "source": "anime-lists"}

    mod.resolve_absolute_episode = resolve_absolute_episode
    mod.absolute_from_imdb_episode = absolute_from_imdb_episode
    return mod


# ------------------------------------------------------------------
# Module wiring
# ------------------------------------------------------------------

def _install_stubs():
    import logging

    pymongo = types.ModuleType("pymongo")
    pymongo.ASCENDING = 1
    errors = types.ModuleType("pymongo.errors")
    errors.DuplicateKeyError = DuplicateKeyError
    pymongo.errors = errors

    httpx_stub = types.ModuleType("httpx")
    httpx_stub.AsyncClient = FakeAsyncClient

    rapidfuzz = types.ModuleType("rapidfuzz")
    fuzz = types.SimpleNamespace(
        token_set_ratio=lambda a, b: 0.0, token_sort_ratio=lambda a, b: 0.0)
    rapidfuzz.fuzz = fuzz

    backend = types.ModuleType("Backend")
    backend.db = fake_db
    config = types.ModuleType("Backend.config")
    config.Telegram = types.SimpleNamespace(TMDB_API="", TVDB_API="")
    logger_mod = types.ModuleType("Backend.logger")
    logger_mod.LOGGER = logging.getLogger("gateway-tests")

    helper = types.ModuleType("Backend.helper")
    metadata_pkg = types.ModuleType("Backend.helper.metadata")
    providers_pkg = types.ModuleType("Backend.helper.metadata.providers")

    common = types.ModuleType("Backend.helper.metadata.common")
    common.caption_with_links = _caption_with_links
    common.year_from_str = _year_from_str
    common.cached_call = _cached_call
    common.ensure_media_ids = lambda payload, seed="": payload
    common.API_SEMAPHORE = asyncio.Semaphore(12)
    common.STRONG_MATCH = 0.92
    common.TVDB_CACHE = {}
    common.TVDB_THRESHOLD = 0.6
    common.KITSU_CACHE = {}
    common.KITSU_THRESHOLD = 0.6
    common.CINEMETA_THRESHOLD = 0.60
    common.IMDB_CACHE = {}
    common.build_query_variants = _build_query_variants
    common.format_imdb_images = lambda imdb_id: {"poster": "", "backdrop": "", "logo": ""}
    common.logo_from_imdb = lambda imdb_id: ""
    common.normalize_rating = lambda value: float(value or 0)
    common.parse_year_range = lambda start=None, end=None: (0, 0)
    common.score_candidate_aliases = lambda *a, **k: 0.0
    common.strip_html = lambda text: text or ""
    # Extra names the REAL tmdb.py imports from common (verified
    # against its import block; only the surface raw_search needs
    # is behaviour-bearing — the rest exist so the import succeeds).
    common.ALT_TITLE_LOOKUPS = 3
    common.ALT_TITLES_CACHE = {}
    common.EPISODE_CACHE = {}
    common.TMDB_DETAILS_CACHE = {}
    common.TMDB_SEARCH_CACHE = {}
    common.TMDB_THRESHOLD = 0.6
    common.format_runtime = lambda value: value or 0
    common.format_tmdb_image = lambda path, size="w500": path or ""
    common.score_candidate = lambda *a, **k: 0.0

    themoviedb_stub = types.ModuleType("themoviedb")

    class _AioTMDbStub:
        def __init__(self, *args, **kwargs):
            pass

    themoviedb_stub.aioTMDb = _AioTMDbStub

    parse = types.ModuleType("Backend.helper.metadata.parse")
    parse.parse_media_name = _parse_media_name
    pyro = types.ModuleType("Backend.helper.pyro")
    pyro.clean_filename = _clean_filename
    pyro.get_readable_file_size = _get_readable_file_size
    split_files = types.ModuleType("Backend.helper.split_files")
    split_files.strip_part_suffix = _strip_part_suffix
    subtitles = types.ModuleType("Backend.helper.subtitles")
    subtitles.is_subtitle_file = _is_subtitle_file
    settings_manager = types.ModuleType("Backend.helper.settings_manager")
    settings_manager.SettingsManager = FakeSettingsManager

    mods = {
        "pymongo": pymongo,
        "pymongo.errors": errors,
        "httpx": httpx_stub,
        "rapidfuzz": rapidfuzz,
        "themoviedb": themoviedb_stub,
        "Backend": backend,
        "Backend.config": config,
        "Backend.logger": logger_mod,
        "Backend.helper": helper,
        "Backend.helper.metadata": metadata_pkg,
        "Backend.helper.metadata.common": common,
        "Backend.helper.metadata.parse": parse,
        "Backend.helper.pyro": pyro,
        "Backend.helper.split_files": split_files,
        "Backend.helper.subtitles": subtitles,
        "Backend.helper.settings_manager": settings_manager,
        "Backend.helper.metadata.providers": providers_pkg,
        "Backend.helper.metadata.providers.tmdb": _make_tmdb_fake(),
        "Backend.helper.metadata.providers.tvdb": _make_tvdb_fake(),
        "Backend.helper.metadata.providers.cinemeta": _make_cinemeta_fake(),
        "Backend.helper.metadata.providers.kitsu": _make_kitsu_fake(),
        "Backend.helper.metadata.providers.anilist": _make_anilist_fake(),
        "Backend.helper.metadata.episode_maps": _make_episode_maps_fake(),
    }
    sys.modules.update(mods)
    helper.metadata = metadata_pkg
    metadata_pkg.common = common
    metadata_pkg.parse = parse


_install_stubs()
store = load_module("Backend.helper.librarian_store", "Backend/helper/librarian_store.py")
gateway = load_module("Backend.helper.librarian_gateway", "Backend/helper/librarian_gateway.py")
anilist_client = load_module("anilist_under_test", "Backend/helper/metadata/providers/anilist.py")
tvdb_real = load_module("tvdb_under_test", "Backend/helper/metadata/providers/tvdb.py")
kitsu_real = load_module("kitsu_under_test", "Backend/helper/metadata/providers/kitsu.py")
cinemeta_real = load_module("cinemeta_under_test", "Backend/helper/metadata/providers/cinemeta.py")
tmdb_real = load_module("tmdb_under_test", "Backend/helper/metadata/providers/tmdb.py")

_ORIG_TTLS = dict(gateway.CACHE_TTL_S)
_ORIG_TIMEOUT = gateway.LEG_TIMEOUT_S
_ORIG_TMDB_GET_CLIENT = tmdb_real.get_tmdb_client
_ORIG_TMDB_DETAILS = tmdb_real.details


def _reset():
    fake_db.dbs["tracking"] = FakeTracking()
    SCRIPT.reset()
    FakeAsyncClient.transport = None
    FakeSettingsManager._current = FakeSettings()
    store._indexes_ready = False
    gateway._indexes_ensured = False
    gateway.CACHE_TTL_S.clear()
    gateway.CACHE_TTL_S.update(_ORIG_TTLS)
    gateway.LEG_TIMEOUT_S = _ORIG_TIMEOUT
    tvdb_real.TVDB_CACHE.clear() if hasattr(tvdb_real, "TVDB_CACHE") else None
    tvdb_real._token = None
    tvdb_real._token_expires = 0.0
    kitsu_real.KITSU_CACHE.clear() if hasattr(kitsu_real, "KITSU_CACHE") else None
    tmdb_real.get_tmdb_client = _ORIG_TMDB_GET_CLIENT
    tmdb_real.details = _ORIG_TMDB_DETAILS
    tmdb_real.TMDB_DETAILS_CACHE.clear() if hasattr(tmdb_real, "TMDB_DETAILS_CACHE") else None
    tmdb_real.TMDB_SEARCH_CACHE.clear() if hasattr(tmdb_real, "TMDB_SEARCH_CACHE") else None


def _cache_docs():
    return fake_db.dbs["tracking"]["provider_cache"].docs


def _cluster_with(result, provider, value):
    for cluster in result["clusters"]:
        if cluster["ids"].get(provider) == value:
            return cluster
    return None


# ------------------------------------------------------------------
# Seam tests — the REAL tvdb/kitsu seam functions over fake httpx
# ------------------------------------------------------------------

def _tvdb_transport(method, url, payload):
    if url.endswith("/login"):
        return FakeResponse(200, {"data": {"token": "tok-123"}})
    if "/search" in url:
        return FakeResponse(200, _fixture("tvdb_search_series_western.json"))
    return FakeResponse(404, {})


def test_tvdb_seam_returns_all_rows_and_normalises_slug():
    _reset()
    FakeAsyncClient.transport = _tvdb_transport

    async def go():
        rows = await tvdb_real.search_candidates("Harbour Lights", entity="series")
        assert len(rows) == 3, rows
        by_name = {r["name"]: r for r in rows}
        assert by_name["Harbour Lights"]["tvdb_id"] == 2001
        assert by_name["Harbour Lights"]["tvdb_id_raw"] == 2001
        slug = by_name["Harbour Lights: Origins"]
        assert slug["tvdb_id"] == 2002, slug
        assert slug["tvdb_id_raw"] == "series-2002", slug
        assert by_name["Harbour Nights"]["tvdb_id"] == 2003
        # The old contract returns a single best dict (or None) —
        # never the list.
        single = await tvdb_real.search("Harbour Lights", entity="series")
        assert not isinstance(single, list)

    run(go())


def test_tvdb_seam_raises_on_outage_and_missing_key():
    _reset()

    async def go():
        FakeAsyncClient.transport = lambda m, u, p: (
            FakeResponse(200, {"data": {"token": "t"}}) if u.endswith("/login")
            else FakeResponse(500, {}))
        try:
            await tvdb_real.search_candidates("Harbour Lights", entity="series")
            raise AssertionError("expected TvdbTransportError on HTTP 500")
        except tvdb_real.TvdbTransportError:
            pass
        FakeSettingsManager._current.tvdb_api = ""
        tvdb_real._token = None
        tvdb_real._token_expires = 0.0
        try:
            await tvdb_real.search_candidates("Harbour Lights", entity="series")
            raise AssertionError("expected TvdbTransportError on missing key")
        except tvdb_real.TvdbTransportError:
            pass

    run(go())


def test_tvdb_seam_all_nondict_rows_raise_partial_rows_skipped():
    _reset()

    async def go():
        def all_bad(method, url, payload):
            if url.endswith("/login"):
                return FakeResponse(200, {"data": {"token": "tok"}})
            return FakeResponse(200, {"data": ["junk", 42, None]})

        FakeAsyncClient.transport = all_bad
        try:
            await tvdb_real.search_candidates("Harbour Lights", entity="series")
            raise AssertionError("expected TvdbTransportError when every row is non-dict")
        except tvdb_real.TvdbTransportError:
            pass

        def partial(method, url, payload):
            if url.endswith("/login"):
                return FakeResponse(200, {"data": {"token": "tok"}})
            return FakeResponse(200, {"data": [
                "junk", {"id": 2001, "name": "Harbour Lights"}]})

        FakeAsyncClient.transport = partial
        rows = await tvdb_real.search_candidates("Harbour Lights", entity="series")
        assert len(rows) == 1 and rows[0]["tvdb_id"] == 2001, rows

    run(go())


def test_kitsu_seam_malformed_payload_message_is_static():
    _reset()

    async def go():
        FakeAsyncClient.transport = lambda m, u, p: FakeResponse(
            200, {"data": {"not": "a list"}})
        try:
            await kitsu_real.search_candidates("Star Blossom Academy")
            raise AssertionError("expected KitsuTransportError on malformed payload")
        except kitsu_real.KitsuTransportError as e:
            # The raise must not embed the query (hardening parity
            # with its sibling raise).
            assert "Star Blossom Academy" not in str(e), str(e)

    run(go())


def test_kitsu_seam_wrapper_returns_raw_list_and_raises():
    _reset()

    async def go():
        FakeAsyncClient.transport = lambda m, u, p: FakeResponse(
            200, {"data": _fixture("kitsu_search_anime.json")["data"]})
        rows = await kitsu_real.search_candidates("Star Blossom Academy")
        assert isinstance(rows, list) and len(rows) == 1
        assert rows[0]["id"] == "5001"
        assert rows[0]["attributes"]["canonicalTitle"] == "Star Blossom Academy"
        FakeAsyncClient.transport = lambda m, u, p: FakeResponse(503, {})
        try:
            await kitsu_real.search_candidates("Star Blossom Academy")
            raise AssertionError("expected KitsuTransportError on HTTP 503")
        except kitsu_real.KitsuTransportError:
            pass

    run(go())


_CINEMETA_RAW_METAS = [
    {"id": "tt1000001", "imdb_id": "tt1000001", "name": "Harbour Lights",
     "releaseInfo": "2019–2021", "poster": "https://example.invalid/p.jpg"},
    {"id": "tt1000009", "name": "Harbour Lights: Origins",
     "releaseInfo": "2022", "poster": ""},
]


def test_cinemeta_seam_returns_converted_rows_and_empty_is_valid():
    _reset()

    async def go():
        FakeAsyncClient.transport = lambda m, u, p: FakeResponse(
            200, {"metas": copy.deepcopy(_CINEMETA_RAW_METAS)})
        rows = await cinemeta_real.search_candidates("Harbour Lights", "tv")
        # Same converted row shape search_title_multi() produces.
        assert rows == [
            {"id": "tt1000001", "type": "tv", "title": "Harbour Lights",
             "year": "2019–2021", "poster": "https://example.invalid/p.jpg"},
            {"id": "tt1000009", "type": "tv",
             "title": "Harbour Lights: Origins", "year": "2022",
             "poster": ""},
        ], rows
        # The existing function is unchanged over the same payload.
        assert await cinemeta_real.search_title_multi(
            "Harbour Lights", "tv") == rows
        # A well-formed zero-result response is a valid empty list,
        # NOT an outage.
        FakeAsyncClient.transport = lambda m, u, p: FakeResponse(
            200, {"metas": []})
        assert await cinemeta_real.search_candidates("Nothing", "tv") == []

    run(go())


def test_cinemeta_seam_raises_on_outage_shapes():
    _reset()

    async def go():
        # Production outage shapes: _fetch_json() returns None on a
        # non-200 response and on any transport exception alike;
        # search_title_multi() collapses both to [] (asserted here
        # to pin that the existing behaviour is untouched), while
        # the seam raises CinemetaTransportError for both.
        FakeAsyncClient.transport = lambda m, u, p: FakeResponse(500, {})
        assert await cinemeta_real.search_title_multi(
            "Harbour Lights", "tv") == []
        try:
            await cinemeta_real.search_candidates("Harbour Lights", "tv")
            raise AssertionError("expected CinemetaTransportError on HTTP 500")
        except cinemeta_real.CinemetaTransportError as e:
            # The raise must not embed the query (hardening parity
            # with the Kitsu seam's static messages).
            assert "Harbour Lights" not in str(e), str(e)

        def boom(method, url, payload):
            raise RuntimeError("connection reset")

        FakeAsyncClient.transport = boom
        try:
            await cinemeta_real.search_candidates("Harbour Lights", "tv")
            raise AssertionError(
                "expected CinemetaTransportError on transport exception")
        except cinemeta_real.CinemetaTransportError:
            pass

    run(go())


def test_cinemeta_seam_malformed_bodies_raise_partial_rows_skipped():
    _reset()

    async def go():
        # Body is not a dict at all.
        FakeAsyncClient.transport = lambda m, u, p: FakeResponse(
            200, ["not", "a", "dict"])
        try:
            await cinemeta_real.search_candidates("Harbour Lights", "tv")
            raise AssertionError("expected CinemetaTransportError on non-dict body")
        except cinemeta_real.CinemetaTransportError:
            pass
        # No metas list in the body.
        FakeAsyncClient.transport = lambda m, u, p: FakeResponse(
            200, {"metas": {"not": "a list"}})
        try:
            await cinemeta_real.search_candidates("Harbour Lights", "tv")
            raise AssertionError("expected CinemetaTransportError on non-list metas")
        except cinemeta_real.CinemetaTransportError:
            pass
        # Every row malformed is a schema failure, not an empty.
        FakeAsyncClient.transport = lambda m, u, p: FakeResponse(
            200, {"metas": ["junk", 42, None]})
        try:
            await cinemeta_real.search_candidates("Harbour Lights", "tv")
            raise AssertionError(
                "expected CinemetaTransportError when every row is non-dict")
        except cinemeta_real.CinemetaTransportError:
            pass
        # A PARTIAL mix is tolerated: non-dict rows are skipped.
        FakeAsyncClient.transport = lambda m, u, p: FakeResponse(
            200, {"metas": ["junk", copy.deepcopy(_CINEMETA_RAW_METAS[0])]})
        rows = await cinemeta_real.search_candidates("Harbour Lights", "tv")
        assert len(rows) == 1 and rows[0]["id"] == "tt1000001", rows

    run(go())


# ------------------------------------------------------------------
# AniList client tests — the REAL client over fake httpx
# ------------------------------------------------------------------

def _anilist_ok_transport(body_data):
    posted = []

    def transport(method, url, payload):
        posted.append(payload)
        return FakeResponse(200, {"data": body_data})

    transport.posted = posted
    return transport


def test_anilist_client_search_ok_and_variable_omission():
    _reset()
    media = _fixture("anilist_search_anime.json")
    transport = _anilist_ok_transport({"Page": {"media": media}})
    FakeAsyncClient.transport = transport

    async def go():
        rows = await anilist_client.search_media(
            "Star Blossom Academy", format="TV", country="JP")
        assert rows == media
        variables = transport.posted[-1]["variables"]
        assert variables == {"search": "Star Blossom Academy",
                             "format": "TV", "country": "JP"}, variables
        assert "type: ANIME" in transport.posted[-1]["query"]
        # Untyped: format/country variables are omitted entirely.
        rows = await anilist_client.search_media("Star Blossom Academy")
        assert rows == media
        variables = transport.posted[-1]["variables"]
        assert variables == {"search": "Star Blossom Academy"}, variables

    run(go())


def test_anilist_client_429_surfaces_retry_after():
    _reset()
    spec = _fixture("anilist_429.json")
    FakeAsyncClient.transport = lambda m, u, p: FakeResponse(
        spec["status"], spec["body"], headers=spec["headers"])

    async def go():
        try:
            await anilist_client.search_media("Star Blossom Academy")
            raise AssertionError("expected AnilistTransportError")
        except anilist_client.AnilistTransportError as e:
            assert e.status == 429, e.status
            assert e.retry_after == 17, e.retry_after

    run(go())


def test_anilist_client_errors_without_data_is_down_and_empty_page_is_empty():
    _reset()

    async def go():
        FakeAsyncClient.transport = lambda m, u, p: FakeResponse(
            200, {"errors": [{"message": "boom"}]})
        try:
            await anilist_client.search_media("Star Blossom Academy")
            raise AssertionError("expected AnilistTransportError")
        except anilist_client.AnilistTransportError:
            pass
        # Partial errors WITH usable data returns the data.
        media = _fixture("anilist_search_anime.json")
        FakeAsyncClient.transport = lambda m, u, p: FakeResponse(
            200, {"data": {"Page": {"media": media}},
                  "errors": [{"message": "partial"}]})
        assert await anilist_client.search_media("Star Blossom Academy") == media
        # Well-formed empty Page returns [].
        FakeAsyncClient.transport = _anilist_ok_transport({"Page": {"media": []}})
        assert await anilist_client.search_media("Nothing") == []

    run(go())


def test_anilist_client_media_by_id():
    _reset()
    media = _fixture("anilist_media_by_id.json")
    FakeAsyncClient.transport = _anilist_ok_transport({"Media": media})

    async def go():
        assert await anilist_client.media_by_id(6001) == media
        FakeAsyncClient.transport = _anilist_ok_transport({"Media": None})
        assert await anilist_client.media_by_id(424242) is None

    run(go())


# ------------------------------------------------------------------
# TMDB provider tests — the REAL raw_search over a fake themoviedb
# client returning v2 collection objects (regression: the gateway's
# TMDB leg reported `down` on EVERY lookup because raw_search
# returned themoviedb's TVs/Movies collection — not a list — and
# the gateway's isinstance(items, list) defence raised _LegDown.
# The 454-test suite never caught it because the gateway fake's
# raw_search returned plain lists: a more generous fake than
# production. These tests pin the production shape.)
# ------------------------------------------------------------------

class _TmdbResultCollection:
    """Recorded-shape stand-in for themoviedb's ``ResultWithPage``
    (the base of ``TVs`` / ``Movies``), recorded from the
    themoviedb 1.0.2 (uv.lock pin) and 2.0.2 wheels
    (``themoviedb/schemas/_result.py``, ``schemas/tv.py``,
    ``schemas/movies.py``): a dataclass holding the page's items
    in ``.results`` plus ``page`` / ``total_pages`` /
    ``total_results``; ``__iter__`` / ``__getitem__`` / ``__len__``
    / ``__bool__`` all delegate to ``.results``. Crucially it is
    NOT a ``list`` and does not subclass one — that single fact is
    the defect under test. (The real library is not importable in
    this environment, so the actual ``TVs`` class cannot be used
    here; this stand-in reproduces exactly the access pattern the
    provider and gateway code exercise.)"""

    def __init__(self, results=None, page=1, total_pages=1,
                 total_results=None):
        self.results = results
        self.page = page
        self.total_pages = total_pages
        self.total_results = (
            len(results) if total_results is None and results is not None
            else total_results)

    def __bool__(self):
        return bool(self.results)

    def __iter__(self):
        if self.results is None:
            return iter(())
        return iter(self.results)

    def __getitem__(self, index):
        if self.results is None:
            raise IndexError("Result is empty")
        return self.results[index]

    def __len__(self):
        if self.results is None:
            return 0
        return len(self.results)


class _FakeTVs(_TmdbResultCollection):
    """themoviedb ``TVs`` (search/tv payload)."""


class _FakeMovies(_TmdbResultCollection):
    """themoviedb ``Movies`` (search/movie payload)."""


def _tmdb_partial_ns(row, media_type):
    """A PartialTV / PartialMovie stand-in: attribute objects whose
    date fields are real ``datetime.date`` objects (as themoviedb's
    dataclasses carry), not the gateway fake's year-only namespace."""
    ns = types.SimpleNamespace(id=row["id"],
                               overview=row.get("overview", ""),
                               poster_path=row.get("poster_path"))
    if media_type == "movie":
        ns.title = row.get("title", "")
        ns.original_title = row.get("original_title", "")
        ns.release_date = (date.fromisoformat(row["release_date"])
                           if row.get("release_date") else None)
    else:
        ns.name = row.get("name", "")
        ns.original_name = row.get("original_name", "")
        ns.first_air_date = (date.fromisoformat(row["first_air_date"])
                             if row.get("first_air_date") else None)
    return ns


class _FakeTmdbSearchResource:
    """``aioTMDb.search()`` stand-in: tv()/movies() return the
    collection objects, never lists — the production shape."""

    async def tv(self, query, **kwargs):
        fixture = _TMDB_SEARCH.get(("tv", query))
        items = ([_tmdb_partial_ns(r, "tv") for r in _fixture(fixture)["results"]]
                 if fixture else [])
        return _FakeTVs(results=items)

    async def movies(self, query, **kwargs):
        fixture = _TMDB_SEARCH.get(("movie", query))
        items = ([_tmdb_partial_ns(r, "movie") for r in _fixture(fixture)["results"]]
                 if fixture else [])
        return _FakeMovies(results=items)


class _FakeTmdbClient:
    def search(self):
        return _FakeTmdbSearchResource()


def _use_fake_tmdb_client():
    tmdb_real.get_tmdb_client = lambda: _FakeTmdbClient()


async def _real_tmdb_details(media_type, item_id):
    fixture = _TMDB_DETAILS.get((media_type, item_id))
    return _tmdb_details_ns(fixture) if fixture else None


def test_tmdb_raw_search_coerces_tv_collection_to_list():
    _reset()
    # Pin the stand-in's production shape first: if this ever
    # becomes a list, the regression tests below stop meaning
    # anything.
    assert not isinstance(_FakeTVs(results=[]), list)
    _use_fake_tmdb_client()

    async def go():
        results = await tmdb_real.raw_search("Harbour Lights", "tv", None)
        assert isinstance(results, list), type(results)
        assert [r.id for r in results] == [1001], results
        assert results[0].name == "Harbour Lights"
        # A well-formed zero-result search is an empty LIST, not an
        # empty collection (the gateway leg must read it `empty`,
        # never `down`).
        empty = await tmdb_real.raw_search("No Such Title", "tv", None)
        assert isinstance(empty, list) and empty == [], empty

    run(go())


def test_tmdb_raw_search_coerces_movie_collection_to_list():
    _reset()
    assert not isinstance(_FakeMovies(results=[]), list)
    _use_fake_tmdb_client()

    async def go():
        # With a year (the movies(query, year=year) call shape) and
        # without one — both go through the same coercion.
        for year in (2021, None):
            results = await tmdb_real.raw_search("The Last Ferry", "movie", year)
            assert isinstance(results, list), (year, type(results))
            assert [r.id for r in results] == [3001], results
            assert results[0].title == "The Last Ferry"
        empty = await tmdb_real.raw_search("No Such Title", "movie", None)
        assert isinstance(empty, list) and empty == [], empty

    run(go())


def _lookup_with_real_tmdb(name, media_type):
    """Run gateway.lookup with the REAL tmdb provider wired in
    (fake themoviedb client returning collection objects; the
    provider's details() is the fixture namespace, as in the
    gateway fake — the search path under test stays fully real)."""
    key = "Backend.helper.metadata.providers.tmdb"
    original = sys.modules[key]
    _use_fake_tmdb_client()
    tmdb_real.details = _real_tmdb_details
    sys.modules[key] = tmdb_real
    holder = {}

    async def go():
        holder["result"] = await gateway.lookup(name, media_type)

    try:
        run(go())  # _loader.run discards the coroutine's return value
    finally:
        sys.modules[key] = original
    return holder["result"]


def test_tmdb_leg_ok_with_real_provider_tv_collection():
    _reset()

    result = _lookup_with_real_tmdb("Harbour Lights", "tv")
    # Before the provider fix this leg was `down` on every lookup
    # (raw_search returned a TVs collection, gateway raised
    # _LegDown on the non-list payload).
    assert result["legs"]["tmdb"]["state"] == "ok", result["legs"]
    assert "tmdb" not in result["providers_down"]
    assert result["providers_down"] == [], result["legs"]
    main = _cluster_with(result, "tmdb", 1001)
    assert main is not None
    assert main["ids"] == {"tmdb": 1001, "tvdb": 2001, "imdb": "tt1000001"}, main
    assert main["year"] == 2019


def test_tmdb_leg_ok_with_real_provider_movie_collection():
    _reset()

    result = _lookup_with_real_tmdb("The Last Ferry", "movie")
    assert result["legs"]["tmdb"]["state"] == "ok", result["legs"]
    assert "tmdb" not in result["providers_down"]
    assert result["providers_down"] == [], result["legs"]
    main = _cluster_with(result, "tmdb", 3001)
    assert main is not None
    assert main["ids"] == {"tmdb": 3001, "tvdb": 4001, "imdb": "tt3000001",
                           "kitsu": 5003, "anilist": 6003, "mal": 7003}, main
    assert main["year"] == 2021


# ------------------------------------------------------------------
# Gateway lookup: candidate lists, clustering, end-to-end cases
# ------------------------------------------------------------------

def test_lookup_western_series_end_to_end():
    _reset()

    async def go():
        result = await gateway.lookup("Harbour Lights", "tv")
        assert result["providers_down"] == []
        for provider in ("tmdb", "tvdb", "cinemeta"):
            assert result["legs"][provider]["state"] == "ok", result["legs"]
        assert result["legs"]["kitsu"]["state"] == "not_queried"
        assert result["legs"]["anilist"]["state"] == "not_queried"
        assert result["legs"]["anizip"]["state"] == "not_queried"
        main = _cluster_with(result, "tvdb", 2001)
        assert main is not None
        assert main["ids"] == {"tmdb": 1001, "tvdb": 2001, "imdb": "tt1000001"}, main
        assert main["year"] == 2019
        assert main["media_type"] == "tv"
        assert "Harbour Lights" in main["titles"]
        # TVDB returned more than its old single best: the slug row
        # and the third row are their own clusters.
        assert _cluster_with(result, "tvdb", 2002) is not None
        assert _cluster_with(result, "tvdb", 2003) is not None
        assert len(result["clusters"]) == 3, result["clusters"]

    run(go())


def test_lookup_movie_end_to_end():
    _reset()

    async def go():
        result = await gateway.lookup("The Last Ferry", "movie")
        assert result["providers_down"] == [], result["legs"]
        main = _cluster_with(result, "tmdb", 3001)
        assert main is not None
        assert main["ids"] == {"tmdb": 3001, "tvdb": 4001, "imdb": "tt3000001",
                               "kitsu": 5003, "anilist": 6003, "mal": 7003}, main
        assert main["year"] == 2021
        assert main["media_type"] == "movie"
        # AniList was queried with the MOVIE format mapping.
        arg_calls = [args for p, args in SCRIPT.calls if p == "anilist_args"]
        assert ("MOVIE", None, "ANIME") in arg_calls, arg_calls

    run(go())


def test_lookup_anime_end_to_end_clusters_by_explicit_ids():
    _reset()

    async def go():
        result = await gateway.lookup("Star Blossom Academy", "anime")
        assert result["providers_down"] == [], result["legs"]
        for provider in ("tmdb", "tvdb", "cinemeta", "kitsu", "anilist", "anizip"):
            assert result["legs"][provider]["state"] == "ok", result["legs"]
        assert len(result["clusters"]) == 1, result["clusters"]
        cluster = result["clusters"][0]
        assert cluster["ids"] == {"tmdb": 9001, "tvdb": 8001, "imdb": "tt5000001",
                                  "kitsu": 5001, "anilist": 6001, "mal": 7001}, cluster
        assert cluster["year"] == 2022
        assert "Star Blossom Academy" in cluster["titles"]
        assert "Hoshizaki Academy" in cluster["titles"]

    run(go())


def test_lookup_donghua_end_to_end():
    _reset()

    async def go():
        result = await gateway.lookup("River of Clouds", "donghua")
        assert result["providers_down"] == [], result["legs"]
        assert len(result["clusters"]) == 1, result["clusters"]
        cluster = result["clusters"][0]
        assert cluster["ids"] == {"tmdb": 9002, "tvdb": 8002, "kitsu": 5002,
                                  "anilist": 6002, "mal": 7002}, cluster
        assert cluster["year"] == 2023
        arg_calls = [args for p, args in SCRIPT.calls if p == "anilist_args"]
        assert ("TV", "CN", "ANIME") in arg_calls, arg_calls

    run(go())


def test_clustering_title_only_never_merges():
    _reset()

    async def go():
        result = await gateway.lookup("Harbour Lights", "tv")
        main = _cluster_with(result, "tvdb", 2001)
        origins = _cluster_with(result, "tvdb", 2002)
        assert main is not origins
        assert "Harbour Lights: Origins" not in main["titles"]
        assert origins["ids"] == {"tvdb": 2002}, origins

    run(go())


def test_anilist_relations_data_only_not_cluster_join_keys():
    _reset()
    # Production-shaped AniList payload (mirrors the real GraphQL
    # Media shape): work 6001 carries a SEQUEL relation to the
    # different work 6009, which is also returned as its own
    # candidate below. Relations are data only — if the gateway
    # used them as join keys, 6001 and 6009 would merge.
    related_title = {"romaji": "Hoshizaki Academy II",
                     "english": "Star Blossom Academy II",
                     "native": "星咲学園II"}
    main_media = {
        "id": 6001, "idMal": 7001, "format": "TV",
        "countryOfOrigin": "JP", "seasonYear": 2022,
        "title": {"romaji": "Hoshizaki Academy",
                  "english": "Star Blossom Academy",
                  "native": "星咲学園"},
        "synonyms": ["SBA"],
        "relations": {"edges": [
            {"relationType": "SEQUEL",
             "node": {"id": 6009, "title": related_title}}]},
    }
    related_media = {
        "id": 6009, "idMal": 7009, "format": "TV",
        "countryOfOrigin": "JP", "seasonYear": 2024,
        "title": related_title,
        "synonyms": [],
        "relations": {"edges": []},
    }

    async def go():
        # The real client returns relations from a recorded payload,
        # on both the search and by-id selection sets.
        transport = _anilist_ok_transport({"Page": {"media": [main_media]}})
        FakeAsyncClient.transport = transport
        rows = await anilist_client.search_media(
            "Star Blossom Academy", format="TV", country="JP")
        assert rows == [main_media], rows
        assert rows[0]["relations"] == main_media["relations"]
        assert "relations" in transport.posted[-1]["query"]
        transport = _anilist_ok_transport({"Media": main_media})
        FakeAsyncClient.transport = transport
        by_id = await anilist_client.media_by_id(6001)
        assert by_id == main_media, by_id
        assert by_id["relations"]["edges"][0]["node"]["id"] == 6009
        assert "relations" in transport.posted[-1]["query"]
        FakeAsyncClient.transport = None

        # Gateway: the AniList leg returns both works, the first
        # carrying the relation to the second. Explicit-ID-only
        # clustering keeps them in separate clusters.
        fake_anilist = sys.modules["Backend.helper.metadata.providers.anilist"]
        original_search = fake_anilist.search_media

        async def search_with_relations(name, *, format=None, country=None,
                                        media_kind="ANIME"):
            return copy.deepcopy([main_media, related_media])

        fake_anilist.search_media = search_with_relations
        try:
            result = await gateway.lookup("Star Blossom Academy", "anime")
        finally:
            fake_anilist.search_media = original_search
        assert result["providers_down"] == [], result["legs"]
        assert len(result["clusters"]) == 2, result["clusters"]
        main = _cluster_with(result, "anilist", 6001)
        assert main is not None
        assert main["ids"] == {"tmdb": 9001, "tvdb": 8001,
                               "imdb": "tt5000001", "kitsu": 5001,
                               "anilist": 6001, "mal": 7001}, main
        assert "Star Blossom Academy II" not in main["titles"]
        related = _cluster_with(result, "anilist", 6009)
        assert related is not None and related is not main
        assert related["ids"] == {"anilist": 6009, "mal": 7009}, related

    run(go())


# ------------------------------------------------------------------
# Leg classification
# ------------------------------------------------------------------

def test_leg_states_down_empty_malformed():
    _reset()

    async def go():
        # Raise/empty classification for every lookup leg, Cinemeta
        # included: since the §18-D14 seam, the gateway's Cinemeta
        # leg consumes search_candidates(), whose production failure
        # shape is a CinemetaTransportError raise — so a Cinemeta
        # `down` state IS attainable, exactly like the TVDB/Kitsu
        # seam legs. (search_title_multi() still collapses an outage
        # to [], but the gateway no longer calls it.)
        for provider in ("tmdb", "tvdb", "kitsu", "anilist", "cinemeta"):
            SCRIPT.reset()
            fake_db.dbs["tracking"] = FakeTracking()
            SCRIPT.modes[provider] = "raise"
            result = await gateway.lookup("Star Blossom Academy", "anime")
            assert result["legs"][provider]["state"] == "down", (provider, result["legs"])
            assert provider in result["providers_down"]
            SCRIPT.modes[provider] = "empty"
            result = await gateway.lookup("Star Blossom Academy", "anime")
            assert result["legs"][provider]["state"] == "empty", (provider, result["legs"])
            assert provider not in result["providers_down"]
            SCRIPT.modes.pop(provider, None)
        # Malformed (non-list) payloads classify down for the legs
        # whose gateway fakes script that gateway-defence shape.
        # The Cinemeta seam never returns a non-list in production
        # (it raises instead), so it has no malformed mode (D1).
        for provider in ("tmdb", "tvdb", "kitsu", "anilist"):
            SCRIPT.reset()
            fake_db.dbs["tracking"] = FakeTracking()
            SCRIPT.modes[provider] = "malformed"
            result = await gateway.lookup("Star Blossom Academy", "anime")
            assert result["legs"][provider]["state"] == "down", (provider, result["legs"])
            SCRIPT.modes.pop(provider, None)

    run(go())


def test_cinemeta_outage_is_down_and_not_cached():
    _reset()
    # Regression (DESIGN §18-D14, replacing the Phase 2 known
    # deviation pinned by the old
    # test_cinemeta_outage_surfaces_as_empty_known_deviation):
    # scripted the production way the seam surfaces an outage —
    # search_candidates() raising CinemetaTransportError, the shape
    # the real seam produces when _fetch_json() returns None on a
    # non-200/exception — the gateway leg is `down`, Cinemeta is in
    # providers_down, and the result is NOT cached as a miss.
    SCRIPT.modes["cinemeta"] = "raise"

    async def go():
        result = await gateway.lookup("Star Blossom Academy", "anime")
        assert result["legs"]["cinemeta"]["state"] == "down", result["legs"]
        assert "cinemeta" in result["providers_down"]
        assert _cache_docs() == [], _cache_docs()
        # Recovery: the next identical call re-queries, succeeds,
        # and only then is the result cached.
        SCRIPT.modes.pop("cinemeta")
        result = await gateway.lookup("Star Blossom Academy", "anime")
        assert result["legs"]["cinemeta"]["state"] == "ok", result["legs"]
        assert result["providers_down"] == []
        assert len(_cache_docs()) == 1, _cache_docs()

    run(go())


def test_cinemeta_genuine_empty_is_empty_and_cached():
    _reset()
    # A well-formed zero-result response from the seam ([]) is a
    # genuine no-match: leg `empty`, never a provider down, and —
    # with no leg down — the result is cacheable.
    SCRIPT.modes["cinemeta"] = "empty"

    async def go():
        result = await gateway.lookup("Star Blossom Academy", "anime")
        assert result["legs"]["cinemeta"]["state"] == "empty", result["legs"]
        assert "cinemeta" not in result["providers_down"]
        assert result["providers_down"] == [], result["legs"]
        assert len(_cache_docs()) == 1, _cache_docs()

    run(go())


def test_cinemeta_results_ok_with_pinned_candidate_shape():
    _reset()

    async def go():
        result = await gateway.lookup("Harbour Lights", "tv")
        assert result["legs"]["cinemeta"]["state"] == "ok", result["legs"]
        assert "cinemeta" not in result["providers_down"]
        # The seam's rows flow through _cinemeta_candidate into the
        # pinned cluster shape: the Cinemeta-sourced IMDb ID joins
        # the TMDB/TVDB cluster by explicit shared ID.
        main = _cluster_with(result, "imdb", "tt1000001")
        assert main is not None
        assert main["ids"] == {"tmdb": 1001, "tvdb": 2001,
                               "imdb": "tt1000001"}, main
        assert "Harbour Lights" in main["titles"]
        assert main["year"] == 2019
        assert main["media_type"] == "tv"
        assert SCRIPT.count("cinemeta", "search_candidates") >= 1
        assert SCRIPT.count("cinemeta", "search_title_multi") == 0

    run(go())


def test_leg_timeout_is_down():
    _reset()
    gateway.LEG_TIMEOUT_S = 0.05
    SCRIPT.modes["tvdb"] = "slow"

    async def go():
        result = await gateway.lookup("Harbour Lights", "tv")
        assert result["legs"]["tvdb"]["state"] == "down", result["legs"]
        assert "tvdb" in result["providers_down"]
        assert result["legs"]["tmdb"]["state"] == "ok"

    run(go())


def test_anilist_429_leg_down_with_retry_after_and_no_retry():
    _reset()
    SCRIPT.modes["anilist"] = "raise429"

    async def go():
        result = await gateway.lookup("Star Blossom Academy", "anime")
        leg = result["legs"]["anilist"]
        assert leg["state"] == "down", leg
        assert leg["retry_after"] == 17, leg
        assert "anilist" in result["providers_down"]
        assert SCRIPT.count("anilist", "search_media") == 1

    run(go())


def test_anizip_leg_states():
    _reset()

    async def go():
        SCRIPT.modes["anizip"] = "none"
        result = await gateway.lookup("Star Blossom Academy", "anime")
        assert result["legs"]["anizip"]["state"] == "down", result["legs"]
        assert "anizip" in result["providers_down"]

    run(go())


# ------------------------------------------------------------------
# Cache discipline: outage never cached, TTLs, key format
# ------------------------------------------------------------------

def test_outage_not_cached_then_success_replaces_and_is_cached():
    _reset()

    async def go():
        SCRIPT.modes["tvdb"] = "raise"
        result = await gateway.lookup("Harbour Lights", "tv")
        assert "tvdb" in result["providers_down"]
        assert _cache_docs() == [], _cache_docs()
        SCRIPT.modes.pop("tvdb")
        result = await gateway.lookup("Harbour Lights", "tv")
        assert result["providers_down"] == []
        assert len(_cache_docs()) == 1
        calls_before = SCRIPT.count("tvdb", "search_candidates")
        again = await gateway.lookup("Harbour Lights", "tv")
        assert again == result
        assert SCRIPT.count("tvdb", "search_candidates") == calls_before

    run(go())


def test_cached_empty_result_is_served_from_cache():
    _reset()

    async def go():
        result = await gateway.lookup("No Such Title Anywhere", "tv")
        assert result["clusters"] == []
        assert result["legs"]["tmdb"]["state"] == "empty"
        assert len(_cache_docs()) == 1
        calls_before = len(SCRIPT.calls)
        again = await gateway.lookup("No Such Title Anywhere", "tv")
        assert again == result
        assert len(SCRIPT.calls) == calls_before

    run(go())


def test_cache_key_format_and_name_normalisation():
    _reset()

    async def go():
        await gateway.lookup("  Harbour   Lights ", "tv")
        assert len(_cache_docs()) == 1
        key = _cache_docs()[0]["key"]
        assert key == 'gw:v1:lookup:{"media_type":"tv","name":"harbour lights"}', key

    run(go())


def test_cache_ttl_constants_and_expiry():
    _reset()
    assert gateway.CACHE_TTL_S == {"lookup": 86400, "series": 604800,
                                   "crossid": 2592000}

    async def go():
        await gateway.lookup("Harbour Lights", "tv")
        await gateway.crossid("tvdb:2001")
        assert len(_cache_docs()) == 2
        # Expire both rows by hand (deterministic; no sleeping):
        # the next calls must miss and re-query providers.
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        for doc in _cache_docs():
            doc["expires_at"] = past
        calls_before = SCRIPT.count("tvdb", "search_candidates")
        await gateway.lookup("Harbour Lights", "tv")
        assert SCRIPT.count("tvdb", "search_candidates") > calls_before
        calls_before = SCRIPT.count("tvdb", "series_extended")
        await gateway.crossid("tvdb:2001")
        assert SCRIPT.count("tvdb", "series_extended") > calls_before
        # Shortened TTLs are honoured on write.
        gateway.CACHE_TTL_S["lookup"] = 60
        fake_db.dbs["tracking"] = FakeTracking()
        before = datetime.now(timezone.utc)
        await gateway.lookup("Harbour Lights", "tv")
        doc = _cache_docs()[0]
        delta = (doc["expires_at"] - before).total_seconds()
        # Phase 4 A2 (QA flake hunt, 2026-11-20): window is 59-65, not
        # 59-61 — do not "tighten" it back. delta is truly 60 + the
        # elapsed time from the `before` capture above to the gateway's
        # internal write timestamp, bounded by the awaited lookup's
        # duration and load-dependent on the Narzo; 65 caps that
        # scheduling slack at 5 s. The 59 lower bound still proves the
        # 60 s TTL constant, and 65 still catches a wrong TTL (the real
        # TTLs differ by orders of magnitude: 60 vs 86,400 here).
        assert 59 <= delta <= 65, delta

    run(go())


def test_series_cache_ttl_expiry():
    _reset()
    # Mirrors the lookup/crossid hand-expired-row cases in
    # test_cache_ttl_constants_and_expiry, for the series cache
    # (TTL 604800 s): a live row is served without re-querying
    # providers; a hand-expired row is re-queried, not served.

    async def go():
        result = await gateway.series("tvdb:2001")
        assert len(_cache_docs()) == 1, _cache_docs()
        calls_before = SCRIPT.count("tvdb", "_iter_series_episodes")
        assert calls_before > 0
        again = await gateway.series("tvdb:2001")
        assert again == result
        assert SCRIPT.count("tvdb", "_iter_series_episodes") == calls_before
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        for doc in _cache_docs():
            doc["expires_at"] = past
        await gateway.series("tvdb:2001")
        assert SCRIPT.count("tvdb", "_iter_series_episodes") > calls_before

    run(go())


# ------------------------------------------------------------------
# AniZip projection regression (DESIGN §13 named test)
# ------------------------------------------------------------------

def test_anizip_projection_regression():
    _reset()
    large = _fixture("anizip_large.json")
    raw_size = len(json.dumps(large, ensure_ascii=False).encode("utf-8"))
    assert raw_size > 53 * 1024, raw_size
    projected = gateway.project_anizip(large)
    projected_size = len(json.dumps(projected, ensure_ascii=False).encode("utf-8"))
    assert projected_size <= 4 * 1024, projected_size
    assert projected["mappings"] == large["mappings"]
    assert "images" not in projected
    for ep in projected["episodes"].values():
        assert set(ep.keys()) == {"title", "airDate", "seasonNumber",
                                  "episodeNumber", "absoluteEpisodeNumber"}, ep

    async def go():
        # An ID lookup over the large document returns IDs only —
        # never the document.
        result = await gateway.crossid("kitsu:9999")
        assert result["ids"] == {"kitsu": 9999, "anilist": 6001, "mal": 7001,
                                 "tvdb": 8001, "tmdb": 9001,
                                 "imdb": "tt5000001"}, result
        assert len(json.dumps(result).encode("utf-8")) <= 4 * 1024

    run(go())


# ------------------------------------------------------------------
# Size bound and concurrency bound
# ------------------------------------------------------------------

def test_size_bound_truncates_leg_at_item_boundary():
    _reset()
    SCRIPT.modes["tvdb"] = "huge"

    async def go():
        result = await gateway.series("tvdb:2001")
        assert result["legs"]["tvdb"]["truncated"] is True, result["legs"]["tvdb"]
        assert result["legs"]["tmdb"]["truncated"] is False
        tvdb_eps = next(r["episodes"] for r in result["results"]
                        if r["provider"] == "tvdb")
        assert 0 < len(tvdb_eps) < 3000, len(tvdb_eps)
        blob = json.dumps(tvdb_eps, ensure_ascii=False, default=str).encode("utf-8")
        assert len(blob) <= gateway.MAX_LEG_PAYLOAD_BYTES + 4096, len(blob)

    run(go())


def test_concurrency_bound_max_four_in_flight():
    _reset()

    async def go():
        await gateway.lookup("Star Blossom Academy", "unknown")
        assert SCRIPT.max_in_flight <= 4, SCRIPT.max_in_flight
        assert SCRIPT.max_in_flight >= 2, SCRIPT.max_in_flight

    run(go())


# ------------------------------------------------------------------
# series()
# ------------------------------------------------------------------

def test_series_tvdb_and_tmdb_side_by_side():
    _reset()

    async def go():
        result = await gateway.series("tvdb:2001")
        providers = {r["provider"] for r in result["results"]}
        assert providers == {"tvdb", "tmdb"}, result
        assert result["legs"]["tvdb"]["state"] == "ok"
        assert result["legs"]["tmdb"]["state"] == "ok"
        assert result["legs"]["anizip"]["state"] == "not_queried"
        by_provider = {r["provider"]: r["episodes"] for r in result["results"]}
        tvdb_eps = by_provider["tvdb"]
        assert len(tvdb_eps) == 3, tvdb_eps
        s0 = [e for e in tvdb_eps if e["season"] == 0]
        assert len(s0) == 1 and s0[0]["title"] == "Behind the Light"
        assert s0[0]["absolute"] is None
        s1 = sorted((e for e in tvdb_eps if e["season"] == 1),
                    key=lambda e: e["episode"])
        assert [e["absolute"] for e in s1] == [1, 2], s1
        assert s1[0]["air_date"] == "2019-04-02"
        assert all(e["source"] == "tvdb" for e in tvdb_eps)
        tmdb_eps = by_provider["tmdb"]
        assert len(tmdb_eps) == 3, tmdb_eps
        assert all(e["absolute"] is None for e in tmdb_eps)
        assert all(e["source"] == "tmdb" for e in tmdb_eps)

    run(go())


def test_series_tvdb_absolute_numbering_crosses_seasons():
    _reset()

    async def go():
        # Regression (review blocker B1): absolute numbering is the
        # episode's POSITION in TVDB's absolute-order list, looked
        # up — never the row's own per-season number. The fixture
        # spans two seasons (S1 has 3 episodes), so S2E1's absolute
        # is 4; the old identity map returned 1, colliding with
        # S1E1.
        result = await gateway.series("tvdb:2007")
        assert result["legs"]["tvdb"]["state"] == "ok", result["legs"]
        tvdb_eps = next(r["episodes"] for r in result["results"]
                        if r["provider"] == "tvdb")
        assert len(tvdb_eps) == 5, tvdb_eps
        by_se = {(e["season"], e["episode"]): e["absolute"] for e in tvdb_eps}
        assert by_se == {(1, 1): 1, (1, 2): 2, (1, 3): 3,
                         (2, 1): 4, (2, 2): 5}, by_se
        assert by_se[(2, 1)] > by_se[(1, 3)]
        absolutes = [e["absolute"] for e in tvdb_eps]
        assert len(set(absolutes)) == len(absolutes), absolutes

    run(go())


def test_series_anime_includes_anizip_with_absolute_correction():
    _reset()

    async def go():
        result = await gateway.series("kitsu:5001")
        providers = {r["provider"] for r in result["results"]}
        assert providers == {"tvdb", "tmdb", "anizip"}, result
        by_provider = {r["provider"]: r["episodes"] for r in result["results"]}
        anizip_eps = sorted(by_provider["anizip"], key=lambda e: e["absolute"])
        assert len(anizip_eps) == 2, anizip_eps
        assert anizip_eps[0]["season"] == 1 and anizip_eps[0]["episode"] == 1
        # Episode 2's AniZip row carries only an absolute number;
        # S/E is filled by the episode_maps correction lookup.
        assert anizip_eps[1]["season"] == 1 and anizip_eps[1]["episode"] == 2, anizip_eps
        assert anizip_eps[1]["title"] == "Petal Fall"
        assert SCRIPT.count("episode_maps", "resolve_absolute_episode") >= 1
        tvdb_eps = by_provider["tvdb"]
        assert [e["absolute"] for e in tvdb_eps] == [1, 2]

    run(go())


def test_series_anizip_skips_unplaceable_entry():
    _reset()

    async def go():
        # Review item M1(ii): an AniZip entry with a season but no
        # episodeNumber and no absolute cannot be placed and is
        # skipped entirely; an entry with a known absolute but
        # unresolved S/E is KEPT (S/E filled by the correction).
        kitsu_mod = sys.modules["Backend.helper.metadata.providers.kitsu"]
        state, _truncated, episodes = await gateway._series_anizip(
            kitsu_mod, 5004, {"kitsu": 5004}, 114)
        assert state == "ok", state
        titles = [e["title"] for e in episodes]
        assert "Unplaceable Middle" not in titles, episodes
        assert len(episodes) == 2, episodes
        by_title = {e["title"]: e for e in episodes}
        opener = by_title["Kept Opener"]
        assert (opener["season"], opener["episode"], opener["absolute"]) == (1, 1, 1)
        closer = by_title["Absolute Only Closer"]
        assert closer["absolute"] == 3, closer
        assert (closer["season"], closer["episode"]) == (1, 3), closer

    run(go())


def test_project_anizip_none_coalesces_and_preserves_season_zero():
    _reset()
    doc = {"mappings": {}, "episodes": {
        "1": {"title": {"en": "Fallback"}, "airDate": "2023-01-01",
              "seasonNumber": None, "season": 2,
              "episodeNumber": None, "episode": 7,
              "absoluteEpisodeNumber": None},
        "2": {"title": {"en": "Zero"}, "airDate": "2023-01-02",
              "seasonNumber": 0, "season": 9,
              "episodeNumber": 0, "episode": 9,
              "absoluteEpisodeNumber": None},
    }}
    projected = gateway.project_anizip(doc)
    # Present-but-None primaries fall back to the alias keys.
    assert projected["episodes"]["1"]["seasonNumber"] == 2, projected
    assert projected["episodes"]["1"]["episodeNumber"] == 7, projected
    # Season/episode 0 are valid values and must survive (no `or`).
    assert projected["episodes"]["2"]["seasonNumber"] == 0, projected
    assert projected["episodes"]["2"]["episodeNumber"] == 0, projected


def test_series_provider_without_resolvable_id_is_not_queried():
    _reset()

    async def go():
        result = await gateway.series("anilist:6001")
        # AniList expands only to MAL in Phase 2 sources; neither
        # TVDB/TMDB nor AniZip IDs resolve, so all legs are
        # not_queried and no result entries exist.
        assert result["results"] == [], result
        assert result["legs"]["tvdb"]["state"] == "not_queried"
        assert result["legs"]["tmdb"]["state"] == "not_queried"
        assert result["legs"]["anizip"]["state"] == "not_queried"

    run(go())


# ------------------------------------------------------------------
# crossid()
# ------------------------------------------------------------------

def test_crossid_from_each_seed():
    _reset()

    async def go():
        from_tmdb = await gateway.crossid("tmdb:1001")
        assert from_tmdb["ids"] == {"tmdb": 1001, "imdb": "tt1000001",
                                    "tvdb": 2001}, from_tmdb
        from_tvdb = await gateway.crossid("tvdb:2001")
        assert from_tvdb["ids"] == {"tvdb": 2001, "imdb": "tt1000001",
                                    "tmdb": 1001}, from_tvdb
        from_imdb = await gateway.crossid("imdb:tt1000001")
        assert from_imdb["ids"]["tmdb"] == 1001, from_imdb
        assert from_imdb["ids"]["tvdb"] == 2001, from_imdb
        from_anilist = await gateway.crossid("anilist:6001")
        assert from_anilist["ids"] == {"anilist": 6001, "mal": 7001}, from_anilist
        from_kitsu = await gateway.crossid("kitsu:5001")
        assert from_kitsu["ids"] == {"kitsu": 5001, "anilist": 6001, "mal": 7001,
                                     "tvdb": 8001, "tmdb": 9001,
                                     "imdb": "tt5000001"}, from_kitsu
        assert "anidb" not in from_kitsu["ids"]

    run(go())


def test_crossid_tmdb_none_outage_not_cached():
    _reset()
    # Production shape: tmdb.details() returns None on any failure
    # (it never raises for these calls). Both types None is an
    # outage — the seed-only result must NOT be cached as a miss.
    fake_tmdb = sys.modules["Backend.helper.metadata.providers.tmdb"]
    original_details = fake_tmdb.details

    async def details_none(media_type, item_id):
        return None

    async def go():
        fake_tmdb.details = details_none
        try:
            result = await gateway.crossid("tmdb:1001")
            assert result["ids"] == {"tmdb": 1001}, result
            assert _cache_docs() == [], _cache_docs()
        finally:
            fake_tmdb.details = original_details
        result = await gateway.crossid("tmdb:1001")
        assert result["ids"] == {"tmdb": 1001, "imdb": "tt1000001",
                                 "tvdb": 2001}, result
        assert len(_cache_docs()) == 1, _cache_docs()

    run(go())


def test_crossid_tvdb_none_outage_not_cached():
    _reset()
    # Production shape: tvdb.series_extended()/movie_extended()
    # return None on failure. Both None is an outage — the
    # seed-only result must NOT be cached as a miss.
    fake_tvdb = sys.modules["Backend.helper.metadata.providers.tvdb"]
    original_series = fake_tvdb.series_extended
    original_movie = fake_tvdb.movie_extended

    async def extended_none(tvdb_id):
        return None

    async def go():
        fake_tvdb.series_extended = extended_none
        fake_tvdb.movie_extended = extended_none
        try:
            result = await gateway.crossid("tvdb:2001")
            assert result["ids"] == {"tvdb": 2001}, result
            assert _cache_docs() == [], _cache_docs()
        finally:
            fake_tvdb.series_extended = original_series
            fake_tvdb.movie_extended = original_movie
        result = await gateway.crossid("tvdb:2001")
        assert result["ids"] == {"tvdb": 2001, "imdb": "tt1000001",
                                 "tmdb": 1001}, result
        assert len(_cache_docs()) == 1, _cache_docs()

    run(go())


def test_crossid_cinemeta_none_outage_not_cached():
    _reset()
    # Production shape: cinemeta.cached_detail() returns None on
    # failure. Both types None is an outage — the seed-only result
    # must NOT be cached as a miss.
    fake_cinemeta = sys.modules["Backend.helper.metadata.providers.cinemeta"]
    original_detail = fake_cinemeta.cached_detail

    async def detail_none(imdb_id, media_type):
        return None

    async def go():
        fake_cinemeta.cached_detail = detail_none
        try:
            result = await gateway.crossid("imdb:tt1000001")
            assert result["ids"] == {"imdb": "tt1000001"}, result
            assert _cache_docs() == [], _cache_docs()
        finally:
            fake_cinemeta.cached_detail = original_detail
        result = await gateway.crossid("imdb:tt1000001")
        assert result["ids"] == {"imdb": "tt1000001", "tmdb": 1001,
                                 "tvdb": 2001}, result
        assert len(_cache_docs()) == 1, _cache_docs()

    run(go())


def test_crossid_kitsu_none_outage_not_cached():
    _reset()
    # Production shape: kitsu.get_anizip_mappings() returns None on
    # failure. None is an outage — the seed-only result must NOT be
    # cached as a miss.
    fake_kitsu = sys.modules["Backend.helper.metadata.providers.kitsu"]
    original_mappings = fake_kitsu.get_anizip_mappings

    async def mappings_none(kitsu_id):
        return None

    fake_cinemeta = sys.modules["Backend.helper.metadata.providers.cinemeta"]
    original_detail = fake_cinemeta.cached_detail

    async def detail_ok(imdb_id, media_type):
        # All-up shape for the success call: expansion joins via
        # the discovered IMDb ID tt5000001, and the default
        # Cinemeta fake returns None for it — which, in production
        # shape, is itself an outage and would (correctly) keep
        # even a Kitsu-successful result uncached. A dict answer
        # (here carrying the already-known TMDB ID) is a success.
        return {"id": imdb_id, "moviedb_id": 9001}

    async def go():
        fake_kitsu.get_anizip_mappings = mappings_none
        try:
            result = await gateway.crossid("kitsu:5001")
            assert result["ids"] == {"kitsu": 5001}, result
            assert _cache_docs() == [], _cache_docs()
        finally:
            fake_kitsu.get_anizip_mappings = original_mappings
        fake_cinemeta.cached_detail = detail_ok
        try:
            result = await gateway.crossid("kitsu:5001")
        finally:
            fake_cinemeta.cached_detail = original_detail
        assert result["ids"] == {"kitsu": 5001, "anilist": 6001, "mal": 7001,
                                 "tvdb": 8001, "tmdb": 9001,
                                 "imdb": "tt5000001"}, result
        assert len(_cache_docs()) == 1, _cache_docs()

    run(go())


def test_series_cinemeta_none_outage_not_cached():
    _reset()
    # B2 / series() case of the same defect: series() consumes the
    # same _expand_ids any_down flag for its cache gate. Production
    # shape: cinemeta.cached_detail() returns None when Cinemeta is
    # down, so series("imdb:tt1000001") discovers no TMDB/TVDB IDs,
    # every leg is not_queried, and the empty result is returned to
    # the caller — but it must NOT be cached (604800 s) as if no
    # episode structures exist.
    fake_cinemeta = sys.modules["Backend.helper.metadata.providers.cinemeta"]
    original_detail = fake_cinemeta.cached_detail

    async def detail_none(imdb_id, media_type):
        return None

    async def go():
        fake_cinemeta.cached_detail = detail_none
        try:
            result = await gateway.series("imdb:tt1000001")
            assert result["results"] == [], result
            assert result["legs"]["tvdb"]["state"] == "not_queried", result["legs"]
            assert result["legs"]["tmdb"]["state"] == "not_queried", result["legs"]
            assert result["legs"]["anizip"]["state"] == "not_queried", result["legs"]
            assert _cache_docs() == [], _cache_docs()
        finally:
            fake_cinemeta.cached_detail = original_detail
        result = await gateway.series("imdb:tt1000001")
        providers = {r["provider"] for r in result["results"]}
        assert providers == {"tvdb", "tmdb"}, result
        assert len(_cache_docs()) == 1, _cache_docs()

    run(go())


def test_malformed_ref_raises_value_error():
    _reset()

    async def go():
        for bad in ("bogus", "tvdb:abc", "unknown:1", "tvdb:", ":12"):
            try:
                await gateway.crossid(bad)
                raise AssertionError(f"expected ValueError for {bad!r}")
            except ValueError:
                pass
        try:
            await gateway.series("nope")
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
        try:
            await gateway.lookup("Harbour Lights", "western")
            raise AssertionError("expected ValueError")
        except ValueError:
            pass

    run(go())


# ------------------------------------------------------------------
# No-specific-series guard (DESIGN §13)
# ------------------------------------------------------------------

def test_no_series_names_in_production_files():
    fixture_titles = ("Harbour Lights", "The Last Ferry",
                      "Star Blossom Academy", "River of Clouds")
    for rel in ("Backend/helper/librarian_gateway.py",
                "Backend/helper/metadata/providers/anilist.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for title in fixture_titles:
            assert title not in text, (rel, title)
