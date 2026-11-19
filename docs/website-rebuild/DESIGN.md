# Website Rebuild — Architecture (PARKED, approved direction)

Status: Yan approved the direction 2026-11-20 ("really good idea to
rebuild the whole thing better") but parked the whole-website scope.
**For now only the Librarian + automations + Hermes-agent parts are
being built** (see docs/librarian/DESIGN-v5.md lineage and the L1–L6
phase list). This document preserves the full design for later.

## Verdict

Keep FastAPI + Jinja (right weight for a phone host). No SPA, no
build pipeline. Three structural changes:

1. **Speed layer** — pages render only from the local DB. Aggregates
   are precomputed (MongoDB Computed Pattern: cheap `$inc` on write +
   scheduled recompute as source of truth, `computed_at` stamp).
   Channel names from the settings cache, refreshed in the
   background. Rule: an outbound call during a page render is a bug
   (stale-while-revalidate + explicit Refresh instead). HTMX (~14 KB,
   no build step) for fragment updates; lists paginate 25–50 with
   click-to-load; heavy panels lazy-load.
2. **Feature registry** — every feature declared once (key, section,
   label, help, type toggle/select/multi/slider/number/text, default,
   options, enforcement point), modelled on Nextcloud's declarative
   settings. One generic settings store + one validated write
   endpoint; the Settings UI is generated from the registry.
   Enforcement in three places: nav omission, route refusal, and
   jobs/agent checks via `feature_enabled(key)`. Existing settings
   migrate with their stored values as defaults. A feature may
   declare a custom template block as an escape hatch.
3. **Automations + agent management** — APScheduler
   (AsyncIOScheduler, in-process; job definitions in code, user
   overrides in a `jobs` collection, runs appended to capped
   `job_runs`; misfires coalesce, no self-overlap). Tasks UI in the
   Sonarr/Jellyfin pattern: toggle, schedule preset, Run now, last /
   next run, run history. Agent panel = five controls: status
   header, Pause vs Kill (server-enforced), per-scope
   allow/ask/deny permission matrix (+ budgets), approval queue for
   irreversible actions only (with expiry), append-only audit feed.

## Build order (when unparked)

1. Speed layer site-wide. 2. Registry + generated Settings.
3. Automations engine + Tasks UI. 4. Librarian v7 (incremental
scans, per-channel policies, page modules, pre-identification).
5. Hermes agent panel.

Research basis: session research 2026-11-20 (HTMX, MongoDB Computed
Pattern, Nextcloud declarative settings, Home Assistant config
entries, APScheduler guide, Sonarr/Jellyfin tasks, n8n human review,
OpenAI Agents SDK approvals, Claude Code permission model).
