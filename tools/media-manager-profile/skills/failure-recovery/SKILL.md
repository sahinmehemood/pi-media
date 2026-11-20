---
name: failure-recovery
description: Recover fixes from ghosts, drift, and partial states.
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [failures, recovery, ghosts, floodwait]
    related_skills: [librarian-toolbox, scope-and-reporting, series-records]
---

# Failure Recovery

Every operation ends fixed, failed with a plain reason, or
escalated. This skill defines how each failure state is recognised
and what happens next. The governing rule throughout: never claim
success while Telegram and the index disagree, and never convert
one kind of failure into a softer story.

All fix mechanics run through the toolbox. This skill never writes
to Telegram directly, never downloads media, and never retries
around the engine's rules.

## When to Use

Use whenever a fix, batch, or undo does not end cleanly:

- `probe` or a fix result suggests the post may be missing.
- A fix returns a refusal, a failure state, or a partial state.
- A FloodWait is returned for any operation in a batch.
- An edit or repost completes on Telegram but indexing is not
  confirmed, or indexing confirms but a later step fails.
- An undo does not fully complete.
- The same file fails a second time.

Don't use for:

- Identity or numbering disputes. Those are research failures,
  handled by `provider-research` and escalation, not recovery.
- Preventing failures by changing scope or mode. Permissions and
  batch sizing live in `scope-and-reporting`; this skill handles
  failures after they occur.
- Manual repair outside the toolbox. There is no manual path:
  no direct edits, no direct deletes, no re-uploads.

## Prerequisites

- The operation's result from `fix`, `fix-batch`, or `undo` is in
  hand, including its state and plain-words error if any.
- `history` is available to read the recorded operation state.
- `probe` is available as the ghost-evidence instrument.
- The ledger key for the file is known, for `escalate` or `park`.

## Procedure

### Step 1 — Read the operation state exactly

Operation states are a closed list. Read the returned state and
act on that state, not on what the operation was hoped to be:

- In progress: `prepared`, `editing`, `reindexing`, `reposting`,
  `deleting_original`. None of these is an outcome; wait for or
  re-read the terminal state via `history` before reporting. A
  non-terminal state cannot survive an app restart — restart
  reconciliation terminalises it as `escalated` — so if a state
  seems stuck, re-read `history` rather than waiting forever.
- Success: `fixed` — only when indexing was confirmed against the
  expected IDs. This is the sole state reportable as fixed.
  `undone` is the terminal state of a successful undo operation.
- Test mode: `dry_run_complete` — the sole terminal state of a
  test-mode operation. It is never a fix and is reported as a dry
  run, in those words.
- Refusals (no budget consumed, nothing written):
  `refused_drift`, `refused_missing_post`, `refused_subtitle`,
  `refused_personal`, `refused_caption_id_mismatch`, and the
  step-1 refusals `refused_precondition`,
  `refused_wrong_status`, `refused_record_conflicted` (these three
  arrive in the response and create no operation record).
- Failures and partials: `flood_wait`, `edit_failed`,
  `repost_failed`, `edited_unindexed`, `repost_unindexed`,
  `delete_original_failed`, `undo_partial`, `escalated`.
- Batch termination: `aborted` — the batch ended (FloodWait,
  budget exhaustion) before this item was attempted. Nothing was
  tried on the file and no budget was consumed; it is not a
  failure of the file.

No state outside this list exists. An unfamiliar state is itself
reported plainly, not rounded to success or failure.

### Step 2 — Classify a possibly missing post (ghosts)

A ghost classification requires the two-client probe, exactly:

1. Run `probe` for the post at its current locator. The probe
   reads independently through both the userbot and the main bot
   client and reports per client: found, missing or empty, or
   error with its plain-words reason.
2. **Before classifying a ghost, probe the skip copy** at its
   `skip_locator` if the ledger row has one. If the skip copy is
   readable, this is **not** a ghost: submit the fix and let the
   engine take the repost path (Step 5). Ghost requires both
   clients missing/empty on the original **and** the skip copy
   missing/unreadable or absent.
3. Ghost only when both clients report missing or empty AND
   neither reports a transport or permission error (and rule 2 is
   satisfied). The result
   state is `refused_missing_post`: nothing is written, no budget
   is consumed, and the ledger row is escalated with the probe
   result as evidence.
4. Any transport or permission error from either client means the
   classification is `edit_failed` with that error quoted. An
   error is not proof of deletion and is never reported as a
   ghost, never softened into one, and never retried as one.
5. One client finding the post means it is not a ghost. Proceed
   from the found reading: caption, drift check, and editability
   are judged on what was actually read.

The skip copy is evidence in ghost disputes and is never edited
or deleted. If the original is missing but a skip copy is
readable, that is not the end of the file: see Step 5.

### Step 3 — Handle drift

`refused_drift` means the live caption differs from the expected
caption supplied with the fix. Someone or something edited the
post after it was last observed. Behaviour:

- Never overwrite. The drifted caption is the current truth.
- Re-read the file with `file`, compare the live caption against
  what the fix assumed, and decide afresh whether the corrected
  caption is still the right write.
- If the drift already contains a correct filing, report that and
  stop; do not re-apply for the sake of the batch.
- If the drift is unrelated or wrong, the file returns to the
  normal pipeline with the live caption as the new expected
  caption. Unexplained repeated drift on one file escalates.

### Step 4 — Handle FloodWait

The engine writes as the Media Manager bot through the Bot API,
which returns a rate limit plainly as `retry_after` seconds in the
error — no client in the write path sleeps or hides a wait. A
FloodWait terminates the operation and the whole batch. Behaviour
is exact and has no exceptions:

- Report the seconds returned, as a number, immediately — with
  when the wait began (e.g. "FloodWait 120s at 14:03 — no retry
  attempted"). If Yan asks again later, state the elapsed time
  rather than repeating the original figure as if it were the
  remaining wait.
- Stop the operation and stop the batch. Remaining files are
  reported as not attempted, with FloodWait as the reason.
- No sleeping and retrying, no internal retry by the engine, and
  no fallback client tried for that post. Switching clients to
  evade a rate limit defeats the rule and is never done.
- Budget treatment follows the engine's accounting: the attempted
  write counts as an attempt; unattempted files do not.
- Work resumes only in a later session or on Yan's instruction,
  sized to the remaining budget; the flooded file is re-read
  before any new attempt, per Step 7.

### Step 5 — Follow the repost path only as the engine returns it

Some posts cannot be edited by any client. The repost fallback
exists only inside the fix engine's result — it is never chosen
freely, never invoked as a separate manual action:

- Order is fixed: the engine edits first — as the Media Manager
  bot, via Bot API; if the post is not editable, the engine
  reposts.
- A repost re-sends the file by its Telegram `file_id` from the
  readable post — the original if readable, otherwise the skip
  copy — into the source channel under the corrected caption.
  (A `file_id` from a user account is not portable to a bot for
  sending; the engine's exact send mechanism is its own Phase 6
  decision. What binds here is the shape: a re-send by
  identifier, never a download.)
- `copy_message` is never used: it silently no-ops on exactly
  these posts. Nothing is ever downloaded or re-uploaded; a
  repost is a re-send by `file_id` only.
- The replacement is indexed and confirmed before any original
  is deleted. On confirmation, the file's current locator moves
  to the replacement; the source locator never changes, and all
  later fixes, probes, and undos address the current locator.
- The skip copy itself is never edited and never deleted, even
  when it was the repost source.

### Step 6 — Handle unindexed and partial outcomes

- `edited_unindexed`: the edit landed on Telegram but the index
  does not confirm the expected IDs. The engine automatically
  restores the previous caption and re-checks: if the restore is
  confirmed, the file is back in its pre-fix state and returns to
  waiting; if the restore fails or also lands unindexed, the
  operation escalates with coordinates. Report it as edited but
  unindexed, with the engine's reason and the recovery outcome.
  Never as success. Never restore by hand — the restore is the
  engine's step, yours is the report.
- `repost_unindexed`: the replacement post exists but indexing is
  not confirmed. There is no automatic recovery for a repost: the
  operation escalates with both locators for Yan's decision.
  Report it as reposted but unindexed. The original, if it still
  exists, has not been deleted at this point; do not delete
  anything manually to tidy up.
- `delete_original_failed`: the replacement is confirmed but the
  original could not be deleted. Report both locators plainly:
  the file is filed correctly and a duplicate original remains.
  Recovery is Yan deleting the original at the recorded locator
  in Telegram — no app action is needed afterwards, because the
  original was never indexed. Never delete it yourself. (A
  FloodWait raised at the delete step also lands here, with its
  seconds recorded in the error.)
- `repost_failed`: the repost itself failed. The original state
  is unchanged; report the plain-words error. The file returns
  to waiting unless Step 8 escalation applies.
- `edit_failed`: the edit failed with a plain-words error that
  is not a ghost classification. Report the error verbatim.
- `undo_partial`: an undo completed on one side only — Telegram
  and the index disagree about the file. Report it as partial
  with the coordinates for manual recovery and escalate. An undo
  is never reported complete while the two disagree.

### Step 7 — Handle ambiguous timeouts by re-reading

Telegram and the index cannot transact together, so a timeout
mid-edit is ambiguous: the caption may or may not have landed.

- The engine re-reads the post before considering any repost: if
  the new caption is already live, it proceeds to re-indexing
  instead of creating a duplicate post.
- If driving a retry at the toolbox level, follow the same rule:
  re-read first with `file` or `probe`. A live new caption means
  treat the file as edited and move to confirmation; never create
  a second post to be safe.
- An ambiguous attempt counts against budget whether or not the
  caption landed. Report the ambiguity honestly if it remains.

### Step 8 — Escalate on the second identical failure

- The same fix failing twice for the same reason ends automated
  attempts on that file: escalate with
  `escalate <key> --reason <text>`, giving the plain-words
  reason, and report the escalation to Yan.
- A whole stem turning out to be ghosts escalates as a group
  fact, not as separate surprises per file.
- Escalation states the evidence: probe results for ghosts, the
  verbatim error for failures, both locators for partial deletes,
  coordinates for partial undos.
- Parking with `park <key> --reason <text>` is for deliberately
  setting a file aside with its reason recorded on the ledger
  row (for example a multipart release); it is not a substitute
  for escalating a failure Yan should see.

## Pitfalls

- Calling a transport error a ghost because the post could not be
  read. Unreadable and deleted are different facts with different
  next steps.
- Probing with one client and classifying from it. One client's
  read can never establish a ghost; both are required.
- Retrying after a FloodWait, or finishing the rest of the batch
  quietly. The batch ends; the seconds are the report.
- Choosing a repost manually because an edit looks hard. Repost
  happens only as the engine's returned path after edit fails as
  not editable.
- Using a copy-style send for a repost, or downloading the file to
  re-upload it. Both are prohibited; re-send by identifier only.
- Reporting `edited_unindexed` as fixed because the caption looks
  right on Telegram. Filing is index confirmation, not appearance.
- Deleting an original by hand after `repost_unindexed` or
  `delete_original_failed`. Manual deletes create the exact
  Telegram/index disagreement this skill exists to prevent.
- Repeating a failing fix a third time to see if it works now.
  Twice with the same reason is the escalation line.

## Verification

- Every failed operation in the batch is named with its exact
  state and its plain-words reason, quoted where one was returned.
- Every ghost claim rests on a two-client probe with no transport
  error from either client, attached to the escalation.
- Any FloodWait stopped the batch, its seconds were reported, and
  no retry or fallback client was attempted.
- No repost occurred outside an engine fix result; every repost
  was confirmed indexed before any original deletion.
- No file is reported fixed unless its state is `fixed` with
  indexing confirmed against the expected IDs.
- Second identical failures are escalated, not re-attempted.
- Partial states carry their coordinates in the report.
