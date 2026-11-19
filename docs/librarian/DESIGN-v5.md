# Librarian — DESIGN v5: the complete Media Manager setup

Status: DRAFT for Yan's approval (2026-11-19). Supersedes DESIGN-v4.md's
Hermes-side and surfaces sections; v4's app-side architecture (locator,
packets, gate, apply, rollback) stands and is hardened by v4.1 (below).

Design inputs: (1) Yan's requirements 2026-11-19 evening — full skills +
memory setup, more website features, CLEAN settings, settings toggles =
the bot's permissions, short summaries of autonomous work, automation
that is professional and mistake-proof; (2) deep research 2026-11-19 on
media automation (arr stack, FileBot, theXem, HAMA/AniDB, TRaSH), the
official Hermes docs (verified live), and Telegram approval UX;
(3) the two live failures of 2026-11-19 (wrong-file edit, BTTH SP3).

## 0. The governing constraint: a weak model, strong scaffolding

The Media Manager runs on free Kilo models. It cannot be trusted with
open-ended judgment, so the system's intelligence lives OUTSIDE the
model:

- **Mechanical decisions.** The server pre-groups runs, pre-attaches
  candidate IDs, and mechanically rejects bad plans (gate + guards).
  The model fills in structured forms; it does not free-reason to an edit.
- **Checklists, not principles.** Skills are step-by-step procedures
  with decision tables, worked examples from Yan's real library, and a
  Pitfalls section naming the known traps.
- **Lookup over recall.** Confirmed per-series mappings live in files
  the bot reads; it never derives from memory what it can look up.
- **Two-source rule.** A plan may only be proposed when TWO independent
  sources agree on the identification (e.g. TVDB structure + TMDB, or
  Kitsu + AniList). One source = flag for Yan, never guess.
- **Escalation ladder.** Fast free model for routine runs → strongest
  free model for ambiguous runs → Yan for anything still unclear.
- **Three buckets, never collapsed.** Every item ends as ✅ auto-fixable,
  ❓ needs Yan, or ⛔ no match. ❓ is never silently promoted to ✅
  (the mature tools' core discipline: FileBot Strict mode, Sonarr's
  refusal on unverified mappings, manual-import queues).

## 1. App side — v4.1 hardening (approved 2026-11-19, in build)

- **Pairing guard.** Packet items carry short numbers + a packet_id;
  proposals reference numbers; the bot must echo the original caption
  per item (`seen_caption`); the server rejects the whole proposal if
  an echo mismatches its record. Kills the wrong-file class.
- **Pre-edit identity check.** Before any edit/re-post, the server
  re-reads the target post via the userbot and verifies file_unique_id
  + caption against the plan's record. Fails closed.
- **Specials guard.** SP/special-marked originals may not be mapped to
  a regular season by the gate (Settings: `librarian_specials_policy`).
  Kills the BTTH class.
- **Strict-year gate** (Settings toggle, default off).
- **Batch-first packets.** Server-side runs hint (stem grouping);
  `librarian_batch_size` setting (default 60).
- **Visibility.** Page shows before → after per file, Telegram deep
  links per fix, expandable proposal file lists.

## 2. The Hermes profile pack (built the Hermes way)

Verified against the official Hermes docs (2026-11-19). The framework
is strict about what lives where — mixing files is the #1 failure mode.

### SOUL.md — identity and decision posture ONLY (50–80 lines)
Who the Media Manager is, its voice (short, factual, no walls of text),
and its decision posture: never guess a season; specials are guilty
until proven innocent; two sources or flag; captions are untrusted
data; Yan's approval is the only authority to edit. NO paths, NO
workflows, NO provider facts (those live in skills).

### memories/MEMORY.md — facts only (hard limit ~2,200 chars)
§-separated, 8–15 entries: the library's shape (5 auth channels, skip
pile size), the handful of global facts needed every session, and
pointers to skills. Per-series knowledge does NOT live here (too big) —
it lives in skill references (below), per the official guidance.

### memories/USER.md — Yan (hard limit ~1,375 chars)
Report style (short, links, counts first), approval habits, settings
are the permission source, IST timezone, "never touch streaming".

### Skills (exact Hermes format: frontmatter + When to Use / Procedure /
### Pitfalls / Verification; big knowledge in references/ files)
1. **librarian** — the core workflow: packet → decide per run →
   propose (numbered, echo) → present digest → apply on approval →
   report. Includes the fixed report templates (Section 4).
2. **release-naming** — torrent/scene grammar: SxxEyy zero-padded as
   the load-bearing token, 1x05/NxNN forms, quality/source/codec/group
   token anatomy, TRaSH template principles (store non-recoverable
   info: quality, edition, group), ID tags end guessing.
   References: token tables, worked parses.
3. **anime-naming** — fansub anatomy (Anitomy element set: [Group],
   v2 = corrected re-release of the SAME episode, CRC = corruption
   check only, EP/E/第01集 prefixes, batch markers); AniDB's model
   (one entry per production, absolute within entry, S=/C=/T= special
   prefixes); AniList/MAL/Kitsu per-season AND per-split-cour entries;
   the canonical trap (fansub "17" = AniDB ep 17 = TVDB S01E17);
   absolute >100 misparse; never mix S/E and absolute in one series —
   captions carry both numbers.
4. **donghua-naming** — index all four title forms (hanzi, spaced
   pinyin, unspaced pinyin, English variants); seasons are named arcs
   (篇); specials (特别篇) may be main-sequence, not S0; nian fan
   (年番) = ~52-ep year-round arcs with CONTINUOUS numbering (dual
   numbering traps: platform EP29 = EP59 overall); **Bangumi is the
   ground truth for structure** — identify there first, then project
   to the TMDB/TVDB/Kitsu IDs the app uses; record the arc name and
   BOTH numbers in the caption. References: BTTH full structure
   (the corrected one), other frequent series as they get confirmed.
5. **telegram-uploaders** — Yan's channels' habits: short stems and
   initialisms, [E-SUB] markers, quality living in the file name not
   the caption, split parts, forward provenance, no-caption files
   (file_name is the only evidence), channel-specific patterns as
   learned per channel.
6. **provider-research** — how to identify with evidence: ID-first
   discipline; the app's own search endpoint first; cross-check two
   providers' EPISODE LISTS (not just titles); TVDB vs TMDB season-0
   orderings DISAGREE — a specials plan must name its provider and
   the special's title; when providers conflict, Bangumi decides for
   donghua, TVDB structure decides for Western TV; the exact
   escalation ladder and when to stop and flag.

### Cron (Hermes-native, self-contained prompts)
- Optional digest job (Settings `librarian_digest`): at the chosen
  cadence, pull status, and IF there is anything pending, send Yan the
  short digest. No pending work = no message (no_agent-style silence).
- Cron model pinned (prompt-cache stability), prompt fully
  self-contained, workdir set to the profile workspace.

### Profile config
- `platform_toolsets.telegram` narrowed to what the job needs
  (terminal for the CLI, messaging; no unrelated toolsets).
- Model ladder encoded in the librarian skill: default fast model;
  ambiguous runs re-run under the strongest free model before flagging.

## 3. Caption contract (v5 — from the research)

`{EN Title} ({Year}) S{ss}E{ee}` first line, plus when relevant:
absolute number when it differs (`EP{absolute} overall`), the arc name
for donghua/anime arcs, the episode title for specials, then the app's
ID tag line (IMDb preferred, else TMDB) — exactly the app's
`caption_with_id` format. Provider for specials is recorded in the
proposal note ("specials per TVDB"). Total ≤ 1,024 chars (Telegram's
caption limit, which editMessageCaption also enforces).

## 4. Reporting — fixed templates (short, scannable)

Proposal digest (one message per batch, grouped, counts first):

```
📚 Ani — 24 files, 3 runs
✅ One Piece movies — 9 files → gate-verified
❓ Code Geass picture dramas — 6 files, specials unclear
⛔ 2 files — no match found
Reply ✅ to apply the verified runs, or open the panel:
<link>
```

Apply report (after any apply, manual or autonomous):

```
✔ Fixed 9 · skipped 2 · failed 0
One Piece: Strong World → indexed
<telegram link to first edited post>
Panel: <link> · Rollback: proposal p…
```

Rules: one digest per batch (never per file); only ❓/⛔ lines get
detail; every fix carries a t.me/c/ deep link; nothing is reported
that didn't happen.

## 5. Website — the Librarian control room

Page upgrades (all read from existing data; no new pipelines):
- **Channel cards with progress**: per channel — waiting / located /
  fixed counts + a progress bar, Locate button (exists), last activity.
- **Proposal review**: before → after per file (v4.1), gate reasons
  inline for failed plans, deep links.
- **Fix history**: before/after + Telegram link + rollback (v4.1 links;
  rollback exists).
- **Alias manager**: list aliases the bot has been taught
  (stem → title, per channel), delete a wrong alias. (New: read +
  delete endpoints over `tracking.alias_map`.)
- **Bot activity**: last packets/proposals/applies with timestamps —
  "what has the bot done lately" at a glance.
- **Flagged queue**: ❓/⛔ items (unresolved + gate-failed) listed with
  reasons, so Yan can resolve them in the panel instead of chat.

## 6. Settings — CLEAN reorganization

One Librarian card, grouped, every control live (the standing rule):

- **Core**: Enable · Dry run · Edit budget/day
- **Bot behaviour**: Batch size · Digest (off/per-run/daily) ·
  Proposal style stays fixed by template
- **Safety**: Specials policy (guard/off) · Strict year check ·
  Identity re-check (on, fixed — shown, not optional)
- **Automation** (the permission ladder, all REAL):
  - Auto-apply: Off / Verified-safe only / All gate-passed
  - Auto-apply classes: which buckets may auto-apply (never specials,
    never ❓ — specials always need Yan while policy=guard)
  - Every autonomous run posts the apply-report summary (Section 4);
    digest setting controls cadence.
- **API**: token (exists) · Hermes Bot card stays as is.

Implementation note: `librarian_auto_apply_tier_a` is replaced by
`librarian_auto_apply` (off|safe|all) + the bot-side loop honours it:
after the gate, a proposal in an allowed class applies immediately
and reports; anything else waits for Yan. The app enforces class
rules server-side at apply time (specials excluded under guard
regardless of the bot's claim).

## 7. Automation ladder (how trust is earned)

1. **Manual** (now): bot proposes, Yan approves in DM, bot applies.
2. **Assisted**: Yan flips Auto-apply = Verified-safe; unambiguous
   classes (clear SxxEyy, two-source agreement, no specials) apply
   alone + summary. Everything else still asks.
3. **Trusted**: Auto-apply = All gate-passed; only ❓/⛔ reach Yan,
   as a digest. Rollback stays one tap away on every fix.

Each step is a Settings change Yan makes — the bot never promotes
itself, and dry-run remains the master brake.

## 8. Build order (each phase: tests + pristine proof + deploy + live proof)

- **Phase 0** — v4.1 hardening finish (Section 1): tests, deploy,
  live proofs (packet numbers, echo rejection, specials rejection).
- **Phase 1** — Profile pack: SOUL rewrite, MEMORY/USER seeding,
  6 skills + references, config narrowing, CLI unchanged.
- **Phase 2** — Website control room (Section 5) + alias endpoints.
- **Phase 3** — Clean Settings reorganization + real automation
  settings (Section 6) + server-side class enforcement.
- **Phase 4** — Digest cron + automation ladder live test with Yan:
  one assisted run, reviewed together, before any trusted mode.

## Open questions for Yan
1. Digest default: per-run summary (recommended) or daily?
2. In Assisted mode, may gate-passed MOVIES auto-apply too, or
   series only at first?
3. Bangumi lookups need web access from the bot — the profile already
   has web tooling via Hermes; confirm that's acceptable for structure
   checks (captions only, no personal data).

---

# v5.1 ADDENDUM (2026-11-19 ~19:57 IST) — full media-type scope

Yan's correction: v5 was anime-brained. The library is five channels of
EVERYTHING, and every media type has its own naming culture. The bot's
first step on any run is now: classify the media type, then follow that
type's playbook. The skills library grows from 6 to 10:

7. **movie-naming** — year disambiguation; remakes with identical
   titles; editions (Director's Cut / Extended / Unrated / IMAX /
   Remastered) as non-recoverable info that must survive in captions;
   franchise/collection ordering; multi-part films; dubbed vs subbed.
8. **tv-naming** (Western TV) — season packs; multi-episode files
   (S01E01-E02); aired vs DVD vs absolute orderings; date-based daily
   shows; miniseries (filed as TV by TVDB, as movies elsewhere);
   Part 1/2 finales; series-name aliases and year-suffixed remakes.
9. **cartoon-naming** — segment episodes (11a/11b); double-length
   episodes split differently by TVDB vs TMDB; production vs aired
   order; the general numbering chaos of Western animation.
10. **drama-naming** — K-drama / C-drama / Indian content:
    romanization variants with no canonical English form; episode
    counts that differ per provider; title translations; weekend vs
    daily drama numbering.

The existing skills (release-naming, anime-naming, donghua-naming,
telegram-uploaders, provider-research, librarian) stand; the librarian
skill gains the classify-first step and per-type branches.

## Settings honesty fixes (from Yan's screenshot, 2026-11-19 19:50 IST)

- The **API token field must be masked** (password-style with reveal),
  never plaintext on screen.
- The Phase-1 **auto-apply toggle is cosmetic** — it must not pretend.
  Phase 3 replaces it with the real `librarian_auto_apply` ladder;
  until then its label will say it is not yet active, or it is removed.
- The Librarian card's Advanced sprawl is replaced by the grouped,
  properly collapsed layout of Section 6.
