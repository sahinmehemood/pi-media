---
name: series-records
description: "Store and read series records and aliases correctly."
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [librarian, records, aliases]
    related_skills: [librarian-toolbox, provider-research, episode-verification, anime-donghua-numbering, failure-recovery, scope-and-reporting]
---

# Series Records

Series facts are stored once, app-side, in series records and the
alias map. They are never re-derived per file and never kept only
in prompts, skills, or your own memory. This skill governs when
and how records and aliases are read, written, and forgotten.

Records are app data. Nothing in a record is a skill fact or a
memory fact: never copy a real series name, ID set, offset, or
numbering decision into a skill, a manual, or memory. Those live
only in records.

## When to Use

Use this skill when:

- Starting work on any stem group — a record may already govern
  it, and reading it comes before any research.
- Storing or refreshing the one series decision produced by a
  Stage 3 research pass.
- Saving the alias after a confirmed successful fix.
- Forgetting an alias that has proved to be a poisoned mapping.
- Checking whether auto mode may act on a stem.

Don't use for:

- Researching identity or numbering — use `provider-research`;
  this skill stores its concluded decision, not its exploration.
- Proving individual episodes — use `episode-verification`. A
  record supplies the rule; proof is still per file.
- Guessing a record into existence. No evidence-standard decision,
  no record.

## Prerequisites

- You know the group's channel and stem, and the channel-derived
  media type. Records and aliases never override the channel as
  ground truth for media type.
- For writing a record: a concluded series decision exists, made
  under `provider-research` and meeting the evidence standard in
  `episode-verification` — identity from the authority provider,
  numbering with its title or air-date proof basis.
- For saving an alias: a fix for a file of that channel and stem
  has completed in terminal fixed state with the index confirming
  the expected IDs. Nothing weaker qualifies.

## Procedure

### 1. Read before researching — always

1. Run `record-get` for the series before any gateway call in
   Stage 3, and before formatting any caption for the group.
2. If a verified record covers the group, its decision governs:
   identity, titles, and numbering are taken from the record, and
   Stage 3 research is skipped entirely. Do not re-research a
   decided series to check whether you agree.
3. If no record exists, proceed to `provider-research`. The
   absence of a record is the normal state for a waiting series;
   it is not an error.
4. If the record's status is `conflicted`, it does not govern.
   See step 5 before doing anything else with the group.

### 2. Know what a record contains

A series record holds exactly these facts:

- An internal surrogate key. The record's own identity is
  internal; provider IDs are indexed fields, never the key,
  because one provider ID can legitimately span several distinct
  entries.
- `canonical_key` — one human-readable handle chosen per entry,
  in provider-and-ID form, used to refer to the record.
- `ids` — the provider ID set for the work: any subset of TMDB,
  TVDB, AniList, MAL, IMDb, and Kitsu IDs, as established by
  explicit mappings in the research pass. Never pad the set with
  guessed or similarity-based IDs.
- `titles` — all known title variants for the work.
- `media_type` — the channel-derived type the decision was made
  under.
- `numbering` — the numbering decision and its proof: `source`
  (which provider's structure), `order` (aired, absolute, or
  specials), `offset` where one applies, and `note` recording the
  proof basis in plain words.
- `status` — `verified` or `conflicted`.
- `related_entries` — links to records for related but distinct
  entries, such as a main series and its separately catalogued
  arc. Related entries share context, never identity.

### 3. Write only after an evidence-standard decision

1. Run `record-set` once per series, after the Stage 3 decision
   and before formatting captions. The record is what formatting
   and per-file proof lookups read from.
2. Store the decision as decided: the ID subset actually
   established, all title variants actually found, and the
   numbering object with its source, order, offset, and proof
   note. Store nothing exploratory — no losing candidates, no web
   leads, no unconfirmed mappings.
3. Set status `verified` only when the decision meets the
   evidence standard. If the pass ended in provider disagreement
   that proof could not settle, do not store a verified record;
   escalate instead, per step 5.
4. For a split shape — one provider ID spanning a main entry and
   a distinct arc — create one record per entry, each with its
   own canonical key, and link them through `related_entries`.
   Never merge them into one record because an ID is shared.
5. Refresh an existing record only when new provider evidence
   changes the decision. A refresh replaces the numbering or ID
   facts with the newly proven ones; it never silently accumulates
   contradictory facts. If new evidence contradicts the record
   without settling the question, mark the record `conflicted`
   and escalate.

### 4. Save the alias only on confirmed success

1. Aliases are keyed by the pair of channel and stem, and point
   to one series record. The key is exact: same stem in a
   different channel is a different alias.
2. After a confirmed successful fix — terminal fixed, index
   confirmed — you must save the alias for that file's channel
   and stem, with `alias-set <channel> <stem> <record>`, before
   reporting the batch complete. This is
   mandatory, not optional: the alias is what later groups of the
   same series, and auto mode, start from.
3. Save the alias only on that confirmed success. Never save one
   on a dry-run, an unindexed result, a refusal, a guess, or a
   research decision alone — a record existing is not an alias
   earned.
4. The app never infers, merges, or invents an alias. Only your
   explicit `alias-set` action writes one. Use `aliases` to inspect current
   mappings before relying on or changing any of them.

### 5. Treat conflicted records as a block

1. A `conflicted` record blocks auto mode for its series
   entirely. Auto requires a verified record and an exact alias;
   a conflicted record satisfies nothing.
2. Do not apply fixes from a conflicted record's numbering, in
   any mode, until the conflict is resolved. Research and present
   in ask terms, or escalate.
3. Resolution means new episode-level proof that settles which
   facts are correct, followed by a `record-set` refresh to
   `verified` — or an explicit decision from Yan, which is
   recorded in the numbering note as the basis.
4. Never resolve a conflict by deleting the record to make the
   block disappear, and never downgrade a genuine conflict to
   verified to unblock a batch.

### 6. Forget a poisoned alias explicitly

1. An alias is poisoned when it maps a channel and stem to the
   wrong record — shown by episode proof failing against the
   record it points to, or by fixes landing on a wrong identity.
2. Use `alias-forget` to remove exactly that mapping. Forgetting
   is an explicit action; it is never a side effect of any other
   command.
3. After forgetting, the stem returns to having no alias: its
   next group starts from `record-get` and, if the surviving
   record does not govern, from a fresh research pass.
4. Never forget an alias as a shortcut around a conflicted
   record, and never re-save the same mapping without a new
   confirmed successful fix under the correct record.

## Pitfalls

- Researching before running `record-get`, and re-deciding a
  series the record already governs.
- Keying a record by a single provider ID, or merging distinct
  entries that share one.
- Storing candidates, web leads, or unconfirmed IDs in a record.
  Records hold decisions and their proof, nothing else.
- Writing series facts into a skill, a manual, or memory instead
  of the record. Those surfaces must never carry real names,
  offsets, or numbering decisions.
- Saving an alias before confirmed success, or forgetting to save
  it after — the first poisons auto mode, the second starves it.
- Treating an alias as proof of identity for a file. Aliases route
  to a record; episode proof still decides every file.
- Letting auto mode act on a conflicted record, or resolving a
  conflict by deletion or relabelling.
- Assuming a stem means the same series in every channel. Alias
  keys include the channel precisely because it does not.

## Verification

Before reporting work on a series complete, confirm:

- `record-get` was run before any research, and a governing
  verified record, where one existed, was followed without a
  redundant research pass.
- Any record written rests on an evidence-standard decision,
  carries only established IDs and titles, and stores its
  numbering source, order, offset, and proof note.
- Split entries sharing a provider ID exist as separate linked
  records, not one merged record.
- The alias for each confirmed fix was saved before the batch
  was reported complete, keyed by the exact channel and stem.
- No conflicted record was used for a fix or for auto mode, and
  any conflict is escalated with its reason.
- No real series fact from any record appears in a skill,
  manual, memory, or report summary beyond what the report to
  Yan requires.
