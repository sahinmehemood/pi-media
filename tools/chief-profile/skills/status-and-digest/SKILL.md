---
name: status-and-digest
description: Answer "anything waiting on me" and build team digests
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [chief, status, digest, reporting]
    category: team
    requires_toolsets: [terminal]
---

# Status and digest

## When to Use

When Yan asks what's happening, what's waiting, or for a summary —
and for any scheduled team digest.

## Procedure

1. **Gather fresh, in this order**: your open-items list (routed
   tasks and their states), approvals pending anywhere you can see,
   each lane's latest report. Nothing older than the current
   session is quoted as "current" without a fresh check.
2. **Group by lane**, counts first, one line per item class:
   waiting-on-Yan, in-progress, done-since-last-digest, blocked
   (with the blocker in five words or fewer).
3. **Waiting-on-Yan goes first**, always — that is the question he
   is really asking.
4. **Invisible lanes are named as invisible** in one line ("Media
   Manager's live counts: ask its chat — I can't see them from
   here"). Never estimate another lane's numbers.

## Template

```
🧭 Team — <waiting> waiting on you · <active> in progress · <done> done
Chief: <line or "clear">
Media Manager: <line or "not visible from here">
Blocked: <item — blocker> (only if any)
```

## Pitfalls

- **Digest drift**: turning a status answer into starting work.
  A digest reports; it never fixes, applies, or researches.
- **Double-counting**: a routed task appears once, under its lane,
  not also under yours.
- **Good-news bias**: blocked and failed items are in the digest
  with the same prominence as done items.

## Verification

- Every number in the digest traces to a check made in this
  session. If Yan asks "says who", you can name the lane and the
  moment.
