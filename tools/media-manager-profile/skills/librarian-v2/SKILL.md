---
name: librarian-v2
description: Work Orders — the only way you change captions now. One series at a time, server-verified, fully undoable.
---

# Librarian v2 — Work Orders

You no longer edit captions directly. All fixes run inside a **Work
Order**: a scoped, server-checked plan that either applies cleanly or
is refused with a reason. The server — not you — computes confidence
from evidence. Your job is research and judgement; the server's job is
verification and the edit itself.

## The flow (one title at a time — never graze the queue)

1. **Pick one series** (or one channel's unidentified pile) and finish
   it end to end, in episode order, before starting another.
2. **Resolve it**: `librarian_cli.py v2 resolve --title "<name>" --kind <kind>`
   - Alias and provider hits come back with a score. ≥0.95 applies
     automatically; 0.80–0.95 needs Yan's approval; below that, or
     `conflicted`, STOP — do not guess.
   - For abbreviated or dual-named titles (pinyin vs western names),
     research the identity yourself (web, your memory, the library)
     and submit it for verification:
     write `{"provider": "tmdb", "provider_id": "12345", "imdb_id": "tt..."}`
     to a file and run `v2 resolve --proposal that-file.json`.
     The server checks the ID is real before trusting it.
3. **See the files**: `v2 unidentified --channel <telegram chat id>` —
   or register a file you located: `v2 register --file f.json`
   (`{chat_id, msg_id, caption, stem, season, episode}`).
4. **Open the order**: `v2 open --scope series --series <series_id>`.
5. **Plan**: write the items file — one entry per file:
   `{"media_file_id": N, "series_id": S, "stem": "<stem>", "bits": {"season": 1, "episode": 2}, "new_caption": "<exact caption>", "base_score": <score from resolve>, "breakdown": {}}`
   then `v2 plan --order <id> --file items.json`. Read the summary:
   every refusal has a reason (out of scope, no episode slot, specials
   guard, caption doesn't parse to the claimed slot, below the floor).
   A refusal is final unless the underlying fact changes — fix the
   fact, never argue with the refusal.
6. **Apply**: `v2 apply --order <id>`. Approval-tier items wait for
   Yan's approval on the Librarian page first.
7. **Report**: `v2 report --order <id>` is the live truth — planned,
   applied, verified, failed, remaining, percent, budget. Check it
   instead of assuming; quote it instead of estimating. When a series
   finishes, tell Yan the gaps (missing episodes) from the report.

## Hard rules

- Never invent an ID, a title, or an episode number. Unresolved means
  unresolved — report it and move on.
- Specials (SP/OVA markers) land in season 0 only. The server refuses
  anything else; do not route around it.
- Subtitle files are never caption-edited.
- If a file's live caption differs from what you planned on, the
  server marks it conflicted and skips it. Someone changed it — never
  force your edit over theirs.
- A successful fix teaches the server the title's names permanently.
  Wrong research therefore poisons future files: when in doubt, stop.
- `v2 undo --order <id>` (or `--file <id>`) restores the exact prior
  captions. Use it the moment a mistake is spotted, then report.
- `v2 kill --order <id>` stops an order dead. Kill switches on the
  Librarian page override you at all times.
