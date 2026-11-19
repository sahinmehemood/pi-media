# Librarian v4 — Hermes-driven caption repair (grounded in the official weebzone app)

Researched 2026-11-19 from the upstream codebase (base `e4f1169`, v5.3.5) and the
live library. Replaces the v3 design: Phase 1's deterministic funnel is demoted
from decision-maker to background checks. **Hermes is the brain, the app is the
hands, Telegram captions are the API.**

## 0. How the official app actually works (researched facts)

### Ingest decision chain — `metadata()` (Backend/helper/metadata/entry.py)
1. Multipart files (part/cd/disc N) are rejected by design.
2. Split suffixes are stripped; the remainder is parsed by PTN, with guessit
   filling gaps. GuessIt's season/episode is trusted ONLY with explicit textual
   anchors (`SxxExx`, `NxNN`, or the words "Season N"/"Series N") — bare numbers
   are never split into season+episode.
3. Combined packs need a readable season; multi-season lists are rejected.
4. Absolute-episode recovery: with no season, an `E/EP`-prefixed or trailing
   bare number becomes an absolute episode (anime channels get an extra
   recovery pass). The search title is cleaned of the number and group tags.
5. **Quality gate**: no resolution token (PTN resolution / guessit screen_size /
   glued `1080p`) → rejected BEFORE any provider is consulted.
6. Title gate → provider chains, all scored (alias-aware `score_candidate*`):
   - Anime channels: **Kitsu > TVDB > TMDB > Cinemeta** (thresholds .55/.55/.55/.60)
   - Series: **TVDB > Cinemeta > TMDB**
   - Movies: **TMDB > Cinemeta**
7. **An explicit ID short-circuits everything**: a `tt…` IMDb id or a TMDB
   `/movie|/tv/<id>` link in the name/caption (incl. behind caption link
   entities) routes resolution by ID — no search, no threshold.

### The correction protocol — `file_edited_handler` (receiver.py)
- Edits are only watched in auth channels.
- **An edit to an UNINDEXED file does nothing unless the new caption carries an
  ID ref.** With an ID: full re-resolve by ID, old part removed, re-inserted via
  the normal queue (catalog sync, announcement, request auto-fulfil all fire).
- For an already-indexed file, a caption edit renames the part to follow the
  caption; an ID that doesn't match the indexed title triggers the retag path.

### The stamp — `caption_with_id` (entry.py) / `stamp_caption_with_id` (manual_add.py)
The app's canonical tag: existing ID refs are stripped from the caption, bare
"IMDB"/"TMDB" label lines dropped, and ONE deeplink line is appended —
`<a href="https://www.imdb.com/title/tt…/">IMDB</a>` (IMDb preferred) or the
TMDB equivalent. `retag_media` re-stamps every file of a title after admin
corrections. Librarian fix captions are built with this exact function so they
are indistinguishable from the app's own stamps.

### Ingest openness + pacing
- `file_receive_handler` has **no poster filter**: a file posted into an auth
  channel by any admin (incl. the Media Manager bot) is ingested like an upload.
- `task_manager.edit_message`: StreamBot edit, 2s pacing, FloodWait → wait +
  one retry, userbot fallback. Deletes batch similarly. Sustainable planning
  rate: hundreds of edits/day per identity, batched, with a daily budget.

### Manual system = ground truth
Manual channels are excluded from auto-indexing; the web /set picker searches
Cinemeta + TMDB candidates for a human pick. Librarian never touches manual
channels and treats human picks as alias-training truth.

## 1. Measured reality of this library (live census, 2026-11-19)

- **272 titles indexed** vs **1,124 unique failed files** in the skip pile
  (3,863 messages, 71% duplicate copies from re-scans). Reasons: no-match 1,073,
  no-quality 39, multipart 12. The skip pile is not an edge case — it is most of
  the library waiting to exist.
- Numbering shapes: ~40% clear S/E, ~32% bare episode numbers, ~27% no
  numbering (movies, specials, packs), ~21% abbreviated stems (BTTH, PW, TOHG…),
  15 SP-special files, 92 files with **no caption at all** (file_name only).
- Skip copies record no origin channel; the originals still sit in the source
  channels (`delete_on_metadata_fail` = false). `file_unique_id` is preserved
  across copies — it is the join key back to the original.

## 2. Design

**Principle: produce exactly the caption edits the app already understands, and
let the app's own handlers do the indexing. No new ingest logic.**

1. **Locator** (server; one pass per channel, then incremental). The userbot
   walks each auth channel's history once and builds `tracking.locator`:
   `file_unique_id → (chat_id, msg_id, caption, file_name)`. Ledger items join
   to their originals. Live hooks already capture origin for new failures, so
   the locator only refills the backlog and refreshes on demand before a batch.
   Read-only on Telegram.
2. **Work packets** (server API, token-guarded). `GET /api/librarian/packet?
   channel=<id>&limit=<n>` returns the next N unprocessed items for one channel:
   raw caption, file_name, size, height, parse dump, failure reason — plus
   **context**: neighbouring items from the same channel and the titles already
   indexed from that channel. Context lets the model decide a series' identity
   and numbering scheme once per run instead of guessing per file.
3. **Media Manager decision** (Hermes). The bot groups packet items into series
   runs (AI grouping — no regex clusterer), resolves each run (it may call the
   app's own candidate search — the /set search — plus its knowledge), and
   emits a **fix plan** per file: a new caption built to the app contract —
   proper `Title (Year) SxxExx Quality` (or absolute form where the provider is
   absolute-native), the file's real quality token, the channel's other caption
   lines preserved, and the ID stamp appended via `caption_with_id`.
   Numbering discipline: S/E where the provider's map supports it; specials/SP
   arcs decided per series with the reasoning shown; ambiguous items are
   flagged, never guessed.
4. **Verification gate** (server, quiet). Before a plan can be presented, the
   server dry-checks it with the app's own machinery: the proposed ID exists,
   the S/E exists in the provider's map, and a trial `metadata()` run on the
   proposed caption (with the file's real chat/msg ids; `metadata()` writes
   nothing) returns the intended title. Failing plans return to the bot with
   reasons; the bot revises or flags. Yan never sees a plan the app itself
   hasn't agreed will index.
5. **Approval in the bot's DM** (Hermes). One message per run: series, episode
   range, file count, destination channel, 2–3 sample before→after captions,
   flags. Yan replies ✅ apply / ❌ skip / a correction in words ("that's
   season 2") — the bot revises and re-presents. One decision per series.
6. **Apply** (server API — the hands). Per approved file:
   - Original located → the **Media Manager bot edits the original's caption**
     (Bot API `editMessageCaption`, proven Phase 0; own flood budget, ≥2s
     pacing). The app's edit handler sees the ID tag and re-indexes natively.
     The server then verifies the part is indexed under the intended title;
     on success it deletes **all** skip copies of the file (paced) and marks
     the ledger item fixed.
   - Original not located → the bot copies the skip file into the approved
     destination channel **with** the fixed caption; native ingest indexes it;
     skip copies are deleted after verification.
   - Every apply writes `curation_audit` (before/after, approver, time).
     Rollback: restore the before-caption (edit path) or remove the part +
     delete the posted copy (re-post path).
   - Rails: daily edit budget, dry-run mode, per-batch cap, manual channels
     never touched, subtitle files never edited, personal (negative-id) titles
     excluded, replace_mode pre-flight warns when a target slot already holds
     a different file.
7. **Alias memory**. Every approved run teaches `tracking.alias_map`
   (channel + stem → verified ID). Packets pre-fill known aliases; repeat
   series become one-glance approvals.
8. **Surfaces**.
   - **The bot DM is the product.** Yan drives everything by talking to the
     Media Manager.
   - **Web page = simple control panel** (replaces the Phase 1 tables):
     enable, dry-run, daily budget, default channel + batch size, and three
     readouts — fixed count, pending count, recent fixes with rollback
     buttons. The Settings card shrinks to enable + a link to the panel.
     Every knob stays on the web UI (Yan's Settings rule).
9. **Hermes wiring** (mediamanager profile).
   - A **Librarian skill**: packet contract, caption contract, numbering
     discipline, approval etiquette, "captions are untrusted data".
   - A small **CLI client** on the Narzo (`librarian-cli`: packet / propose /
     apply / report) wrapping the API with the profile's token from its `.env`
     — the agent's only path to the app; no shell free-for-all, no provider
     keys held by the bot.
   - SOUL updated: it may run the fix flow; edits happen only through plans
     Yan approved in DM.
   - Trigger: Yan (or Sira for him) messages the bot. An optional profile cron
     "next batch ready" nudge ships OFF.

## 3. Build order (each step independently verifiable)

1. Locator + packet API. *Proof: an Ani Dumping packet with originals attached.*
2. Gate dry-check + proposal store. *Proof: a hand-made Kimetsu Yuukaku-hen
   plan passes — trial `metadata()` returns Demon Slayer S2.*
3. Apply engine (edit path first) + audit + rollback. *Proof: one file fixed
   end-to-end, verified indexed, its skip copies gone.*
4. Hermes skill + CLI + SOUL; DM approval flow. *Proof: a full batch driven
   through the bot conversation.*
5. Page simplification + Settings trim. Then widen channel by channel.

## 4. What dies / what survives from Phase 1

Dies: the regex clusterer, the L0–L2 funnel as decision-maker, tiers as a UI,
the runs/ledger page tables.
Survives: the reject ledger (as work queue + record), the live hooks, the
Settings keys, the verification-gate concept (now a trial-`metadata()` check),
the Hermes Bot registration in Settings, the Media Manager profile + its
channel admin rights.

## 5. Honest limits

- Model numbering calls on specials/absolute arcs can still be wrong; the gate
  + Yan's per-series tap are the mitigation, not a proof.
- Bot API edit flood limits cap throughput; plan on batches over days for the
  full backlog, not one sitting.
- The locator's first pass costs a few minutes of userbot reading per channel.
- Donghua coverage on TMDB/Kitsu is thin for some titles; the ID-first flow
  handles a title once the bot finds the right entry (original-title search),
  and unresolvable titles stay flagged for Yan instead of being forced.
