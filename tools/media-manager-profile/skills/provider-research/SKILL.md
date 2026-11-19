---
name: provider-research
description: Identify titles with evidence — IDs first, two sources agree
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [research, tmdb, tvdb, kitsu, bangumi, identification]
    category: media
---

# Provider research — how to identify with evidence

## When to Use

For every run you must identify, before building a proposal. This is
the method; the naming skills hold the per-type knowledge.

## The evidence ladder (strongest first)

1. **An explicit ID** (IMDb tt…, TMDB id, TVDB id, Kitsu id) attached
   to the title by any reliable source. An ID settles identity; only
   numbering remains to verify.
2. **Two providers agreeing** on title + structure (episode list,
   season layout, year).
3. **One provider + the file's own internal evidence** (episode count
   in the batch, episode titles, air dates) agreeing.
4. Anything less → ❓ flag for Yan. Never propose from a single fuzzy
   title match alone.

## Procedure

1. **Start with the app's own search**: `librarian-cli.py search`
   returns the app's /set candidates (the same matcher family the
   gate uses). Note candidate IDs.
2. **Cross-check the EPISODE LIST, not just the title.** For series,
   fetch the candidate's episode list from a second provider and
   confirm: total episodes per season, the episode your file claims
   to be, and any specials. A title match with the wrong episode
   count is a wrong match.
3. **Providers by type** (also in the naming skills):
   - Anime: Kitsu and AniList for identity/season splits; TVDB/TMDB
     for the numbering the app will use.
   - Donghua: **Bangumi (bgm.tv) is the ground truth for structure**
     (arcs, specials, counts). Identify there, then project to the
     TMDB/TVDB/Kitsu ID the app can stamp.
   - Western TV: TVDB structure first; TMDB second.
   - Movies: TMDB first; IMDb id preferred for the stamp.
   - Cartoons: TVDB and TMDB OFTEN DISAGREE on splits — check both,
     record which one your plan follows in the note.
4. **Specials rule.** A specials plan must name its provider and the
   special's title (note format: "specials per TVDB: '<the special's
   listed title>'"). TVDB and TMDB season-0 lists differ in count
   and order; a bare S00Exx without a named provider is not evidence.
5. **Record the IDs you used** in the proposal note. When the app
   teaches an alias from an approved fix, later runs of the same stem
   inherit it — the packet shows taught aliases; trust them over a
   fresh fuzzy match.
6. **Escalate before flagging.** If the fast pass is ambiguous, retry
   the identification once with deeper research (episode titles, air
   dates, Bangumi/AniList pages). Still ambiguous → ❓ with the two
   candidate answers stated, so Yan decides in one glance.

## Pitfalls

- **Same title, different work**: remakes and same-name series are
  separated by YEAR — always carry the year in your search and plan.
- **A season that "exists" proves nothing**: S03E01 existing does not
  make an SP3 file S03E01. Structure (what the file IS) outranks
  slot availability (where it could fit).
- **Provider episode counts conflict** on long runners and donghua —
  state both counts in the note when they differ; follow the type
  skill's tie-break rule.
- **Cinemeta (IMDb) series data** can lag specials by months; for
  specials prefer TVDB/TMDB structure + an IMDb stamp only when the
  IMDb episode list also carries the special.
- Never invent an ID. An ID you cannot cite from a provider page or
  the app's search does not go in a plan.

## Verification

- Every ✅ run in your digest can answer: which two sources, which
  IDs, which episode list was checked. If you cannot, it is ❓.
