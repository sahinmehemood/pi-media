"""Librarian provider gateway (DESIGN §7, plugin file).

One gateway module between the Librarian bot and the metadata
providers; the bot never calls providers raw. Three operations:

- ``lookup(name, media_type)`` — candidate lists per provider,
  clustered by explicit shared IDs only (union over TVDB remoteIds,
  AniList ``idMal``, AniZip mappings, TMDB external IDs). Clustering
  by shared ID is mechanical joining, not similarity scoring, and it
  is the only merge permitted. The gateway returns facts and never
  selects a winner.
- ``series(ref)`` — episode structures per provider, side by side,
  never collapsed (comparing TVDB and TMDB *is* the donghua
  verification).
- ``crossid(ref)`` — every other provider ID for the same work, from
  explicit mappings only.

``ref`` grammar: ``"<provider>:<id>"`` with provider in
{tmdb, tvdb, anilist, mal, kitsu, imdb}; the id is an int for all
but ``imdb`` (a ``tt…`` string). A malformed ref raises ValueError.

Every provider leg returns an explicit state — ``ok | empty | down |
not_queried`` — and outages are reported, never cached as no-match.
Only results with no ``down`` leg are written to ``provider_cache``
(DESIGN §6); a partially-down result is returned but not stored, so
the next identical call re-queries providers. Concurrency is
bounded (max 4 provider calls in flight), each leg has a 20 s
timeout, and each leg's payload is bounded to 256 KB with
truncation at an item boundary.

The gateway ships dark: nothing in the live request paths imports
it in this phase. Provider modules, ``librarian_store`` and
``episode_maps`` are imported lazily inside functions (the tree's
own pattern — importing ``Backend`` at module import time builds
app state), and providers are called only through their public
functions plus the DESIGN §4 seam functions
(``tvdb.search_candidates`` / ``kitsu.search_candidates``) and the
§18-D14 seam (``cinemeta.search_candidates``), and one
private function DESIGN §7 explicitly sanctions as a same-codebase
internal consumed as-is: ``tvdb._iter_series_episodes`` (the
absolute-order episode list absolute numbering is read off).

The Cinemeta lookup leg uses the ``search_candidates`` seam, not
``search_title_multi``: the latter collapses a provider outage
into ``[]`` — its transport helper returns None on any non-200 or
exception and the search converts that to an empty list — while
the seam raises ``CinemetaTransportError`` on a failed fetch, so
a real Cinemeta outage surfaces as leg state ``down`` and a
genuine no-match as ``empty`` (DESIGN §18-D14).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

LOGGER = logging.getLogger(__name__)

#----- Bounds and cache TTLs (DESIGN §6/§7, pinned) ---------------
CACHE_TTL_S = {"lookup": 86400, "series": 604800, "crossid": 2592000}
MAX_IN_FLIGHT = 4
LEG_TIMEOUT_S = 20
MAX_LEG_PAYLOAD_BYTES = 256 * 1024
# Candidate caps (Phase 2 plan OQ-10): TMDB's own search breadth,
# Kitsu's page limit is 8, TVDB's search limit is 10.
MAX_TMDB_CANDIDATES = 10
MAX_TMDB_DETAILS = 10
MAX_ANIZIP_FETCHES = 8

_PROVIDERS = ("tmdb", "tvdb", "anilist", "mal", "kitsu", "imdb")
_LOOKUP_LEGS = ("tmdb", "tvdb", "cinemeta", "kitsu", "anilist", "anizip")
_YEAR_PRECEDENCE = {"tmdb": 0, "tvdb": 1, "anilist": 2, "kitsu": 3, "cinemeta": 4}

# Stored type -> per-provider parameters (DESIGN §7 table, extended
# with the Cinemeta/Kitsu columns per the Phase 2 plan §5.2: Cinemeta
# is the IMDb join source and is queried for every type; Kitsu is
# queried exactly when AniList is, plus ``movie``). A None entry
# means the leg is not_queried for that type. For AniList, the tuple
# is (format, country); (None, None) means queried untyped.
_LEG_PLAN = {
    "movie":   {"tmdb": ["movie"], "tvdb": "movie", "cinemeta": ["movie"],
                "kitsu": "movie", "anilist": ("MOVIE", None)},
    "tv":      {"tmdb": ["tv"], "tvdb": "series", "cinemeta": ["tv"],
                "kitsu": None, "anilist": None},
    "anime":   {"tmdb": ["tv"], "tvdb": "series", "cinemeta": ["tv"],
                "kitsu": "", "anilist": ("TV", "JP")},
    "donghua": {"tmdb": ["tv"], "tvdb": "series", "cinemeta": ["tv"],
                "kitsu": "", "anilist": ("TV", "CN")},
    "cartoon": {"tmdb": ["tv"], "tvdb": "series", "cinemeta": ["tv"],
                "kitsu": None, "anilist": None},
    "drama":   {"tmdb": ["tv"], "tvdb": "series", "cinemeta": ["tv"],
                "kitsu": None, "anilist": None},
    "ova":     {"tmdb": ["tv"], "tvdb": "series", "cinemeta": ["tv"],
                "kitsu": "", "anilist": ("OVA", None)},
    "special": {"tmdb": ["tv"], "tvdb": "series", "cinemeta": ["tv"],
                "kitsu": "", "anilist": ("SPECIAL", None)},
    "unknown": {"tmdb": ["movie", "tv"], "tvdb": "series",
                "cinemeta": ["movie", "tv"], "kitsu": "",
                "anilist": (None, None)},
}

_sem: Optional[asyncio.Semaphore] = None
_sem_loop = None
_indexes_ensured = False


def _semaphore() -> asyncio.Semaphore:
    """The gateway-owned concurrency bound (max 4 in flight).

    Created lazily per event loop so the module stays usable across
    successive loops (the test harness runs one loop per test).
    """
    global _sem, _sem_loop
    loop = asyncio.get_running_loop()
    if _sem is None or _sem_loop is not loop:
        _sem = asyncio.Semaphore(MAX_IN_FLIGHT)
        _sem_loop = loop
    return _sem


async def _call(fn, *args, **kwargs):
    """Run one provider call under the gateway semaphore."""
    async with _semaphore():
        return await fn(*args, **kwargs)


def _provider(name: str):
    return importlib.import_module(f"Backend.helper.metadata.providers.{name}")


def _episode_maps():
    return importlib.import_module("Backend.helper.metadata.episode_maps")


def _year_from_str(value) -> int:
    common = importlib.import_module("Backend.helper.metadata.common")
    return common.year_from_str(value)


#----- provider_cache (DESIGN §6) ---------------------------------

def _tracking():
    # Lazy, mirroring librarian_store: importing Backend builds app state.
    from Backend import db

    return db.dbs["tracking"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalise_name(name: str) -> str:
    return " ".join(str(name).split()).lower()


def _cache_key(kind: str, args: dict) -> str:
    normalised = json.dumps(args, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False)
    return f"gw:v1:{kind}:{normalised}"


async def _cache_read(key: str):
    """Return the stored payload on a live hit, else None.

    Expiry is checked here (Mongo's TTL monitor deletion is lazy).
    Cache errors degrade to "no cache" for the call — never to a
    failed operation, never to a down leg.
    """
    try:
        doc = await _tracking()["provider_cache"].find_one({"key": key})
    except Exception as e:
        LOGGER.debug(f"[gateway] cache read failed for {key}: {e}")
        return None
    if not doc:
        return None
    expires_at = doc.get("expires_at")
    if expires_at is None or expires_at <= _utcnow():
        return None
    return doc.get("payload")


async def _cache_write(key: str, payload: dict, ttl_s: int) -> None:
    global _indexes_ensured
    try:
        if not _indexes_ensured:
            store = importlib.import_module("Backend.helper.librarian_store")
            await store.ensure_indexes()
            _indexes_ensured = True
        await _tracking()["provider_cache"].update_one(
            {"key": key},
            {"$set": {
                "key": key,
                "payload": payload,
                "expires_at": datetime.fromtimestamp(
                    _utcnow().timestamp() + ttl_s, tz=timezone.utc
                ),
            }},
            upsert=True,
        )
    except Exception as e:
        LOGGER.debug(f"[gateway] cache write failed for {key}: {e}")


#----- Small shared helpers ----------------------------------------

def _opt_int(value) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fmt_date(value) -> Optional[str]:
    """Normalise a provider date to 'YYYY-MM-DD' or None."""
    if value is None:
        return None
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%Y-%m-%d")
        except Exception:
            return None
    text = str(value)
    return text[:10] if re.match(r"\d{4}-\d{2}-\d{2}", text) else None


def _dedupe_titles(values) -> list:
    out = []
    for value in values:
        if isinstance(value, str) and value and value not in out:
            out.append(value)
    return out


def _truncate(items: list):
    """Bound a leg payload to MAX_LEG_PAYLOAD_BYTES at an item
    boundary. Returns (kept_items, truncated)."""
    try:
        blob = json.dumps(items, ensure_ascii=False, default=str).encode("utf-8")
    except (TypeError, ValueError):
        return items, False
    if len(blob) <= MAX_LEG_PAYLOAD_BYTES:
        return items, False
    kept = []
    size = 2  # "[]"
    for item in items:
        piece = len(json.dumps(item, ensure_ascii=False, default=str).encode("utf-8")) + 1
        if size + piece > MAX_LEG_PAYLOAD_BYTES:
            break
        kept.append(item)
        size += piece
    return kept, True


def _leg(state: str, truncated: bool = False, retry_after=None) -> dict:
    leg = {"state": state, "truncated": truncated}
    if retry_after is not None:
        leg["retry_after"] = retry_after
    return leg


class _LegDown(Exception):
    """Internal: the leg cannot produce a well-formed response."""


async def _run_leg(factory):
    """Execute one lookup leg under the per-leg timeout and classify
    it per DESIGN §7: a raise (transport error, timeout, schema
    failure) is ``down``; a well-formed empty list is ``empty``; a
    non-empty list is ``ok``. Returns (state, truncated, items,
    retry_after)."""
    try:
        items = await asyncio.wait_for(factory(), timeout=LEG_TIMEOUT_S)
    except Exception as e:
        retry_after = getattr(e, "retry_after", None)
        if not isinstance(retry_after, int) or isinstance(retry_after, bool):
            retry_after = None
        return "down", False, [], retry_after
    if not isinstance(items, list):
        return "down", False, [], None
    kept, truncated = _truncate(items)
    return ("ok" if items else "empty"), truncated, kept, None


#----- Explicit-ID join keys (the ONLY merges permitted) -----------

def _ids_from_remote_ids(elements) -> dict:
    """Parse TVDB remoteIds/remote_ids elements {sourceName|type,
    id|value} into cluster ids. Unparseable values are skipped,
    never guessed."""
    out: dict = {}
    if not isinstance(elements, list):
        return out
    for el in elements:
        if not isinstance(el, dict):
            continue
        source = str(el.get("sourceName") or el.get("type") or "").lower()
        val = el.get("id") or el.get("value")
        if val is None or val == "":
            continue
        if "imdb" in source:
            text = str(val)
            if not text.startswith("tt"):
                text = f"tt{text}" if text.isdigit() else text
            out["imdb"] = text
        elif "themoviedb" in source or source == "tmdb":
            num = _opt_int(val)
            if num is not None:
                out["tmdb"] = num
        elif "anilist" in source:
            num = _opt_int(val)
            if num is not None:
                out["anilist"] = num
        elif "myanimelist" in source or source == "mal":
            num = _opt_int(val)
            if num is not None:
                out["mal"] = num
        elif "kitsu" in source:
            num = _opt_int(val)
            if num is not None:
                out["kitsu"] = num
    return out


def _ids_from_anizip_mappings(mappings) -> tuple:
    """(cluster ids, anidb_id) from an AniZip mappings block.
    anidb is internal only — it is not a cluster ids key; it keys
    episode_maps calls."""
    ids: dict = {}
    anidb_id = None
    if not isinstance(mappings, dict):
        return ids, anidb_id
    for key, dest in (("anilist_id", "anilist"), ("mal_id", "mal"),
                      ("thetvdb_id", "tvdb"), ("themoviedb_id", "tmdb")):
        num = _opt_int(mappings.get(key))
        if num is not None:
            ids[dest] = num
    imdb = mappings.get("imdb_id")
    if imdb:
        text = str(imdb)
        if not text.startswith("tt"):
            text = f"tt{text}" if text.isdigit() else text
        ids["imdb"] = text
    anidb_id = _opt_int(mappings.get("anidb_id"))
    return ids, anidb_id


def project_anizip(doc) -> dict:
    """Project an AniZip document to the mappings block (verbatim)
    plus the episode fields the gateway needs — nothing else.

    The historical failure this guards: a whole AniZip document
    (53 KB with overviews and images) once returned as an "ID
    lookup". The projection is applied before the size bound is
    measured and before anything is cached or returned.
    """
    if not isinstance(doc, dict):
        return {"mappings": {}, "episodes": {}}
    mappings = doc.get("mappings")
    mappings = dict(mappings) if isinstance(mappings, dict) else {}
    episodes: dict = {}
    raw_eps = doc.get("episodes")
    if isinstance(raw_eps, dict):
        for key, ep in raw_eps.items():
            if not isinstance(ep, dict):
                continue
            # None-coalescing fallback (not ``dict.get`` with a
            # default, which only falls back when the key is ABSENT,
            # and not ``or``, which would drop the valid season 0):
            # a present-but-None primary falls back to the alias key.
            season_raw = ep.get("seasonNumber")
            if season_raw is None:
                season_raw = ep.get("season")
            episode_raw = ep.get("episodeNumber")
            if episode_raw is None:
                episode_raw = ep.get("episode")
            episodes[str(key)] = {
                "title": _anizip_title(ep.get("title")),
                "airDate": _fmt_date(ep.get("airDate") or ep.get("airdate")),
                "seasonNumber": _opt_int(season_raw),
                "episodeNumber": _opt_int(episode_raw),
                "absoluteEpisodeNumber": _opt_int(ep.get("absoluteEpisodeNumber")),
            }
    return {"mappings": mappings, "episodes": episodes}


def _anizip_title(value) -> Optional[str]:
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        for key in ("en", "en_jp", "x-jat", "romaji", "ja_jp"):
            text = value.get(key)
            if isinstance(text, str) and text:
                return text
        for text in value.values():
            if isinstance(text, str) and text:
                return text
    return None


#----- Candidate normalisation (per leg, before clustering) --------

def _tmdb_item_candidate(item, media_type: str) -> Optional[dict]:
    tmdb_id = _opt_int(getattr(item, "id", None))
    if tmdb_id is None:
        return None
    if media_type == "movie":
        titles = _dedupe_titles([getattr(item, "title", None),
                                 getattr(item, "original_title", None)])
        date = getattr(item, "release_date", None)
    else:
        titles = _dedupe_titles([getattr(item, "name", None),
                                 getattr(item, "original_name", None)])
        date = getattr(item, "first_air_date", None)
    year = getattr(date, "year", None) if date is not None else None
    return {
        "ids": {"tmdb": tmdb_id},
        "titles": titles,
        "year": year if isinstance(year, int) and year > 0 else None,
        "source": "tmdb",
    }


def _tvdb_candidate(row: dict) -> dict:
    ids: dict = {}
    if isinstance(row.get("tvdb_id"), int) and not isinstance(row.get("tvdb_id"), bool):
        ids["tvdb"] = row["tvdb_id"]
    ids.update(_ids_from_remote_ids(row.get("remote_ids") or row.get("remoteIds")))
    titles = []
    if row.get("name"):
        titles.append(str(row["name"]))
    translations = row.get("translations")
    if isinstance(translations, dict) and translations.get("eng"):
        titles.append(str(translations["eng"]))
    aliases = row.get("aliases")
    if isinstance(aliases, list):
        titles.extend(a for a in aliases if isinstance(a, str))
    year = _year_from_str(row.get("year") or row.get("first_air_time")
                          or row.get("release_date"))
    return {"ids": ids, "titles": _dedupe_titles(titles),
            "year": year or None, "source": "tvdb"}


def _cinemeta_candidate(row: dict) -> dict:
    ids: dict = {}
    if isinstance(row.get("id"), str) and row["id"]:
        ids["imdb"] = row["id"]
    year = _year_from_str(row.get("year"))
    return {"ids": ids, "titles": _dedupe_titles([row.get("title")]),
            "year": year or None, "source": "cinemeta"}


def _kitsu_candidate(row: dict) -> Optional[dict]:
    kitsu_id = _opt_int(row.get("id"))
    if kitsu_id is None:
        return None
    attrs = row.get("attributes") or {}
    titles = []
    if attrs.get("canonicalTitle"):
        titles.append(str(attrs["canonicalTitle"]))
    title_map = attrs.get("titles")
    if isinstance(title_map, dict):
        titles.extend(v for v in title_map.values() if isinstance(v, str))
    abbrev = attrs.get("abbreviatedTitles")
    if isinstance(abbrev, list):
        titles.extend(a for a in abbrev if isinstance(a, str))
    year = None
    start = attrs.get("startDate")
    if isinstance(start, str) and len(start) >= 4 and start[:4].isdigit():
        year = int(start[:4])
    return {"ids": {"kitsu": kitsu_id}, "titles": _dedupe_titles(titles),
            "year": year, "source": "kitsu"}


def _anilist_candidate(media: dict) -> Optional[dict]:
    anilist_id = _opt_int(media.get("id"))
    if anilist_id is None:
        return None
    ids: dict = {"anilist": anilist_id}
    mal = _opt_int(media.get("idMal"))
    if mal is not None:
        ids["mal"] = mal
    titles = []
    title_map = media.get("title")
    if isinstance(title_map, dict):
        titles.extend(title_map.get(k) for k in ("romaji", "english", "native"))
    synonyms = media.get("synonyms")
    if isinstance(synonyms, list):
        titles.extend(s for s in synonyms if isinstance(s, str))
    year = _opt_int(media.get("seasonYear"))
    return {"ids": ids, "titles": _dedupe_titles(titles),
            "year": year, "source": "anilist"}


#----- Lookup legs ---------------------------------------------------

async def _tmdb_leg(name: str, media_types: list) -> list:
    tmdb = _provider("tmdb")
    candidates: list = []
    for media_type in media_types:
        items = await _call(tmdb.raw_search, name, media_type, None)
        if not isinstance(items, list):
            raise _LegDown("TMDB search returned a non-list payload")
        leg_candidates = []
        for item in items[:MAX_TMDB_CANDIDATES]:
            cand = _tmdb_item_candidate(item, media_type)
            if cand is not None:
                leg_candidates.append(cand)
        # Search items carry no external IDs; the join keys require
        # one details() call per candidate (capped). A failed
        # details() contributes no extra IDs — it does not down the
        # leg, the search itself succeeded.
        for cand in leg_candidates[:MAX_TMDB_DETAILS]:
            try:
                det = await _call(tmdb.details, media_type, cand["ids"]["tmdb"])
            except Exception:
                det = None
            if det is None:
                continue
            ext = getattr(det, "external_ids", None)
            if ext is None:
                continue
            imdb = getattr(ext, "imdb_id", None)
            if isinstance(imdb, str) and imdb:
                cand["ids"]["imdb"] = imdb
            tvdb_id = _opt_int(getattr(ext, "tvdb_id", None))
            if tvdb_id is not None:
                cand["ids"]["tvdb"] = tvdb_id
        candidates.extend(leg_candidates)
    return candidates


async def _tvdb_leg(name: str, entity: str) -> list:
    tvdb = _provider("tvdb")
    rows = await _call(tvdb.search_candidates, name, None, entity)
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise _LegDown("TVDB search returned a malformed payload")
    return [_tvdb_candidate(row) for row in rows]


async def _cinemeta_leg(name: str, types_: list) -> list:
    # The search_candidates seam (DESIGN §18-D14) raises
    # CinemetaTransportError on a failed fetch, so an outage is
    # classified down by _run_leg; a well-formed [] is empty.
    # search_title_multi is NOT used here: it collapses an outage
    # into [], indistinguishable from a genuine no-match.
    cinemeta = _provider("cinemeta")
    candidates: list = []
    for type_ in types_:
        rows = await _call(cinemeta.search_candidates, name, type_)
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise _LegDown("Cinemeta search returned a malformed payload")
        candidates.extend(_cinemeta_candidate(row) for row in rows)
    return candidates


async def _kitsu_leg(name: str, subtype: Optional[str]) -> list:
    kitsu = _provider("kitsu")
    rows = await _call(kitsu.search_candidates, name, subtype)
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise _LegDown("Kitsu search returned a malformed payload")
    candidates = []
    for row in rows:
        cand = _kitsu_candidate(row)
        if cand is not None:
            candidates.append(cand)
    return candidates


async def _anilist_leg(name: str, format_: Optional[str],
                       country: Optional[str]) -> list:
    anilist = _provider("anilist")
    media = await _call(anilist.search_media, name, format=format_,
                        country=country, media_kind="ANIME")
    if not isinstance(media, list) or not all(isinstance(m, dict) for m in media):
        raise _LegDown("AniList search returned a malformed payload")
    candidates = []
    for entry in media:
        cand = _anilist_candidate(entry)
        if cand is not None:
            candidates.append(cand)
    return candidates


async def _anizip_lookup_leg(kitsu_candidates: list):
    """Dependent AniZip leg: one mappings fetch per Kitsu candidate.

    State (Phase 2 plan OQ-7): down if every fetch fails; ok if at
    least one returned document has a mappings block; empty if
    documents returned but every mappings block was empty/absent.
    Returns (state, truncated, {kitsu_id: projected_doc}).
    """
    kitsu = _provider("kitsu")
    docs: dict = {}
    successes = 0
    any_mappings = False
    payload: list = []
    for cand in kitsu_candidates[:MAX_ANIZIP_FETCHES]:
        kitsu_id = cand["ids"]["kitsu"]
        try:
            doc = await _call(kitsu.get_anizip_mappings, kitsu_id)
        except Exception:
            doc = None
        if not isinstance(doc, dict):
            continue
        successes += 1
        projected = project_anizip(doc)
        docs[kitsu_id] = projected
        payload.append(projected)
        if projected["mappings"]:
            any_mappings = True
    if successes == 0:
        return "down", False, {}
    kept, truncated = _truncate(payload)
    if len(kept) < len(payload):
        kept_ids = set()
        for projected in kept:
            for cand in kitsu_candidates[:MAX_ANIZIP_FETCHES]:
                if docs.get(cand["ids"]["kitsu"]) is projected:
                    kept_ids.add(cand["ids"]["kitsu"])
        docs = {k: v for k, v in docs.items() if k in kept_ids}
    return ("ok" if any_mappings else "empty"), truncated, docs


def _cluster(candidates: list, media_type: str) -> list:
    """Union-find over explicit (provider, id) pairs — the only
    merge permitted. Candidates sharing only a title never merge."""
    parent = list(range(len(candidates)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    owner: dict = {}
    for i, cand in enumerate(candidates):
        for provider, value in cand["ids"].items():
            key = (provider, value)
            if key in owner:
                union(i, owner[key])
            else:
                owner[key] = i

    groups: dict = {}
    for i in range(len(candidates)):
        groups.setdefault(find(i), []).append(i)

    clusters = []
    for idxs in groups.values():
        members = [candidates[i] for i in idxs]
        by_precedence = sorted(
            members, key=lambda m: _YEAR_PRECEDENCE.get(m["source"], 99)
        )
        ids: dict = {}
        for member in by_precedence:
            for key, value in member["ids"].items():
                ids.setdefault(key, value)
        titles: list = []
        for member in members:
            for title in member["titles"]:
                if title not in titles:
                    titles.append(title)
        year = None
        for member in by_precedence:
            if member["year"]:
                year = member["year"]
                break
        clusters.append({"ids": ids, "titles": titles, "year": year,
                         "media_type": media_type})
    return clusters


#----- Public operation: lookup --------------------------------------

async def lookup(name: str, media_type: str) -> dict:
    if media_type not in _LEG_PLAN:
        raise ValueError(f"unknown media_type: {media_type!r}")
    plan = _LEG_PLAN[media_type]
    key = _cache_key("lookup", {"media_type": media_type,
                                "name": _normalise_name(name)})
    cached = await _cache_read(key)
    if cached is not None:
        return cached

    factories = {
        "tmdb": lambda: _tmdb_leg(name, plan["tmdb"]),
        "tvdb": lambda: _tvdb_leg(name, plan["tvdb"]),
        "cinemeta": lambda: _cinemeta_leg(name, plan["cinemeta"]),
        "kitsu": (lambda: _kitsu_leg(name, plan["kitsu"] or None))
                 if plan["kitsu"] is not None else None,
        "anilist": (lambda: _anilist_leg(name, plan["anilist"][0],
                                         plan["anilist"][1]))
                   if plan["anilist"] is not None else None,
    }
    providers = ["tmdb", "tvdb", "cinemeta", "kitsu", "anilist"]
    outcomes = await asyncio.gather(*[
        _run_leg(factories[p]) if factories[p] is not None
        else _not_queried()
        for p in providers
    ])
    legs: dict = {}
    per_leg: dict = {}
    for provider, (state, truncated, items, retry_after) in zip(providers, outcomes):
        legs[provider] = _leg(state, truncated, retry_after)
        per_leg[provider] = items

    # Dependent AniZip leg: queried per Kitsu candidate, only when
    # the Kitsu leg produced candidates.
    kitsu_candidates = per_leg.get("kitsu") or []
    if kitsu_candidates:
        # The dependent leg runs under its own timeout; a timeout is
        # a transport failure -> down.
        try:
            az_state, az_truncated, az_docs = await asyncio.wait_for(
                _anizip_lookup_leg(kitsu_candidates), timeout=LEG_TIMEOUT_S
            )
        except Exception:
            az_state, az_truncated, az_docs = "down", False, {}
        legs["anizip"] = _leg(az_state, az_truncated)
        for cand in kitsu_candidates:
            projected = az_docs.get(cand["ids"]["kitsu"])
            if projected:
                az_ids, _anidb = _ids_from_anizip_mappings(projected["mappings"])
                for key_, value in az_ids.items():
                    cand["ids"].setdefault(key_, value)
    else:
        legs["anizip"] = _leg("not_queried")

    candidates: list = []
    for provider in ("tmdb", "tvdb", "cinemeta", "kitsu", "anilist"):
        candidates.extend(per_leg.get(provider) or [])
    clusters = _cluster(candidates, media_type)

    ordered_legs = {p: legs[p] for p in _LOOKUP_LEGS}
    providers_down = [p for p in _LOOKUP_LEGS if ordered_legs[p]["state"] == "down"]
    result = {"clusters": clusters, "legs": ordered_legs,
              "providers_down": providers_down}
    if not providers_down:
        await _cache_write(key, result, CACHE_TTL_S["lookup"])
    return result


async def _not_queried():
    return "not_queried", False, [], None


#----- Cross-ID expansion (shared by crossid and series) ------------

async def _expand_ids(provider: str, ident) -> tuple:
    """Iterative explicit-ID expansion, at most 2 rounds.

    Returns (ids, anidb_id, any_down). A source that raises marks
    any_down (its result is not cacheable). So does a
    None-collapsing source (TMDB, TVDB, Cinemeta, Kitsu) whose
    attempted calls all return None / a non-dict: those providers
    signal failure that way, so an all-None result is an outage,
    never a genuine no-match (DESIGN §6). A dict/object response
    that merely yields no new IDs is a success, not down. AniList
    is the exception: its client raises on failure, so
    ``media_by_id`` returning None is a genuine not-found and does
    NOT mark any_down. IDs are never inferred from titles.
    """
    ids: dict = {provider: ident}
    anidb_id: Optional[int] = None
    any_down = False
    attempted: set = set()

    async def fetch(source: str, source_id):
        nonlocal any_down, anidb_id
        marker = (source, source_id)
        if marker in attempted:
            return
        attempted.add(marker)
        try:
            if source == "tmdb":
                tmdb = _provider("tmdb")
                det = await _call(tmdb.details, "tv", source_id)
                if det is None:
                    det = await _call(tmdb.details, "movie", source_id)
                if det is not None:
                    ext = getattr(det, "external_ids", None)
                    if ext is not None:
                        imdb = getattr(ext, "imdb_id", None)
                        if isinstance(imdb, str) and imdb:
                            ids.setdefault("imdb", imdb)
                        tvdb_id = _opt_int(getattr(ext, "tvdb_id", None))
                        if tvdb_id is not None:
                            ids.setdefault("tvdb", tvdb_id)
                else:
                    # tmdb.details() returns None on any failure:
                    # both types None is an outage, not a miss.
                    any_down = True
            elif source == "tvdb":
                tvdb = _provider("tvdb")
                doc = await _call(tvdb.series_extended, source_id)
                if not isinstance(doc, dict):
                    doc = await _call(tvdb.movie_extended, source_id)
                if isinstance(doc, dict):
                    for key_, value in _ids_from_remote_ids(
                            doc.get("remoteIds") or doc.get("remote_ids")).items():
                        ids.setdefault(key_, value)
                else:
                    # Both extended calls None/non-dict: the TVDB
                    # helpers collapse failure to None, so this is
                    # an outage, not a miss.
                    any_down = True
            elif source == "imdb":
                cinemeta = _provider("cinemeta")
                cinemeta_results = []
                det = await _call(cinemeta.cached_detail, source_id, "series")
                cinemeta_results.append(det)
                moviedb_id = det.get("moviedb_id") if isinstance(det, dict) else None
                if not moviedb_id:
                    det = await _call(cinemeta.cached_detail, source_id, "movie")
                    cinemeta_results.append(det)
                    moviedb_id = det.get("moviedb_id") if isinstance(det, dict) else None
                if all(result is None for result in cinemeta_results):
                    # cached_detail() collapses failure to None:
                    # both types None is an outage, not a miss. A
                    # dict with no moviedb_id is a success above.
                    any_down = True
                tmdb_id = _opt_int(moviedb_id)
                if tmdb_id is not None:
                    ids.setdefault("tmdb", tmdb_id)
            elif source == "anilist":
                anilist = _provider("anilist")
                media = await _call(anilist.media_by_id, source_id)
                # AniList raises on failure, so None here is a
                # genuine well-formed not-found — NOT down.
                if isinstance(media, dict):
                    mal = _opt_int(media.get("idMal"))
                    if mal is not None:
                        ids.setdefault("mal", mal)
            elif source == "kitsu":
                kitsu = _provider("kitsu")
                doc = await _call(kitsu.get_anizip_mappings, source_id)
                if isinstance(doc, dict):
                    projected = project_anizip(doc)
                    az_ids, az_anidb = _ids_from_anizip_mappings(projected["mappings"])
                    for key_, value in az_ids.items():
                        ids.setdefault(key_, value)
                    if az_anidb is not None:
                        anidb_id = az_anidb
                else:
                    # get_anizip_mappings() collapses failure to
                    # None: None/non-dict is an outage, not a miss.
                    any_down = True
        except Exception as e:
            LOGGER.debug(f"[gateway] crossid source {source}:{source_id} failed: {e}")
            any_down = True

    for _round in range(2):
        before = dict(ids)
        for source in ("tmdb", "tvdb", "imdb", "anilist", "kitsu"):
            if source in ids:
                await fetch(source, ids[source])
        if ids == before:
            break
    return ids, anidb_id, any_down


def _parse_ref(ref: str) -> tuple:
    if not isinstance(ref, str) or ":" not in ref:
        raise ValueError(f"malformed ref: {ref!r}")
    provider, _, raw = ref.partition(":")
    provider = provider.strip().lower()
    raw = raw.strip()
    if provider not in _PROVIDERS or not raw:
        raise ValueError(f"malformed ref: {ref!r}")
    if provider == "imdb":
        return provider, raw
    num = _opt_int(raw)
    if num is None:
        raise ValueError(f"malformed ref: {ref!r}")
    return provider, num


def _canonical_ref(provider: str, ident) -> str:
    return f"{provider}:{ident}"


#----- Public operation: crossid -------------------------------------

async def crossid(ref: str) -> dict:
    provider, ident = _parse_ref(ref)
    key = _cache_key("crossid", {"ref": _canonical_ref(provider, ident)})
    cached = await _cache_read(key)
    if cached is not None:
        return cached
    ids, _anidb_id, any_down = await _expand_ids(provider, ident)
    result = {"ids": ids}
    if not any_down:
        await _cache_write(key, result, CACHE_TTL_S["crossid"])
    return result


#----- Public operation: series --------------------------------------

async def _series_tvdb(tvdb_mod, tvdb_id: int):
    ext = await _call(tvdb_mod.series_extended, tvdb_id)
    if ext is None or not isinstance(ext, dict):
        # The ID was explicit, so absence means the fetch failed.
        return "down", False, None
    default_eps = await _call(tvdb_mod._iter_series_episodes, tvdb_id, "default")
    absolute_eps = await _call(tvdb_mod._iter_series_episodes, tvdb_id, "absolute")
    if not isinstance(default_eps, list) or not isinstance(absolute_eps, list):
        return "down", False, None
    # Absolute numbering is LOOKED UP, never derived (DESIGN §7):
    # an episode's absolute number is its POSITION in the
    # absolute-order list — its own per-season `number` restarts at
    # 1 every season, so copying `number` would collide across
    # seasons (S2E1 would claim absolute 1). Rows are matched
    # between the two lists by episode `id` when both carry one
    # (real TVDB v4 rows do), else by (seasonNumber, number) — the
    # recorded-payload shape in tests/fixtures/gateway carries no
    # `id`, so the (season, number) map is the one that binds there.
    abs_by_id: dict = {}
    abs_by_se: dict = {}
    for pos, row in enumerate(absolute_eps, 1):
        if not isinstance(row, dict):
            continue
        ep_id = _opt_int(row.get("id"))
        if ep_id is not None:
            abs_by_id[ep_id] = pos
        season = _opt_int(row.get("seasonNumber"))
        number = _opt_int(row.get("number"))
        if season is not None and number is not None:
            abs_by_se[(season, number)] = pos
    episodes = []
    for row in default_eps:
        if not isinstance(row, dict):
            continue
        season = _opt_int(row.get("seasonNumber"))
        number = _opt_int(row.get("number"))
        if season is None or number is None:
            continue
        absolute = None
        ep_id = _opt_int(row.get("id"))
        if ep_id is not None:
            absolute = abs_by_id.get(ep_id)
        if absolute is None:
            absolute = abs_by_se.get((season, number))
        if absolute is None:
            for key_ in ("absoluteNumber", "absoluteIndex", "absNumber"):
                absolute = _opt_int(row.get(key_))
                if absolute is not None:
                    break
        episodes.append({
            "season": season,
            "episode": number,
            "absolute": absolute,
            "title": row.get("name") or None,
            "air_date": _fmt_date(row.get("aired") or row.get("firstAired")),
            "source": "tvdb",
        })
    kept, truncated = _truncate(episodes)
    return ("ok" if episodes else "empty"), truncated, kept


async def _series_tmdb(tmdb_mod, tmdb_id: int):
    det = await _call(tmdb_mod.details, "tv", tmdb_id)
    if det is None:
        return "down", False, None
    seasons = getattr(det, "seasons", None) or []
    client = tmdb_mod.get_tmdb_client()
    episodes = []
    for season in seasons:
        season_number = _opt_int(getattr(season, "season_number", None))
        if season_number is None:
            continue
        # Verified surface (themoviedb 1.0.2, the uv.lock pin):
        # aioTMDb.season(tv_id, season_number).details() returns a
        # Season object whose .episodes list carries
        # season_number / episode_number / name / air_date.
        season_det = await _call(lambda: client.season(tmdb_id, season_number).details())
        if season_det is None:
            return "down", False, None
        for ep in getattr(season_det, "episodes", None) or []:
            ep_season = _opt_int(getattr(ep, "season_number", None))
            ep_number = _opt_int(getattr(ep, "episode_number", None))
            if ep_number is None:
                continue
            episodes.append({
                "season": ep_season if ep_season is not None else season_number,
                "episode": ep_number,
                "absolute": None,  # TMDB carries no absolute numbering.
                "title": getattr(ep, "name", None) or None,
                "air_date": _fmt_date(getattr(ep, "air_date", None)),
                "source": "tmdb",
            })
    kept, truncated = _truncate(episodes)
    return ("ok" if episodes else "empty"), truncated, kept


async def _series_anizip(kitsu_mod, kitsu_id: int, ids: dict, anidb_id):
    doc = await _call(kitsu_mod.get_anizip_mappings, kitsu_id)
    if doc is None or not isinstance(doc, dict):
        return "down", False, None
    projected = project_anizip(doc)
    episodes = []
    for ep in projected["episodes"].values():
        season = ep["seasonNumber"]
        number = ep["episodeNumber"]
        absolute = ep["absoluteEpisodeNumber"]
        if absolute is None and (season is None or number is None):
            # No absolute to correct from and S/E is incomplete —
            # the entry cannot be placed, so it is skipped entirely
            # (mirrors _series_tvdb skipping rows that lack a season
            # or number). An entry WITH a known absolute but
            # unresolved S/E is kept below: the correction path
            # fills S/E from the absolute.
            continue
        if absolute is not None and (season is None or number is None):
            # Anime correction path — the ONLY episode_maps use in
            # series(): absolute -> S/E via anime-lists + anibridge,
            # keyed by anidb/anilist/mal IDs. Conversion is looked
            # up, never summed.
            try:
                resolved = await _call(
                    _episode_maps().resolve_absolute_episode,
                    absolute,
                    anidb_id=anidb_id,
                    anilist_id=ids.get("anilist"),
                    mal_id=ids.get("mal"),
                )
            except Exception:
                resolved = None
            if isinstance(resolved, dict):
                if season is None:
                    season = _opt_int(resolved.get("season_number"))
                if number is None:
                    number = _opt_int(resolved.get("episode_number"))
        episodes.append({
            "season": season,
            "episode": number,
            "absolute": absolute,
            "title": ep["title"],
            "air_date": ep["airDate"],
            "source": "anizip",
        })
    kept, truncated = _truncate(episodes)
    return ("ok" if episodes else "empty"), truncated, kept


async def _run_series_leg(factory):
    try:
        state, truncated, episodes = await asyncio.wait_for(
            factory(), timeout=LEG_TIMEOUT_S
        )
    except Exception:
        return "down", False, None
    return state, truncated, episodes


async def series(ref: str) -> dict:
    provider, ident = _parse_ref(ref)
    key = _cache_key("series", {"ref": _canonical_ref(provider, ident)})
    cached = await _cache_read(key)
    if cached is not None:
        return cached
    ids, anidb_id, expansion_down = await _expand_ids(provider, ident)

    legs: dict = {}
    results: list = []

    async def collect(name: str, factory):
        state, truncated, episodes = await _run_series_leg(factory)
        legs[name] = _leg(state, truncated)
        if episodes is not None and state != "down":
            results.append({"provider": name, "episodes": episodes})

    pending = []
    if "tvdb" in ids:
        tvdb_mod = _provider("tvdb")
        pending.append(collect("tvdb", lambda: _series_tvdb(tvdb_mod, ids["tvdb"])))
    else:
        legs["tvdb"] = _leg("not_queried")
    if "tmdb" in ids:
        tmdb_mod = _provider("tmdb")
        pending.append(collect("tmdb", lambda: _series_tmdb(tmdb_mod, ids["tmdb"])))
    else:
        legs["tmdb"] = _leg("not_queried")
    if "kitsu" in ids:
        kitsu_mod = _provider("kitsu")
        pending.append(collect(
            "anizip", lambda: _series_anizip(kitsu_mod, ids["kitsu"], ids, anidb_id)
        ))
    else:
        legs["anizip"] = _leg("not_queried")
    if pending:
        await asyncio.gather(*pending)

    ordered_legs = {p: legs[p] for p in ("tvdb", "tmdb", "anizip")}
    result = {"results": results, "legs": ordered_legs}
    if not expansion_down and all(
            leg["state"] != "down" for leg in ordered_legs.values()):
        await _cache_write(key, result, CACHE_TTL_S["series"])
    return result


async def _absolute_from_se(imdb_id: Optional[str], season: int, episode: int):
    """Internal helper (not a public operation): S/E -> absolute for
    anime via episode_maps.absolute_from_imdb_episode, when an IMDb
    ID is known. Returns the absolute episode number or None."""
    if not imdb_id:
        return None
    try:
        resolved = await _call(
            _episode_maps().absolute_from_imdb_episode, imdb_id, season, episode
        )
    except Exception:
        return None
    if isinstance(resolved, dict):
        return _opt_int(resolved.get("absolute_episode"))
    return None
