---
name: librarian
description: Core workflow — fix rejected files via the Librarian API
version: 2.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [librarian, telegram, media, workflow]
    category: media
    requires_toolsets: [terminal]
---

# Librarian — core workflow

## When to Use

Whenever Yan asks you to work on the library, fix rejected files, or
check the Librarian — and for scheduled digest runs.

## Tools

Run everything through the CLI (it reads the API URL + token from your
profile environment itself):

```
python3 ~/librarian-cli.py status
python3 ~/librarian-cli.py packet --channel <channel_id> [--limit N]
python3 ~/librarian-cli.py search --q "<title>" --type tv|movie
python3 ~/librarian-cli.py propose --file plan.json
python3 ~/librarian-cli.py apply --id <proposal_id>
python3 ~/librarian-cli.py rollback --id <proposal_id>
```

Channels: Ani -1004395609351 · Movie -1003981572300 ·
Series -1003966209940 · Donghua -1004494510060 · Cartoon -1004450903732.

## Procedure

1. **Status first.** Run `status`. If `enabled` is false, say so and
   stop. Note `dry_run` and today's edit count vs the budget — they
   bound everything below.
2. **One channel at a time.** Use the channel Yan named, or ask which.
   Pull a `packet`. Remember its `packet_id`; items are numbered (`n`).
3. **Classify every run** in `runs_hint` by media type (the channel is
   the default) and open the matching naming skill BEFORE deciding:
   anime / donghua / movie / tv / cartoon / drama. Telegram-uploader
   habits: `telegram-uploaders`. Identification method:
   `provider-research`.
4. **Decide per run, not per file.** A run shares one identification;
   only the episode numbers differ. Apply the two-source rule
   (provider-research): two independent sources agree, or the run is
   ❓ (flag) — never guess.
5. **Build ONE proposal per run** as plan.json:
   - Top level: `channel`, `packet_id`, a short `label`, a `note` with
     your evidence (sources used; for specials, the provider whose
     specials list you used).
   - Per item: `n` (the packet number — NEVER a raw key),
     `seen_caption` (the item's original caption copied EXACTLY from
     the packet), `media_type`, `tmdb_id` and/or `imdb_id`, `season`,
     `episode` (or `absolute: true`), `year` when known, and
     `new_caption` built by the caption contract below.
   - The server rejects the whole proposal if any echo mismatches —
     that means you misread the packet; re-pull and rebuild.
6. **Submit** with `propose`. Read the gate result:
   - `pending` → the plan is verified; present it (template below).
   - `gate_failed` → read each item's reason, fix the plan or flag the
     run ❓ with the reason. Never argue with the gate.
7. **Present and wait.** Send the proposal digest (template). Do NOT
   apply until Yan replies ✅ — unless the automation setting covers
   this class (see status / Yan's standing instruction) — and even
   then, send the apply report afterwards.
8. **Apply** with `apply --id`. Then send the apply report (template)
   with the Telegram link(s) from the results.
9. **Close the loop.** Runs you could not decide stay ❓/⛔ in your
   digest with a one-line reason each. Nothing silently disappears.

## Caption contract

- First line: `Title (Year) SxxExx Quality` for episodes,
  `Title (Year) Quality` for movies. Quality is a REAL token from the
  file (its height, or the quality already in its name) — the app
  rejects captions with no quality token.
- Specials: `S00Exx` form + the special's title where known; donghua
  and dual-numbered shows also carry the arc name and the overall
  episode number (see donghua-naming).
- Preserve the original caption's other lines after the first line.
- End with exactly ONE ID tag line, in the app's format:
  `<a href="https://www.imdb.com/title/ttXXXXXXX/">IMDB</a>`
  (IMDb preferred) or the TMDB equivalent.
- Total caption ≤ 1,000 characters.

## Report templates (use exactly)

Proposal digest:

```
📚 <Channel> — <N> files, <R> runs
✅ <Run label> — <n> files → verified
❓ <Run label> — <n> files, <one-line reason>
⛔ <n> files — no match found
Reply ✅ to apply the verified runs.
```

Apply report:

```
✔ Fixed <a> · skipped <b> · failed <c>
<Title> → indexed <telegram link>
Panel shows before/after for every file.
```

## Pitfalls

- **Raw file IDs.** Never copy a `key` from a packet into a plan. A
  plan of yours once captioned the WRONG FILE this way (a Code Geass
  post received a One Piece caption). Numbers + echoes exist because
  of that failure.
- **SP means special.** SP3 is not Season 3 (see donghua-naming; the
  BTTH fix that did this is still live as a warning). The gate now
  refuses SP→season mappings — do not try to route around it.
- **Picture dramas / bonus shorts** (e.g. Code Geass "Turn x.xx"
  files) are specials with provider-specific placement: default ❓
  unless a provider's specials list settles them.
- **Decimal episodes** (7.5), **split parts** (Part 1/2 of one
  episode), and **batch posts** each have rules in the naming skills —
  check before proposing.
- **No caption at all:** the file_name is the only evidence; say so in
  the note.
- **Personal titles** (negative tmdb_id in the index) are intentional —
  never "correct" them.

## Verification

- Every proposal you present has gate status `pending` — quote its
  proposal_id in your notes to Yan when asked.
- After apply: `applied` count equals the ✅ count you promised, or
  the difference is itemised in the apply report.
- `status` after a run shows `fixed` increased by exactly the applied
  count.
