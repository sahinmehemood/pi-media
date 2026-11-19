---
name: release-naming
description: Torrent & scene release-name grammar — tokens, quality, groups
version: 2.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [torrent, scene, naming, parsing]
    category: media
    requires_toolsets: [terminal]
---

# Release naming — torrent & scene grammar

## When to Use

When reading any release-style file name (torrent rips, scene
releases, P2P encodes) in a packet, before applying a type skill.

## The grammar

A scene/torrent name is a token stream. Read it in this order:

`[Group] Title (Year) SxxExx / absolute — Episode Title — Source
Resolution Codec Audio — Group / tags [CRC]`

- **Numbering tokens**: `S01E05` (zero-padded, universal) ·
  `S01E01-E02` multi-episode · `1x05` legacy form · bare absolute
  numbers (anime, see anime-naming) · date form `2024.01.15` (daily
  shows, see tv-naming).
- **Quality tokens** (a fix caption MUST end up with a real one):
  resolution `2160p/1080p/720p/480p` (`4K` = 2160p) · source
  `WEB-DL/WEBRip/BluRay/BDRip/HDTV/DVDRip/Remux` · codec
  `x264/x265/HEVC/AV1` · HDR `HDR/DV/HDR10+` · audio `AAC/FLAC/DDP5.1`.
  A name with NO quality token fails the app's quality gate — derive
  the resolution from the file's real height instead.
- **Edition tokens** (non-recoverable info — preserve in the caption
  or the note): `Director's Cut, Extended, Unrated, Theatrical,
  IMAX, Remastered, Criterion, Open Matte, PROPER, REPACK`.
  PROPER/REPACK = a corrected release of the same item, superseding
  the earlier one.
- **Version tags**: `v2` attached to an episode = corrected re-release
  of the SAME episode (supersedes v1). Not a different episode.
- **Group tags**: `[Group]` at the front (fansub/P2P style) or
  `-GROUP` at the end (scene style). The group is provenance, never
  part of the title.
- **CRC** `[A1B2C3D4]`: 8 hex digits, a corruption checksum only —
  ignore for identification.
- **Language/sub tags**: `[E-SUB]`, `MultiSub`, `Dubbed`, `RAW`,
  language codes — provenance, not title.
- **Junk to strip when reading**: URLs, `www.`, channel ads,
  `@handles`, "HD" alone, file sizes, `Part` of split archives.

## Reading procedure

1. Strip group, CRC, URLs, ads. What remains is title + numbering +
   quality + edition.
2. Extract the numbering token FIRST — it decides which type skill's
   numbering rules apply.
3. Extract year if present; no year in a movie name means you must
   research it (movie-naming).
4. Note edition/version tokens — they change identity (a Director's
   Cut is a different TMDB edition entry in practice) or supersede.
5. What you cannot parse is evidence too: an unparseable name usually
   means an uploader short form (telegram-uploaders) or a bare-number
   anime release (anime-naming).

## Pitfalls

- `S1E5` unpadded and `105`-style compact numbers parse
  inconsistently — rewrite to zero-padded SxxExx in fix captions.
- A trailing number is not always an episode: sequel numbers
  ("Movie 2"), years (2019), and resolutions (1080) all sit at the
  end of names. Position + the type skill decide.
- `Complete`, `Batch`, `Season Pack` markers mean the post may hold
  MANY episodes — check the file itself, not the pack's label.
- Never carry a release group's name, a CRC, or a source-site tag
  into a fix caption.

## Verification

- You can point at every token of the original name and say what it
  is. Any token you cannot classify gets mentioned in the note.
