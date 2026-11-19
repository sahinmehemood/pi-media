---
name: anime-naming
description: Anime — fansub anatomy, AniDB/AniList/Kitsu numbering, cours
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [anime, anidb, kitsu, anilist, fansub]
    category: media
---

# Anime naming

## When to Use

Runs from the Ani channel and anime anywhere else.

## Fansub anatomy

`[Group] Title — 05 (720p) [CRC].mkv` — group first, title, episode,
resolution, checksum. Variants: `EP05`, `E05`, `— 05v2` (v2 =
corrected re-release of the SAME episode), episode titles after the
number, `[Batch]` for whole-cour packs. Chinese groups join tags
with `&` and often carry dual (Chinese + English) titles.

## The numbering models (the heart of anime)

- **AniDB**: one entry per production; episodes numbered ABSOLUTELY
  within the entry; specials prefixed separately (S = special,
  C = credits, T = trailer). A new season is usually a NEW AniDB
  entry with its own numbering from 1.
- **AniList / MAL / Kitsu**: a separate entry (own ID) for every
  season AND every split-cour part. Kitsu is the app's first anime
  provider — its per-season entries are why "Season 2" of a show may
  carry a completely different Kitsu ID.
- **TVDB / TMDB**: one series entry; seasons S01, S02…; absolute
  numbers run across seasons; specials in season 0.
- **Split cours**: a 24-episode production aired as two 12-episode
  cours with a break. AniList/Kitsu: two entries. TVDB: usually ONE
  season of 24 (or two seasons — decided by announcement timing).
  The SAME file can be "Part 2 ep 5", "S01E17", or "S02E05"
  depending on the database. All three can be right — for different
  IDs. State which ID your numbering follows.

## Procedure

1. Identify the work on Kitsu/AniList (per-season entries) AND find
   its TVDB/TMDB series entry. Record both ID families in the note.
2. Decide the numbering the CAPTION will use: the app indexes anime
   via Kitsu > TVDB > TMDB. Prefer the form the gate verifies:
   SxxExx against the TVDB/TMDB series, with the ID stamp of the
   provider you verified against.
3. **Absolute numbers**: long-runners (hundreds of episodes) are
   uploaded by absolute episode. If the name's number exceeds the
   season's episode count, it is absolute — convert via the
   provider's episode list (the app has absolute-episode recovery;
   your caption should still use SxxExx + the correct ID).
4. **Arc names in titles** ("— Yuukaku-hen — EP03"): the arc is a
   SEASON on Kitsu/AniList and usually a season on TVDB too. Map arc
   → season via the episode lists, never by guessing the arc's
   position.
5. **Movies/OVAs**: theatrical films are separate entries (movie
   rules + this skill). OVAs/specials go to season 0 per the named
   provider; picture dramas and bonus shorts are ❓ by default.

## Pitfalls

- Bare numbers >100 can be misread as S+E (e.g. 105 → S01E05).
  Zero-padded SxxExx in fix captions prevents it.
- "EP29" of a 12-episode season is absolute numbering, not an error.
- Decimal episodes (7.5) are recap/interlude specials — season 0
  candidates, never regular episodes.
- A season's Kitsu ID differs from the series' TVDB ID — stamping
  the Kitsu series ID with TVDB numbering (or vice versa) produces
  confident wrong matches. The gate checks ID vs resolution; your
  note must say which pairing you used.
- Release-group "batch" posts re-pack episodes from multiple cours —
  decide per file.

## Verification

- For ✅ items: the episode exists in the stamped provider's list
  for that season, and the total season count accommodates it.
