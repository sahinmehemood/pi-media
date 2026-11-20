# Media Manager Bot Pack — Install Steps

**You install this pack yourself, Yan.** The team does not configure
your live `mediamanager` Hermes profile — nothing in this phase
touches it. These are the exact steps, in order. There are no
terminal steps for you beyond the Hermes app actions you already
perform; file placement is done with Sira's help where your setup
requires it.

The pack must not be installed against an unpinned surface: the
argument encodings for `fix` / `record-get` / `record-set` /
`alias-set` are pinned in the `librarian-toolbox` skill
("Argument encodings (pinned in Phase 3)"), and pack acceptance
happens only after that subsection exists and the smoke session
(step 6) has run against the pinned CLI.

## 1. What is being installed, and where each file goes

The staged pack is a pack, not a profile. Installation maps it onto
the real profile layout:

| Pack file (in this directory) | Installed location |
|---|---|
| `SOUL.md` | `~/.hermes/profiles/mediamanager/SOUL.md` (profile root) |
| `MEMORY.md` | `~/.hermes/profiles/mediamanager/memories/MEMORY.md` |
| `skills/<name>/SKILL.md` (× 9) | `~/.hermes/profiles/mediamanager/skills/<name>/SKILL.md` |
| `AGENTS.md` | The bot's project / working-directory context — **not** the profile root. Hermes loads `AGENTS.md` from the session's working directory, not from the profile home. The exact directory is the one the step 5 `/context` proof validates. |
| Pairing token (plaintext) | The profile's `.env` as `LIBRARIAN_API_TOKEN` — entered by you at pairing (step 3). It is never in any pack file. |
| CLI base URL | The profile's `.env` as `LIBRARIAN_API_URL`, only if it differs from the default (step 3). |

## 2. Generate the pairing token

In the website: **Settings → Librarian card → Generate token**.

Copy the token **shown once**. The app stores only a hash of it; the
plaintext is shown this one time and cannot be retrieved again. If
it is lost, generate a new one (that is rotation, step 7).

## 3. Enter the token (and, if needed, the base URL) in the profile

In the mediamanager profile's `.env`, enter the plaintext:

```
LIBRARIAN_API_TOKEN=<the token>
```

The CLI learns the app's base URL from the `LIBRARIAN_API_URL`
environment variable, with the pinned default
`http://127.0.0.1:8000` when it is unset (the bot and the app both
run on the Narzo, so a localhost default is the starting point).
Only if Sira confirms the live app's actual base URL differs from
that default, also set:

```
LIBRARIAN_API_URL=<app base URL>
```

The smoke session (step 6) is the proof that the configured value
is right.

## 4. Restart the Hermes gateway, then start a new session

**Restart the Hermes gateway.** The CLI's environment is inherited
from the gateway process — a new session alone does not reload it.

Then start a **new session** (`/new`) so SOUL, MEMORY, and the
skills load fresh. This applies after **any** brain-file install or
edit: memory and SOUL are frozen snapshots at session start, and
gateway chats are one long session, so an edit is not visible until
`/new`.

## 5. Prove the operating manual loaded

Run `/context` in the fresh session and confirm the AGENTS manual
is loaded. Send the output to Sira for the pack-acceptance record.

**Pack acceptance is not declared without this proof.** If the
manual does not load, the fallbacks, in order, are: set the
profile's working directory to the pack's project directory; or
carry the manual as an always-loaded skill bundle. The bot is not
"set up" on any other evidence.

## 6. Smoke session

Ask the bot to run, through the CLI:

1. `status`
2. `waiting --grouped`
3. one `lookup`
4. one `record-get`

The bot drives the CLI; Sira verifies the results against the live
counts. All four must return sane results.

## 7. Rotating the token

Rotate **only between batches**:

1. Generate the new token in the website Settings Librarian card
   (step 2) — the old token is invalid immediately.
2. Enter the new plaintext in the profile's `.env` as
   `LIBRARIAN_API_TOKEN` (the profile's secrets).
3. **Restart the Hermes gateway** — a new session alone does not
   reload the CLI's environment.
4. Confirm with a `status` call before resuming work.

An authentication failure on *every* call, including `status`,
means the CLI's token no longer matches the stored hash — the bot
reports exactly that and stops.

**Warning:** the token is shown once and lives only in the
profile's `.env`. It never appears in any pack file, in settings
responses, on the Librarian page, or in logs.
