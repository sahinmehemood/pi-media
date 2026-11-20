---
name: librarian-simple
description: The Librarian toolbox — you verify and write captions; the app applies them. Supersedes the proposal/gate flow and the Work Orders (v2) skill.
---

# The Librarian toolbox

You are the intelligence; the app is hands and memory. Your
operating manual (AGENTS.md) holds the loop and the rules; this
skill is the command reference.

## Commands (all live)

- `librarian_cli.py simple status` — live waiting/fixed counts.
- `librarian_cli.py simple waiting [--channel <id>] [--stem <stem>] [--grouped]`
- `librarian_cli.py find <text>` — whole-library search.
- `librarian_cli.py file --chat <id> --msg <id>` — one file's state.
- `librarian_cli.py probe --chat <id> --msg <id>` — does the post
  still exist? (Ghosts: deleted posts — report, never fight.)
- `librarian_cli.py lookup <name> [--type <type>]` — identity.
- `librarian_cli.py series <provider:id>` — episode lists.
- `librarian_cli.py crossid <provider:id>` — all IDs for a work.
- `librarian_cli.py simple fix --file fix.json` — one fix:
  {chat_id, msg_id, expected_caption, new_caption, key, stem}.
- `librarian_cli.py fix-batch --file fixes.json` — a run of fixes.
- `librarian_cli.py simple undo --chat <id> --msg <id>`
- `librarian_cli.py history [--stem <stem>] [--today]`
- `librarian_cli.py record-get --channel <id> --stem <stem>` /
  `record-set --file rec.json` — per-series numbering truth.
- `librarian_cli.py aliases [--channel <id>]` / `alias-forget`
- `librarian_cli.py park --key <key>` / `unpark --key <key>`
- `librarian_cli.py scan [--channel <id>]` / `scan-status`
- `librarian_cli.py mode` — standing Settings.

## Reading fix results

- ok + indexed — done. ok + index not confirmed — say so plainly.
- action "repost" — the original post could not be edited; the
  file was reposted with the corrected caption. Normal.
- "Caption changed by someone else" — stop on that file.
- "post may no longer exist" — a ghost; probe it, report it.
- flood wait — wait the stated seconds, then continue.
- budget spent / test mode / Librarian off — hard stops; report.

## Discipline

- One series at a time, beginning to end, episode order, unless
  Yan says otherwise.
- Verify by episode title/air date (provider-research skill);
  store numbering decisions as series records.
- Report from re-run status numbers, never estimates.
