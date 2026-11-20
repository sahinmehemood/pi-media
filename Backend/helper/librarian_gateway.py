"""Librarian Provider Gateway (v6 design §5).

All provider traffic for the bot lives here: one client, one
cache, outage honesty. The bot asks three questions:

    lookup(name, type)   -> who is this? (every id + every title)
    series(ref)          -> episode lists: aired / absolute /
                            specials, with titles + air dates
    crossid(ref)         -> every other id for the same work

Routing (research-settled): TMDB owns western/movie identity,
AniList owns anime/donghua identity + titles, TVDB owns
numbering (aired + absolute + Season 0 on one record set),
Jikan supplies MAL episode lists, Kitsu is a mappings oracle,
Cinemeta is the keyless IMDb-keyed fallback.

A provider being down is reported in `providers_down` and
NOTHING is cached for it — an outage is weather, not truth.
"""

import time

ANILIST_URL = "https://graphql.anilist.co"
JIKAN_URL = "https://api.jikan.moe/v4"
CACHE_TTL = {"lookup": 24 * 3600, "series": 7 * 24 * 3600,
             "crossid": 7 * 24 * 3600}

_ANILIST_QUERY = """
query ($search: String) {
  Page(page: 1, perPage: 6) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id idMal
      title { romaji english native userPreferred }
      synonyms seasonYear episodes format status
      relations { edges { relationType node { id
        title { romaji english native } } } }
    }
  }
}"""


# ---------------------------------------------------------------------------
# Dependencies — real ones bind to the app's providers; tests fake them.
# ---------------------------------------------------------------------------

class GwDeps:
    def __init__(self, tmdb_candidates, tvdb_search, tvdb_episodes,
                 tvdb_extended, cinemeta_search, cinemeta_detail,
                 kitsu_search, kitsu_mappings, http_post, http_get,
                 cache):
        self.tmdb_candidates = tmdb_candidates  # async q -> [cand]
        self.tvdb_search = tvdb_search          # async title -> dict|None
        self.tvdb_episodes = tvdb_episodes      # async (id, order) -> [ep]
        self.tvdb_extended = tvdb_extended      # async id -> dict|None
        self.cinemeta_search = cinemeta_search  # async (q, type) -> [hit]
        self.cinemeta_detail = cinemeta_detail  # async imdb -> dict|None
        self.kitsu_search = kitsu_search        # async title -> dict|None
        self.kitsu_mappings = kitsu_mappings    # async id -> dict|None
        self.http_post = http_post              # async (url, json) -> dict
        self.http_get = http_get                # async url -> dict
        self.cache = cache                      # motor coll | None


def real_deps() -> GwDeps:
    import httpx

    from Backend import db
    from Backend.helper.metadata import entry
    from Backend.helper.metadata.providers import cinemeta, kitsu, tvdb

    tracking = (getattr(db, "dbs", None) or {}).get("tracking")
    client = httpx.AsyncClient(timeout=20.0)

    async def http_post(url, payload):
        r = await client.post(url, json=payload)
        r.raise_for_status()
        return r.json()

    async def http_get(url):
        r = await client.get(url)
        r.raise_for_status()
        return r.json()

    return GwDeps(
        tmdb_candidates=entry.search_any_candidates,
        tvdb_search=tvdb.search,
        tvdb_episodes=tvdb._iter_series_episodes,
        tvdb_extended=tvdb.series_extended,
        cinemeta_search=cinemeta.search_title_multi,
        cinemeta_detail=cinemeta.get_detail,
        kitsu_search=kitsu.search_anime,
        kitsu_mappings=kitsu.get_anizip_mappings,
        http_post=http_post, http_get=http_get,
        cache=tracking["provider_cache"] if tracking is not None else None)


# ---------------------------------------------------------------------------
# Cache — outage writes nothing; only real answers are stored.
# ---------------------------------------------------------------------------

async def _cache_get(deps: GwDeps, key: str):
    if deps.cache is None:
        return None
    doc = await deps.cache.find_one({"_id": key})
    if not doc:
        return None
    if time.time() - (doc.get("fetched_at") or 0) > (doc.get("ttl") or 0):
        return None
    return doc.get("payload")


async def _cache_put(deps: GwDeps, key: str, payload, kind: str):
    if deps.cache is None:
        return
    await deps.cache.update_one(
        {"_id": key},
        {"$set": {"payload": payload, "fetched_at": time.time(),
                  "ttl": CACHE_TTL.get(kind, 24 * 3600)}},
        upsert=True)


# ---------------------------------------------------------------------------
# AniList + Jikan (keyless; no app provider existed for these)
# ---------------------------------------------------------------------------

async def anilist_search(deps: GwDeps, name: str) -> list:
    data = await deps.http_post(
        ANILIST_URL, {"query": _ANILIST_QUERY, "variables": {"search": name}})
    media = (((data or {}).get("data") or {}).get("Page") or {}).get("media")
    return media or []


async def jikan_search(deps: GwDeps, name: str) -> list:
    from urllib.parse import quote
    data = await deps.http_get(f"{JIKAN_URL}/anime?q={quote(name)}&limit=5")
    return (data or {}).get("data") or []


async def jikan_episodes(deps: GwDeps, mal_id: int) -> list:
    data = await deps.http_get(f"{JIKAN_URL}/anime/{mal_id}/episodes")
    return (data or {}).get("data") or []


# ---------------------------------------------------------------------------
# lookup — merged identity: every id + every title, per candidate
# ---------------------------------------------------------------------------

def _blank_candidate():
    return {"ids": {}, "titles": {"canonical": None, "native": None,
                                  "english": None, "synonyms": []},
            "year": None, "media_type": None, "sources": []}


def _merge(cands: list, cand: dict):
    """Merge cand into the first candidate sharing any id; else append."""
    ids = {k: v for k, v in (cand.get("ids") or {}).items() if v}
    for existing in cands:
        eids = existing["ids"]
        if any(str(eids.get(k)) == str(v) for k, v in ids.items()):
            for k, v in ids.items():
                eids.setdefault(k, v)
            et = existing["titles"]
            for name in (cand["titles"].get("canonical"),
                         cand["titles"].get("english"),
                         cand["titles"].get("native")):
                if name and name not in et["synonyms"] \
                        and name != et.get("canonical"):
                    et["synonyms"].append(name)
            for s in cand["titles"].get("synonyms") or []:
                if s and s not in et["synonyms"]:
                    et["synonyms"].append(s)
            if not et.get("native"):
                et["native"] = cand["titles"].get("native")
            if not et.get("english"):
                et["english"] = cand["titles"].get("english")
            existing["year"] = existing["year"] or cand.get("year")
            for src in cand.get("sources") or []:
                if src not in existing["sources"]:
                    existing["sources"].append(src)
            return
    cands.append(cand)


async def lookup(deps: GwDeps, name: str, media_type: str = ""):
    key = f"lookup:{media_type}:{name.strip().lower()}"
    hit = await _cache_get(deps, key)
    if hit is not None:
        return {**hit, "cached": True}
    kind = (media_type or "").lower()
    animeish = kind in ("anime", "donghua")
    cands, down = [], []

    async def guard(provider, coro):
        try:
            return await coro
        except Exception:
            down.append(provider)
            return None

    # TMDB (+Cinemeta) — western & movie identity; also searched for
    # donghua (joint ownership) and as a general fallback.
    if kind != "anime":
        rows = await guard("tmdb", deps.tmdb_candidates(name))
        for r in rows or []:
            c = _blank_candidate()
            c["ids"] = {"tmdb": r.get("tmdb_id"), "imdb": r.get("imdb_id")}
            c["titles"]["canonical"] = r.get("title")
            c["year"] = r.get("year") or None
            c["media_type"] = r.get("media_type")
            c["sources"] = ["tmdb"]
            _merge(cands, c)
        hits = await guard("cinemeta", deps.cinemeta_search(
            name, "movie" if kind == "movie" else "tvSeries"))
        for h in hits or []:
            c = _blank_candidate()
            c["ids"] = {"imdb": h.get("id")}
            c["titles"]["canonical"] = h.get("title")
            c["year"] = h.get("year") or None
            c["sources"] = ["cinemeta"]
            _merge(cands, c)
    # AniList — anime/donghua identity + the title bridge.
    if animeish or not kind:
        rows = await guard("anilist", anilist_search(deps, name))
        for m in rows or []:
            t = m.get("title") or {}
            c = _blank_candidate()
            c["ids"] = {"anilist": m.get("id"), "mal": m.get("idMal")}
            c["titles"] = {
                "canonical": t.get("userPreferred") or t.get("romaji"),
                "native": t.get("native"), "english": t.get("english"),
                "synonyms": list(m.get("synonyms") or [])}
            if t.get("romaji") and t["romaji"] not in c["titles"]["synonyms"]:
                c["titles"]["synonyms"].append(t["romaji"])
            c["year"] = m.get("seasonYear")
            c["media_type"] = kind or "anime"
            c["sources"] = ["anilist"]
            _merge(cands, c)
    # TVDB — best single record (it carries aliases + remote ids).
    row = await guard("tvdb", deps.tvdb_search(name))
    if row:
        c = _blank_candidate()
        imdb_id, tmdb_id = _tvdb_remote_ids(row)
        c["ids"] = {"tvdb": row.get("id") or row.get("tvdb_id"),
                    "imdb": imdb_id, "tmdb": tmdb_id}
        c["titles"]["canonical"] = row.get("name")
        c["titles"]["synonyms"] = [a for a in (row.get("aliases") or [])
                                   if isinstance(a, str)]
        c["year"] = row.get("year") or None
        c["sources"] = ["tvdb"]
        _merge(cands, c)
    # Jikan — MAL-side titles for anime/donghua.
    if animeish:
        rows = await guard("jikan", jikan_search(deps, name))
        for a in rows or []:
            c = _blank_candidate()
            c["ids"] = {"mal": a.get("mal_id")}
            c["titles"] = {
                "canonical": a.get("title"), "native": a.get("title_japanese"),
                "english": a.get("title_english"),
                "synonyms": [t.get("title") for t in (a.get("titles") or [])
                             if t.get("title")]}
            c["year"] = a.get("year")
            c["media_type"] = kind
            c["sources"] = ["jikan"]
            _merge(cands, c)
    out = {"ok": True, "candidates": cands, "providers_down": down}
    if not down:
        await _cache_put(deps, key, out, "lookup")
    return out


def _tvdb_remote_ids(doc: dict):
    imdb_id = tmdb_id = None
    for rid in (doc or {}).get("remoteIds") or []:
        src = str(rid.get("sourceName") or "").lower()
        if "imdb" in src:
            imdb_id = rid.get("id")
        elif "themoviedb" in src or "tmdb" in src:
            try:
                tmdb_id = int(rid.get("id"))
            except (TypeError, ValueError):
                tmdb_id = rid.get("id")
    return imdb_id, tmdb_id


# ---------------------------------------------------------------------------
# crossid — every other id for one work
# ---------------------------------------------------------------------------

def parse_ref(ref: str):
    provider, _, value = (ref or "").partition(":")
    return provider.strip().lower(), value.strip()


async def crossid(deps: GwDeps, ref: str):
    provider, value = parse_ref(ref)
    key = f"crossid:{provider}:{value}"
    hit = await _cache_get(deps, key)
    if hit is not None:
        return {**hit, "cached": True}
    ids, down = {}, []
    try:
        if provider == "tvdb":
            doc = await deps.tvdb_extended(int(value))
            imdb_id, tmdb_id = _tvdb_remote_ids(doc or {})
            ids = {"tvdb": int(value), "imdb": imdb_id, "tmdb": tmdb_id}
        elif provider == "anilist":
            ids = {"anilist": int(value)} if value.isdigit() else {}
            # idMal hop: fetch the media record by id.
            data = await deps.http_post(ANILIST_URL, {
                "query": "query($id:Int){Media(id:$id){id idMal}}",
                "variables": {"id": int(value)}}) if value.isdigit() else None
            media = ((data or {}).get("data") or {}).get("Media") or {}
            if media.get("idMal"):
                ids["mal"] = media["idMal"]
        elif provider == "mal":
            data = await deps.http_get(f"{JIKAN_URL}/anime/{value}/full")
            ext = ((data or {}).get("data") or {}).get("external") or []
            ids = {"mal": int(value)}
            for e in ext:
                u = (e.get("url") or "").lower()
                if "anilist.co" in u:
                    ids["anilist"] = u.rstrip("/").split("/")[-1]
                elif "kitsu" in u:
                    ids["kitsu"] = u.rstrip("/").split("/")[-1]
        elif provider == "kitsu":
            maps = await deps.kitsu_mappings(int(value))
            ids = {"kitsu": int(value)}
            for k, v in (maps or {}).items():
                if v:
                    ids[str(k).lower()] = v
        elif provider in ("tmdb", "imdb"):
            ids = {provider: value}
            # TMDB<->IMDb bridging for series lives in the app search:
            # a Cinemeta detail confirms the imdb side; tmdb side is
            # resolved by lookup() merging. Kept honest: report what
            # is directly known.
        else:
            return {"ok": False, "message": f"unknown provider '{provider}'"}
    except Exception:
        down.append(provider)
    ids = {k: v for k, v in ids.items() if v}
    out = {"ok": True, "ids": ids, "providers_down": down}
    if not down:
        await _cache_put(deps, key, out, "crossid")
    return out


# ---------------------------------------------------------------------------
# series — episode lists in the numbering the bot asks for
# ---------------------------------------------------------------------------

def _tvdb_ep_row(ep: dict, absolute_n=None):
    return {"season": ep.get("seasonNumber"), "episode": ep.get("number"),
            "absolute": absolute_n if absolute_n is not None
                        else ep.get("absoluteNumber"),
            "title": ep.get("name"), "air_date": ep.get("aired")}


async def series(deps: GwDeps, ref: str):
    provider, value = parse_ref(ref)
    key = f"series:{provider}:{value}"
    hit = await _cache_get(deps, key)
    if hit is not None:
        return {**hit, "cached": True}
    ids = (await crossid(deps, ref)).get("ids") or {}
    if provider and provider not in ids:
        ids[provider] = int(value) if value.isdigit() else value
    out = {"ok": True, "ids": ids, "aired": [], "absolute": [],
           "specials": [], "notes": []}
    down = []
    tvdb_id = ids.get("tvdb")
    if tvdb_id:
        try:
            aired = await deps.tvdb_episodes(int(tvdb_id), "default")
            for ep in aired or []:
                row = _tvdb_ep_row(ep)
                if row["season"] == 0:
                    out["specials"].append(row)
                else:
                    out["aired"].append(row)
            absolute = await deps.tvdb_episodes(int(tvdb_id), "absolute")
            for i, ep in enumerate(absolute or [], start=1):
                row = _tvdb_ep_row(ep, absolute_n=ep.get("number") or i)
                out["absolute"].append(row)
        except Exception:
            down.append("tvdb")
    mal_id = ids.get("mal")
    if mal_id and not out["aired"]:
        try:
            eps = await jikan_episodes(deps, int(mal_id))
            for ep in eps or []:
                out["aired"].append({
                    "season": 1, "episode": ep.get("mal_id"),
                    "absolute": ep.get("mal_id"), "title": ep.get("title"),
                    "air_date": ep.get("aired")})
            if out["aired"]:
                out["notes"].append("episodes from MAL via Jikan "
                                     "(single-season listing)")
        except Exception:
            down.append("jikan")
    imdb_id = ids.get("imdb")
    if imdb_id and not out["aired"]:
        try:
            detail = await deps.cinemeta_detail(imdb_id)
            for v in ((detail or {}).get("videos") or []):
                row = {"season": v.get("season"), "episode": v.get("episode"),
                       "absolute": None, "title": v.get("name"),
                       "air_date": v.get("released")}
                (out["specials"] if row["season"] == 0
                 else out["aired"]).append(row)
            if out["aired"] or out["specials"]:
                out["notes"].append("episodes from Cinemeta (IMDb-keyed)")
        except Exception:
            down.append("cinemeta")
    if ids.get("anilist") and not (out["aired"] or out["absolute"]):
        out["notes"].append(
            "AniList has no episode list (one entry per cour) — "
            "numbering must come from TVDB or MAL for this series")
    out["providers_down"] = down
    if not down and (out["aired"] or out["absolute"] or out["specials"]):
        await _cache_put(deps, key, out, "series")
    return out
