# Librarian v2 — Build Spec

Status: Phase 0 deliverable. Approved direction: Yan, 2026-11-20 ("Start", on the
presented defaults: in-app module; migrate aliases + fix history; command-centre
page; guardrailed full-power bot autonomy). Design source: the ground-up proposal
+ self-audit delivered 2026-11-20 (chat) and the research report in
`~/workspace/research_notes/librarian-ground-up-redesign-research-20261120-0505/`.

This spec is the contract for the build. Changes to it are recorded as amendments
at the bottom, with reasons.

## 1. Purpose and non-goals

Replace the Librarian (v1: `Backend/helper/librarian.py`, Mongo tracking
collections, packets/proposals) with a series-first system:

- Work is organised around **series**, not around a pile of failed files.
- The Media Manager bot acts only through **Work Orders** — scoped, budgeted,
  expiring capabilities — with plan → (approve) → apply and one-click undo.
- Identification is resolved **once per series**, scored by the server from
  evidence, and slotted locally.
- The admin page is fast by budget (≤50 KB first load, 0 third-party origins,
  ≤100 ms p95 server time on the Narzo) and never renders a silent blank.

Non-goals (explicit refusals): no changes to streaming, DM Link, the DL pool,
MongoDB schemas, or the app's ingest code; no new daemons; no SPA framework;
no subtitle work; no separate app/process.

## 2. Architecture

New package `Backend/librarian_v2/`, one module per concern:

| Module | Responsibility |
|---|---|
| `store.py` | SQLite access; the only module that talks to the DB |
| `identify.py` | Series resolution ladder, provider clients/cache/breakers, slotting, evidence scoring |
| `orders.py` | Work Orders, plans, tiers, apply, undo |
| `jobs.py` | Leased job runner + reaper |
| `sync.py` | Read-only mirror of new failures from Mongo into `media_file` |
| `api.py` | Bot-facing endpoints (the 5 tools + read-only search) |
| `page.py` | The six admin pages + SSE stream, own shell template |

Data lives in a single SQLite file (WAL mode), path from the
`librarian_v2_db_path` setting, default `<app root>/librarian_v2.sqlite3`.
MongoDB is read (sync mirror, gate, verify lookups) through the app's existing
functions and written by nothing in v2. Caption fixes still happen by the
Media Manager bot editing the Telegram caption; the app re-indexes natively,
exactly as in v1.

Seams into the existing app (complete list — anything beyond this needs a spec
amendment): (a) route registration for `api.py` in `api_routes.py`;
(b) route registration for `page.py` in `template_routes.py`; (c) one settings
card in `settings.html` + defaults in `settings_manager.py`; (d) the jobs
ticker started from the existing automations ticker hook (30 s), guarded by
`librarian_v2_enabled`.

## 3. Data model (DDL: `Backend/librarian_v2/schema.sql`)

- `source_channel` — served channels; `trust_tier` (manual/auth/skip/dl),
  `default_rule` (allow/ask/deny).
- `series` — canonical title/year; `ordering`
  (aired/dvd/absolute/tmdb/manual); owning `provider` + `provider_id`,
  `imdb_id`; `status` (identified/unidentified/conflicted/manual);
  **materialised counters** `episode_count_cached`, `file_count_cached`,
  `missing_count_cached`, updated in the same transaction as any assignment.
- `series_alias` — learned abbreviations; `UNIQUE(alias_normalised, kind)`.
- `season` — `UNIQUE(series_id, season_number)`; season 0 = specials, always.
- `episode` — the slot; `UNIQUE(series_id, season_number, episode_number)`;
  carries `absolute_number`, title, air date.
- `media_file` — one row per Telegram file: channel/message refs,
  `file_unique_id`, **immutable `original_caption`**, `current_caption`,
  parsed bits, dimensions/size, `match_status`
  (unmatched/proposed/matched/conflicted/ignored), nullable `series_id` /
  `episode_id`, `is_primary`, `evidence_score`, `match_method`,
  `location_status` (located/unknown).
  - `series_id IS NULL` **is** "unidentified". Partial index on
    `(source_channel_id) WHERE series_id IS NULL` makes channel-scoped
    unidentified an index scan.
  - `UNIQUE(episode_id) WHERE episode_id IS NOT NULL AND is_primary = 1` —
    one primary file per slot; duplicates are versions, never overwrites.
- `work_order` — the bot's capability: `scope_kind`
  (series/channel_unidentified) + exactly one scope FK; `state`
  (opened/planning/planned/applying/verifying/done/paused/killed/expired);
  `max_files`, `budget_applies`, `auto_floor`, `approval_floor`,
  `cursor_episode_id` (resume point), `opened_by` (owner/bot), `expires_at`.
- `plan` — one locked artefact per order revision: `plan_hash` (sha256 of the
  canonical item list), `state` (draft/locked/approved/applied/void).
  Applying executes the artefact whose hash was approved; any change voids it.
- `plan_item` — file, target episode, `new_caption`, `evidence_score`,
  `score_breakdown` (JSON, auditable), `tier` (auto/approval/refused),
  `refusal_reason`; `UNIQUE(plan_id, media_file_id)`.
- `job` — the leased queue: `kind`, JSON `payload`, `state`
  (ready/running/snoozed/dead/done), `claimed_by`, `lease_expires_at`,
  `heartbeat_at`, `attempts`, `max_attempts`, `next_run_at`, `last_error`.
- `caption_revision` — undo log: before/after, actor, work order,
  `reverted_at`. History is reverted, never deleted.
- `provider_cache` — key, payload, `expires_at`, `negative` flag.
  TTLs: series/episode lists 30 d, searches 7 d, negative results 24 h.
- `sync_state` — mirror watermarks (last seen ingest id/timestamp per source).

All timestamps are UTC epoch seconds (INTEGER). All text is UTF-8; normalised
alias/stem forms are produced by one function in `identify.py` and nowhere else.

## 4. Identification and evidence scoring

Resolution ladder (cheapest first): explicit provider ID in caption/filename →
`series_alias` hit → provider cross-ID lookup (TMDB /find by IMDb/TVDB ID) →
title+year search with fuzzy scoring (RapidFuzz token_set / partial / WRatio).
One provider owns a series' numbering; others enrich only. The episode list is
fetched once, cached 30 d, and files are slotted locally: SxxEyy → absolute →
air-date → channel sequence. Per-provider circuit breakers: after 3 consecutive
failures a provider is skipped for 10 min; jobs needing it snooze, the chain
falls through, nothing retries in a hot loop. A shared rate budgeter serialises
all provider calls (bot + background jobs).

**Evidence score is computed by the server, never supplied by the bot.**
Components (stored per plan item in `score_breakdown`):

| Signal | Points |
|---|---|
| Explicit provider ID in the file's own caption/name | 1.00 |
| Alias-table hit (previously confirmed) | 0.97 |
| Two providers agree on the same series ID | 0.95 |
| Single provider + year match + fuzzy ≥ 0.92 | 0.90 |
| Single provider + fuzzy ≥ 0.92, no year evidence | 0.84 |
| Single provider + fuzzy 0.85–0.92 | 0.78 |
| Episode-list fit bonus (file's episode exists in the list) | +0.03 |
| Providers disagree on the series | cap 0.50, series → conflicted |

Tiers: score ≥ `auto_floor` (default 0.95) → auto tier; ≥ `approval_floor`
(default 0.80) → approval tier; below → refused, with the reason recorded.
Manual-map series (§5) score their slotting at 0.97 (owner/bot-confirmed map).

## 5. Manual episode maps

When no provider's numbering matches a series' files (donghua splits are the
known case), the bot proposes a slot↔label map; it takes effect only via the
owner's decision on the compare card (or an owner-opened order). The series'
`ordering` becomes `manual`, `status` becomes `manual`, and the stored map is
the authority for slotting that series thereafter.

## 6. Work Orders, plans, apply, undo

- The bot's token is read-only by default. Mutation exists only inside a live
  Work Order, and every apply re-checks scope server-side: a file outside the
  order's scope is refused (403) regardless of what the bot asks.
- Plan flow: bot submits items → server validates each (file in scope, target
  slot exists, new caption re-parses via the app's own parser to the claimed
  slot, specials guard, evidence score + tier) → plan locks with its hash →
  auto-tier items may apply immediately if the order allows; approval-tier
  items wait for the owner's tap (page review shows all below-tier items +
  a random sample of 10 auto-tier items, labelled as a spot-check).
- Apply (per file, via jobs): claim (lease 120 s, heartbeat 30 s) → live
  identity re-check on Telegram → Media Manager bot edits caption → native
  re-index → verify via `db.get_media_ids_by_part` → in ONE SQLite
  transaction: caption_revision row, counters, job done. Verify failure →
  caption rolled back from the revision, job reports the reason. Pacing:
  per-channel 1.0 s + global 0.34 s (v1 engine values); FLOOD_WAIT snoozes
  the pool for `retry_after` without consuming an attempt.
- Undo: per file or per whole order — restores `before_caption` via the same
  edit path, marks revisions reverted, counters corrected in the same
  transaction. Undo is itself audited.
- Kill switches: global (`librarian_v2_enabled` off / bot state killed) and
  per-order; budgets enforced server-side (order `budget_applies`, daily
  `librarian_v2_daily_budget`).

## 7. Jobs engine

One `job` table; claim = single `BEGIN IMMEDIATE` transaction selecting the
oldest due `ready` job and stamping claim/lease. Reaper runs on the ticker:
expired leases → `ready` (attempts unchanged; a crashed worker's job is
indistinguishable from a slow one, and idempotent handlers make re-execution
safe). Outcomes: done / retry with backoff (attempts+1; dead at
`max_attempts`, default 4) / snooze (no attempt consumed; provider
Retry-After, flood waits) / dead (visible on the page, owner-requeueable).
Job kinds: `apply_caption`, `verify_fix`, `identify_series`, `sync_mirror`,
`reconcile`, `digest`, `backup`. Handlers must be idempotent — this is a
reviewed invariant, with the chaos test (Phase 1 gate) killing a worker
mid-apply and asserting recovery.

## 8. Bot API (all under `/api/librarian2`, header `x-librarian-token`)

| Tool | Call | Returns |
|---|---|---|
| series | `GET /series?name=` | series card: identity, ordering, counters, episode map, open order + cursor |
| open_order | `POST /orders` {scope, limits} | order id + effective limits |
| plan | `POST /orders/{id}/plan` {items[]} | per-item validation, tiers, refusals; plan hash |
| apply | `POST /orders/{id}/apply` | accepted; progress via report/SSE |
| report | `GET /orders/{id}/report` | **live progress at any moment**: state, cursor, items planned / applied / verified / failed / remaining + percent, budget used, fixed/gaps/conflicts, needs-eyes list. Callable mid-apply, not just at the end — the bot always knows exactly how much of its work has landed (Yan's requirement, 2026-11-20) |
| search | `GET /search?q=&scope=` | read-only series/file search |

The v1 CLI (`tools/librarian_cli.py`) gains a v2 mode in Phase 3; the bot's
librarian skill is rewritten thin against these five tools. The bot's SOUL.md
and MEMORY.md (Yan's own files) are never touched by this build.

## 9. Page (own shell — does NOT extend `base.html`)

Shell: `templates/librarian2/base.html` — hand-written CSS (~≤15 KB), vendored
`htmx.min.js` (~14 KB gz), zero third-party origins, far-future cache headers
on static assets. Pages: **Home** (counters, active orders with progress,
needs-eyes count, kill switch), **Series** (search + progress list),
**Series detail** (episode grid: slot × file, missing/conflict states,
manual-map editor entry), **Unidentified** (channel filter, manual picking),
**Work Order** (plan review with sampling, approve/apply, live progress,
undo), **Audit** (revisions, revert buttons, dead-letter jobs + requeue).
Progress: one SSE stream per page (`/admin/librarian2/events`,
`Last-Event-ID` resume, 15 s heartbeat comments) with an
`hx-trigger="every 5s"` fallback if the stream drops. Any fragment error
renders a visible error state with retry — never an empty section.
Lists: 50 rows, keyset pagination. Budgets (Phase 4 gate, measured on the
Narzo): first load ≤50 KB total, server p95 ≤100 ms, usable on LAN ≤500 ms.

## 10. Settings (all on the Settings page — no backend-only fields)

`librarian_v2_enabled` (default **false** — v2 ships dark), `librarian_v2_db_path`,
`librarian_v2_daily_budget` (default 5000), `librarian_v2_auto_floor` (0.95),
`librarian_v2_approval_floor` (0.80), `librarian_v2_dry_run` (default true until
cutover), `librarian_v2_auto_window_enabled` (false),
`librarian_v2_auto_window_hours` (""), `librarian_v2_auto_window_budget` (200),
`librarian_v2_specials_policy` (guard/allow, default guard).
Per-channel defaults live on `source_channel.default_rule`, edited from the
Librarian page's Library-data area and mirrored to Settings.

## 11. Sync mirror (ingest — zero streamer changes)

`sync_mirror` job every 5 min: reads new reject/skip records from Mongo
(the app's existing tracking collections, read-only) past the stored
watermark and upserts them into `media_file` (unmatched, location from the
locator data when present, else `unknown`). It never writes Mongo. A file
whose caption gets fixed outside v2 (manual edit in Telegram) is reconciled
by the `reconcile` job comparing `current_caption` against the live caption
for recently touched files.

## 12. Migration (Phase 5, read-only import)

Sources: v1 `alias_map` → `series_alias`; `curation_audit` + fixed ledger
items → opening `caption_revision` history + matched `media_file` rows where
identity is known; waiting ledger items → unmatched `media_file` rows,
locations joined from the locator collection (ledger docs carry no msg_id);
location-less items enter `location_status=unknown` and are resolved by a
fresh skip-pile harvest into the v2 model. Scan records (indexed/unindexed)
do NOT migrate — re-derivable state. The import prints before/after counts
per category and must reconcile exactly or it does not ship. v1 code and its
Mongo data are not modified by the import; v1 remains fully runnable until
Yan orders its removal after cutover.

## 13. Testing (production-shaped — the standing lesson)

- Test doubles mimic production shapes: collection handles are **fresh
  objects per call** (the counts-cache lesson), module imports are the real
  ones (the L3 lesson), accessors are the real ones.
- SQLite tests run against the real `schema.sql` on a temp file with WAL on.
- Telegram is a recording fake (scriptable failures, flood waits, identity
  drift). Providers are scripted stubs with an outage mode.
- Every production bug found live earns a regression test before its fix
  ships. The suite is `python3 tests/run_all.py`; v2 tests live in
  `tests/librarian_v2/` and are added to the runner's module list and to
  `tools/reapply.sh` PLUGIN_FILES in the phase that introduces them.

## 14. Phases and gates (from the Final Plan, 2026-11-20)

0. **Spec + harness** — this document, `schema.sql`, `tests/librarian_v2/`
   harness + smoke tests. Gate: Yan approves the spec.
1. **Core** — `store.py`, `jobs.py`, counters. Gate: suite green incl. the
   chaos test (kill worker mid-apply → lease recovery, no stuck claims).
2. **Identification** — `identify.py`, scoring, breakers, manual maps.
   Gate: shadow run vs live library; accuracy diffed against the bot's real
   v1 fixes; report to Yan.
3. **Work Orders + bot** — `orders.py`, `api.py`, CLI v2 mode, thin skill.
   Gate: one full series end-to-end on the device staging clone.
4. **Page** — `page.py`, shell, six pages, SSE. Gate: budgets measured on
   the Narzo (see §9).
5. **Migration + cutover** — §12 import, parallel run, proven series, switch.
   Gate: Yan's word. Rollback at every phase: `librarian_v2_enabled=false`
   + v1 untouched.

Deployment follows the fork's delivery model throughout: v2 files join
PLUGIN_FILES; seams (§2) join `patches/integrate.patch`; the pristine-reapply
byte-compare + full suite is the release proof for every phase.

## 15. Risks and open items

- SSE through the Tailscale serve proxy may buffer; Phase 4 validates on
  device, fallback polling already specified.
- Evidence-score weights (§4) are starting points; Phase 2's shadow run
  tunes them against real fixes before any auto-apply is possible.
- SQLite single-writer: apply concurrency is bounded by Telegram's own
  ~1 edit/s/channel, far below any SQLite contention at this scale; WAL
  keeps page reads unblocked during writes. Phase 1 load-test asserts
  page-read latency during an active apply run.
- TMDB/TVDB rate limits unverified from official docs; the design is
  insensitive (shared budgeter + snooze on Retry-After).

## Amendments

1. 2026-11-20 (Phase 1, at Yan's question): §8 `report` made explicitly a
   **live progress** read — planned/applied/verified/failed/remaining +
   percent + budget used, callable at any time during an apply, so the bot
   can always see how much of its work has been applied. Progress derives
   from plan_item/job states and caption_revision counts; no extra storage.
