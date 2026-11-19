---
name: specials-and-arcs
description: Specials, OVAs, arcs — never file them as seasons
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [librarian, media, specials, identification]
    category: media
    requires_toolsets: [terminal, web]
---

# Specials and arcs — the highest-risk identification there is

## When to Use

Whenever a file is marked or shaped like a special: `SP`, `Special`,
`OVA`, `OAD`, `ONA special`, a named arc, a decimal/bonus episode,
a picture drama, a recap, a prologue/epilogue, or a season-shaped
label on content that aired outside the season's run. Open this
skill BEFORE proposing anything for such a file — including when it
arrives inside an otherwise clean episode run.

## The one rule

**A special is never a regular season.** The classic disaster: an
arc labelled `SP3` (the third special arc) read as `S03` (season 3).
Both are "3". Only one is true, and the wrong one indexes cleanly —
the gate's matcher accepts it, the file lands in the wrong season,
and the error is invisible until a human notices. The server's
specials gate blocks SP→season plans mechanically, but it can only
see the marker; unmarked specials are your responsibility.

## How specials actually work

- Providers file specials under **Season 0**, but TMDB and TVDB
  frequently **disagree on the ordering and grouping** inside
  Season 0. A plan for a special must name WHICH provider's specials
  list it follows and the special's title there — "S00E05" alone is
  not an identification.
- A named arc released as a special often has its OWN episode count
  (e.g. a 13-episode special arc next to 12-episode seasons). Count
  the files in the run: if the count matches the special's listing
  and not any season's, that is strong evidence.
- Air dates separate lookalikes: specials air between seasons, in
  their own blocks. A file whose arc aired a year after the season
  it is numbered near is a special until proven otherwise.
- Donghua and anime add shapes: recap specials, "SP" episode bundles
  between cours, movie-length specials, and picture dramas
  (decimal-numbered audio/bonus shorts bundled with disc releases —
  these live in provider specials lists under their own titles, or
  nowhere: default ❓).

## Procedure

1. **Isolate.** Pull the special-marked files out of any episode run
   (`packet-triage`). They are planned separately, always.
2. **Find the specials list.** On the show's provider page (TMDB and
   TVDB both), open Season 0 / Specials. Identify the arc by title,
   episode count, and air dates — all three should agree.
3. **Cross-check a second source** (the other provider, or Bangumi
   for donghua, or AniList/Kitsu for anime). If the two providers
   order Season 0 differently, follow the one the plan's ID points
   to, and say so in the note.
4. **Build the plan** with `S00Exx` numbering per that provider's
   specials list, the special's title in the caption where the
   contract requires it, and the note naming: provider, special arc
   title, its episode count, its air window.
5. **If the specials list does not settle it → ❓.** Specials are
   where guessing does the most damage; a flagged special costs
   nothing, a misfiled one poisons a season.

## Decision table

| Evidence | Verdict |
|---|---|
| SP marker + specials list match (title, count, dates) | ✅ plan as S00 per that provider |
| SP marker, no specials-list entry anywhere | ❓ flag with what you found |
| No marker, but count/dates fit a special arc, not the season | Treat as special; research as above |
| Marker says SP but content is a regular episode (mislabelled upload) | ❓ — never override a marker on your own; Yan decides |
| Two providers order Season 0 differently | Follow the plan-ID provider; note the disagreement |

## Pitfalls

- **"3YA", "Part 3", "Vol. 3"** and similar are arc labels, not
  season numbers, until a provider's season list proves otherwise.
- **Season-shaped numbering on a special** (a special arc whose
  episodes are numbered like a season by the uploader) is the trap
  in its purest form — check air dates.
- **Recap/compilation movies** of a season are movies or specials,
  never episodes of that season.
- Never let a run's momentum carry a special along: "the other 12
  were S03" is not evidence about the 13th file.

## Verification

- The plan note names provider + special title + count + air window.
- Removing the SP marker from the file name would NOT change your
  identification — it rests on the specials list, not the marker.
