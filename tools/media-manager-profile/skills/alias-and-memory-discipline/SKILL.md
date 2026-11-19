---
name: alias-and-memory-discipline
description: What gets taught, written, or never recorded
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [librarian, media, memory, aliases]
    category: media
    requires_toolsets: [terminal]
---

# Alias and memory discipline — the bot's long-term memory hygiene

## When to Use

Whenever an apply teaches the alias map, after any rollback, and
whenever you are about to write something into MEMORY.md.

## The three stores, and what belongs in each

| Store | Holds | Never holds |
|---|---|---|
| **Alias map** (app) | Approved stem → ID mappings, taught ONLY by a successful apply | Guesses, one-source mappings, mappings from rolled-back applies |
| **MEMORY.md** (yours) | Library state: fixed / parked / poisoned items, Yan's standing orders, live permission summary | Series structures, provider facts, anything a skill should carry |
| **Skills** | General method that would help with a series you have never seen | Any fact about a specific series, ever |

If a fact would be useless for a different library, it is not a
skill fact. If a fact is about the world (a show's structure), it is
researched fresh, not memorised.

## Alias rules

1. Aliases are taught by the server on a successful apply — you do
   not write them directly, and you never ask for one to be taught
   for an unverified plan.
2. The packet shows taught aliases for its stems. Trust a taught
   alias over a fresh fuzzy match — it is Yan-approved history.
3. **Rollback does not unteach.** After any rollback, check whether
   the rolled-back apply taught an alias. If it did and the mapping
   is wrong, report it to Yan in the rollback message so it can be
   deleted from the panel's alias manager. A poisoned alias silently
   re-teaches the mistake to every future run of that stem — this
   has already happened once; it is why this rule exists.
4. If a taught alias and fresh two-source research disagree, the
   research wins and the disagreement is reported — the alias may
   be a survivor from a bad apply.

## Memory rules

1. MEMORY.md has a hard size cap and is read every session. Every
   line must earn its place: state, orders, warnings.
2. Write corrections the same session Yan gives them. A correction
   that lives only in chat is lost.
3. Date nothing relatively ("recently", "the other day") — state
   what is true now, or the order Yan gave.
4. Never store credentials, tokens, or IDs of Yan's accounts in
   memory. The CLI reads its token from the environment; you never
   repeat it.
5. When state changes (a parked item is released, a wrong fix is
   rolled back), UPDATE the memory line — two contradictory lines
   are worse than none.

## Verification

- After a rollback, you can answer: "did that apply teach an alias,
  and is it still there?" — checked, not assumed.
- Anything in your MEMORY.md can be traced to a Yan order or a
  verified event. If you cannot trace it, delete it.
