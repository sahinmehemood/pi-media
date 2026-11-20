---
name: librarian-simple
description: The Librarian, simple. You verify and write the caption; the API applies it. This supersedes the Work Orders (librarian-v2) skill.
---

# Librarian, simple

One loop, nothing else:

1. **Get the waiting files**: `librarian_cli.py simple waiting --channel <chat id>`
   (add `--stem <name>` to work one series at a time — one series,
   beginning to end, before starting another).
2. **Verify the identity yourself** — research is your job: providers,
   your memory, the library. When two names exist for one show
   (pinyin vs western), find the ID that joins them. Never guess an ID.
3. **Write the corrected caption** in the standard format, with the
   provider ID link.
4. **Apply it**: write `{chat_id, msg_id, expected_caption, new_caption, key, stem}`
   (expected_caption = the caption exactly as `waiting` showed it) to a
   file and run `librarian_cli.py simple fix --file fix.json`.
   The API edits the caption, confirms it indexed, records the fix,
   and teaches the stem→ID mapping from your fix.
5. **Read the answer plainly**: fixed / changed by someone else (stop —
   never force) / flood wait (wait, then retry) / budget spent (stop
   and say so) / test mode (nothing was edited — report what would happen).
6. **Report from the numbers**: `librarian_cli.py simple status` —
   waiting, fixed, fixed today. Quote those, never estimates.

Undo the moment a mistake is spotted:
`librarian_cli.py simple undo --chat <chat id> --msg <msg id>` —
the exact old caption goes back.

Hard rules stay hard: specials land in season 0 only; subtitle files
are never caption-edited; an unresolvable title is reported as
unresolvable, never guessed into a fix.
