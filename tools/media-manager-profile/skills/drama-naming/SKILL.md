---
name: drama-naming
description: K-drama, C-drama & Indian content — romanization, counts
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [kdrama, cdrama, indian, romanization]
    category: media
---

# Drama naming (K-drama / C-drama / Indian)

## When to Use

Korean, Chinese (live-action) and Indian series/films in any channel.

## The core problem: no canonical name

- **Romanization varies**: Revised Romanization vs McCune–Reischauer
  vs the streaming platform's own title (Viki, Netflix, iQIYI,
  Tencent all romanize differently). One drama may have 3+ English
  forms plus a literal translation. Search providers with at least
  two forms + the native title.
- **C-drama** adds the donghua title-form problem (hanzi / pinyin /
  English) — but for live-action, TMDB coverage is decent and
  Bangumi still helps for structure.
- **Indian content**: Hindi/regional titles in Latin script vary by
  uploader spelling; films split by language version (Hindi original
  vs Tamil/Telugu dub are different releases of the same TMDB entry —
  the LANGUAGE is non-recoverable info: keep it in the caption note).

## Procedure

1. Identify by native title + year first (native titles are unique;
  romanizations are not). TMDB carries native titles — search them.
2. **Episode counts differ per provider** for Asian dramas: the
  broadcast count (e.g. 40 × 45 min) vs the international cut
  (e.g. 80 × 22 min, or merged episodes) can BOTH exist. Determine
  which cut the FILE is (duration is evidence) and follow the
  provider whose list matches that cut; state it in the note.
3. K-drama seasons are rare (most are single-season complete works);
  a "Season 2" claim needs a provider citation, not the uploader's
  word. Weekend/family dramas run 50-120 episodes — large numbers
  are normal, not absolute-numbering errors.
4. Indian series: daily soaps run into the hundreds/thousands of
  episodes with date-based identification on providers; use the
  date form like tv-naming's daily shows.
5. Films: movie-naming rules + language version noted.

## Pitfalls

- Same romanized title, different dramas (short common titles
  collide across years) — year + native title disambiguate.
- Platform-exclusive English titles (Netflix's) may not exist on
  TMDB under that name; search the native title there.
- "EP" counts in uploader names for C-drama often count the
  INTERNATIONAL cut while TMDB lists the broadcast cut (or the
  reverse) — a systematic offset across a whole run is the symptom;
  decide the run once, with the offset stated.

## Verification

- ✅ requires the native title matched on the provider page + the
  episode present in that provider's list for the chosen cut.
