---
name: packet-triage
description: Read a packet, batch runs, keep file pairing exact
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [librarian, telegram, media, workflow]
    category: media
    requires_toolsets: [terminal]
---

# Packet triage — batches, runs, and exact pairing

## When to Use

Immediately after pulling a packet, before any identification work.
This skill decides *what gets worked together* — the librarian skill
decides what happens next.

## What a packet gives you

- `packet_id` — quote it in every plan from this packet. Plans from
  a stale packet are rejected; if you re-pull, use the new id.
- Items, each with: `n` (its number in this packet), the original
  caption, the file name, size/dimensions when known, the reason it
  was rejected (no-match / no-quality / multipart), and whether the
  original post has been located.
- `runs_hint` — the server's grouping by number-stripped stem. A
  *run* is a set of files that almost certainly share one
  identification (same show, same release pattern) and differ only
  in episode number.

## Procedure

1. **Trust runs_hint as a hypothesis, not a fact.** Verify each run
   with your eyes: same stem, same uploader pattern, sequential
   numbers. A run that mixes two patterns is two runs — split it.
2. **Order the work.** Biggest clean runs first (they clear the most
   backlog per proposal). Park runs that need research Yan hasn't
   approved (e.g. heavy web work) for the end, and say they're parked.
3. **One identification per run.** If two files in a run would need
   different identifications (a special hiding among episodes, a
   movie among episodes), split them out FIRST — specials go through
   `specials-and-arcs`, never inside an episode run.
4. **Echo discipline.** When building a plan, copy each item's
   caption from the packet into `seen_caption` character-for-character
   at the moment you write that item. Never retype from memory, never
   copy a caption from a previous packet, never use the item's `key`.
5. **Count check.** Before proposing: items in plan == items claimed
   in the run. A mismatch means a file was dropped or doubled — find
   it before the gate has to.

## Decision table

| Packet shape | Action |
|---|---|
| Clean run, sequential episodes | One proposal for the run |
| Run + 1–2 SP-marked files | Split: episodes as one run, specials via specials-and-arcs |
| Same stem, two quality tiers | Two runs (quality differs per file) |
| Multipart items (Part 1/2) | Hold for `quality-and-editions` rules; never merge blindly |
| No-caption items | Group by file_name stem; flag evidence as file-name-only |
| Duplicate stems across channels | Work the channel Yan named; note the other, don't cross channels |

## Pitfalls

- **The `key` field is not an identifier for plans.** It exists for
  the server. Using it caused the wrong-file disaster; plans use
  `n` + echo only.
- **Stale packets.** A packet pulled before a long research detour
  may no longer match the ledger (another run may have fixed items).
  If a proposal comes back with pairing errors, re-pull — don't
  argue.
- **Run greed.** A 60-file packet is not one proposal. Runs are the
  unit; a run bigger than ~25 files is worth a second look for
  hidden sub-patterns (season change mid-run is the classic).

## Verification

- Every plan you build can be traced: packet_id → run → items, with
  counts equal at each level.
- If Yan asks "which files are in this proposal", you can list them
  by number and caption from the packet without re-pulling.
