---
name: gate-and-apply
description: Gate results, applying plans, verification, rollback
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [librarian, telegram, media, workflow]
    category: media
    requires_toolsets: [terminal]
---

# Gate and apply — the server checks, the server edits

## When to Use

From the moment a plan is submitted until the apply is verified —
and whenever a rollback is needed.

## What the gate is

The gate trial-runs the app's OWN matcher on every proposed caption,
exactly as if the file were being indexed fresh. A plan becomes
`pending` only when every item resolves to the intended ID (and
season/episode for TV). The gate is not an obstacle to argue with —
it is the verifier. Maker ≠ checker: you propose, it checks.

## Reading gate results

| Result | Meaning | Your move |
|---|---|---|
| `pending` | Every item verified | Present the digest; apply per permissions |
| Item fail: wrong resolution | Caption resolves to a different ID/title than intended | Your identification or caption is wrong — recheck sources; never resubmit the same caption hoping |
| Item fail: no quality | Caption lacks a true quality token | `quality-and-editions`; source it or flag the item |
| Item fail: specials guard | SP-marked original planned as a regular season | `specials-and-arcs`. The guard is right until proven otherwise |
| Item fail: year mismatch (strict-year on) | Plan year ≠ resolved year | Wrong work (remake trap) or wrong year — recheck |
| Whole proposal rejected: echo mismatch | A `seen_caption` doesn't match the packet | You misread the packet. Re-pull, rebuild (`packet-triage`) |
| Whole proposal rejected: identity check | The live file no longer matches the ledger item | State changed under you (fixed elsewhere / re-uploaded). Re-pull and reassess |

Gate-failed proposals RELEASE their items back to the ledger — a
failed plan never strands files. Fix and resubmit, or flag.

## Applying

1. Apply only: Yan's ✅ in chat, or the server's auto-apply under
   his Settings (the `propose` result tells you which happened).
2. `apply --id <proposal_id>` — the server edits the original's
   caption via the Media Manager bot, re-indexes, verifies the file
   landed, deletes the skip-pile copies, writes the audit row, and
   teaches the alias.
3. Honour the pacing: the server paces caption edits itself. Never
   parallel-apply two proposals.
4. **Test mode ON** → apply is a dry run: nothing changes. Report
   it as a dry run, in those words.

## Verifying an apply (before you report)

- `status`: fixed count increased by exactly the applied count.
- The apply result's per-file outcomes match your report's
  fixed / skipped / failed numbers.
- Every fixed file in your report carries its Telegram link from
  the result — never construct a link yourself.
- If verification disagrees with the apply result, report the
  DISAGREEMENT, not the rosier number.

## Rollback

- `rollback --id <proposal_id>` restores the before-caption and
  removes the indexed part. The ledger item returns to open.
- After ANY rollback: check the alias map for an alias taught by
  that apply (`alias-and-memory-discipline`). Rollback does not
  unteach aliases; a surviving wrong alias is reported to Yan in
  the same message as the rollback report.
- Rollback is Yan's call or the Chief relaying Yan — never your
  unilateral cleanup of a fix Yan approved and hasn't questioned.

## Pitfalls

- **Resubmitting a failed plan unchanged.** The gate is
  deterministic; the same plan fails the same way. Change the
  evidence or the caption, or flag.
- **Applying a plan Yan hasn't seen** because it "looks like" one he
  approved. Approval attaches to a proposal_id, not a resemblance.
- **Reporting from memory.** All numbers come from the CLI result
  of THIS apply, read after it finished.
