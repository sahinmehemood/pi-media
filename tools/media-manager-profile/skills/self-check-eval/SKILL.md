---
name: self-check-eval
description: Score every plan against the rubric before submitting
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [librarian, media, evaluation, quality]
    category: media
    requires_toolsets: [terminal]
---

# Self-check eval — no plan ships below full marks

## When to Use

After building a plan, BEFORE `propose`. Every run, every time.
This is the unit test you run on your own work — the gate will
check the captions; this checks the thinking.

## The rubric — score each item, out loud in your notes

| # | Check | Pass condition |
|---|---|---|
| 1 | Pairing | Every item uses packet `n` + exact echo; counts match the run |
| 2 | Identity | Two independent sources agree (or one ID + structure) |
| 3 | Numbering | Episode list of the chosen provider was actually opened and matches count + position |
| 4 | Specials | Nothing SP/arc/bonus-shaped is planned as a regular season; specials name provider + title |
| 5 | Quality | Every caption's quality token traces to dimensions or the original name |
| 6 | Year | Year in caption matches the identified work (remake check done) |
| 7 | Caption contract | First-line format, preserved lines, exactly one ID tag, ≤ 1,000 chars |
| 8 | Permissions | Plan fits test mode / automation / budget as `status` reported |
| 9 | Memory | No parked item (MEMORY.md standing orders) is included |
| 10 | Evidence note | The plan note names sources, IDs, and any disagreement between providers |

## Rules

- **Full marks or flag.** A run that fails any row is not submitted
  "to see what the gate says". Fix the row, or the run goes ❓ with
  the failed row as the reason.
- **Score the run, not your confidence.** "I'm fairly sure" is not
  a row. Each row passes on evidence you can point at.
- **Row 4 is never skipped** because the run "looks like episodes".
  Look at the markers again, specifically.
- If the same row fails for a run twice, escalate to Yan with the
  evidence (AGENTS.md escalation list) instead of a third attempt.

## Why this exists

Every historical failure of this bot maps to a row: the wrong-file
apply (row 1), the SP-as-season fix (row 4), quality-less captions
(row 5), a poisoned alias teaching a rolled-back mapping (row 2's
evidence was never real). The rubric is those lessons, made
mechanical — because the next model on this profile may be weaker
than you, and the rows don't care.

## Verification

- Your digest's ✅ means: rubric 10/10 AND gate `pending`. If Yan
  asks "did you check", the scored rows are in your session notes.
