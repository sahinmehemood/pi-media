#!/usr/bin/env python3
"""librarian-cli — the Media Manager's only path into Telegram-Stremio.

Wraps the Librarian API (packet / search / propose / apply / rollback /
status) with the token from the environment. Stdlib only.

Env:
  LIBRARIAN_API_URL    default http://127.0.0.1:8000
  LIBRARIAN_API_TOKEN  the Librarian API token from Settings

Usage:
  librarian_cli.py status
  librarian_cli.py packet --channel -1004395609351 --limit 30
  librarian_cli.py search --q "Kimetsu no Yaiba" --type tv
  librarian_cli.py propose --file plan.json
  librarian_cli.py apply --id p1700000000_12345
  librarian_cli.py rollback --id p1700000000_12345

plan.json shape (v4.1 — reference packet items by NUMBER, never raw keys;
"seen_caption" must echo the item's original caption from the packet
exactly, or the server rejects the whole proposal):
  {"channel": -1004395609351, "label": "Demon Slayer — Yuukaku-hen (S2)",
   "note": "optional reasoning for Yan",
   "packet_id": "<packet_id from the packet command>",
   "items": [{"n": 1, "seen_caption": "<original caption from the packet>",
              "media_type": "tv",
              "tmdb_id": 85937, "imdb_id": "tt9335498",
              "season": 2, "episode": 1, "absolute": false,
              "new_caption": "Demon Slayer: Kimetsu no Yaiba (2019) S02E01 1080p\n<a href=\"https://www.imdb.com/title/tt9335498/\">IMDB</a>"}]}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

BASE = os.environ.get("LIBRARIAN_API_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ.get("LIBRARIAN_API_TOKEN", "")


def call(method: str, path: str, payload: dict | None = None) -> dict:
    url = BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("x-librarian-token", TOKEN)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "status": e.code, "message": str(e)}
    except Exception as e:
        return {"ok": False, "message": f"request failed: {e}"}


def main() -> int:
    ap = argparse.ArgumentParser(prog="librarian-cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("packet")
    p.add_argument("--channel", required=True)
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--scope", default="waiting",
                   choices=["waiting", "indexed", "unindexed", "all"])
    p = sub.add_parser("search")
    p.add_argument("--q", required=True)
    p.add_argument("--type", default="any")
    p = sub.add_parser("propose")
    p.add_argument("--file", required=True)
    p = sub.add_parser("apply")
    p.add_argument("--id", required=True)
    p = sub.add_parser("rollback")
    p.add_argument("--id", required=True)
    args = ap.parse_args()

    if args.cmd == "status":
        out = call("GET", "/api/librarian/status")
    elif args.cmd == "packet":
        out = call("GET", f"/api/librarian/packet?channel={args.channel}&limit={args.limit}&scope={args.scope}")
    elif args.cmd == "search":
        from urllib.parse import quote
        out = call("GET", f"/api/librarian/search?q={quote(args.q)}&type={args.type}")
    elif args.cmd == "propose":
        with open(args.file, "r", encoding="utf-8") as fh:
            out = call("POST", "/api/librarian/proposals", json.load(fh))
    elif args.cmd == "apply":
        out = call("POST", "/api/librarian/apply", {"proposal_id": args.id})
    elif args.cmd == "rollback":
        out = call("POST", "/api/librarian/rollback", {"proposal_id": args.id})
    else:  # pragma: no cover
        out = {"ok": False, "message": "unknown command"}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
