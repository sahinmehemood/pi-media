---
name: anime-donghua-numbering
description: Decide anime and donghua numbering from provider proof.
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [numbering, anime, donghua, providers]
    related_skills: [provider-research, episode-verification, series-records, caption-writing]
---

# Anime and Donghua Numbering

This skill is about how a numbering decision is reached: which
provider's order governs a series, what token each file receives,
and what proof supports it. It defines no caption syntax. Output
shapes live only in `caption-writing`; this skill hands that skill
a decided numbering and expects the grammar to be applied there.

A numbering decision is made once per series, proved at episode
level, and stored once in the series record. It is never re-derived
per file and never computed by arithmetic.

## When to Use

Use when an anime or donghua group needs its numbering decided:

- A new anime or donghua stem group has no series record yet.
- A record exists but is marked conflicted, or new files contradict
  its stored numbering.
- A series spans cours, split arcs, specials, or related entries.
- Providers disagree on season or episode placement.
- A long-running series needs its filing order confirmed.

Don't use for:

- Western TV, cartoons, movies, or drama numbering. Those follow
  their own authority rules in `provider-research` and do not use
  this skill's cour and arc reasoning.
- Caption formatting. Once numbering is decided, `caption-writing`
  owns how the token is written.
- Single-file episode proof on an already-decided series. That is
  a lookup under `episode-verification`, not a new decision.
- Guessing an offset to make a batch fit. An unproved offset is an
  escalation, not a decision.

## Prerequisites

- Recognition is complete per `recognition-and-context`: media
  type, name form, file form, and the whole stem group are held.
- The channel is known. The channel is ground truth for media
  type: a file in the Donghua channel is donghua whatever any
  provider says, and the gateway is always queried with the
  channel-derived type.
- `record-get` has been checked. A verified stored decision
  governs and this skill is not re-run for that series.
- Gateway access works: `lookup`, `series`, and `crossid`.

## Procedure

### Step 1 — Fix the identity frame first

Run `lookup <name> [--type]` with the channel-derived media
type and use `crossid <provider:id>` to assemble the full ID
set for the work. Keep related entries distinct while
assembling: clustering by shared IDs joins records as related;
it does not merge them into one series.

Note the cataloguing models before comparing any numbers:

- AniList commonly catalogues entry-per-cour: each cour or split
  part may be its own entry with its own episode count starting
  from its own first episode.
- TVDB and TMDB commonly catalogue one-series: cours and parts sit
  inside one series record as seasons or episode ranges.

Neither model is wrong. A mismatch between an entry-per-cour count
and a one-series count is expected and proves nothing by itself.

### Step 2 — Fetch every order, side by side

Run `series <provider:id>` for the governing providers and
hold all structures the gateway returns: aired order, absolute
order, and Season 0,
with episode titles and air dates. When two providers both hold
the series, keep both lists side by side; comparing them is the
verification, so never collapse them into one list early.

Authority defaults, subject to per-show proof:

- Anime series: AniList owns identity; numbering is read from
  TVDB aired and absolute orders, corrected where the mapping
  sources the gateway supplies show a correction.
- Long-running anime: AniList owns identity; TVDB absolute order
  only. Season and episode filing is not used for these series.
- Anime movie or OVA entry: the entry itself is the unit; there
  is no episode numbering to decide beyond its own placement.
- Donghua: AniList and TMDB jointly own identity; numbering is
  decided per show from the fetched structures and stored.

### Step 3 — Look up conversions; never compute them

Convert between absolute and season/episode positions only by
reading the fetched absolute-order episode list: find the episode
by its title or air date and read off its position in the other
order. The same rule holds in reverse.

A season the providers do not recognise as a season — common
for donghua and long anime, where a release season may have no
matching season in any provider structure — is resolved the
same way, through the providers' absolute-numbering crosswalk:
TMDB episode groups, TVDB absolute order, and the AniZip
mappings the gateway supplies. Find where that season starts
in the series' absolute order — the absolute position of its
first episode, proved by title or air date — and map its
episodes from that anchor by lookup, episode by episode. The
resolution is decided once per series and stored in the series
record (Step 8); later files of that series reuse the stored
decision instead of re-deriving it.

Scope: this absolute resolution is the default for donghua
only. For anime it applies only when the file's caption
already carries absolute numbering — those numbers are used
as-is, with no conversion — or when Yan has explicitly
directed absolute handling for that series, recorded in the
series record. Otherwise anime keeps standard season/episode
handling and no conversion is looked up. TV and movies never
use this machinery.

Never sum season lengths. Never add cour lengths to reach an
absolute number. Never derive a Season 0 position from a season's
episode count. Summed arithmetic assumes the provider's seasons
are contiguous and complete; the fetched list is the only place
that assumption is checked, so use the list.

Arithmetic conversion is forbidden in both directions, without
exception: never compute an absolute position, or a
season/episode position, by summing season lengths or episode
counts. Providers split seasons differently and count specials
differently, so a summed total can silently land on the wrong
episode; only a provider lookup is evidence.

### Step 4 — Handle split arcs and related entries

When part of a series is catalogued as its own entry — a special
arc, a sequel cour, a side story — treat it as its own decision:

- Give the arc its own series record, keyed by its own canonical
  entry, and link it to the main series record as a related entry.
- One provider ID may legitimately span the main series and the
  arc. Shared IDs are a relationship, not a reason to merge the
  records or force one numbering onto both.
- Decide the arc's numbering from the provider structure that
  actually carries the arc's episodes, which may be a different
  provider or a different order than the main series uses.
- Verify each arc file individually against episode titles or air
  dates in that structure. The relationship to the main series
  exempts no file from proof.

### Step 5 — Prove any offset before using it

An offset — a constant shift between release numbering and filed
numbering — is allowed only when episode-level proof establishes
it: release episode titles or air dates match provider episodes at
the shifted positions, at more than one point in the range.

- A matching episode count is not proof of an offset.
- A single matching point suggests an offset; it does not store
  one. Check the start, middle, and end of the range.
- If titles and air dates point to different shifts, there is no
  offset decision yet; go to Step 7.
- Store the proved offset in the series record with its source,
  order, and the proof basis, per `series-records`.

Illustrative shape only: if invented Release Episodes 1 to 4 of a
special arc carry titles matching a provider's Season 0 Episodes
3 to 6 in order, with air dates agreeing, the offset is proved for
that arc. The numbers here are placeholders, not a real mapping.

### Step 6 — Place specials deliberately

Specials, OVAs, and bonus episodes are placed from the fetched
Season 0 or entry structure, never appended to a season by count:

Scope: this placement research applies to donghua series
only. Anime, TV, and movie specials keep the providers'
standard Season 0 treatment — no positional mapping and no
placement override — even if placement data is somehow
present in a record.

- Confirm the special exists in the provider structure by title
  or air date before assigning any specials position.
- A special known only by an arc name, a short name, or an
  abbreviation is placed by provider lookup, not treated as
  unknowable: establish where it sits in the series order —
  after which season or episodes — from provider evidence,
  matching its episodes by title or air date in the TMDB, TVDB,
  or Kitsu structures. Such a special is never parked for the
  reason that only a name is known, and its placement is never
  guessed; if provider evidence cannot settle the placement, it
  follows Step 7 like any other unproved placement.
- A special with its own AniList or MAL entry is filed as that
  entry's unit, not forced into the main series' Season 0.
- A special found in no provider structure follows Step 7: web
  lead, provider confirmation, or escalation. It is never filed
  by guessing the next free number.

### Step 7 — Resolve disagreement in the fixed order

When providers disagree, or the fetched structures cannot settle
placement, proceed in exactly this order:

1. Re-check episode-level proof: titles and air dates from the
   already-fetched lists, per `episode-verification`. Most
   disagreements are count disagreements, and counts settle
   nothing; titles and dates settle most real disputes.
2. If proof still does not settle it, use web research as a lead
   only, per `provider-research`: find the claimed mapping or
   entry, then confirm it against a provider record reachable
   through the gateway — an explicit ID, or an episode list
   carrying the title or air date.
3. If the lead cannot be confirmed through the gateway, escalate
   to Yan with the conflict stated plainly. Never fix on web
   evidence alone, and never store a decision built on a lead.

### Step 8 — Store the decision once

Store one decision per series or arc in the series record:
canonical key, full ID set, titles, media type, and the numbering
block — source provider, order (aired, absolute, or specials),
offset if proved, and a note of the proof basis. Mark the record
verified only when Step 3 to Step 6 proof supports it; otherwise
mark it conflicted and escalate. After storage, per-file work is
proof lookup plus formatting; the decision is not reopened per
file unless a file contradicts it, which is itself an escalation.

## Pitfalls

- Summing season or cour lengths to compute an absolute number.
  This is the single most damaging shortcut in this skill's area.
- Treating an AniList entry-per-cour count and a TVDB one-series
  count as a contradiction and picking the larger one.
- Merging a split arc into the main record because they share a
  provider ID, then numbering arc files inside the main seasons.
- Storing an offset proved at one episode and applying it across
  a range whose far end was never checked.
- Filing long-running anime by season and episode because the
  release names carry season tokens. The decision for these series
  is absolute only; release tokens do not override it.
- Numbering a special as the next episode of the current season
  when no Season 0 or entry proof was fetched.
- Treating a special known only by an arc name, a short name, or
  an abbreviation as unknowable and parking it for that reason,
  instead of placing it by provider lookup.
- Resolving a season no provider recognises by adding up earlier
  season lengths, instead of reading where it starts in the
  absolute-numbering crosswalk.
- Accepting a web mapping as the decision. Web supplies leads;
  only gateway-confirmed records supply decisions.
- Re-deciding numbering mid-batch. One series, one decision, one
  format; a contradiction stops the batch, it does not fork it.

## Verification

- The series record holds the decision: source, order, offset if
  any, and proof basis, stored before the first fix in the batch.
- Every conversion used can be pointed to in a fetched episode
  list; no number in the batch was produced by summing lengths.
- Split arcs have their own records, linked as related entries,
  with their own proved numbering.
- Long-running series in the batch use absolute numbering only.
- Every special in the batch matches a fetched structure by title
  or air date, or was escalated.
- Every special known only by an arc name, short name, or
  abbreviation has a provider-evidenced placement in the series
  order, or was escalated — none was parked as unknowable and
  none was placed by guessing.
- Any season no provider recognises as a season was resolved
  through the absolute-numbering crosswalk, and that resolution
  is the one stored in the series record.
- Any disagreement followed the Step 7 order, and any decision
  resting on a web lead names its gateway confirmation.
- Unproved numbering was escalated, not applied.
