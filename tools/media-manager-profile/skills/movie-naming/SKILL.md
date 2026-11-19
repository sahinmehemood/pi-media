---
name: movie-naming
description: Movie identification — years, remakes, editions, franchises
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [movies, tmdb, editions, remakes]
    category: media
---

# Movie naming

## When to Use

Any run from the Movie channel, and any film-like item elsewhere
(including anime/donghua films — combine with their type skill).

## Procedure

1. **Year is identity.** A movie plan always carries the release
   year. No year in the name → research it (provider-research).
   Same-title remakes (and same-title unrelated films) are separated
   ONLY by year + ID.
2. **ID-first.** Get the TMDB id and the IMDb id from the app's
   search (`--type movie`) or TMDB. The stamp prefers IMDb.
3. **Editions are different products.** Director's Cut / Extended /
   Unrated / Theatrical / IMAX / Remastered: keep the edition word in
   the fix caption's first line after the quality token, and note the
   edition in the proposal note. An edition mismatch is a wrong fix.
4. **Franchises**: identify each film individually — never let a
   collection/bundle caption ("Trilogy pack") stand for a single
   file. Multi-part films (Part 1/Part 2 released separately) are
   separate TMDB entries; a single film split into file parts is ONE
   entry (and the app rejects multipart — flag those ⛔ with the
   reason "split file").
5. **Sequels by number or subtitle**: the number belongs to the
   title ("Movie 2"), it is not an episode. Roman numerals in titles
   stay as the provider writes them.
6. **Foreign-language films**: the provider's canonical English title
   + year; keep the original-language title in the note when the
   uploader used it.

## Decision table

| Evidence | Verdict |
|---|---|
| Title + year + one provider ID | ✅ propose |
| Title + year, two providers agree on ID | ✅ propose |
| Title only, multiple same-title films | ❓ list candidates |
| Edition unclear (cut not stated anywhere) | ❓ |
| Animated film in Ani/Donghua channel | ✅/❓ per this + type skill |

## Pitfalls

- **Year in the name may be the UPLOAD year or a sequel number** —
  verify against the provider, never trust position alone.
- **"Complete Collection" posts**: each file is judged alone.
- Bollywood/Indian films: year + language version matter (Hindi
  original vs dubbed) — see drama-naming for romanization traps.
- A movie with an episode-like token (`E01`) is usually a
  behind-the-scenes extra or a misfiled series episode — look closer
  before treating it as a film.

## Verification

- The plan's year equals the provider's release year for the stamped
  ID (the strict-year gate, when on, enforces this mechanically).
