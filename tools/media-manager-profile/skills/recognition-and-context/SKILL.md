---
name: recognition-and-context
description: Classify files and assemble context before research.
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [recognition, context, pipeline, librarian]
    related_skills: [provider-research, episode-verification, caption-writing, librarian-toolbox]
---

# Recognition and Context

This skill implements Stages 1 and 2 of the work pipeline in
DESIGN section 10.2: understand the file and assemble its context
before any provider is called. The DESIGN section 10 tree does not
list a separate skill for this stage; this skill exists as the
section 10.2 implementation skill so the understanding layer is
never skipped, merged into research, or left to habit.

No fix starts with research. A file is first classified, its group
and neighbourhood are read, and only then is a research question
formed. Researching before understanding produces the wrong
question efficiently.

## When to Use

Use at the start of every work session and for every new stem
group, before `lookup`, `series`, or `crossid`:

- A group is taken from `waiting` and has not been classified yet.
- A single file looks anomalous inside an otherwise known group.
- A name is abbreviated, token-dense, or in an unfamiliar shape.
- A file form is unclear: split, multipart, pack, archive, subtitle.
- Neighbouring indexed entries may already establish the format.

Don't use for:

- Applying a fix. Application is mechanical once the decision and
  format exist; use `librarian-toolbox` for the apply step.
- Caption syntax. Output shapes live only in `caption-writing`;
  this skill classifies inputs, it does not define outputs.
- Numbering decisions. This skill gathers the evidence a numbering
  decision needs; `anime-donghua-numbering` reaches the decision.
- Re-researching a series with a verified record and exact alias.
  A stored decision governs; classification then only confirms the
  file belongs to that group.

## Prerequisites

- `status` and `mode` have been checked for this session.
- The scope Yan named is known; classification stays inside it.
- Toolbox access works: `waiting`, `file`, `find`, `record-get`,
  and `aliases` are available.
- The channel for the group is known. The channel is ground truth
  for media type and is never overridden by a provider result.
  Its type is inferred from the channel's NAME (a channel named
  "Donghua Dumping" holds donghua); when a name is unclear,
  research the channel — never guess a type for it.

## Procedure

### Step 1 — Classify media type

Assign exactly one media type using three signals together: the
channel (ground truth), the numbering shape, and the name.

- Movie: single work, no episode token, no season structure.
- TV series: season and episode structure, non-animated origin.
- Anime: Japanese animation; season, cour, or absolute shapes.
- Donghua: Chinese animation; the Donghua channel decides this
  even if a provider catalogues the work as generic animation.
- Cartoon: non-Japanese, non-Chinese animation with series shape.
- Drama: live-action serial drama shape distinct from Western TV
  conventions in the channel where it appears.
- OVA / special: recognised by tokens such as SP or OVA markers,
  by special markers in the name, and by series context.

Specials and OVAs are not recognised by parsing a Season 0 token.
The app normalises Season 0 away, so a Season 0 token in a source
name is a hint for classification only, never a filing mechanism.
If the three signals conflict, stop and escalate; do not average
them and do not let a provider reclassify the file.

### Step 2 — Classify name form

Assign the name form. Every form below occurs and must be read:

- Full clean name: readable title, year or numbering present.
- Short form: acronym, initials-only stem, or uploader short form.
  An initials-only stem is a research question. Answer it from the
  group and the channel first; if needed, the web may expand the
  abbreviation as a lead — the sole pre-gateway web use (DESIGN
  §7), confirmed through the gateway before any use. Never guess
  an expansion from the letters alone.
- Torrent / scene release name: dotted, token-dense, with group,
  source, codec, and quality tokens packed into the stem.
- App indexed style: a filename-shaped line plus an ID tag line.
  A file already carrying an ID link is stating an identity claim.
  Record the claim; verify it later; do not trust it on sight.
- Hybrid / messy: mixed separators, promotions, handles, URLs,
  emoji, or partial tokens from more than one form above.

Read names against the app parser's real behaviour: guessed
season and episode numbers are trustworthy only with explicit
season/episode evidence; non-ASCII characters are erased before
parsing, so a title carried only in non-ASCII script is destroyed
and only its romanised form is readable; URLs, handles, emoji, and
a small codec-tag set are stripped before parsing.

### Step 3 — Classify file form

Assign the file form before any caption work is planned:

- Single: one file, one work or one episode. The normal case.
- Split part: a trailing part suffix after the extension. Parts
  merge under one index entry by group; the suffix is the grouping
  signal and must survive into the corrected caption.
- Glued multipart: glued tokens such as Part1, CD1, or disc
  immediately before the extension. The app rejects these by
  design and can never combine them.
  Recognise, then park or escalate. Never fix by caption.
- Spaced multipart: the spaced form, for example Part 1. It escapes
  the app's detector and merely pollutes the title, but the file
  may still be physically one part of a multipart release. Same
  treatment
  as glued multipart: park or escalate, never caption-fix.
- Combined-episode pack: a range token covering several episodes
  in one file. The app files it as a synthetic combined entry;
  classify it as a pack, not as its first episode. Packs are
  **fixable**: they route through the normal pipeline — research,
  proof of both range endpoints, and a range-token caption per
  `caption-writing` — never park-only routing.
- Archive: only the .zip form exists in this app: a single .zip
  is ingestible live only; split .zip.NNN parts are ingestible
  live and by scans. Other archive forms are dropped before the
  funnel, never reach the pile, and will never be seen here; if a
  name claims one, treat the classification as suspect and verify
  with `file` before proceeding.
- Subtitle: recognised and excluded. Subtitles are never fixable
  targets and never caption-edited, at any stage.
- Non-video / unsupported: out of scope. Do not research it.

### Step 4 — State why it is waiting

Read the caption against the known rejection shapes and name the
reason in plain words before researching:

- No resolution or quality token present.
- Multipart shape.
- Multi-season or multi-episode list in one name.
- A `combined` token with no parseable range.
- No parseable title.
- Provider no-match on the original caption.

Most of the pile explains itself at this stage. The explanation
decides what research is even needed: a missing resolution token
needs a truthful token from the release, not an identity search;
a no-match needs identity research, not reformatting alone.

### Step 5 — Assemble context

Hold all of the following before Stage 3 research begins:

- The whole stem group via
  `waiting [--channel] [--stem] [--grouped]`: siblings reveal
  the season and episode structure, the group's numbering
  convention, and which files are specials. One file alone
  shows none of this.
- The channel and its conventions: media type (inferred from the
  channel's name; research when the name is unclear, never
  guessed), established title language, and how neighbouring
  groups in the channel are filed.
- The series record via `record-get` and the alias via `aliases`,
  if any. A stored decision governs; if one exists and the file
  fits it, research is skipped entirely.
- Already-indexed entries for the same series via `find`: the
  document IDs held, which seasons, episodes, and qualities are
  already filed, and the stored filenames' established format.
- The release's own patterns: group tag and quality, edition, and
  language tokens that the format stage must preserve.
- Yan's current instruction: what he actually asked for in this
  session governs what gets touched and outranks the pile's order.

**Cross-stem groups.** One series can arrive as several stem
groups — a scene-style full title and an initials short form stem
differently, so the group you pulled may be only part of the
series. If two stem groups in one channel resolve — via `find`, a
stored record's title variants, or gateway-confirmed identity —
to the same series record, treat them as **one series** for
research and decision: one record governs both, research is not
repeated for the second stem, and an alias is saved per
(channel, stem) as each stem's first fix confirms. Never research
the short-form group in isolation from the full-name siblings that
answer it.

### Step 6 — Run the pipeline in order

Proceed only in this order, without skipping ahead:

1. Stage 1 — Understand: Steps 1 to 4 above. No provider is
   called in this stage.
2. Stage 2 — Assemble context: Step 5 above, including
   `record-get`. A verified record governs, and Stage 3 is then
   skipped entirely.
3. Stage 3 — Research, once per series only: gateway
   `lookup <name> [--type]`, `series <provider:id>`, and
   `crossid <provider:id>` with the channel-derived media type,
   per `provider-research`. Web research is a fallback only —
   once per series, after the gateway cannot settle the
   question, as a lead that must be confirmed through the
   gateway; never before the gateway (beyond the Stage 1
   initials-only expansion, Step 2) and never as proof. Store
   the one series decision with `record-set` before formatting.
4. Stage 4 — Tag: apply the app's two tag forms only — the
   provider-ID link stamp and the normalised token set — as
   defined in `caption-writing`. No other tag system exists.
5. Stage 5 — Format: prove each file's episode first, per
   `episode-verification`, then format its caption per
   `caption-writing`, using the series decision and the group's
   single format.
6. Stage 6 — Apply via the toolbox: `fix` or `fix-batch`,
   carrying the record's `placement` whenever the correct filing
   differs from what the caption's parse yields (Season 0 above
   all — the parser normalises it away, so the seam, not the
   caption, files the special). For specials and
   absolute-numbered files, read back the filed season and
   episode from the index after apply. On confirmed success
   only, save the alias, per `series-records`.

### Step 7 — Apply the speed rules

- One research and decision pass per series or group. Never one
  pass per file.
- Reuse the series record, the alias, and the provider cache.
  After the first confirmed fix, later groups of the same series
  start at the record with no research at all.
- After the decision, per-file work is mechanical only: episode
  proof lookup into the already-fetched episode list, then format,
  then apply. No per-file re-research, no per-file web search.
- Web research at most once per series, only on gateway failure
  or disagreement.
- Batch coherently: finish one series beginning to end in episode
  order before opening the next group.

## Pitfalls

- Researching the first file seen and forcing siblings to fit it.
  The group is the unit of understanding; the file is the unit of
  application only.
- Expanding an acronym or short form by plausibility. Short forms
  are answered from group, channel, and confirmed leads, or they
  are escalated.
- Treating an existing ID link as verified identity. It is a claim
  from whoever wrote that caption, including the app's matcher.
- Caption-fixing a multipart release. No caption can make the app
  combine physical parts; the attempt wastes budget and misfiles.
- Classifying a spaced multipart name as a clean single file
  because the app's detector missed it. The detector's miss does
  not change the physical release.
- Filing a combined-episode pack under its first episode number.
- Letting a provider's media type override the channel's. Channel
  is ground truth in both directions.
- Researching a subtitle or unsupported file. Excluded means stop
  at classification.
- Holding context in memory only. Decisions that matter are stored
  in the series record; unstored context is lost between sessions.

## Verification

- Every file in the group has a stated media type, name form, and
  file form, and a plain-words reason for waiting.
- Multipart, pack, archive, subtitle, and unsupported files have
  been routed to park, escalate, or exclusion — none is queued for
  a caption fix.
- Context is complete: group, channel, record and alias state,
  indexed neighbours, release tokens, and Yan's scope are all held
  before the first gateway call.
- If a verified record governed, no research calls were made (a
  single `series()` fetch against the record's stored IDs for
  proof structures is not a research call).
- If research was needed, it happened once for the series, and
  per-file steps after it were lookups and formatting only.
- Any ambiguity or conflicting context was escalated with the
  conflict stated, not resolved by guessing.
