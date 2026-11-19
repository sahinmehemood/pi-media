---
name: quality-and-editions
description: Quality tokens, editions, multipart and no-caption files
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [librarian, media, quality, captions]
    category: media
    requires_toolsets: [terminal]
---

# Quality and editions — the caption's physical facts

## When to Use

When a plan's caption needs its quality token, when files are
multipart or split, when an item was rejected for no-quality, and
when a file has no caption at all.

## The quality rule

The app rejects any fix caption without a quality token — and the
token must be TRUE. A quality token is earned from exactly two
sources, in this order:

1. **The file's real dimensions** (in the packet, when known):
   height ≥ 2000 → `2160p`, ~1080 → `1080p`, ~720 → `720p`,
   ~480 → `480p`. Width/height both count; use the height.
2. **A quality token already in the file's own name/caption**
   (720p, 1080p, HD, FHD, CAM…). Carry it over as-is.

**Never** infer quality from file size, bitrate guesses, the
uploader's reputation, or what "this group usually posts". If
neither source gives a token, the item is ❓ no-quality — that is
why the no-quality bucket exists. (39 files sit in it; they wait
for evidence, not invention.)

## Editions

- Edition words in a name — `Extended`, `Director's Cut`,
  `Remastered`, `Uncut`, `IMAX`, `Criterion` — are part of the
  work's identity for movies: keep the edition in the caption's
  first line after the year when the provider lists that edition,
  and prefer the provider entry for that edition when one exists.
- `WEB-DL`, `WEBRip`, `BluRay`, `HDTV`, `REMUX` are source tags,
  not quality and not edition. They may stay in the preserved lines
  of the caption; they never substitute for the quality token.
- Dub/sub markers (`Dual Audio`, `Multi-Audio`, language tags)
  describe this copy, not the work. Keep them out of the first line;
  they belong to the preserved lines.

## Multipart and split files

- **Split files** (Part 1 / Part 2, .001/.002 — ONE video in
  several files): the app cannot index them. They are ⛔ with the
  reason "split file" — never proposed as episodes, and never as
  consecutive episodes (numbering split parts as E01 + E02 is a
  classic silent corruption). This is a known ledger bucket; Yan
  decides their fate, not you.
- **Multi-episode files** (ONE file spanning S01E01-E02): propose
  the first-episode form and note the span in the plan note; if the
  gate refuses the form, flag ❓ rather than forcing it.
- **Batch posts** (one message, several files): each file is its
  own item. Never let the batch's shared caption text convince you
  the files share an episode number.

## No-caption files

92 files in the ledger have no caption at all. The file name is the
ONLY evidence:

- Say so in the plan note (`evidence: file name only`).
- The two-source rule still applies — the name must be corroborated
  by provider structure (counts, dates) exactly as a captioned file.
- If the name is only an abbreviation with no corroboration → ❓.

## Decision table

| Situation | Caption action |
|---|---|
| Dimensions known | Quality from height |
| No dimensions, token in name | Carry the token |
| No dimensions, no token | ❓ no-quality — do not invent |
| Edition word + provider edition entry | Edition in first line, edition's ID |
| Split parts (one video, several files) | ⛔ "split file" — never proposed |
| One file, two episodes | First-episode form, span noted |

## Verification

- Read your finished caption's first line: the quality token in it
  can be pointed at — a dimension in the packet or a token in the
  original name. If you cannot point, remove it and flag the item.
