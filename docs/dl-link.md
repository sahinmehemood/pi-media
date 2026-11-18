# DM File-to-Link (`/dl`)

Send a file to the bot in a private chat; get a personal streaming /
download link back. No indexing, no library entry. With a DL Channel
configured (below), the file is copied into that channel and streamed
by the whole bot pool; otherwise the link is served by the Userbot.

## Usage

- **New file:** send any document/video to the bot in DM.
  It replies with the link automatically.
- **Older file:** reply to the file message with `/dl`.
- **Channel-ID helper:** forward a post *from a channel* to the bot
  in DM and it replies with that channel's `-100…` ID (for Settings →
  DL Channel) instead of a link. Reply `/dl` to the forwarded file if
  you wanted its link.
- **Diagnostics:** reply to a file with `/check` for a staged
  pre-flight of the whole link path. In pool mode: DL Channel
  configured, bot admin status there, copy/dedupe state, file
  properties, a live 1 MiB probe fetch through a pool bot, pool size.
  In userbot mode: userbot presence, peer resolution, message
  resolution in the userbot's view, file properties, the same probe
  through the userbot. Both end with the current buffer
  policy/metrics and a verdict (healthy / slow / degraded / broken,
  naming the failing stage).

The reply contains the file name, its size, and a `/dl/<token>/…` link
that plays in browsers and players (VLC, MX Player, mpv) and supports
range requests (seeking/resume), like every other link this server
issues.

The `/dl` and `/check` menu entries live in upstream's bot-command
list (`Backend/helper/pyro.py`, part of the integration patch):
`setup_bot_commands()` wipes and rewrites that list at every startup,
so it is the only place a menu entry survives a restart. The handlers
themselves are entirely in the plugin.

## How it works

### Pool mode — DL Channel (recommended)

Set **Settings → DL Channel** to a private channel's `-100…` ID
(create the channel, then Tools → **Bot Admin Manager** → Apply to
make every bot an admin there; it appears in that tool's list with
the `dl` role automatically).

On `/dl`, the primary bot **copies** the DM message into the DL
Channel — a Telegram server-side copy: no download, no re-upload —
and the link encodes the *channel post* (`chat_id` + `msg_id`, the
library convention), so the standard stream route serves it through
the **multi-client bot pool** and the
[buffer engine](stream-buffer/README.md), exactly like a library
file. The Userbot session is never used — not for the copy, not for
streaming.

- **Dedupe:** copies are deduplicated by `file_unique_id`; the
  mapping lives in the tracking DB (`dl_link_map` collection).
  Sending the same file again returns the existing link instantly.
- **Strict, no fallback:** if the copy fails (bot not admin, channel
  unreachable), the bot replies with the reason. It never silently
  falls back to the userbot — that is the point of the mode.
- The file must stay in the DL Channel — delete the post and the
  link dies. The DM original can be deleted freely.
- The DL Channel cannot double as an auth/manual/anime/announce/skip
  channel; the settings API rejects the overlap, so DM files are
  never indexed into the library.

### Userbot mode — no DL Channel

The link encodes the DM message itself (bot id + message id) and rides
the pre-existing **global stream route**: the file is served by the
**Userbot** session, because the userbot account is a party to the DM
and can read the message. Streaming runs through the buffer engine
like all other playback.

Consequences of that design, all deliberate:

- The file must stay in the DM — delete the message and the link dies.
- Streaming is single-client (the userbot). The multi-client bot pool
  cannot read another account's DM; that is a Telegram constraint, not
  a setting.
- A configured Userbot is required; without one the bot says so.

## Access control

Issuing mirrors `/start` exactly:

- **Owner-only mode** (subscriptions off): only the owner gets links;
  everyone else is silently ignored.
- **Subscription mode:** any user holding a valid API token gets links;
  the owner always can.

The link carries the requester's personal token — treat it like the
addon manifest URL: private.

## Implementation

The feature logic is one file, `Backend/pyrofork/plugins/dl_link.py`
— a pyrofork plugin, auto-loaded by the primary bot client. The extra
multi-client bots are streaming-only workers (`no_updates=True`, no
plugins) in upstream's design, so DM features can only ever live on
the primary bot.

Pool mode needs three small seams in `patches/integrate.patch`,
because plugins cannot register settings fields or tool rows:

- `settings_manager.py` — the `dl_channel_id` default + property, and
  its inclusion in channel-title sync.
- `settings.html` — the DL Channel card, its `saveSettings` payload
  entry, and its post-save refresh (mirroring the announcement/skip
  channel fields).
- `api_routes.py` — `-100…` validation for the field, its entry in
  the cross-field channel-overlap check, and the `dl` role in the
  Bot Admin Manager's `_bot_served_channels()` list.

Tests: `tests/test_dl_link.py` (auth gating, payload shapes, `/dl`
usage path, userbot-missing message, pool copy/dedupe/no-fallback,
forward helper, pool `/check`) and `tests/test_static_seams.py`
(DL Channel seam assertions) — part of `tests/run_all.py`.
