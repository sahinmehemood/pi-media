---
name: caption-writing
description: Write corrected captions in the app's exact grammar.
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [captions, grammar, formatting, librarian]
    related_skills: [recognition-and-context, anime-donghua-numbering, episode-verification, librarian-toolbox]
---

# Caption Writing

This skill is the sole home of caption syntax. Every corrected
caption is written from this skill, every time — never from memory
of a grammar, never by copying another skill. Other skills decide
identity, numbering, and proof; this skill alone defines how the
decided facts are written.

Examples use invented titles and placeholder numbers only. No
real series name, offset, or numbering decision ever appears in
this skill; those facts live only in series records.

## When to Use

Use when a corrected caption is being written or rewritten:

- The series decision is stored and episode proof is held for the
  file; a complete new caption must now be formed.
- A group format is being decided once, with the series decision.
- An original release name must be parsed before it is rewritten.
- A draft caption needs checking against the parser constraints.

Don't use for:

- Deciding identity or numbering. Decide first, per
  `provider-research`, `episode-verification`, and
  `anime-donghua-numbering`; this skill formats decisions only.
- Files with no episode proof. No proof, no caption, no fix.
- Multipart releases. They are parked or escalated, never given a
  corrected caption that pretends they are single files.
- Subtitle files. They are never caption-edited.

## Prerequisites

- Recognition and context are complete per
  `recognition-and-context`: media type, name form, file form,
  group, channel, and neighbouring indexed entries are held.
- The series record holds the decision: provider IDs, title form,
  numbering source and order, and any proved offset.
- Episode proof for this file is held: title or air date matched.
- The original caption exactly as last observed is held; it is
  both the parse source and the drift-check reference.

## Procedure

### Step 1 — Apply the universal skeleton

Every corrected caption has exactly two parts, in this order:

1. One filename-shaped line: title, year where the grammar for
   the media type requires it, the explicit numbering token where
   the grammar requires it, then the normalised preserved tokens:
   resolution, source, codec, audio, language or subtitle tags,
   edition, release group — and the file extension last.
2. One ID tag line after the filename line: the provider-ID link
   in the app's existing link format — an HTML anchor on its own
   line, exactly `<a href="https://www.imdb.com/title/tt0000000/">IMDB</a>`
   for an IMDb ID, or `<a href="https://www.themoviedb.org/tv/00000">TMDB</a>`
   for a TMDB TV ID (`/movie/` in place of `/tv/` for a movie).
   Nothing else appears on that line. (IDs shown are invented.)

Two hard parser constraints shape the skeleton:

- The extension ends the filename line. The app's finalising step
  truncates everything after the first video extension, so every
  meaningful token precedes the extension and nothing meaningful
  is ever placed after it.
- Titles are romanised. The app's filename cleaning erases every
  non-ASCII character before parsing, so a non-ASCII title would
  be destroyed; the readable romanised form is always written.

### Step 2 — Apply the tagging rules (the only tags that exist)

Adding tags means exactly two things, because the app reads
exactly two things. Invent no other tag system.

1. The provider-ID link stamp on the tag line: IMDb preferred,
   else TMDB; positive IDs only. A corrected caption without it
   is incomplete and must not be submitted: the link is mandatory.
   Later edits read it to identify the post, and it makes
   re-indexing deterministic. Never write a negative ID, and
   never write an ID the decision did not prove.
2. The normalised token set inside the filename line: resolution,
   source, codec, audio, language and subtitle tags, edition
   tokens, and release group — normalised in spelling and position
   per this skill, preserved in substance from the release.

### Step 3 — Format for the parser and for the display

- The resolution token is mandatory and truthful. Without one the
  app rejects the file outright; with one it becomes the stored
  quality and the displayed resolution. Never invent a resolution
  and never upgrade one the release does not carry.
- Numbering is always explicit. Never rely on the parser inferring
  numbering from bare numbers, outside the anime absolute case it
  was built for — and even there, the decided token is written
  explicitly.
- A split part's caption keeps its part suffix after the
  extension. The suffix is the grouping signal at re-index; the
  stored name strips it and the group persists. Dropping it
  ungroups the parts.
- Preserve meaningful original tokens: quality, edition, language,
  group. Drop noise: uploader promotions, handles, URLs, emoji.
  Noise that survives pollutes the stored name.
- The display matters too: the catalogue name comes from the
  provider document, but the stream label is re-parsed from the
  stored filename at request time. Dropping codec, audio, or
  group tokens degrades every stream label even when filing is
  correct.

### Step 4 — Apply the grammar for the media type

Title form, year placement, and numbering token are per type.
The series decision says which provider's title and which order;
this step says how they are written.

#### Movie

- Title: the identity provider's canonical romanised title.
- Year: immediately after the title, always present; it is the
  primary disambiguator between same-title works.
- Numbering: none. No season or episode token is ever written.
- Preserved: resolution, source, codec, audio, language tags,
  edition tokens such as extended or remastered markers, group.
- Shape, invented example (both lines, in full):
  `Invented Harbour Tale 2021 1080p WEB x264 AAC Dual.mkv`
  `<a href="https://www.imdb.com/title/tt0000000/">IMDB</a>`

#### Western TV

- Title: the identity provider's series title, romanised.
- Year: after the title when needed to disambiguate the series
  (remake or same-title collision); otherwise omitted so the
  group's established format stays uniform.
- Numbering: season-episode token in SxxExx form, from the
  governing aired order. Absolute tokens are not used.
- Specials: S00Exx form, only where the decision placed the file
  in Season 0 from provider proof.
- Preserved: resolution, source, codec, audio, language tags,
  edition, group.
- Shape, invented example: `Invented Valley Clinic S02E05 1080p
  WEB x265 HEVC.mkv` followed by the ID tag line.

#### Anime

- Title: the AniList romanised title form held in the record.
- Year: generally omitted; season and cour identity is carried by
  the series record and related entries, not by a year token.
  Include a year only when the group's established format does.
- Numbering: SxxExx form where the decision uses aired order;
  absolute token where the decision says absolute — always for
  long-running series, whose filing is absolute only. Write the
  token the decision specifies; never convert forms at this step.
- Specials: S00Exx form for provider-proved specials, or the
  special's own entry treatment where the decision filed it as
  its own entry.
- Preserved: resolution, source, codec, audio, subtitle-language
  tags exactly as meaningful, fansub or release group.
- Shape, invented example: `Invented Blade Academy - 014 1080p
  WEB x264 AAC [ExampleGroup].mkv` followed by the ID tag line,
  for an absolute-decided series.

#### Donghua

- Title: the romanised (pinyin-shaped) title form held in the
  record; never a non-ASCII title, which cleaning would erase.
- Year: omitted unless the group's established format carries it
  or a same-title collision requires disambiguation.
- Numbering: exactly the token the per-show decision specifies —
  SxxExx or absolute or S00Exx for specials — because donghua
  numbering is decided per show and stored, never assumed from
  another show's convention.
- Preserved: resolution, source, codec, audio, subtitle-language
  tags (donghua releases commonly carry meaningful subtitle
  language markers — preserve them exactly), edition, group.
- Shape, invented example: `Invented Cloud Sect S01E07 1080p WEB
  x264 AAC EN SUB.mkv` followed by the ID tag line.

#### Cartoon

- Title: the identity provider's series title, romanised.
- Year: after the title when needed for disambiguation, matching
  the group's established format otherwise.
- Numbering: SxxExx form from aired order, or absolute token
  where the decision specifies absolute order. Cartoons are the
  type where both orders legitimately occur; the decision, not
  habit, picks the token.
- Specials: S00Exx form, provider-proved only.
- Preserved: resolution, source, codec, audio, language tags,
  edition, group.
- Shape, invented example: `Invented Harbour Cats S00E02 720p
  WEB x264 AAC.mkv` followed by the ID tag line, for a proved
  special.

#### Drama

- Title: the identity provider's romanised title form.
- Year: after the title by default for this type; drama titles
  collide across years and remakes more often than any other
  type in the library, and the year is the standing disambiguator.
- Numbering: SxxExx form, always — including single-season runs
  (S01Exx). Bare episode numbers are never written; the parser
  does not infer numbering from them outside the anime-absolute
  case.
- Preserved: resolution, source, codec, audio, subtitle-language
  tags exactly (drama releases are subtitle-led; the language tag
  is often the token users choose by), edition, group.
- Shape, invented example: `Invented Paper Moon 2020 S01E11
  1080p WEB x264 AAC KO SUB.mkv` followed by the ID tag line.

#### Combined-episode packs (any series type)

A pack is one file carrying several consecutive episodes of one
season. It is fixable, and its numbering token is a **range**:

- Token form: the season-episode token with an episode range —
  `S01E01-E05`: the full `SxxExx` token for the first episode,
  then `-E` and the last episode's number. Both endpoints are
  written; a range is never shortened to its first episode.
- The range covers exactly the episodes the proof established:
  each endpoint episode proved per `episode-verification`, and
  the range spans only the consecutive episodes between them.
  Never widen or narrow a range to match a release's claim.
- Title, year, preserved tokens, and the ID tag line follow the
  media type's grammar above, unchanged.
- The app files the pack as a synthetic combined entry; the
  caption's job is to state the proved range exactly.
- Shape, invented example: `Invented Valley Clinic S02E03-E06
  1080p WEB x265 HEVC.mkv` followed by the ID tag line.

### Step 5 — Parse the original release name before rewriting

Read the original caption token by token and sort every token
into keep, normalise, or drop before writing anything:

- Scene and release groups: bracketed or trailing group names.
  Keep, normalised to one position at the end of the token run.
- Quality tokens: resolution, source, codec, audio format and
  channels. Keep in substance; normalise spelling and order to
  the skeleton order.
- Edition tokens: extended, uncut, remastered shapes. Keep
  exactly where meaningful; an edition token changes what the
  file is, so dropping one misdescribes the file.
- Subtitle and language tokens: keep exactly where meaningful.
  Normalise position, never the language itself.
- Uploader short forms and acronym titles: the title portion may
  be an initials form or a shortened name. Do not carry the short
  form into the corrected caption; write the decided full title.
  The short form's only job was to be researched.
- Pinyin shapes: romanised Chinese titles often arrive as spaced
  syllables, joined words, or initials. Recognise the shape, map
  it to the record's title form, and write that form uniformly.
- Torrent-style versus app-style: torrent style is dotted and
  token-dense with no spaces; app style is the filename-shaped
  line plus ID tag line. Parse torrent style by token meaning,
  not by position alone, then write app style. Never submit a
  caption still in torrent style.
- Noise: promotions, handles, URLs, emoji, site names. Drop all.

### Step 6 — Enforce group and library consistency

- One format per group and series, decided once with the series
  decision. A batch never mixes styles, token orders, or title
  forms inside one series. Smart formatting means consistent and
  parser-shaped, not decorative: same title capitalisation as
  the record, same token order, same group placement, every file.
- When the series already has indexed entries, their established
  format is the default for new fixes. Match it even where this
  skill would phrase a fresh series slightly differently.
- If the established library format is itself wrong, stop: that
  is an escalation and a report to Yan, never a silent divergence
  inside one series.

### Step 7 — Final check before submitting

Re-read the finished caption against the original and the
decision: title and year per grammar, token per decision,
preserved tokens all present, noise all gone, extension last,
split suffix kept if present, ID tag line present with the
proved positive ID. Only then hand it to the toolbox via `fix`
or `fix-batch`, with the exact observed caption as the expected
caption.

## Pitfalls

- Submitting a corrected caption with no ID tag line. It files
  unreliably and leaves later edits with no ID to key on.
- Placing any token after the extension, where finalising
  truncates it away.
- Inventing or upgrading a resolution to satisfy the mandatory
  token rule. The rule requires a truthful token from the
  release; a file with none provable is escalated, not upgraded.
- Carrying an acronym or uploader short form into the corrected
  title because it was recognised. Recognition is not the title.
- Dropping subtitle-language or edition tokens as if they were
  noise. They are preserved substance; promotions are noise.
- Mixing SxxExx and absolute tokens inside one group, or changing
  title form mid-series.
- Writing a real series' facts into an example or note here.
  Examples stay invented; facts stay in series records.
- Copying grammar fragments into another skill or note and then
  writing from the copy. This skill is the only source; copies
  drift.

## Verification

- The caption has exactly two parts: filename line with extension
  last, then the ID tag line with a proved positive ID (IMDb
  preferred, else TMDB).
- Title form, year placement, and numbering token match the
  media-type grammar and the stored series decision.
- Every meaningful original token — quality, edition, language,
  group — is present, normalised in position, unchanged in
  substance; all noise tokens are gone.
- Split parts keep their suffix; multipart files were not given
  captions at all.
- Every caption in the group uses the identical format, matching
  the library's established format for the series where one
  exists.
- No caption was written for a file lacking episode proof.
