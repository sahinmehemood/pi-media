"""Librarian v2 Phase 2 tests: the identification ladder, evidence
scores (SPEC §4), circuit breakers, caching, and slotting — driven by a
scripted provider fake implementing the identify protocol."""

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "librarian_v2"))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


store_mod = _load("libv2_store2", "Backend/librarian_v2/store.py")
identify = _load("libv2_identify", "Backend/librarian_v2/identify.py")

T0 = 1_700_000_000


class FakeProviders:
    def __init__(self):
        self.search_results = {}
        self.by_id = {}
        self.eps = {}
        self.down = set()
        self.calls = []

    def search(self, provider, query, kind, year):
        self.calls.append(("search", provider, query))
        if provider in self.down:
            raise identify.ProviderDown(provider)
        return list(self.search_results.get((provider, query), []))

    def lookup_by_id(self, provider, pid, kind):
        self.calls.append(("lookup", provider, pid))
        if provider in self.down:
            raise identify.ProviderDown(provider)
        return self.by_id.get((provider, pid))

    def episodes(self, provider, pid, kind):
        self.calls.append(("episodes", provider, pid))
        if provider in self.down:
            raise identify.ProviderDown(provider)
        return list(self.eps.get((provider, pid), []))


def _cand(provider, pid, title, year, imdb=None):
    return {"provider": provider, "provider_id": pid, "title": title,
            "year": year, "imdb_id": imdb}


def _setup():
    path = str(Path(tempfile.mkdtemp(prefix="libv2id-")) / "t.sqlite3")
    store = store_mod.open_store(path)
    prov = FakeProviders()
    return store, prov, identify.Identifier(store, prov)


def test_alias_hit_makes_no_provider_calls():
    store, prov, ident = _setup()
    sid = store.upsert_series("Perfect World", kind="donghua", year=2021,
                              provider="tmdb", provider_id="1", now=T0)
    store.add_alias(sid, identify.normalise("pw"), kind="donghua")
    res = ident.resolve("pw", kind="donghua", now=T0)
    assert res.method == "alias" and res.score == 0.97
    assert res.series_id == sid
    assert prov.calls == []


def test_explicit_id_short_circuits():
    store, prov, ident = _setup()
    prov.by_id[("tmdb", "41498")] = _cand("tmdb", "41498", "Strong World",
                                          2009, "tt1485763")
    prov.eps[("tmdb", "41498")] = [
        {"season_number": 0, "episode_number": 1, "absolute_number": None,
         "title": "Movie", "air_date": "2009-12-12"}]
    res = ident.resolve("one piece strong world", kind="movie",
                        explicit={"provider": "tmdb", "provider_id": "41498"},
                        now=T0)
    assert res.method == "explicit_id" and res.score == 1.00
    assert store.list_episodes(res.series_id)  # materialised + episodes


def test_search_agreement_scores_095():
    store, prov, ident = _setup()
    prov.search_results[("tmdb", "Perfect World")] = [
        _cand("tmdb", "1", "Perfect World", 2021, "tt123")]
    prov.search_results[("cinemeta", "Perfect World")] = [
        _cand("cinemeta", "tt123", "Perfect World", 2021, "tt123")]
    res = ident.resolve("Perfect World", kind="tv", year=2021, now=T0)
    assert res.status == "identified" and res.score == 0.95
    assert res.breakdown["agreement"] is True


def test_search_single_year_fuzzy_scores_090_and_084():
    store, prov, ident = _setup()
    prov.search_results[("tmdb", "Steins Gate")] = [
        _cand("tmdb", "7", "Steins;Gate", 2011, "tt1910272")]
    res = ident.resolve("Steins Gate", kind="tv", year=2011, now=T0)
    assert res.score == 0.90 and res.breakdown["year_match"] is True
    store2, prov2, ident2 = _setup()
    prov2.search_results[("tmdb", "Steins Gate")] = [
        _cand("tmdb", "7", "Steins;Gate", None, "tt1910272")]
    res2 = ident2.resolve("Steins Gate", kind="tv", year=2011, now=T0)
    assert res2.score == 0.84 and res2.breakdown["year_match"] is False


def test_search_mid_fuzzy_scores_078():
    store, prov, ident = _setup()
    prov.search_results[("tmdb", "Mob Psycho")] = [
        _cand("tmdb", "3", "Mob Psycho 100", 2016, "tt5897304")]
    res = ident.resolve("Mob Psycho", kind="anime", now=T0)
    assert res.score == 0.78 and res.status == "identified"


def test_unresolved_is_negative_cached():
    store, prov, ident = _setup()
    res = ident.resolve("zzzz nothing", kind="tv", now=T0)
    assert res.status == "unresolved"
    calls_after_first = len(prov.calls)
    res2 = ident.resolve("zzzz nothing", kind="tv", now=T0 + 60)
    assert res2.method == "search_cached_negative"
    assert len(prov.calls) == calls_after_first  # no new provider calls


def test_conflict_caps_score_and_marks_series():
    store, prov, ident = _setup()
    prov.search_results[("tmdb", "Knight Run")] = [
        _cand("tmdb", "10", "Knight Run", 2022, "ttA")]
    prov.search_results[("tvdb", "Knight Run")] = [
        _cand("tvdb", "20", "Knight Runs", 2022, "ttB")]
    res = ident.resolve("Knight Run", kind="tv", year=2022, now=T0)
    assert res.status == "conflicted" and res.score == 0.50
    assert store.get_series(res.series_id)["status"] == "conflicted"


def test_breaker_trips_and_falls_through():
    store, prov, ident = _setup()
    prov.down.add("tmdb")
    prov.search_results[("tvdb", "Show Three")] = [
        _cand("tvdb", "9", "Show Three", 2020, "tt9")]
    for i, title in enumerate(("Show One", "Show Two", "Show Three")):
        ident.resolve(title, kind="tv", now=T0 + i)
    tmdb_calls = [c for c in prov.calls if c[0] == "search"
                  and c[1] == "tmdb"]
    assert len(tmdb_calls) == 3  # three failures trip the breaker
    res = ident.resolve("Show Three", kind="tv", now=T0 + 10)
    tmdb_calls = [c for c in prov.calls if c[0] == "search"
                  and c[1] == "tmdb"]
    assert len(tmdb_calls) == 3  # 4th resolve never touches tmdb
    assert res.status == "identified"  # fell through to tvdb


def test_search_cache_avoids_repeat_calls():
    store, prov, ident = _setup()
    prov.search_results[("tmdb", "Bleach")] = [
        _cand("tmdb", "5", "Bleach", 2004, "tt0434665")]
    ident.resolve("Bleach", kind="tv", year=2004, now=T0)
    n = len([c for c in prov.calls if c[0] == "search"])
    ident.resolve("Bleach", kind="tv", year=2004, now=T0 + 60)
    assert len([c for c in prov.calls if c[0] == "search"]) == n


def test_slotting_and_fit_bonus():
    store, prov, ident = _setup()
    sid = store.upsert_series("Donghua Show", kind="donghua", year=2022,
                              provider="tvdb", provider_id="42", now=T0)
    store.replace_episode_list(sid, [
        {"season_number": 1, "episode_number": 1, "absolute_number": 1,
         "title": "A", "air_date": "2022-01-01"},
        {"season_number": 1, "episode_number": 2, "absolute_number": 2,
         "title": "B", "air_date": "2022-01-08"},
        {"season_number": 0, "episode_number": 1, "absolute_number": None,
         "title": "SP1", "air_date": "2022-02-01"}], T0)
    ep, method = ident.slot_file(sid, {"season": 1, "episode": 2})
    assert method == "sxxexx" and ep["title"] == "B"
    ep, method = ident.slot_file(sid, {"absolute": 1})
    assert method == "absolute" and ep["title"] == "A"
    ep, method = ident.slot_file(sid, {"air_date": "2022-02-01"})
    assert method == "air_date" and ep["title"] == "SP1"
    ep, method = ident.slot_file(sid, {"season": 9, "episode": 9})
    assert ep is None and method is None
    assert ident.finalise_score(0.95, sid, {"season": 1, "episode": 1}) == 0.98
    assert ident.finalise_score(0.99, sid, {"season": 1, "episode": 1}) == 1.0
    assert ident.finalise_score(0.90, sid, {"season": 9, "episode": 9}) == 0.90
