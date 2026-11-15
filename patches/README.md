# pi-media feature patches

This fork tracks upstream `weebzone/Telegram-Stremio` and carries exactly **two**
features on top of it, kept deliberately isolated so a new upstream release can
be re-patched in minutes without hunting through the diff.

| # | Feature | Self-contained? |
|---|---------|-----------------|
| 1 | Smart streaming buffer (adaptive read-ahead, worker pool, shared RAM cache, centralized FloodWait handling) | Partially — 2 new modules are drop-in; 6 integration touch points |
| 2 | Donation & promotional-content removal (no injected "Support" stream in addon responses, no donate links in UI/docs) | Fully — pure deletions, re-apply by re-deleting |

## File manifest

### Feature 1 — new modules (drop-in, copy over on every rebase)

- `Backend/helper/stream_buffer.py` — buffer policy + shared cache. The only
  upstream knowledge it has is `Settings` key names; it never touches Telegram.
- `Backend/helper/flood_wait.py` — centralized FloodWait handler. Depends only
  on `pyrogram.errors`.
- `tests/` — regression suite (run per-file: `python3.14 tests/test_*.py`).

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
     `record_success` / `BUFFER_METRICS`
   - producer/consumer: surface `"failed"` instead of silently truncating;
     stall timeout comes from `policy.stall_timeout`
   - consumer `finally`: `record_stream_speed(avg_mbps)` (adaptive depth)
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

6. `Backend/fastapi/templates/settings.html`
   - "Streaming Buffer" card (profile select, read-ahead/workers/cache/stall
     inputs, adaptive toggle, live RAM estimate)
   - `BUFFER_PROFILES`, `toggleBufferFields()`, `applyBufferProfile()`,
     `updateBufferEstimate()` helpers
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
