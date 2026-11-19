# Wave 2 — Design & Architecture

Status: decisions approved by Yan 2026-11-19 (flood alert = opt-in toggle
default off; Pre-Warm default profile = smart; DL Index = bot + web;
Cockpit = after Phase B). Audit-first: every item was checked against
the existing code so nothing already present is rebuilt.

## Audit corrections (things that already exist — NOT rebuilt)

- Per-bot stream visibility: admin Stream Analytics already records
  every finished stream with its primary bot, DC, bytes, speeds
  (database.log_stream_stats), and the dashboard shows live streams
  via /stream/stats. No telemetry system is built.
- The ONLY analytics gap: no source label. Stream records already
  store chat_id, so a read-time resolver labels even historical rows.
- The pool picker already balances by flood penalty + in-flight count
  and upstream maintains client_avg_mbps / client_failures /
  work_loads. Wave 2 refines, never replaces.
- Background-task pattern exists (DeadLinkChecker in __main__).
  Wave 2 avoids new __main__ seams anyway: sweeps start lazily from
  the owning plugin.
- Subtitles for library titles exist (helper/subtitles.py, sourced
  from channel files). DL sidecars are a different problem (DL files
  have no library media id) and are scoped accordingly.
- OWNER_ID exists in config — the flood alert has a recipient.
- channel_titles is already synced into settings — the Source
  resolver's title source.
- tools/reapply.sh + upstream-check CI exist — the Update Manager
  drives them instead of inventing an update mechanism.

## Cross-cutting rules

- Plugin-shaped delivery, same as Wave 1: features live in plugin /
  helper modules; upstream files change only via the seam patch,
  regenerated and re-proven (pristine reapply + full suite) per phase.
- Every new setting follows the existing pattern end-to-end:
  default in settings_manager, typed property, API validation,
  settings.html control, live effect without restart where the
  existing machinery allows it.
- Cross-module signalling without import cycles: stream_buffer
  exposes listener registration (set_flood_listener,
  set_progress_listener); plugins register callbacks at startup.
  With no listener registered, the hooks cost one None check.
- config.env / bot.session* are never touched by any phase.

## Phase A — Streaming engine (buffer plugin + analytics label)

### A1. Source column for stream analytics
- Read-time enrichment in api_routes.get_stream_analytics_api
  (already a seam file): resolve each record's chat_id via
  SettingsManager.channel_titles + dl_channel_id.
- Label: the channel title when known; type badge derived by
  comparing chat_id against the configured channel sets
  (dl / auth / manual / anime / announce / skip); positive chat_id
  (a user) = "DM · Global"; unknown = raw id.
- Templates: one SOURCE column in admin_dashboard.html (new seam
  file) and a source label in dashboard.html's live list (new seam
  file). Historical rows gain labels retroactively.

### A2. Flood-slot fix (buffered_streamer.fetch_one)
- Today: in_flight[pos] is held across `await asyncio.sleep(wait)`
  in the flood branch, so a penalised client blocks one of its own
  cap slots for the whole wait (up to the 300 s stream budget).
- Fix: the flood branch records the wait, lets the finally block
  release in_flight, sleeps OUTSIDE the hold, then loops to re-pick
  (possibly a different, unpenalised client). No other behaviour
  changes; MAX_FLOOD_TRIES accounting is unchanged.

### A3. DC-aware pool picker
- New DcSpeedTable in stream_buffer: per (client_index, dc_id)
  gated EWMA of GetFile throughput, same gating style as
  SpeedTracker. Fed in fetch_one on every successful fetch
  (bytes / send duration).
- pick_pool_pos ordering becomes: not shed → under cap → lowest
  flood penalty → lowest in-flight → highest DC speed for THIS
  file's dc_id. Flood safety stays dominant; DC speed only breaks
  ties among equally healthy clients.
- client_avg_mbps (upstream, per client) is untouched.

### A4. Seek-burst starts
- A stream whose initial offset > 0 (player seek or tail probe)
  initialises its AdaptiveWindow at the policy maximum; AIMD
  settles it within a few chunks. Registry gains
  buffer_seek_burst: true for observability. Sequential streams
  (offset 0) are unchanged.

### A5. Persistent chunk cache (DiskTier inside ChunkCache)
- ChunkCache gains an optional disk tier. Memory eviction spills
  the evicted chunk to disk instead of dropping it, up to
  buffer_disk_cache_mb (default 512, 0 = off). get(): memory →
  disk (promotes back to memory). One file per chunk named by a
  hash of the cache key; a JSON manifest (key → size, last touch)
  is rewritten lazily and on policy refresh; startup rehydrates
  the manifest and verifies file sizes — corrupt or missing
  entries are dropped silently (a miss, never an error).
- Location: <app dir>/.chunk_cache/. All operations are
  best-effort: any disk error degrades to the memory-only cache.
- Rationale: restarts currently cold-start the cache; the disk
  tier makes Pre-Warm (Phase B) survive restarts too.

### A6. Flood alert (opt-in, default off)
- stream_buffer.FloodAlerter: aggregates register_flood events
  (client, wait, stream title). First event after a quiet period
  notifies immediately; further events inside 10 min are batched
  into one summary. Only waits >= 10 s count (smaller waits are
  routine noise).
- The adapter calls the alerter beside the existing floods_seen
  metric. dl_link registers the sender at plugin load:
  StreamBot.send_message(Telegram.OWNER_ID, ...) — only when
  flood_alert_enabled is true. Unregistered = zero cost.

## Phase B — Pre-Warm (new plugin)

### Modules
- Backend/helper/prewarm.py — planner + runner + budgets.
- Backend/pyrofork/plugins/prewarm_hooks.py — trigger wiring
  (ingest posts), callback registration.
- The runner consumes REAL prefetch_stream generators with
  meta {"prewarm": True} and discards the bytes: identical pool,
  flood and cap behaviour, chunks land in the shared (now
  persistent) cache. The adapter skips db.log_stream_stats for
  prewarm streams so analytics stay honest (adapter is our file).

### Settings (prewarm_*)
- prewarm_enabled (bool, default False) — master toggle; off =
  no tasks, no hooks fire, zero cost.
- prewarm_profile: lite | balanced | aggressive | smart
  (default smart once enabled).
  - lite: head 8 MB, next-episode only, ingest heads off.
  - balanced: head+tail 24 MB, next-episode, ingest heads on.
  - aggressive: head+tail 48 MB + full next episode up to
    prewarm_mb_per_file, ingest heads on.
  - smart: balanced amounts, but next-episode warming only for
    series with >= 2 episodes streamed in the last 14 days
    (stream_analytics history); skips warming while any flood
    was seen in the last 10 min or MemAvailable is low.
- prewarm_mb_per_file (cap for aggressive full-file warming),
  prewarm_episodes_ahead (1–2), prewarm_trigger_pct (10–95,
  default 70), prewarm_daily_budget_mb (default 2048),
  prewarm_only_charging (bool, default False; charging state is
  best-effort from /sys/class/power_supply — unreadable = allow).

### Triggers
1. Progress: the adapter's consumer reports stream progress via
   set_progress_listener; crossing prewarm_trigger_pct resolves
   the next episode from the media DB (same seasons/episodes
   structure the meta route uses) and queues its warm.
2. DL pool: dl_link calls planner.warm_pooled(chat_id, msg_id)
   after a successful pool copy (head+tail per profile).
3. Ingest: prewarm_hooks listens to channel posts in auth
   channels (parallel handler, later group than receiver) and
   queues a head warm (balanced/aggressive/smart only).

### Safety (the Narzo rules)
- Concurrency 1; a warm runs only when no real stream is active
  for that pool, and aborts if a real stream starts.
- Daily MB budget is a hard stop, persisted in the tracking DB
  so restarts can't reset it.
- Floods during a warm shed clients exactly like real streams;
  a warm never retries past the shared FloodBudget.

## Phase C — DM Link / DL Channel evolution (dl_link.py grows)

### C1. Batch pooling
- Per-user pending batch in the plugin: files arriving within a
  3 s quiet window — or sharing one media_group_id — pool as a
  batch. One reply lists every link; an .m3u playlist of the
  batch is attached when the batch has >= 2 playable files.
- Single files behave exactly as today.

### C2. DL Channel Index
- dl_link_map documents gain: name, size, pooled_at, pinned.
  Existing entries backfill lazily on first touch.
- Bot: /dl list [query] — recent or matching pooled files with
  re-issued links; pin/unpin by replying /dl pin to a pooled
  file (or from the web page).
- Web: /admin/dl-index page (template_routes seam +
  templates/dl_index.html): search, re-issue link, pin, delete.
  Delete removes the channel post (bot is channel admin) and
  the map entry.
- BotCommand menu gains nothing new (list/pin are /dl arguments).

### C3. Pooled lifecycle
- dl_expire_days (int, default 0 = never). A daily sweep task
  starts lazily on the plugin's first handler: deletes channel
  posts + map entries older than the setting unless pinned,
  and prunes map entries whose post no longer exists.
- Sweep results are logged; nothing is deleted while the
  setting is 0.

### C4. Subtitle sidecar
- Subtitle files (.srt/.vtt/.ass) sent in DM are intercepted
  before the generic auto-link: replied-to a pooled video, or
  sent within 10 min of pooling one, the subtitle is pooled
  too and stored on the video's map doc as sub_msg_id.
- The bot's reply for such a video includes both links and
  shows the pairing; the Index shows a "SUB" badge.
- A standalone subtitle pools as a normal file.
- Out of scope (stated, not faked): Stremio-native subtitle
  injection — DL files have no library media id for the
  addon's subtitle route to key on.

## Phase D — Update Manager

### Module + surfaces
- Backend/helper/update_manager.py: a small state machine
  (idle → checking → staged → tested → reported → armed),
  all git work via subprocess in the staging clone.
- Surfaces: one card on the existing Tools page (tools.html +
  api_routes seams, both already seam files) with Check /
  Stage & Test / Deploy / Roll back buttons and the last
  report rendered inline.

### Flow
1. Check: fetch upstream; count new commits; list changed
   files intersected with our seam files = breakage risk.
2. Stage & Test: reset the staging clone to upstream head,
   run tools/reapply.sh, then the full test suite and a
   compile pass. Report = green/red per step with the seam
   diff summary. Nothing touches the live repo.
3. Deploy (only from a green report, only on Yan's press):
   record the live head as the rollback point, check out the
   tested tree in the live repo, restart the service via a
   detached setsid subprocess (spawned from the web process,
   not an SSH session; the mechanism is verified against the
   staging clone before this phase ships).
4. Roll back: check out the recorded head + same restart path.
- config.env and sessions are outside git and never touched.
- The weekly CI remains as an early-warning signal; the
  manager is the acted workflow on top of it.

## Settings added (summary)

buffer_disk_cache_mb (512), flood_alert_enabled (false),
prewarm_enabled (false), prewarm_profile (smart),
prewarm_mb_per_file (256), prewarm_episodes_ahead (1),
prewarm_trigger_pct (70), prewarm_daily_budget_mb (2048),
prewarm_only_charging (false), dl_expire_days (0).

## Testing & delivery per phase

- New unit tests per phase (targets: A +10, B +10, C +8, D +6)
  on top of the current 71; pristine-reapply proof per phase;
  live deploy per phase with config md5 + startup-log checks.
- Phase order: A → B → C → D. Cockpit decision after Phase B,
  when the data it would render (DC speeds, pre-warm state,
  flood alerts) actually exists.
