# DM File-to-Link (`/dl`)

Send a file to the bot in a private chat; get a personal streaming /
download link back. No channel posting, no indexing, no library entry.

## Usage

- **New file:** send or forward any document/video to the bot in DM.
  It replies with the link automatically.
- **Older file:** reply to the file message with `/dl`.

The reply contains the file name, its size, and a `/dl/<token>/…` link
that plays in browsers and players (VLC, MX Player, mpv) and supports
range requests (seeking/resume), like every other link this server
issues.

## How it works

The link encodes the DM message itself (bot id + message id) and rides
the pre-existing **global stream route**: the file is served by the
**Userbot** session, because the userbot account is a party to the DM
and can read the message. Streaming runs through the
[buffer engine](stream-buffer/README.md) like all other playback.

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

One file, `Backend/pyrofork/plugins/dl_link.py` — a pyrofork plugin,
auto-loaded by the primary bot client. **Zero edits to upstream
files**, so `patches/integrate.patch` is untouched and the reapply
tool copies this file along with the buffer plugin. The extra
multi-client bots are streaming-only workers (`no_updates=True`, no
plugins) in upstream's design, so DM features can only ever live on
the primary bot.

Tests: `tests/test_dl_link.py` (auth gating, payload shape, `/dl`
usage path, userbot-missing message) — part of `tests/run_all.py`.
