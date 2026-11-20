---
name: scope-and-reporting
description: Hold scope, modes, and live counts in every report.
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [scope, reporting, modes, permissions]
    related_skills: [librarian-toolbox, failure-recovery, series-records, recognition-and-context]
---

# Scope and Reporting

Yan directs the work; this skill keeps the work inside his
direction and makes the report match what actually happened.
Scope is not a preference: the files Yan named are the batch, the
order he set is the order, and the Settings are the permissions
that bind everything done in his absence.

Reports are built from live numbers re-run at report time, in a
plain direct style. An honest zero is a correct report. A dressed
up success is a failure of this skill.

## When to Use

Use continuously, at three moments in every session:

- At the start, to fix the scope and read the permissions before
  any work is chosen.
- During work, whenever a stop condition, mode gate, or scope
  question arises.
- At the end, to build the report from re-run live counts.

Don't use for:

- Deciding identity, numbering, or captions. This skill governs
  what may be worked on and how it is reported, never what a file
  is or how its caption reads.
- Overriding a stop condition because the batch is nearly done.
  Stop conditions end batches; they are reported, not negotiated.
- Inventing work when no scope was given and the mode does not
  authorise unrequested work. The correct action then is to ask
  or report, not to pick a target.

## Prerequisites

- Toolbox access works: `status`, `mode`, `waiting`, and
  `history` are the minimum reporting instruments.
- Yan's instruction for this session is in hand, including any
  scope, order, or exclusions he named in conversation.
- The escalation and parking commands are available: `escalate`,
  `park`, `unpark`.

## Procedure

### Step 1 — Fix the scope before touching the pile

- Take the scope Yan named: a series, a channel, a stem group, or
  named files. His named scope governs the batch and outranks the
  pile's order.
- Work only the named scope. Files outside it are untouched, even
  if they look easy, related, or urgent. A discovery outside scope
  is mentioned in the report, not worked on.
- If Yan named no scope, the mode decides what is permitted
  (Step 2). If nothing is permitted, ask or report plainly; never
  select a target unilaterally.
- Yan's conversational exclusions bind immediately and outrank
  Settings: if he names a personal or protected entry, say so and
  stop on it.

### Step 2 — Read the modes and Settings as permissions

Check `status` and `mode` first, every session. The precedence
reading is exactly this:

1. Librarian disabled: the API refuses everything except `status`
   and `mode`. Report that and stop. Capture settings are not a
   work permission and change nothing here.
2. Mode governs autonomy only when the Librarian is enabled:
   - `off`: Yan-requested work only. Nothing unrequested.
   - `ask`: research and present the decision and proposed
     captions; apply only on Yan's confirmation. Confirmation
     means his approval of a **presented** decision and its
     proposed captions — a general instruction to fix something
     is scope, not confirmation. With test mode also on, the dry
     run runs **first** (a read-only check, not an apply, needing
     no confirmation) and its outcomes are presented **with** the
     captions; the confirmation then covers the real apply once
     test mode is off, provided the captions are unchanged — any
     caption changed after confirmation is re-presented.
   - `auto`: apply without per-batch confirmation only where a
     verified series record and an exact alias both exist for the
     file's stem. Both must exist; the app checks existence, and
     so does this skill before relying on it. Anything without
     both is handled as in `ask`, or escalated — never applied.
3. Any Telegram mutation additionally requires all of: writes
   enabled, test mode off, budget remaining, and the target
   channel in the allowlist. Skip and announcement channels are
   never write targets, even if listed.
4. Test mode overrides every mode. Under test mode a fix performs
   its checks and returns a would-be result with zero mutations;
   it is reported as a dry run, never as a fix.

Settings are Yan's permissions when he is absent: never exceed
them, and never silently do nothing because of them — a block is
reported with its exact reason (Step 5).

### Step 3 — Work one series at a time, in episode order

- One series or stem group at a time, beginning to end, in
  episode order. Do not interleave series, cherry-pick easy files
  across groups, or leave a group half-decided to start another.
- The batch is sized to the remaining daily budget before it
  starts. Attempts count against budget, including ambiguous or
  failed attempts; refusals, dry runs, and pre-write failures do
  not.
- The budget is never raised by the bot. Only Yan sets it. If it
  runs out mid-batch, the batch stops at that file.
- There is no scheduler and no background work. Nothing runs that
  Yan did not ask for in session or the mode does not authorise.

### Step 4 — Complete a batch properly

A batch is not complete until all of the following hold:

- Every file in scope has a terminal outcome: fixed with confirmed
  indexing, failed with a plain reason, escalated with a reason,
  parked with its recorded reason (`park --reason`), or
  explicitly not attempted with its stop reason.
- On confirmed success, the alias for each fixed file's channel
  and stem has been saved with `alias-set`. This is
  mandatory before reporting the batch complete: a batch reported
  complete without its aliases starves later groups and Auto mode
  of their starting point. Check with `aliases` if unsure.
- Series decisions made during the batch are stored in series
  records, per `series-records` — not held in the report, not held
  in memory only.
- Failures were handled per `failure-recovery`, including the
  rule that a second identical failure escalates.

### Step 5 — Stop conditions, reported with the exact reason

Stop the batch and report, naming the exact blocking reason, when
any of these occurs:

- Budget exhausted: state the cap and that it was reached.
- FloodWait: state the returned seconds; the batch ends there.
- Mode block: state the mode and what it permits instead (for
  example, researched and presented, awaiting confirmation).
- Test mode on: state that results are dry runs with zero writes.
- Writes disabled, Librarian disabled, or channel not in the
  allowlist: name the specific switch or list as the reason.
- Escalation line reached on a file: that file stops; whether the
  batch continues depends on whether the cause is file-local or
  batch-wide (FloodWait and budget are always batch-wide).

A stop is a reportable outcome, not an embarrassment. The report
states what was done before the stop and what was not attempted
because of it.

### Step 6 — Build the report from re-run live numbers

- After the batch, re-run `status` and `history`, and `waiting`
  for the worked scope. Report from those results only.
- Counts are counted live at the moment of reporting: never
  estimated, never cached and trusted, never taken from a batch
  log or from memory of what the batch attempted.
- Report in this shape, plainly and directly:
  - Fixed: the files, each with its confirmed expected IDs.
  - Failed: the files, each with its exact state and plain-words
    reason, quoting the API's error verbatim where it is the
    reason. Do not paraphrase an error into something softer or
    more certain.
  - Escalated: the files, each with the reason Yan is being asked
    to decide.
  - Untouched: everything in scope that was not attempted, with
    its stop reason — budget, FloodWait, mode, or test mode.
  - Zero case: if nothing was done, say that nothing was done and
    why. Silence is never a report; neither is a vague summary.
- Never claim a success whose index or Telegram state is
  incomplete, and never present a dry-run result as a fix.
- If Yan's evidence during the session contradicted provider
  data and his evidence was followed, the report says that is
  what was done.

## Pitfalls

- Drifting outside the named scope to finish a neighbouring group
  that looked related. Related is not named; it is reported, not
  worked.
- Reporting from the batch's own tally instead of re-run counts.
  The batch log is a claim; `status` and `history` are the count.
- Calling a batch complete before its aliases are saved. The
  fixes are real and the batch is still incomplete.
- Softening a failure reason when quoting it. Verbatim errors keep
  the next decision honest; paraphrase hides the cause.
- Treating a mode or test-mode block as doing nothing and saying
  nothing. A block with no report reads as a silent failure.
- Presenting dry-run outcomes in the fixed list with a footnote.
  They are reported as dry runs, in their own words, or the report
  is wrong.
- Continuing past a FloodWait or budget stop on the remaining
  files because they are a different channel or series. Batch-wide
  stops are batch-wide.
- Estimating a count that could have been re-run. Every number in
  a report is either a live count or is labelled as not counted.

## Verification

- The worked files are exactly Yan's named scope, in his order,
  one series at a time in episode order.
- `status` and `mode` were checked before work, and every apply
  in the batch was permitted under the Step 2 precedence reading.
- Every in-scope file has a named terminal outcome, including
  not-attempted files with their exact stop reason.
- Aliases for all confirmed fixes are saved; `aliases` confirms
  them before the batch is called complete.
- All counts in the report come from `status`, `history`, and
  `waiting` re-run at report time — none from batch memory.
- Fixed, failed, escalated, and untouched are all reported,
  including an explicit zero where nothing was done.
- No dry-run result is reported as a fix, and no incomplete index
  or Telegram state is reported as success.
