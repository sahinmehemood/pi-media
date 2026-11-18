# Streaming Buffer Plugin — Design

Branch: `own` · Base: official weebzone/Telegram-Stremio `f6e1e97` (v5.3.4)
Spec for this work. The code-review Spec axis reviews the diff against this file.

## 1. Purpose

A streaming buffer system for Telegram-Stremio, built fresh on the official
codebase, delivered as a **plugin package + one minimal seam patch** so future
official updates never require a re-fork:

- All feature logic lives in new files (the plugin).
- Edits to upstream files are few, small, additive, and listed in §6. They are
  exported as `patches/integrate.patch`.
- `tools/reapply.sh` applies the plugin to any fresh official checkout and
  runs the test suite. A weekly CI job does the same against upstream master.

Non-goals: no theme/UI work beyond the buffer settings section, no other fork
features, no behaviour change when the buffer is disabled (§5, invariant I6).

## 2. Why (what is wrong with upstream streaming)

Upstream `ByteStreamer.prefetch_stream` (Backend/helper/custom_dl.py):

1. Parallelism/prefetch are a fixed formula — `ceil(bot_clients / 5)`, max 5 —
   chosen once at stream start and never adapted. The userbot is forced to
   (1, 1) by `stream_routes._prefetch_for`.
2. No chunk cache: seeks, replays and overlapping ranges refetch from Telegram.
3. FloodWait handling is local to each chunk fetch: unbounded cumulative
   waits, no shared accounting, no client shedding.
4. A failed chunk returns the same sentinel as end-of-file, so the consumer
   ends the stream as if complete — **silent truncation**.
5. It records stream speeds in the registry but nothing ever uses them.

Protocol facts the design is built on (researched, see README references):
`upload.getFile` returns at most 1 MiB per request; throughput is capped per
connection/session, so speed comes from parallelism **across clients**, not
from more requests down one session; Telegram's own clients run only a few
concurrent large-file operations per DC.

## 3. Speed preservation requirement (hard)

The owner runs a buffer setup today whose speed he is happy with. This design
must be **at least as fast on a healthy link, by construction**:

- Default (balanced) profile keeps ≥ upstream's maximum concurrency and a
  strictly deeper readahead queue (16 MiB vs upstream's ≤ 5 chunks).
- The adaptive controller may only reduce concurrency/window in response to
  observed flood/stall signals, and never below 1 worker / 1 chunk.
- A chunk cache is added on top — upstream has none — so repeat/seek traffic
  is strictly faster.
- Profiles: conservative (2 workers / 8 MiB / 16 MiB cache), balanced
  (4 / 16 MiB / 48 MiB), aggressive (8 / 32 MiB / 128 MiB), custom.

## 4. Modules (codebase-design vocabulary)

### 4.1 `Backend/helper/stream_buffer.py` — the deep module (stdlib only)

Small interface, all policy/scheduling intelligence inside:

- `BufferPolicy` — immutable value: enabled, profile, adaptive, workers,
  readahead_mb, cache_mb, stall_timeout_s, per_client_cap, max_flood_wait_s.
- `build_policy(data) -> BufferPolicy` — profiles + strict clamps; pure.
- `AdaptiveWindow` — per-stream AIMD controller. Interface:
  `window` (current target concurrency), `note_success()`, `note_flood()`,
  `note_stall()`. Starts at the policy window; +1 per full window of
  successes up to the policy max; halves on flood/stall (floor 1).
- `SpeedTracker` — gated EWMA of observed stream throughput. Outliers
  (zero-byte, absurd) are rejected so one bad sample cannot poison it.
  `observe(mbps)`, `mbps()`, `multiplier()` (0.5–2.0 scaling for new streams'
  initial window).
- `ChunkCache` — LRU, byte-budgeted. `get(key)`, `put(key, bytes)`,
  `resize(budget)`, `stats()`. Key = (media_id, chunk_size, chunk_index).
- `FloodBudget` — one per stream, shared by all its workers:
  `charge_wait(seconds) -> bool` (False when the stream's cumulative flood
  budget is exhausted), `penalty(client_index)`, `record_flood(client_index,
  seconds)`, `record_success(client_index)`, `shed(client_index)`,
  `is_shed(client_index)`. A shed client receives no more chunks this stream.
- `BufferMetrics` — counters (chunks_fetched, chunks_failed, cache_hits,
  cache_misses, floods_seen, workers_shed, stall_timeouts) + `snapshot()`.
- Module state: `get_policy()`, `refresh_policy(data) -> BufferPolicy`,
  `get_cache()`, `note_stream_speed(mbps)`, `snapshot()` (policy + cache +
  metrics + speed, for the settings page).

No imports from `Backend` or third-party packages: the module is testable
through its interface with the standard library alone.

### 4.2 `Backend/helper/buffered_streamer.py` — the adapter at the seam

`BufferedByteStreamer(ByteStreamer)` overrides only `prefetch_stream`, with
the **identical external contract** as upstream: same signature, same
`ACTIVE_STREAMS` registry keys and finish-time accounting (avg/instant/peak
Mbps, `db.log_stream_stats`, `work_loads`, RECENT_STREAMS hand-off), same
in-order trimmed-byte generator, same disconnect handling. Inside, the
producer is the buffer engine:

- Session pool built exactly like upstream (primary + `extra_clients`), each
  pool member wrapped with its client index for FloodBudget accounting.
- Scheduler keeps up to `AdaptiveWindow.window` fetches in flight (bounded
  also by the readahead queue), chooses the least-penalised non-shed client
  round-robin, caps concurrency per client (`per_client_cap`).
- Every chunk resolves to exactly one state: `ok`, `eof` (Telegram returned
  empty bytes), `failed` (retries exhausted). Retries rotate clients;
  FILE_REFERENCE errors trigger that client's location refresh, as upstream.
- Cache is consulted before fetching and populated after, keyed by the
  file's media id — seeks and replays skip Telegram entirely.
- On stream end it reports the measured average Mbps to the SpeedTracker.

### 4.3 Settings (upstream files, additive)

Keys (defaults): `buffer_enabled=True`, `buffer_profile="balanced"`,
`buffer_adaptive=True`, `buffer_readahead_mb=16`, `buffer_workers=4`,
`buffer_cache_mb=48`, `buffer_stall_timeout=90`.
Validation is strict (integers must be integers — no silent float/bool
coercion; out-of-range clamps to the documented range; bad profile = 400).
Changing any buffer key hot-reloads the policy via `refresh_policy` — no
restart. The settings page gains one card: enable toggle, profile select,
the four numeric fields, a live readahead estimate, and a live health block
(policy, speed EWMA, cache stats, metrics) rendered from `snapshot()`.

## 5. Invariants

- I1 Byte-exact, in-order delivery; first/last part cuts identical to upstream.
- I2 A stream that cannot complete ends as an error (registry status
  `error`, connection closes short of Content-Length) — never as a
  complete file. Silent truncation is a release-blocking defect.
- I3 Bounded memory: in-flight + queued chunks ≤ window + readahead;
  cache ≤ its byte budget; both derived from the policy, itself clamped.
- I4 No chunk fetch waits on FloodWait beyond the stream's flood budget
  (`max_flood_wait_s`, default 300 s cumulative); exhaustion sheds the
  client, and an empty pool fails the chunk (see I2).
- I5 Per-client concurrency never exceeds `per_client_cap`; concurrency
  scales across clients first.
- I6 `buffer_enabled=False` ⇒ `prefetch_stream` delegates to upstream
  unchanged. The kill switch is exact upstream behaviour.
- I7 Flood/speed state is per-stream (FloodBudget, AdaptiveWindow) or
  read-only shared (SpeedTracker, cache, metrics) — no cross-stream
  penalty leakage beyond the documented speed EWMA.

## 6. Seam inventory (everything `patches/integrate.patch` may touch)

| File | Change |
|---|---|
| `Backend/fastapi/routes/stream_routes.py` | `_get_streamer` constructs `BufferedByteStreamer` (+ import) |
| `Backend/helper/settings_manager.py` | buffer keys in `_DEFAULTS`, `Settings` properties, reinit hook calling `refresh_policy` |
| `Backend/fastapi/routes/api_routes.py` | buffer keys in `bool_keys`; strict validation block in `update_settings_api` |
| `Backend/fastapi/templates/settings.html` | one settings card; save/restore JS lines |
| `Backend/fastapi/routes/template_routes.py` | pass buffer snapshot into settings page context |
| `Backend/__main__.py` | one startup block: `refresh_policy(SettingsManager.current().to_dict())` |

Everything else is new files: the two plugin modules, `tests/`,
`patches/`, `tools/reapply.sh`, `.github/workflows/upstream-check.yml`,
this design doc and the plugin README.

## 7. Donation / promotion removal (separate commit)

Remove donate links/badges/support sections from templates, README and docs
(the scope of the owner's earlier removal), keeping functional upstream
references (licence, attribution, artwork CDN, proxy helper).

## 8. Verification

- Unit/integration tests under `tests/`, runnable with plain `python3`
  (stdlib only) and under pytest: policy building/clamps, cache LRU/budget,
  flood budget/shedding, AIMD behaviour, speed gating, engine end-to-end
  against fake sessions (in-order delivery, cache hit path, failure ⇒ error
  not EOF, flood ⇒ shed, disabled ⇒ upstream delegation).
- `tools/reapply.sh` green against a pristine official checkout.
- Two-axis code review (Standards / Spec) of `f6e1e97...HEAD` before push.
