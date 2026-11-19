---
name: telegram-uploaders
description: How Telegram uploaders name files — short forms & habits
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [telegram, uploaders, short-forms, captions]
    category: media
---

# Telegram uploaders — naming habits

## When to Use

For every packet. Telegram naming is its own dialect, distinct from
torrent naming; read files through this lens first, then the type
skill.

## The habits

- **Short forms rule.** Uploaders compress titles to initialisms
  (BTTH, PW, TOHG, MDZS) or clipped stems ("Kimetsu", "Doupo").
  Initialisms are channel-local conventions: expand them from the
  channel's context, the packet's taught aliases, and episode-count
  evidence — never from the letters alone.
- **The caption is often NOT the file name.** Quality and numbering
  may live only in the file_name; the caption may be a short form,
  an ad, or empty. The packet gives you both — read both, every
  time. 92 files in this library have no caption at all.
- **Uploader tags**: `@ChannelName`, join-links, "HD" / "4K" badges,
  `[E-SUB]` (English subtitles), language tags, batch announcements.
  All provenance — strip when reading, never copy into fix captions.
- **Forward provenance**: files forwarded from other channels keep
  the SOURCE channel's naming dialect, not this channel's. A sudden
  style change inside a channel usually means a different origin.
- **Split files**: big files arrive as Part 1/Part 2 (or .001/.002)
  of ONE video. The app rejects multipart — those items are ⛔ with
  reason "split file", never proposed as episodes.
- **Batch posts**: one message can carry a whole season's files over
  several posts; numbering may restart per post or run on — the
  packet's runs_hint is a hint, your per-file check is the truth.
- **Re-uploads**: the same file appears multiple times (the skip
  pile holds up to 6 copies of some files). Copies share one ledger
  item — you decide once per file, not per copy.

## Reading procedure

1. Read caption AND file_name together; note where the numbering
   and quality actually live.
2. Strip uploader tags/ads; expand the short form using aliases +
   channel + counts.
3. Classify the media type; hand off to the type skill.
4. If the name is ONLY an initialism with no other evidence, it is
   ❓ until the channel's other files or the alias map expand it.

## Pitfalls

- An uploader's season label ("S5") may be their own arc counting,
  not any provider's — verify against provider structure.
- "Complete" in a caption describes the UPLOADER's set, which may
  silently miss episodes — count files against the provider list.
- Emoji and decorative unicode in captions are noise; the app's
  parser ignores them and so do you.

## Verification

- For every item you can state: which field (caption/file_name) the
  title, numbering, and quality came from.
