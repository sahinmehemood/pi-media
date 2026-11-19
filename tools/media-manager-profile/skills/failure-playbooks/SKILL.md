---
name: failure-playbooks
description: Exact recovery steps when anything errors or misbehaves
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [librarian, media, errors, recovery]
    category: media
    requires_toolsets: [terminal]
---

# Failure playbooks — stop, diagnose, recover or report

## When to Use

The moment anything behaves unexpectedly: CLI errors, gate
surprises, provider failures, count mismatches. One rule above all:
**never retry blindly, and never hide it.** A failure reported
plainly costs a minute; a failure papered over costs a library.

## Playbooks

**CLI errors / connection refused / 401**
The Librarian API or the app is down, or the token is wrong.
→ Run `status` once. If it fails the same way, STOP. Report to Yan:
what you ran, the error, and that the app may need attention. Do
not loop retries; do not look for the token yourself.

**`propose` rejected: echo or pairing**
You misread the packet (see `packet-triage`).
→ Re-pull the packet, rebuild the plan from the fresh packet, run
`self-check-eval` row 1 twice. Second rejection on the same run →
escalate with both packet_ids.

**`propose` rejected: identity check failed**
The live file no longer matches the ledger item (fixed elsewhere,
re-uploaded, deleted).
→ Re-pull; if the item is gone from the packet, it is resolved —
say so and move on. If it changed shape, treat it as a new item.

**Gate failed on some items**
Normal operation, not an error. Read each reason (`gate-and-apply`
table), fix what's fixable, flag the rest. Items are released back
to the ledger automatically — nothing is stranded.

**Apply result: partial failures**
Report the exact split (fixed / skipped / failed) from the result.
→ Failed items return to the ledger; name them in the report with
their reason. Never re-apply the whole proposal to chase the
failures — that double-edits the successes.

**Counts don't move after apply**
Verification failed (AGENTS.md duties).
→ Re-run `status` once after a few seconds (indexing can lag). If
still unmoved, report the discrepancy to Yan with the proposal_id —
do not re-apply.

**Provider site down / search empty / page changed**
→ Switch to the second provider for the cross-check; if both are
unreachable, the run waits: ❓ "providers unreachable", stated in
the digest. Never fill the gap from memory of a series' structure.

**FLOOD_WAIT or rate-limit text anywhere**
→ Stop all work immediately, report to Yan with the wait duration
if shown. The app handles its own flood discipline; your job is to
not add pressure.

**Yan says a fix is wrong**
→ Playbook D (rollback) in the librarian skill, same session,
including the alias check. Acknowledge the error plainly first —
what was wrong, in one line — then roll back, then report.

**You are unsure what happened**
→ Say exactly that, with the last command and its output. "I don't
know yet" is an acceptable report; a confident invention is not.

## Verification

- Every failure in a session appears in that session's report to
  Yan, with the playbook step taken. If it isn't in the report, it
  didn't get handled.
