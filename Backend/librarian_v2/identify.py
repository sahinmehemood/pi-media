"""Librarian v2 identification — resolve a series ONCE, slot files locally.

Implements SPEC §4: the resolution ladder (explicit ID → learned alias →
provider search with agreement detection), server-computed evidence
scores, per-provider circuit breakers, provider/search caching, and
episode slotting. Pure stdlib; provider access goes through a small
synchronous protocol so tests drive it with scripted stubs and the app
adapter (Phase 3) binds it to the app's async metadata functions.

Provider protocol (any object with these methods; each may raise
ProviderDown):
    search(provider, query, kind, year) -> [candidate]
    lookup_by_id(provider, provider_id, kind) -> candidate | None
    episodes(provider, provider_id, kind) -> [episode]
candidate: {provider, provider_id, title, year, imdb_id}
episode:   {season_number, episode_number, absolute_number, title, air_date}
"""

import re
import time
from difflib import SequenceMatcher

SEARCH_TTL = 7 * 24 * 3600
NEGATIVE_TTL = 24 * 3600
EPISODES_TTL = 30 * 24 * 3600
BREAKER_THRESHOLD = 3
BREAKER_OPEN_SECONDS = 600

CHAINS = {
    "movie": ("tmdb", "cinemeta"),
    "tv": ("tmdb", "tvdb", "cinemeta"),
    "series": ("tmdb", "tvdb", "cinemeta"),
    "anime": ("tmdb", "kitsu", "tvdb", "cinemeta"),
    "donghua": ("tmdb", "kitsu", "tvdb", "cinemeta"),
    "cartoon": ("tmdb", "tvdb", "cinemeta"),
}


class ProviderDown(Exception):
    pass


# ---------------------------------------------------------------------------
# Normalisation + fuzzy scoring (the ONLY implementations — SPEC §3/§4).
# ---------------------------------------------------------------------------

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalise(text):
    """Lowercase, punctuation/space-stripped form used for aliases/stems."""
    if not text:
        return ""
    return _NON_ALNUM.sub("", str(text).lower())


def _tokens(text):
    return set(_NON_ALNUM.sub(" ", str(text or "").lower()).split())


def fuzzy(a, b):
    """0..1 similarity: max of token-set overlap and sequence ratio.
    Deterministic pure-python stand-in for RapidFuzz WRatio/token_set —
    no third-party dependency on the phone."""
    a, b = str(a or ""), str(b or "")
    if not a or not b:
        return 0.0
    ta, tb = _tokens(a), _tokens(b)
    token_score = 0.0
    if ta and tb:
        token_score = len(ta & tb) / max(len(ta), len(tb))
    seq = SequenceMatcher(None, normalise(a), normalise(b)).ratio()
    return max(token_score, seq)


# ---------------------------------------------------------------------------
# Circuit breaker — state persisted via store sync_state so it survives
# restarts and is shared by every worker in the process.
# ---------------------------------------------------------------------------

class Breaker:
    def __init__(self, store):
        self.store = store

    def _key(self, provider):
        return f"breaker:{provider}"

    def _state(self, provider):
        raw = self.store.get_watermark(self._key(provider))
        if not raw:
            return {"fails": 0, "open_until": 0}
        try:
            fails, open_until = raw.split(":")
            return {"fails": int(fails), "open_until": int(open_until)}
        except ValueError:
            return {"fails": 0, "open_until": 0}

    def allow(self, provider, now):
        return now >= self._state(provider)["open_until"]

    def record_success(self, provider):
        self.store.set_watermark(self._key(provider), "0:0")

    def record_failure(self, provider, now):
        st = self._state(provider)
        fails = st["fails"] + 1
        open_until = now + BREAKER_OPEN_SECONDS if fails >= BREAKER_THRESHOLD \
            else st["open_until"]
        self.store.set_watermark(self._key(provider), f"{fails}:{open_until}")


# ---------------------------------------------------------------------------
# Identifier
# ---------------------------------------------------------------------------

class Resolution:
    def __init__(self, series_id, method, score, breakdown, status,
                 candidate=None):
        self.series_id = series_id
        self.method = method
        self.score = score
        self.breakdown = breakdown
        self.status = status          # identified|conflicted|unresolved
        self.candidate = candidate

    def __repr__(self):
        return (f"Resolution(series={self.series_id}, method={self.method},"
                f" score={self.score:.2f}, status={self.status})")


class Identifier:
    def __init__(self, store, providers):
        self.store = store
        self.providers = providers
        self.breaker = Breaker(store)

    # -- provider call wrappers (breaker + bookkeeping) --------------------
    def _search(self, provider, query, kind, year, now):
        if not self.breaker.allow(provider, now):
            return None  # skipped, provider considered down
        try:
            results = self.providers.search(provider, query, kind, year)
        except ProviderDown:
            self.breaker.record_failure(provider, now)
            return None
        self.breaker.record_success(provider)
        return results or []

    def _lookup(self, provider, provider_id, kind, now):
        if not self.breaker.allow(provider, now):
            return None
        try:
            result = self.providers.lookup_by_id(provider, provider_id, kind)
        except ProviderDown:
            self.breaker.record_failure(provider, now)
            return None
        self.breaker.record_success(provider)
        return result

    def _episodes(self, provider, provider_id, kind, now):
        if not self.breaker.allow(provider, now):
            return None
        try:
            eps = self.providers.episodes(provider, provider_id, kind)
        except ProviderDown:
            self.breaker.record_failure(provider, now)
            return None
        self.breaker.record_success(provider)
        return eps or []

    # -- the ladder ----------------------------------------------------------
    def resolve(self, raw_title, kind="tv", year=None, explicit=None, now=None):
        """explicit: {provider, provider_id, imdb_id} if the file's own
        caption/name carried an ID. Returns a Resolution."""
        now = int(time.time()) if now is None else now
        kind = kind or "tv"

        # 1. Explicit provider ID in the file itself — strongest evidence.
        if explicit and explicit.get("provider_id"):
            cand = self._lookup(explicit["provider"], explicit["provider_id"],
                                kind, now)
            if cand:
                sid = self._materialise(cand, kind, now, teach_names=True)
                return Resolution(sid, "explicit_id", 1.00,
                                  {"explicit_id": 1.00}, "identified", cand)

        # 2. Learned alias — solved once, free forever.
        series = self.store.find_series_by_alias(normalise(raw_title), kind)
        if series:
            return Resolution(series["id"], "alias", 0.97,
                              {"alias_hit": 0.97}, "identified")

        # 3+4. Provider search chain with agreement detection, cached.
        cache_key = f"search:{kind}:{normalise(raw_title)}:{year or ''}"
        cached = self.store.cache_get(cache_key, now)
        if cached is not None:
            if cached["negative"]:
                return Resolution(None, "search_cached_negative", 0.0,
                                  {"cached_negative": True}, "unresolved")
            return self._score_candidates(cached["payload"], raw_title, year,
                                          kind, now, from_cache=True)

        chain = CHAINS.get(kind, CHAINS["tv"])
        candidates = []
        answered = 0
        for provider in chain:
            results = self._search(provider, raw_title, kind, year, now)
            if results is not None:
                answered += 1
                candidates.extend(results)
        if not candidates:
            if answered == 0:
                # Every provider is down or breaker-skipped: this is an
                # OUTAGE, not a dead end. Never negative-cache it — the
                # stem stays retryable (SPEC amendment 3).
                return Resolution(None, "search", 0.0,
                                  {"providers_down": True}, "unresolved")
            self.store.cache_put(cache_key, [], NEGATIVE_TTL, now,
                                 negative=True)
            return Resolution(None, "search", 0.0, {"no_candidates": True},
                              "unresolved")
        if answered == len(chain):
            # Only a complete picture is worth caching; a partial one
            # (some providers down) is used now but never frozen.
            self.store.cache_put(cache_key, candidates, SEARCH_TTL, now)
        return self._score_candidates(candidates, raw_title, year, kind, now)

    def _score_candidates(self, candidates, raw_title, year, kind, now,
                          from_cache=False):
        scored = []
        for cand in candidates:
            f = fuzzy(raw_title, cand.get("title"))
            year_match = bool(year and cand.get("year")
                              and int(cand["year"]) == int(year))
            scored.append((f, year_match, cand))
        scored.sort(key=lambda t: t[0], reverse=True)
        best_f, best_year, best = scored[0]

        # Agreement: a second candidate for the SAME series (shared imdb or
        # provider id, or same normalised title+year) from another provider.
        def same_series(a, b):
            ai, bi = a.get("imdb_id"), b.get("imdb_id")
            if ai and bi:
                return ai == bi  # both carry IMDb: it decides
            if a.get("provider") == b.get("provider") and \
                    a.get("provider_id") and b.get("provider_id"):
                return a["provider_id"] == b["provider_id"]
            return normalise(a.get("title")) == normalise(b.get("title")) \
                and str(a.get("year")) == str(b.get("year"))

        agreement = any(same_series(best, other)
                        for _, _, other in scored[1:])
        # Conflict: a DIFFERENT series scored nearly as well.
        conflict = any((not same_series(best, other)) and f2 >= 0.85
                       for f2, _, other in scored[1:])

        breakdown = {"fuzzy": round(best_f, 3), "year_match": best_year,
                     "agreement": agreement, "conflict": conflict,
                     "provider": best.get("provider")}
        if conflict:
            sid = self._materialise(best, kind, now, status="conflicted")
            return Resolution(sid, "search", 0.50, breakdown, "conflicted",
                              best)
        if agreement and best_f >= 0.85:
            score = 0.95
        elif best_year and best_f >= 0.92:
            score = 0.90
        elif best_f >= 0.92:
            score = 0.84
        elif best_f >= 0.85:
            score = 0.78
        else:
            return Resolution(None, "search", round(best_f, 3), breakdown,
                              "unresolved")
        sid = self._materialise(best, kind, now)
        return Resolution(sid, "search", score, breakdown, "identified", best)

    def verify_proposal(self, proposal, kind="tv", year=None, now=None):
        """Verify an identity the BOT researched (SPEC amendment 2).

        The shadow run (2026-11-20) proved the ladder alone cannot crack
        this library's abbreviated stems when a primary provider is down:
        expansion of 'pw' -> 'Perfect World' is research, and research is
        the bot's job. The server's job is to VERIFY the bot's claim
        against the provider and score the evidence:
          - provider confirms the proposed ID            -> 0.90
          - + the proposed IMDb ID matches the record    -> 0.97
          - ID does not verify and no title to fall back -> unresolved
        Slotting fit (+0.03) is added later, at plan time."""
        now = int(time.time()) if now is None else now
        kind = kind or "tv"
        cand = None
        if proposal.get("provider") and proposal.get("provider_id"):
            cand = self._lookup(proposal["provider"],
                                str(proposal["provider_id"]), kind, now)
        if cand is None:
            if proposal.get("title"):
                return self.resolve(proposal["title"], kind=kind, year=year,
                                    now=now)
            return Resolution(None, "verify_proposal", 0.0,
                              {"proposal_unverified": True}, "unresolved")
        sid = self._materialise(cand, kind, now, teach_names=True)
        breakdown = {"proposal_verified": True,
                     "provider": cand.get("provider")}
        score = 0.90
        if proposal.get("imdb_id") and \
                cand.get("imdb_id") == proposal["imdb_id"]:
            score = 0.97
            breakdown["imdb_corroborated"] = True
        return Resolution(sid, "verify_proposal", score, breakdown,
                          "identified", cand)

    def _materialise(self, cand, kind, now, status="identified",
                     teach_names=False):
        sid = self.store.upsert_series(
            cand.get("title") or "", kind=kind, year=cand.get("year"),
            provider=cand.get("provider"), provider_id=cand.get("provider_id"),
            imdb_id=cand.get("imdb_id"), status=status, now=now)
        # Dual-named titles (romaji vs western vs native): teach every
        # name the provider knows — canonical title included — as an
        # alias, so a file using any of them resolves instantly. ONLY
        # from high-trust paths (explicit ID, verified proposal): a
        # series that came from a weak search must not teach its names,
        # or one wrong match would spread to every variant (the BTTH
        # failure mode). Search-born series earn aliases when a fix
        # actually applies (orders hook).
        if teach_names:
            if cand.get("title"):
                self.store.add_alias(sid, normalise(cand["title"]), kind)
            for alt in cand.get("also_known_as") or []:
                if alt and normalise(alt) != normalise(cand.get("title")):
                    self.store.add_alias(sid, normalise(alt), kind)
        self.ensure_episodes(sid, now)
        return sid

    # -- episodes --------------------------------------------------------------
    def ensure_episodes(self, series_id, now):
        """Fetch + store the episode list once (cached in the DB itself;
        refetch only when the series has none and the provider allows)."""
        series = self.store.get_series(series_id)
        if series is None or not series["provider"]:
            return False
        if self.store.list_episodes(series_id):
            return True
        eps = self._episodes(series["provider"], series["provider_id"],
                             series["kind"], now)
        if eps:
            self.store.replace_episode_list(series_id, eps, now)
            return True
        return False

    # -- slotting ----------------------------------------------------------------
    def slot_file(self, series_id, bits):
        """bits: {season, episode, absolute, air_date}. Returns
        (episode_row, method) or (None, None)."""
        if bits.get("season") is not None and bits.get("episode") is not None:
            ep = self.store.get_episode(series_id, bits["season"],
                                        bits["episode"])
            if ep:
                return ep, "sxxexx"
        if bits.get("absolute") is not None:
            ep = self.store.get_episode_by_absolute(series_id,
                                                    bits["absolute"])
            if ep:
                return ep, "absolute"
        if bits.get("air_date"):
            for ep in self.store.list_episodes(series_id):
                if ep["air_date"] == bits["air_date"]:
                    return ep, "air_date"
        return None, None

    def episode_fit(self, series_id, bits):
        ep, _ = self.slot_file(series_id, bits)
        return ep is not None

    def finalise_score(self, base, series_id, bits):
        """SPEC §4: +0.03 when the file's episode exists in the list."""
        if series_id and self.episode_fit(series_id, bits):
            return min(1.0, round(base + 0.03, 3))
        return base
