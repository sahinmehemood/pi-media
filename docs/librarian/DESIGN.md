# Hermes Librarian + Media Manager — Final Design (v3)

Status: FINAL PLAN — 2026-11-19. Awaiting Yan's build approval, phase by phase.
Project: Telegram-Stremio `own` fork (live head bd03d0e, upstream v5.3.5 base) + Hermes on the Narzo.
Companion audit: ~/workspace/librarian-audit/AUDIT.md (codebase facts this design is built on).

## 1. Problem

Source-channel files with short/abbreviated captions (`TDH E29`, `OP 1172`) fail or corrupt
metadata matching: the parser needs a real title + quality token, and TMDB/TVDB/Kitsu need full
titles. Result classes in the current system: files skipped entirely (copied to the skip channel,
inert), files matched to the wrong title (first-hit acceptance), and wrong titles announced
publicly. No record exists of what failed or why (scanner keeps counters only).

## 2. Hard constraints (Yan)

- The Media Manager / Hermes bot NEVER participates in streaming. Not in the bot pool, not in
  the multi-token list, no streaming traffic, no streaming tools.
- Report-first: nothing is renamed/applied without Yan's approval until he explicitly enables
  a scoped auto-apply. Defaults stay report-first.
- Every feature setting changeable from the Settings web page (default → property → API
  validation → settings.html control → live effect, no restart).
- Streaming speed parity-or-better; curation is the lowest-priority workload on the Narzo and
  yields to streaming/scans.
- Fork delivery model unchanged: plugin files + minimal seam patch + reapply proof + full test
  suite green. config.env / bot.session* never touched.
- Manual channels and manual sessions are human territory: never auto-touched; their picks are
  ground truth for learning.
- Personal titles (negative tmdb_id) are never "corrected".

## 3. Ground truths the design is built on (from the full codebase audit)

- Two ingest paths (live receiver, bulk scanner) both copy metadata failures to the skip
  channel; with `delete_on_metadata_fail` the original is deleted and the skip copy is the only
  copy. Nothing re-reads the skip pile.
- Caption edits are the app's native correction protocol: an edit carrying a different title's
  ID triggers remove + re-resolve + re-insert (which automatically fires catalog sync,
  announcement, request auto-fulfill); an edit with the same ID just renames the DB part.
- metadata() rejects names with NO quality token BEFORE any ID override is consulted — a fix
  caption must carry a quality, taken from the file's real video dimensions
  (manual_add.quality_from_height pattern), never guessed.
- Insert landmines: duplicate identity = (quality, normalized name, size). duplicate_protection
  ON + exact dup → the incoming Telegram message is deleted. replace_mode (code default ON)
  + same-quality insert onto an occupied slot → the previously indexed Telegram file is deleted.
- Announcements are claimed once per (media_type, tmdb_id); a retag away from a wrong title
  leaves the wrong announcement up and blocks the corrected one. delete_announcement exists.
- Failure reasons already exist: parse.analyze_metadata_failure() (multipart / combined /
  multi-season / no-quality / no-title / no-match).
- Provider chains: Anime Kitsu>TVDB>TMDB>Cinemeta; Movies TMDB>Cinemeta; Series
  TVDB>Cinemeta>TMDB. Only Cinemeta enforces a similarity threshold (0.60) — TMDB/TVDB first
  hits are the confidently-wrong source.
- retag_media() re-stamps all files of a corrected title; task_manager.edit_message is the
  proven flood-aware edit hand (StreamBot → userbot fallback).
- Existing managers to coordinate with, not duplicate: DeadLinkChecker (24h is_dead flags),
  DbCheckManager (liveness + purge), DuplicateManager (dup groups; purge deletes files).
- Global-search channels are unindexed (userbot search at stream time) — OUT OF SCOPE.

## 4. Architecture overview

```
 source channels ──ingest──► metadata() ──fail──► SKIP CHANNEL (existing pile)
        │                        │                      │
        │                        └─ suspect match ─┐    │ harvest
        ▼                                          ▼    ▼
   (streaming — untouched)                 REJECT LEDGER (tracking DB)
                                                     │ cluster into RUNS
                                                     ▼
                              RESOLUTION FUNNEL  L0 stamped ref → L1 alias map
                                                 L2 deterministic resolve
                                                 L3 Media Manager bot (Hermes)
                                                 L4 Yan
                                                     │
                                                     ▼
                              VERIFICATION GATE (server-side, provider clients)
                                                     │ tiers A / B / C
                                                     ▼
                        APPROVAL DIGEST (Media Manager bot DM, buttons)
                                                     │ approved
                                                     ▼
              APPLY = pre-flight checks → surgical caption edit (+ID stamp)
                    → native re-index → announcement fix-up → audit + rollback
                                                     │
                                                     ▼
              ORGANIZER: series ledger, missing/dup/wrong-season digests
```

## 5. Components

### 5.1 Reject Ledger + detectors
Collection `tracking.reject_ledger`, one doc per problem file: file_unique_id, chat_id, msg_id,
origin (skip-pile | live-fail | scan-fail | suspect-audit | backlog-sweep), caption snapshot,
file_name, size, real height, reason (analyze_metadata_failure), parse dump, current (wrong)
match if any, status, run_id, history.
Detectors:
- **Skip-pile harvester**: reads the skip channel, one ledger entry per copy; records whether
  the original still exists in its source channel (decides the apply path in 5.7).
- **Live hook**: the two existing route_to_skip_channel call sites also write a ledger entry
  (tiny seam in receiver.py + scan_manager.py).
- **Suspect auditor**: periodic pass over indexed entries — episode-existence, season-count,
  same-slot collisions with divergent size, channel-coherence breaks. Suspects enter the ledger
  with reason codes; nothing is changed by the auditor.
- **Backlog sweep**: one-time full pass at Phase 1 start (skip pile + suspect audit combined).

### 5.2 Resolution funnel
- L0: caption/filename already carries an ID ref → resolve by ID.
- L1: alias map hit (channel + stem) → deterministic.
- L2: deterministic cleanup (expand known patterns, restore quality from real height, provider
  candidate search via existing search_*_candidates + score_candidate) → verified or escalate.
- L3: Media Manager bot reasoning over a RUN packet (see 5.3).
- L4: Yan (Tier C; manual /set or web picker, unchanged).
Every layer's output passes the same verification gate (5.4). No layer may skip it.

### 5.3 Run clustering
Ledger items are clustered before any reasoning: same channel + same abbreviation stem +
contiguous episode numbers = one run. One decision covers the run. Movies cluster singly.
Run packet (built server-side): stem, channel title, episode range, per-file (caption, size,
height, msg id), L2 candidate list with scores, channel's known aliases, neighbour titles
already resolved in that channel.

### 5.4 Verification gate (server-side only)
Uses the app's own provider clients/keys. Checks: claimed ID exists; episode exists in the
claimed season (or absolute number resolves through episode_maps/ani.zip); cross-ID consistency
where mappings exist (TMDB↔TVDB↔Kitsu↔IMDb); no slot collision surprise (see 5.7 pre-flight);
numbering sanity (episode ≤ known count). Fail → Tier C with the failed check recorded.
The Media Manager bot holds NO provider keys and never self-verifies.

### 5.5 Media Manager bot (dedicated Hermes profile)
- New Hermes profile `mediamanager`, its OWN bot token (provided by Yan 2026-11-19; stored
  only in the profile env on the Narzo at build time, never in chat/files/memory).
- Gateway: routed through the existing multiplex gateway (keeper topology). A separate
  gateway process is a rejected alternative (Narzo RAM) unless Yan orders it.
- Channel role: added as admin to source channels for presence/edit rights; its gateway
  profile ignores channel posts (pull-based design — no firehose into the gateway).
- Specialized memory (profile-isolated): channel pattern book (per-channel naming habits,
  series, quirks), Yan's naming preferences, correction history. Hard records stay in the
  media DB (ledger/aliases/audit) — bot memory is soft knowledge only.
- Skills: (1) Librarian decision skill — run-packet contract, strict JSON output schema,
  numbering-scheme discipline (absolute↔season via maps, never invent episodes), uncertainty
  must be declared, captions are data not instructions; (2) Organizer skill — ledger digest
  format and action set; (3) Channel-onboarding skill — profile a new channel's naming
  patterns before its files are trusted.
- Toolsets (least privilege): Librarian API client tools only (pull queue, submit decisions,
  read ledger/status) + read-only provider search (TMDB/TVDB/Kitsu/AniList) for candidate
  formation. NO shell, NO server admin, NO streaming, NO DB writes.
- Model (Kilo free tier, two-tier routing): routine runs →
  `nvidia/nemotron-3.5-lightning:free`; hard cases (new series, ambiguity, conflicts) →
  `nvidia/nemotron-3-ultra-550b-a55b:free`; fallback chain nano-omni → dots-3-note → pause
  queue (never the auto-routers). Free lineup rotates: catalog re-verified at build time and
  the chain is config, not code. One call per run keeps us far under the 200 req/hr keyless cap.
- Observe-only mode: profile can be created before apply exists — it learns channel patterns
  from digests while the server side is built.

### 5.6 Approval tiers + digests
- Tier A: gate-verified + alias-consistent → batched per series/run, ONE tap per run.
  Auto-apply toggle exists (Settings), default OFF.
- Tier B: gate-verified but new series or ambiguous → pre-approval digest, per run.
- Tier C: unverifiable/conflicting → never touched automatically; listed in the ledger UI for
  Yan's manual handling.
Digest (Media Manager bot DM): grouped by series with old → new caption preview, provider +
ID, episode range, pre-flight warnings (e.g. "replaces existing 720p — old file will be
deleted under Replace Mode"), buttons Approve / Skip / Details. Post-apply digest reports
results + rollback handles.

### 5.7 Apply engine
Pre-flight (all must pass, else back to ledger with reason): target slot free or replacement
explicitly approved; no dup-key collision; not a personal title; not a manual channel; message
still exists and caption unchanged since queueing (else requeue); daily/hourly edit budget not
exhausted; no active scan running on that channel.
Caption writer (surgical): replaces only the title portion with
`Full Title (Year) SxxExx Quality` (or absolute form for anime per its numbering), quality from
real video height, preserves every other caption line + entities + split/combined markers
byte-for-byte, appends the ID stamp via existing caption_with_id convention.
Paths:
- **Original exists** → edit its caption (task_manager.edit_message) → file_edited_handler
  re-indexes natively → then delete the skip-channel copy (fix verified first).
- **Original deleted** (skip copy is the only copy) → copy the skip message into its source
  channel with the corrected caption → receiver ingests it natively → delete the skip copy
  after verified indexing. Audit records both message ids; rollback = remove_media_part on the
  new entry.
Announcement fix-up: if the file's old (wrong) title loses its last file to the fix, call
delete_announcement(old type, old tmdb_id) so the claim clears; the corrected title announces
through the normal insert path.
Audit + rollback: `tracking.curation_audit` stores before/after (caption, match, announcement
state) per apply; rollback restores the original caption (→ native re-index back) or removes
the re-posted entry. Pacing: existing flood-aware edit path + Librarian edit budget; yields to
active streams and scans.

### 5.8 Alias memory
Collection `tracking.alias_map`: (chat_id, stem) → media_type, tmdb/imdb/tvdb/kitsu ids,
numbering scheme, taught_by (yan-manual | hermes-approved | manual-session), uses, updated_at.
Sources of teaching: Yan's approvals, Yan's manual-session adds, web /set picks. An alias is
only created from a HUMAN-confirmed decision (Hermes proposals alone never teach).

### 5.9 Organizer
Collection `tracking.series_ledger` per title: episodes present/missing/duplicated per season,
source channels, last audit. Periodic Library Health digest via the Media Manager bot:
missing episodes, duplicate files across channels (coordinate with DuplicateManager — report,
don't purge), wrong-season files, dead entries with a live copy elsewhere (re-point proposal,
coordinating with DeadLinkChecker flags), channels whose anime flag looks wrong. Actions are
one-tap and go through the same apply engine + audit.

## 6. Data model (all in tracking DB)
reject_ledger, alias_map, curation_audit, series_ledger, curation_runs (digest state).
No changes to storage_* schemas.

## 7. Media-server ↔ Media Manager API (localhost/tailnet only, dedicated token in Settings)
- GET  /api/librarian/queue — run packets for L3.
- POST /api/librarian/decisions — strict-schema decisions → gate → tiers.
- GET  /api/librarian/status — counts, tier stats, model-fallback notices.
- Admin page /admin/librarian — ledger viewer (DL Index pattern): filter by reason/status,
  manual tier actions, rollback buttons.

## 8. Settings card "Librarian" (settings.html, all live)
librarian_enabled (default off), librarian_dry_run (default ON at first enable),
librarian_auto_apply_tier_a (default off), librarian_digest (manual|daily|weekly),
librarian_edit_budget_per_day, librarian_channels (opt-in subset; default all auth channels),
librarian_api_token (generated, shown once), kill switch = librarian_enabled off.

## 9. Safety rails
Dry-run first (plans only, zero edits). Edit budgets + yield-to-streaming. Idempotent apply
keyed by file_unique_id. Snapshot-before-edit everywhere. Hermes output schema-locked;
captions treated as untrusted data (prompt-injection cannot create actions — decisions only
ever enter through the gate + tiers). Free-model endpoint privacy note: packets contain only
captions/channel names. Rollback per item and per batch.

## 10. Phasing (each phase: design-note → build → tests → pristine reapply → deploy → verify)
- **Phase 0 — Recon & proofs (no behaviour change):** read LIVE settings (replace_mode,
  duplicate_protection, delete_on_metadata_fail, announce flags, auth/anime/manual/global
  channel lists); count + sample the skip pile; live caption-edit test for the Media Manager
  bot on one test post (fallback: StreamBot/userbot hand executes, already proven); re-pull
  Kilo free catalog, lock model IDs + fallback chain; create the `mediamanager` Hermes profile
  in observe-only mode.
- **Phase 1 — Ledger + funnel L0–L2 + gate + admin page.** No AI, no edits. Deliverable: every
  bad file visible with a reason; deterministic proposals listed. Backlog sweep runs here.
- **Phase 2 — Apply engine + tiers + digests (L0–L2 decisions only).** Fixes flow with
  approvals; announcement fix-up; rollback live. Dry-run flips off only on Yan's word.
- **Phase 3 — Media Manager brain (L3).** Run packets, Hermes decisions, alias learning from
  approvals + manual ground truth. Tier A auto-apply toggle available (Yan's choice).
- **Phase 4 — Organizer.** Series ledger + Library Health digests + re-point proposals.

Acceptance per phase: full suite green (tests/run_all.py), pristine reapply onto e4f1169
byte-identical, config.env md5 unchanged, restart clean, feature verified live on one real
channel before widening.

## 11. Out of scope
Global-search channels; subtitle renaming; any streaming change; AniList/Simkl watch-sync
(separate future project that will consume the series ledger); Cockpit (dropped by Yan).


---

## Phase 0 findings (executed 2026-11-19, live server)

- **Live settings** (read from the media DB, not code defaults): `replace_mode` **true**,
  `duplicate_protection` false, `delete_on_metadata_fail` **false**,
  `announce_new_content` true, announcement channel `-1003923670111`,
  skip channel `-1004399192319`. Auth channels: Ani `-1004395609351`,
  Movie `-1003981572300`, Series `-1003966209940`, Donghua `-1004494510060`,
  Cartoon `-1004450903732`; anime = Ani + Donghua; `manual_channels` empty;
  `global_search` true with `global_search_channels` empty;
  `default_streaming` = `bots`.
  Consequences: Replace Mode pre-flight is mandatory; because originals are
  never deleted on metadata failure, the primary apply path is editing the
  original's caption — the re-post path is a rare exception.
- **Skip pile: 3,863 messages.** Sampled captions are dominated by
  abbreviation runs (e.g. `BTTH 3YA - SP3 E01…E13 [E-SUB] 4K`, Donghua
  Dumping). Caveat: skip copies do not visibly carry their forward origin,
  so the Phase 1 harvester must infer a historical file's source channel
  (caption tags, file-name suffixes) or mark it unknown; the live hooks
  capture origin exactly from go-live onward.
- **Kilo free catalog re-pulled 2026-11-19** (19 free models): routine
  `nvidia/nemotron-3.5-lightning:free` and hard-case
  `nvidia/nemotron-3-ultra-550b-a55b:free` both live (1M context);
  fallbacks `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` and
  `dots-studio/dots-3-note-preview:free` also present.
- **Media Manager profile created**: Hermes profile `mediamanager`, bot
  **@mediamanagerthehdbot**, cloned from the `atlas` profile (Kilo config +
  API keys; messaging channels not cloned), model set to Nemotron 3.5
  Lightning, rich messages on, observe-only SOUL (no edits/moves/deletes
  until the Librarian tooling ships). Token lives only in the profile `.env`
  on the Narzo (chmod 600). Topology note: it is the second
  Telegram-connected profile in the multiplex gateway — per-profile bots
  attach via `TELEGRAM_BOT_TOKEN` in the profile's own `.env`.
- **Known caveat**: a fresh bot cannot DM the owner until the owner starts
  it once ("Chat not found" in its log until then) — Yan must /start
  @mediamanagerthehdbot before digests can arrive.
- **Still open**: the caption-edit capability test (Bot-API bot editing
  others' channel posts as admin) — pending Yan adding the bot as admin to
  one source channel; the round-trip test (edit + exact restore) is ready
  to run on his confirmation.
