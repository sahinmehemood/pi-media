---
name: librarian
description: Router + core workflow for all Librarian work
version: 3.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [librarian, telegram, media, workflow, router]
    category: media
    requires_toolsets: [terminal]
required_environment_variables:
  - LIBRARIAN_API_URL
  - LIBRARIAN_API_TOKEN
---

# Librarian — router and core workflow

## When to Use

Every library work session: Yan asks you to fix rejected files, work
a channel, review what's waiting, roll something back — and for
scheduled digest check-ins. This skill routes the work; the other
skills carry the detail.

## Tools

Everything runs through the CLI (it reads the API URL + token from
your profile environment itself):

```
python3 ~/librarian-cli.py status
python3 ~/librarian-cli.py packet --channel <channel_id> [--limit N] [--scope waiting|indexed|unindexed|all]
python3 ~/librarian-cli.py search --q "<title>" --type tv|movie
python3 ~/librarian-cli.py propose --file plan.json
python3 ~/librarian-cli.py apply --id <proposal_id>
python3 ~/librarian-cli.py rollback --id <proposal_id>
```

Channels: Ani -1004395609351 · Movie -1003981572300 ·
Series -1003966209940 · Donghua -1004494510060 · Cartoon -1004450903732.

## Scope — your remit is every file, not just the rejects

Packets default to `--scope waiting`: the rejected files. When Yan
scans a channel from the Librarian page, the ledger also holds every
other file in it, and the scope widens:

- `--scope indexed` — files already in the library. Each item carries
  `current_match`: what it is indexed as now. A plan for one of these
  REPLACES its match — the app cleans up the old claim on apply. Use
  this when Yan asks you to review or re-check a channel or a series.
- `--scope unindexed` — files in the channel that were never indexed
  and never rejected.
- `--scope all` — everything on record for the channel.

When Yan names a channel or a series generally ("review Ani",
"check One Piece"), work scope `all`. When he says fix the rejects
or the skip pile, stay on `waiting`.

## Playbooks — pick one, copy its steps, skip nothing silently

Match the request to ONE playbook. Work its steps in order. If a
step cannot be done, write `skip: <reason>` in your notes and
continue — a skipped step is visible, never invisible.

**Playbook A — channel run** (Yan names a channel, or says "fix files")
1. `status` — permissions noted (AGENTS.md run protocol).
2. `packet` for the channel → `packet-triage` for batching.
3. Per run: classify → naming skill → `provider-research` →
   `specials-and-arcs` if anything is SP-marked or arc-shaped.
4. Build the plan (contract below) → `self-check-eval` → `propose`.
5. Present the digest (template). Apply on ✅, or report the
   server's auto-apply.
6. Verify counts; close every run as ✅ / ❓ reason / ⛔.

**Playbook B — single title** (Yan names a show/movie, or forwards a file)
1. `status`. 2. `search` the title to see what the index already
holds. 3. Pull a packet filtered by the channel it lives in; find
the run. 4. Continue as Playbook A steps 3–6 for that run only.

**Playbook C — digest check-in** (scheduled run, no human present)
1. `status`. 2. If the digest setting is Manual, or weekly on a
non-Monday, or nothing waits and nothing is pending: produce NO
output at all. 3. Otherwise send the digest template (below) —
report only; never pull packets, propose, or apply in a check-in.

**Playbook D — rollback** (Yan says a fix was wrong)
1. Find the proposal (Yan's message, the panel, or `status` counts).
2. `rollback --id`. 3. Check the alias map for an alias the apply
taught (`alias-and-memory-discipline`) — a surviving poisoned alias
is reported to Yan the same message. 4. Report what was restored,
with the link.

**Playbook E — flagged review** (Yan asks "what's stuck / what needs me")
1. `status` for counts. 2. Summarise the ❓/⛔ backlog grouped by
reason (no-match, no-quality, multipart, specials) — counts first,
top examples only. 3. Offer the next action per group; do not start
fixing inside a review unless Yan says so.

## The plan contract (propose)

- Top level: `channel`, `packet_id`, short `label`, `note` with the
  evidence (sources named; for specials, the provider whose specials
  list was used).
- Per item: `n` (packet number — NEVER a raw key), `seen_caption`
  (the item's original caption copied EXACTLY from the packet),
  `media_type`, `tmdb_id` and/or `imdb_id`, `season`, `episode`
  (or `absolute: true`), `year` when known, `new_caption` per the
  caption contract.
- One proposal per run. A run shares one identification; only
  episode numbers differ.
- The server rejects the WHOLE proposal on any echo mismatch — that
  means you misread the packet. Re-pull, rebuild, do not patch
  blindly.

## Caption contract

- First line: `Title (Year) SxxExx Quality` for episodes,
  `Title (Year) Quality` for movies. Quality is a REAL token from
  the file (its height, or the quality already in its name) — the
  app rejects captions with no quality token (`quality-and-editions`).
- Specials: `S00Exx` form + the special's title where known; donghua
  and dual-numbered shows also carry the arc name and the overall
  episode number (donghua-naming, specials-and-arcs).
- Preserve the original caption's other lines after the first line.
- End with exactly ONE ID tag line, in the app's format:
  `<a href="https://www.imdb.com/title/ttXXXXXXX/">IMDB</a>`
  (IMDb preferred) or the TMDB equivalent.
- Total caption ≤ 1,000 characters.

## Gate and apply

- `pending` → verified plan; present it.
- `gate_failed` → read each item's reason (`gate-and-apply`), fix or
  flag. Never argue with the gate, never route around it.
- If the `propose` result carries `auto_apply`, the server is
  applying under Yan's Settings: do not ask, wait a few seconds,
  re-check, send the apply report for what was actually fixed.
- After any apply: verify `status` fixed-count moved by exactly the
  applied number before reporting.

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

Digest (check-in):

```
📚 Librarian — <waiting> waiting · <fixed> fixed so far · <pending> proposal(s) awaiting approval · <today> fixes today
Reply with a channel name and I will prepare its batch.
```
(Second line only when waiting > 0.)

## Pitfalls

- **Raw file IDs.** Never copy a `key` from a packet into a plan —
  a plan once captioned the WRONG FILE that way. Numbers + echoes
  exist because of that failure.
- **One file at a time.** Yan rejected drip work. Batch by run,
  report by batch.
- **Presenting before the gate.** An unverified plan is not shown as
  ✅. Only `pending` plans are "verified".
- **Personal titles** (negative tmdb_id in the index) are
  intentional — never "correct" them.
- **No caption at all:** the file_name is the only evidence; say so
  in the note (`quality-and-editions`).

## Verification

- Every ✅ in a digest corresponds to a `pending` proposal_id.
- Apply report numbers equal the CLI's applied/skipped/failed.
- Session ends with `status` re-checked: fixed total consistent
  with everything you reported.
