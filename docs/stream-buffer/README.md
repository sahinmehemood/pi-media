# Streaming Buffer Plugin

An adaptive streaming buffer for Telegram-Stremio, built fresh on the
official codebase and packaged so official updates never need a re-fork.
Design and rationale: [DESIGN.md](DESIGN.md).

## Getting started

**New install** — clone this branch and follow the normal upstream
setup (configuration, tokens, database); the buffer is included and
enabled by default:

```bash
git clone --branch own https://github.com/sahinmehemood/pi-media.git
```

**Existing official install** — apply the plugin onto your checkout
with the reapply tool (see *Updating when official releases* below).

Then open **Settings → Streaming Buffer** in the admin panel: pick a
profile (Balanced is the recommended default), or switch the buffer off
entirely to return to exact upstream streaming behaviour. Settings take
effect immediately — no restart needed.

## Verification record

The test suite is the contract: 36 tests covering the policy engine,
the streaming engine end-to-end against fake Telegram sessions
(in-order delivery, cache hits, flood shedding, failure-is-error), and
the integration seams.

| Date | Where | Result |
|---|---|---|
| 2026-11-18 | Realme Narzo 50 Pro 5G, Termux, Python 3.14.8, commit `059f539` | **36 passed, 0 failed** (~7 s) |
| 2026-11-18 | Development machine, Python 3.12.3, commit `059f539` | **36 passed, 0 failed** |
| 2026-11-18 | Reapply onto a pristine official v5.3.4 checkout | Applied cleanly, **36 passed** |

Run it yourself any time with `python3 tests/run_all.py` — standard
library only, no Telegram account or network needed.

## What it does

Every stream (Stremio playback and `/dl` file-to-link downloads alike) is
served by an engine that:

- keeps an **adaptive readahead window** of chunks in flight (AIMD: grows
  on success, halves on FloodWait/stall), seeded by a gated EWMA of
  previously measured stream speeds;
- schedules chunks **across all bot clients** (and the userbot), with
  per-client concurrency caps — Telegram caps throughput per connection,
  so speed comes from spreading across clients, not hammering one;
- shares **one FloodWait budget per stream** across all workers, with
  capped waits and automatic shedding of a flooded client;
- serves seeks/replays from a **byte-budgeted LRU chunk cache**;
- ends a stream as an **error** when a chunk cannot be fetched — never
  as a silently truncated "complete" file (an upstream defect).

Profiles (Settings → Streaming Buffer): Conservative (2 workers, 8 MiB
read-ahead, 16 MiB cache), Balanced (4 / 16 / 48, default), Aggressive
(8 / 32 / 128), or Custom. Settings hot-reload; disabling the buffer
restores upstream behaviour exactly.

## Layout

| Path | Role |
|---|---|
| `Backend/helper/stream_buffer.py` | Engine core (stdlib only): policy, AIMD, speed tracker, cache, flood budget, metrics |
| `Backend/helper/buffered_streamer.py` | `BufferedByteStreamer` — adapter subclassing upstream `ByteStreamer` |
| `patches/integrate.patch` | Every edit to upstream files (seams + donation removal) |
| `tools/reapply.sh` | Apply the plugin to a fresh official checkout and test it |
| `tests/` | 31 tests, `python3 tests/run_all.py`, stdlib only |
| `.github/workflows/upstream-check.yml` | Weekly check against upstream master; opens an issue on breakage |

## Updating when official releases

```bash
git clone https://github.com/weebzone/Telegram-Stremio.git ts-new
path/to/pi-media/tools/reapply.sh ts-new master
```

Green output = the plugin applied and all tests pass on the new
upstream. Red = upstream moved a seam listed in DESIGN.md §6; fix the
seam in this branch, regenerate the patch with:

```bash
git diff f6e1e97 HEAD -- Backend/__main__.py Backend/fastapi/routes/api_routes.py \
  Backend/fastapi/routes/stream_routes.py Backend/fastapi/routes/template_routes.py \
  Backend/fastapi/templates/settings.html Backend/helper/settings_manager.py \
  Backend/fastapi/routes/stremio_routes.py Backend/fastapi/templates/base.html \
  README.md docs/index.html docs/setup.html > patches/integrate.patch
```

(adjust the base ref when the branch is rebased onto a newer official).

## Compatibility

- Developed and tested against official **v5.3.4 (`f6e1e97`)** — the
  version this branch is based on.
- The engine core and tests need only Python 3.11+ stdlib to verify.

## Research notes

The design follows the techniques of the best file-to-link/streaming
bots (multi-client load balancing as in EverythingSuckz/TG-FileStreamBot
and the pyrofork FileToLink bots) and Telegram protocol facts:
`upload.getFile` returns at most 1 MiB per request; throughput is
per-connection, and Telegram's own clients run only a few concurrent
large-file operations per DC — hence cross-client scheduling with
per-client caps, a shared flood budget, and 1 MiB chunks throughout.
