---
name: tv-naming
description: Western TV — orderings, packs, multi-episode, daily shows
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [tv, tvdb, seasons, episodes]
    category: media
---

# TV naming (Western series)

## When to Use

Runs from the Series channel, and live-action series anywhere else.

## The ordering problem (read first)

One series can have FOUR different episode orders: **aired** (TVDB
default), **DVD**, **absolute**, and **production**. Uploaders use
aired order unless the name says otherwise (`DVD`, `DVDRip` season
packs often follow DVD order). Your fix caption must follow the
ordering of the provider whose ID you stamp — and say which in the
note when it is not aired order.

## Procedure

1. Identify the series (ID-first; year separates remakes —
   "Series (2019)" vs the original).
2. Confirm the season/episode against TVDB's episode list for that
   season: count + episode title when available.
3. **Multi-episode files** (`S01E01-E02`, `S01E01E02`): one file, two
   episodes. The app's matcher rejects some combined forms — propose
   the first-episode form and note the span; if the gate refuses,
   flag ❓ rather than forcing it.
4. **Season packs**: a post whose files span a season is decided
   once (one series ID), then each file numbered individually.
5. **Daily/date-based shows** (talk shows, soaps, news-adjacent):
   the date IS the numbering (`2024.03.11`). TVDB lists them by
   date; fix captions keep `Title (Year) <date> Quality`.
6. **Miniseries**: TVDB files them as one season of a series; TMDB
   agrees (type TV). Stamp as TV with S01 numbering even when the
   uploader called it a "movie collection".
7. **Finales split as Part 1 / Part 2** may be one TVDB episode or
   two — check the episode list; never assume.
8. **Specials** (Christmas specials, recap episodes, behind-the-
   scenes): TVDB season 0. Name the special's title in the note;
   see the specials rule in provider-research.

## Pitfalls

- **Season numbering restarts**: some series number by production
  blocks or Netflix "parts" (Part 1 = S01, Part 2 = S02 on TMDB but
  one season on TVDB). State the provider you follow.
- **Episode 0 / pilot** unaired episodes live in season 0 on TVDB.
- British series: "Series 1" = Season 1; episode counts are small
  (6 is normal) — a "missing episodes" gap may not be missing.
- Animated Western series: use cartoon-naming instead.

## Verification

- For every ✅ episode: TVDB shows that season having at least that
  many episodes, and the year of the series matches the plan.
