---
name: donghua-naming
description: Donghua — pinyin titles, named arcs, nian fan, Bangumi-first
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [donghua, bangumi, pinyin, arcs, specials]
    category: media
---

# Donghua naming

## When to Use

Runs from the Donghua channel and Chinese animation anywhere else.

## Title forms — index all four

Every donghua has up to four names: **hanzi**, **spaced pinyin**,
**unspaced pinyin**, and **English** — and the English side often
has two or more competing translations. Uploaders mix all four,
plus initialisms built from the pinyin. Search providers with at
least two forms before concluding no-match.

## Structure: arcs, not seasons

- A donghua "season" is usually a **named story arc** (篇). Providers
  slice arcs differently: TMDB may collapse arcs into "Season N",
  TVDB may lack the show entirely, AniList/MAL cover popular titles
  per-arc as separate entries.
- **Specials (特别篇) are often MAIN STORY**, released between
  seasons, and may be numbered as their own series (SP1, SP2, SP3…)
  with many episodes each. They are NOT automatically season 0
  fillers — but on TVDB they live in season 0 ("Specials").
- **Nian fan (年番)** = a year-round arc of ~52 episodes with
  CONTINUOUS numbering across "parts". Platforms dual-number (their
  playlist EP29 may be overall EP59). Captions for nian fan carry
  BOTH numbers: `SxxExx` per the provider + overall in the note/first
  line when they differ.
- **Bangumi (bgm.tv) is the ground truth** for donghua structure:
  arcs, specials, episode counts, air dates. Identify structure
  there FIRST, then project to the TMDB/TVDB/Kitsu ID the app stamps.

## Special arcs — the standing trap

Uploader form `XX <arc-tag> - SP3 E01` means: the THIRD special arc,
episode 1 — a multi-episode special structure with its OWN count,
not "Season 3 episode 1". Special arcs routinely run 3–13 episodes
of main story. The mapping is always: verify the arc on Bangumi,
find it in the chosen provider's specials list, file as season 0
with the arc named. There is no series-specific table to consult —
every series' structure is looked up fresh, every time. (One such
arc was once misfiled as a regular season in this very library; the
gate now refuses that mapping mechanically.)

## Procedure

1. Expand the title: hanzi ↔ pinyin ↔ English ↔ initialism. The
   packet's taught aliases and this skill's references first;
   Bangumi search second.
2. Fix the STRUCTURE on Bangumi: which arc/season, how many
   episodes, is it a special arc, what are its dates.
3. Project to a stampable ID (TMDB preferred for donghua series in
   this app, TVDB when it carries the show, Kitsu for ONA-style
   entries). Verify the episode exists in THAT provider's list.
4. Fix captions carry: English title (year of the SERIES), SxxExx
   per the chosen provider, the arc name for arc releases, and both
   numbers when continuous numbering differs.
5. SP-marked files: follow the specials rule (provider-research) —
   provider's specials list + special title in the note, or ❓.

## Pitfalls

- Initialisms collide (many shows share 3-4 letter forms) — expand
  via channel context + episode counts, never by initialism alone.
- "Season 5" of a donghua on TMDB may be the nian fan arc with
  continuous numbering starting at 53+, not 1.
- Remake arcs (a later arc retelling an early season under its own
  name) are separate entries — do not file remake episodes under
  the original season.
- Chinese uploader tags (首发, 完结, 合集 = complete collection)
  are provenance/completion markers, not title parts.

## Verification

- ✅ requires: Bangumi structure noted + the stamped provider's
  episode list contains the claimed episode. One of two missing → ❓.
