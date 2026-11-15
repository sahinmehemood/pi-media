# pi-media feature patches

This fork tracks upstream `weebzone/Telegram-Stremio` and carries exactly **two**
features on top of it, kept deliberately isolated so a new upstream release can
be re-patched in minutes without hunting through the diff.

| # | Feature | Self-contained? |
|---|---------|-----------------|
| 1 | Smart streaming buffer (adaptive read-ahead, worker pool, shared RAM cache, centralized FloodWait handling) | Partially — 2 new modules are drop-in; 6 integration touch points |
| 2 | Donation & promotional-content removal (no injected "Support" stream in addon responses, no donate links in UI/docs) | Fully — pure deletions, re-apply by re-deleting |

## Update runbook — what to do on every new upstream release

You only need to say: **"New official release — update the final branch."**

The agent then does this (also runnable by hand):

```bash
bash patches/reapply.sh
```

The script:
1. Fetches the newest `weebzone/Telegram-Stremio` and starts a fresh branch from it
2. Copies the two drop-in modules (`stream_buffer.py`, `flood_wait.py`) and
   `tests/` whole from the previous `final` branch — safe, they depend only on
   `pyrogram.errors`, `Backend.logger` and settings key names
3. Prints the exact per-file merge diffs for the 6 integration touch points and
   the Feature 2 deletion list — each hunk is merged against the *new* upstream
   file, never copied wholesale (upstream may have edited the same lines)
4. Runs the regression suite (`for f in tests/test_*.py; do python3.14 "$f"; done`)
   — `test_donation_removed.py` alone tells you if any promotional content
   slipped back in during the merge
5. Pushes the rebuilt `final` branch to GitHub

Commit layout after a re-patch is the same 6-commit series as the original
(see bottom of this file), so the branch history stays reviewable.

## File manifest

### Feature 1 — new modules (drop-in, copy over on every rebase)

- `Backend/helper/stream_buffer.py` — buffer policy + shared cache. The only
  upstream knowledge it has is `Settings` key names; it never touches Telegram.
  Owns the live-signal seam: `note_speed_sample()` (mid-stream, time-gated),
  `record_stream_speed()` (per-stream aggregate), `shedded_workers()` (pure
  mid-stream flood-shedding rule), `snapshot()` (admin visibility).
- `Backend/helper/flood_wait.py` — centralized FloodWait handler. Depends only
  on `pyrogram.errors`.
- `tests/` — regression suite (run per-file: `python3.14 tests/test_*.py`;
  `test_buffer_live.py` covers the live/sampled behaviours).

### Feature 1 — integration touch points (the only upstream code that changes)

1. `Backend/helper/custom_dl.py` (`ByteStreamer`)
   - imports: `flood_handler`, `stream_buffer` seam functions
   - `_track_task` / `shutdown_background_tasks` task registry
   - `_is_flood_error` strict flood detector
   - in `prefetch_stream`: build `policy = stream_policy(...)`, honour disabled
     fallback (1 chunk / 1 worker / no cache), add `buffer` block to the stream
     registry entry, register stream only after media session setup
   - `fetch_chunk_with_retries` returns `(seq, bytes, status)` with
     `"ok" | "eof" | "failed"`, consults the shared cache, routes genuine
     FloodWaits through `flood_handler.handle()` (capped), feeds
     `record_success` / `BUFFER_METRICS`, and samples link speed at the
     network edge via `note_speed_sample` (per-chunk GetFile latency scaled
     by worker count, time-gated inside the window)
   - producer/consumer: surface `"failed"` instead of silently truncating;
     stall timeout comes from `policy.stall_timeout`
   - producer scheduling honours the live worker allowance
     (`shedded_workers(start_parallel, start_penalty, penalty_now)`): a
     FloodWait landing mid-stream sheds in-flight workers and they recover
     automatically as `record_success` decays the penalty
   - consumer `finally`: `record_stream_speed(avg_mbps)` (ground-truth
     aggregate; the frequent mid-stream samples come from the fetcher)
   - `_speed_test_single_client`: drain the queue on EOF so `queue.join()` ends

2. `Backend/fastapi/routes/stream_routes.py`
   - build headers and answer `HEAD` **before** calling `prefetch_stream`
     (a HEAD must not register a phantom stream)

3. `Backend/__main__.py` — call `warm_streamers()` after client init.

4. `Backend/helper/settings_manager.py`
   - `_DEFAULTS`: seven `buffer_*` keys
   - `Settings`: matching clamped properties
   - `SettingsManager.reinit`: on buffer-key change call
     `refresh_policy(new_snapshot)` and report the effective policy

5. `Backend/fastapi/routes/api_routes.py`
   - `_coerce_bounded_int()` helper (strict int coercion, 400 on `3.9`/`true`)
   - `buffer_enabled` / `buffer_adaptive` added to `bool_keys`
   - `buffer_profile` enum check + `buffer_int_bounds` + `buffer_stall_timeout`
     bound check in `update_settings_api` — bounds must mirror
     `stream_buffer.build_policy`
   - `get_system_stats_api()`: adds `"buffer": buffer_snapshot()` (defensive
     import) so the admin panel can see live buffer health

6. `Backend/fastapi/templates/settings.html`
   - "Streaming Buffer" card (profile select, read-ahead/workers/cache/stall
     inputs, adaptive toggle, live RAM estimate)
   - `BUFFER_PROFILES`, `toggleBufferFields()`, `applyBufferProfile()`,
     `updateBufferEstimate()` helpers
   - `pollBufferLive()` — polls `/api/system/stats` every 10 s and renders
     measured link speed / cache hit rate / flood penalty into the card
   - collect + restore of all seven keys in the save/load JS

### Feature 2 — deletion list (re-delete after every upstream merge)

- `Backend/fastapi/routes/stremio_routes.py`: the donation helper and its two
  injection points in `get_streams` (empty result now returns `{"streams": []}`)
- `Backend/fastapi/templates/base.html`: donate links in the theme menu and
  the mobile sheet
- `README.md`: donate badge and the support section
- `docs/index.html`, `docs/setup.html`: footer "Support the project" links

Do **not** remove functional upstream references (artwork CDN, proxy helper,
licence/attribution) — `tests/test_donation_removed.py` asserts both that promo
patterns are gone *and* that attribution survives.

## Re-applying after an upstream update

1. `git fetch official && git checkout -B final official/master`
2. Copy the two drop-in modules and `tests/` from the previous `final` branch
   (`git checkout final -- Backend/helper/stream_buffer.py Backend/helper/flood_wait.py tests/`)
3. Walk the 6 integration touch points above; each is small and annotated with
   `buffer` / `flood` comments — search for `stream_policy`, `_is_flood_error`,
   `_coerce_bounded_int`, `buffer_` in the old branch to find the hunks:
   `git diff official/master final -- Backend/`
4. Re-apply the Feature 2 deletion list (or simply run step 4's test, find what
   upstream re-added, delete it).
5. Run the full suite: `for f in tests/test_*.py; do python3.14 "$f"; done`
6. Smoke: `python3.14 -m py_compile Backend/helper/*.py Backend/fastapi/routes/*.py Backend/__main__.py`

## Commit layout on `final`

```
feat(buffer): add stream_buffer and centralized FloodWaitHandler modules
feat(buffer): integrate policy-driven prefetch into ByteStreamer
feat(settings): buffer settings with strict validation and admin panel
feat: remove donation and promotional content
test: add buffer, flood-wait and donation-removal regression suite
docs: add feature patch re-application guide
```

Each re-patch repeats this series on top of the new upstream base — `git log`
then reads as one clean feature block per upstream release.
