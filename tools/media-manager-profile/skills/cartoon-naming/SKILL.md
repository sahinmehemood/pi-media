---
name: cartoon-naming
description: Western cartoons — segment episodes, split numbering chaos
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [cartoons, animation, tvdb, tmdb]
    category: media
---

# Cartoon naming

## When to Use

Runs from the Cartoon channel and Western animated series anywhere.

## Why cartoons are special

Western cartoons were broadcast as **segments**: one 22-minute slot =
two 11-minute stories (11a / 11b). Providers disagree about what an
"episode" is:

- **TVDB** usually lists each segment as its own episode
  (S01E01 = segment A, S01E02 = segment B).
- **TMDB** often lists the pair as one episode, or splits them
  differently again.
- Uploaders use BOTH conventions, plus production numbers.

A numbering that is correct for one provider is wrong for the other.
Your plan must state which provider's numbering it follows, and the
stamped ID must be that provider's.

## Procedure

1. Identify the series (ID-first; cartoons are heavily remade —
   year matters: 1987 vs 2003 vs 2018 versions are different shows).
2. Determine the file's form: single segment (~11 min), full pair
   (~22 min), or full episode. File duration/size is evidence.
3. Pull BOTH TVDB and TMDB episode lists for the season. Map the
   file by episode TITLE whenever the name carries one — titles are
   far more reliable than numbers here.
4. Segment letters: `11a` = first segment of broadcast episode 11.
   Convert per the chosen provider's list (often TVDB S0xE(2n-1)).
   Never convert by formula alone — confirm against the list.
5. If the two providers' structures cannot both be satisfied (they
   often cannot), follow **TVDB** (the app's series chain tries TVDB
   first) and note "numbering per TVDB; TMDB splits differ".

## Pitfalls

- **Production order vs aired order** differs wildly in cartoons;
  uploaders' "E05" may be production #5. Episode titles settle it.
- Holiday specials and TV-movies of a cartoon series live in
  season 0 or as separate movie entries — check both before ❓.
- Compilation posts ("every episode ever") are packs: decide the
  series once, number files individually, expect gaps.
- A segment file captioned with the PAIR's number by the uploader is
  the single most common cartoon error — duration + title check
  catches it.

## Verification

- For ✅ items: the episode title in the provider's list matches the
  file's title/segment, not just the number.
