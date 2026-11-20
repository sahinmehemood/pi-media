"""Provider Gateway tests (v6 §5): merged identity, outage
honesty, TVDB numbering split (aired/absolute/specials), crossid."""

import asyncio
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "librarian_gateway", ROOT / "Backend/helper/librarian_gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gw = _load()


class FakeCache:
    def __init__(self):
        self.docs = {}

    async def find_one(self, query):
        return self.docs.get(query.get("_id"))

    async def update_one(self, query, update, upsert=False):
        self.docs[query["_id"]] = {"_id": query["_id"],
                                   **(update.get("$set") or {})}


def _deps(**over):
    async def tmdb_candidates(q, kind):
        return [{"title": "Perfect World", "year": 2021, "source": "tmdb",
                 "tmdb_id": 999, "imdb_id": "tt14986786",
                 "media_type": "tv"}]

    async def tvdb_search(title):
        return {"id": "series-555", "tvdb_id": "555",
                "name": "Perfect World",
                "aliases": ["Wanmei Shijie"],
                "remoteIds": [{"sourceName": "IMDb", "id": "tt14986786"},
                              {"sourceName": "TheMovieDB", "id": "999"}]}

    async def tvdb_episodes(tvdb_id, order):
        if order == "absolute":
            return [{"seasonNumber": 1, "number": 1, "name": "Ep 1"},
                    {"seasonNumber": 1, "number": 2, "name": "Ep 2"}]
        return [{"seasonNumber": 0, "number": 1, "name": "Special 1",
                 "aired": "2021-01-01"},
                {"seasonNumber": 1, "number": 1, "name": "Ep 1",
                 "aired": "2021-04-01", "absoluteNumber": 1}]

    async def tvdb_extended(tvdb_id):
        return {"remoteIds": [{"sourceName": "IMDb", "id": "tt14986786"},
                              {"sourceName": "TheMovieDB", "id": "999"}]}

    async def cinemeta_search(q, type_):
        return []

    async def cinemeta_detail(imdb):
        return None

    async def kitsu_search(title):
        return None

    async def kitsu_mappings(kid):
        return None

    async def http_post(url, payload):
        return {"data": {"Page": {"media": [{
            "id": 777, "idMal": 888,
            "title": {"romaji": "Wanmei Shijie", "english": "Perfect World",
                      "native": "完美世界", "userPreferred": "Perfect World"},
            "synonyms": ["Perfect World"], "seasonYear": 2021}]}}}

    async def http_get(url):
        return {"data": []}

    deps = gw.GwDeps(tmdb_candidates, tvdb_search, tvdb_episodes,
                     tvdb_extended, cinemeta_search, cinemeta_detail,
                     kitsu_search, kitsu_mappings, http_post, http_get,
                     FakeCache())
    for k, v in over.items():
        setattr(deps, k, v)
    return deps


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_lookup_merges_by_shared_ids():
    deps = _deps()
    out = _run(gw.lookup(deps, "Perfect World", "donghua"))
    assert out["ok"] and not out["providers_down"]
    merged = [c for c in out["candidates"]
              if c["ids"].get("imdb") == "tt14986786"]
    assert len(merged) == 1
    c = merged[0]
    assert c["ids"]["tmdb"] == 999 and c["ids"]["tvdb"] == 555
    assert "Wanmei Shijie" in c["titles"]["synonyms"]


def test_lookup_anilist_titles_bridge_dual_names():
    deps = _deps()
    out = _run(gw.lookup(deps, "Wanmei Shijie", "donghua"))
    ani = [c for c in out["candidates"] if c["ids"].get("anilist") == 777]
    assert ani and ani[0]["titles"]["native"] == "完美世界"
    assert ani[0]["ids"]["mal"] == 888


def test_lookup_outage_is_honest_and_uncached():
    deps = _deps()

    async def boom(q, kind):
        raise RuntimeError("connection reset")
    deps.tmdb_candidates = boom
    out = _run(gw.lookup(deps, "Perfect World", "donghua"))
    assert out["ok"] and out["providers_down"] == ["tmdb"]
    assert out["candidates"]  # tvdb/anilist still answered
    assert deps.cache.docs == {}  # nothing cached from a partial answer


def test_lookup_second_call_is_cached():
    deps = _deps()
    _run(gw.lookup(deps, "Perfect World", "donghua"))
    out = _run(gw.lookup(deps, "Perfect World", "donghua"))
    assert out.get("cached") is True


def test_crossid_tvdb_remote_ids():
    deps = _deps()
    out = _run(gw.crossid(deps, "tvdb:555"))
    assert out["ids"] == {"tvdb": 555, "imdb": "tt14986786", "tmdb": 999}


def test_series_splits_specials_and_absolute():
    deps = _deps()
    out = _run(gw.series(deps, "tvdb:555"))
    assert [e["title"] for e in out["specials"]] == ["Special 1"]
    assert [e["episode"] for e in out["aired"]] == [1]
    assert [e["absolute"] for e in out["absolute"]] == [1, 2]


def test_series_unknown_provider_is_plain():
    deps = _deps()
    out = _run(gw.series(deps, "bogus:1"))
    assert out["aired"] == [] and out["absolute"] == []
