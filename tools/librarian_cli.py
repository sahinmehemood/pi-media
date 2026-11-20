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


def _simple(args) -> dict:
    """Librarian, simple — thin pass-throughs to /api/librarian/simple/*.
    fix.json: {chat_id, msg_id, expected_caption, new_caption, key, stem}"""
    cmd = args.smpcmd
    if cmd == "status":
        return call("GET", "/api/librarian/simple/status")
    if cmd == "waiting":
        if args.grouped:
            return call("GET", "/api/librarian/v6/waiting-grouped")
        q = f"limit={args.limit}"
        if args.channel:
            q += f"&channel={args.channel}"
        if args.stem:
            from urllib.parse import quote
            q += f"&stem={quote(args.stem)}"
        return call("GET", f"/api/librarian/simple/waiting?{q}")
    if cmd == "fix":
        with open(args.file, "r", encoding="utf-8") as fh:
            return call("POST", "/api/librarian/simple/fix", json.load(fh))
    if cmd == "undo":
        return call("POST", "/api/librarian/simple/undo",
                    {"chat_id": int(args.chat), "msg_id": int(args.msg)})
    return {"ok": False, "message": f"unknown simple command {cmd}"}


def _v6(args) -> dict:
    """Librarian v6 toolbox — thin pass-throughs (design §3)."""
    from urllib.parse import quote
    cmd = args.cmd
    if cmd == "find":
        return call("GET", f"/api/librarian/v6/find?q={quote(args.text)}")
    if cmd == "file":
        return call("GET", f"/api/librarian/v6/file?chat={args.chat}"
                           f"&msg={args.msg}")
    if cmd == "lookup":
        q = f"q={quote(args.name)}"
        if args.type:
            q += f"&type={quote(args.type)}"
        return call("GET", f"/api/librarian/gw/lookup?{q}")
    if cmd == "series":
        return call("GET", f"/api/librarian/gw/series?ref={quote(args.ref)}")
    if cmd == "crossid":
        return call("GET", f"/api/librarian/gw/crossid?ref={quote(args.ref)}")
    if cmd == "history":
        q = "today=1" if args.today else ""
        if args.stem:
            q += ("&" if q else "") + f"stem={quote(args.stem)}"
        return call("GET", f"/api/librarian/v6/history?{q}")
    if cmd == "fix-batch":
        with open(args.file, "r", encoding="utf-8") as fh:
            return call("POST", "/api/librarian/v6/fix-batch",
                        {"fixes": json.load(fh)})
    if cmd == "record-get":
        return call("GET", f"/api/librarian/v6/record?channel={args.channel}"
                           f"&stem={quote(args.stem)}")
    if cmd == "record-set":
        with open(args.file, "r", encoding="utf-8") as fh:
            return call("POST", "/api/librarian/v6/record", json.load(fh))
    if cmd == "aliases":
        q = f"?channel={args.channel}" if args.channel else ""
        return call("GET", f"/api/librarian/v6/aliases{q}")
    if cmd == "alias-forget":
        return call("POST", "/api/librarian/v6/alias-forget",
                    {"channel": int(args.channel), "stem": args.stem})
    if cmd in ("park", "unpark"):
        return call("POST", "/api/librarian/v6/park",
                    {"key": args.key, "parked": cmd == "park"})
    if cmd == "scan":
        body = {"channel": int(args.channel)} if args.channel else {}
        return call("POST", "/api/librarian/v6/scan", body)
    if cmd == "scan-status":
        return call("GET", "/api/librarian/v6/scan-status")
    if cmd == "mode":
        return call("GET", "/api/librarian/v6/mode")
    return {"ok": False, "message": f"unknown v6 command {cmd}"}


def _v2(args) -> dict:
    """Librarian v2 Work Orders — the bot's only capability surface.
    Thin pass-throughs to /api/librarian/v2/*; all rules live server-side."""
    cmd = args.v2cmd
    if cmd == "status":
        return call("GET", "/api/librarian/v2/status")
    if cmd == "series":
        q = f"?kind={args.kind}" if args.kind else ""
        return call("GET", f"/api/librarian/v2/series{q}")
    if cmd == "resolve":
        payload: dict = {"kind": args.kind, "year": args.year}
        if args.proposal:
            with open(args.proposal, "r", encoding="utf-8") as fh:
                payload["proposal"] = json.load(fh)
        else:
            payload["title"] = args.title
        return call("POST", "/api/librarian/v2/series/resolve", payload)
    if cmd == "unidentified":
        q = f"?channel={args.channel}" if args.channel else ""
        return call("GET", f"/api/librarian/v2/unidentified{q}")
    if cmd == "register":
        with open(args.file, "r", encoding="utf-8") as fh:
            return call("POST", "/api/librarian/v2/files", json.load(fh))
    if cmd == "open":
        payload = {"scope": args.scope, "series_id": args.series,
                   "channel_id": args.channel, "budget": args.budget}
        return call("POST", "/api/librarian/v2/orders", payload)
    if cmd == "plan":
        with open(args.file, "r", encoding="utf-8") as fh:
            items = json.load(fh)
        if isinstance(items, dict):
            items = items.get("items") or []
        return call("POST", "/api/librarian/v2/orders/plan",
                    {"order_id": args.order, "items": items})
    if cmd == "apply":
        return call("POST", "/api/librarian/v2/orders/apply",
                    {"order_id": args.order})
    if cmd == "report":
        return call("GET", f"/api/librarian/v2/orders/report"
                           f"?order_id={args.order}")
    if cmd == "kill":
        return call("POST", "/api/librarian/v2/orders/kill",
                    {"order_id": args.order})
    if cmd == "undo":
        payload = {"order_id": args.order} if args.order \
            else {"file_id": args.file}
        return call("POST", "/api/librarian/v2/undo", payload)
    return {"ok": False, "message": f"unknown v2 command {cmd}"}


def main() -> int:
    ap = argparse.ArgumentParser(prog="librarian-cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("packet")
    p.add_argument("--channel", required=True)
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--scope", default="waiting",
                   choices=["waiting", "ready", "indexed", "unindexed", "all"])
    p = sub.add_parser("search")
    p.add_argument("--q", required=True)
    p.add_argument("--type", default="any")
    p = sub.add_parser("propose")
    p.add_argument("--file", required=True)
    p = sub.add_parser("apply")
    p.add_argument("--id", required=True)
    p = sub.add_parser("rollback")
    p.add_argument("--id", required=True)
    # --- Librarian, simple: bot verifies + writes, API applies ---
    smp = sub.add_parser("simple")
    smpsub = smp.add_subparsers(dest="smpcmd", required=True)
    smpsub.add_parser("status")
    p = smpsub.add_parser("waiting")
    p.add_argument("--channel", default="")
    p.add_argument("--stem", default="")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--grouped", action="store_true")
    p = smpsub.add_parser("fix")
    p.add_argument("--file", required=True)
    p = smpsub.add_parser("undo")
    p.add_argument("--chat", required=True)
    p.add_argument("--msg", required=True)
    # --- Librarian v6 toolbox ---
    p = sub.add_parser("find")
    p.add_argument("text")
    p = sub.add_parser("file")
    p.add_argument("--chat", required=True)
    p.add_argument("--msg", required=True)
    p = sub.add_parser("lookup")
    p.add_argument("name")
    p.add_argument("--type", default="")
    p = sub.add_parser("series")
    p.add_argument("ref")
    p = sub.add_parser("crossid")
    p.add_argument("ref")
    p = sub.add_parser("history")
    p.add_argument("--stem", default="")
    p.add_argument("--today", action="store_true")
    p = sub.add_parser("fix-batch")
    p.add_argument("--file", required=True)
    p = sub.add_parser("record-get")
    p.add_argument("--channel", required=True)
    p.add_argument("--stem", required=True)
    p = sub.add_parser("record-set")
    p.add_argument("--file", required=True)
    p = sub.add_parser("aliases")
    p.add_argument("--channel", default="")
    p = sub.add_parser("alias-forget")
    p.add_argument("--channel", required=True)
    p.add_argument("--stem", required=True)
    p = sub.add_parser("park")
    p.add_argument("--key", required=True)
    p = sub.add_parser("unpark")
    p.add_argument("--key", required=True)
    p = sub.add_parser("scan")
    p.add_argument("--channel", default="")
    sub.add_parser("scan-status")
    sub.add_parser("mode")
    # --- Librarian v2 (Work Orders) ---
    v2 = sub.add_parser("v2")
    v2sub = v2.add_subparsers(dest="v2cmd", required=True)
    v2sub.add_parser("status")
    p = v2sub.add_parser("series")
    p.add_argument("--kind", default="")
    p = v2sub.add_parser("resolve")
    p.add_argument("--title", default="")
    p.add_argument("--kind", default="tv")
    p.add_argument("--year", type=int, default=None)
    p.add_argument("--proposal", default="",
                   help="JSON file: {provider, provider_id, imdb_id, title}")
    p = v2sub.add_parser("unidentified")
    p.add_argument("--channel", default="")
    p = v2sub.add_parser("register")
    p.add_argument("--file", required=True)
    p = v2sub.add_parser("open")
    p.add_argument("--scope", default="series",
                   choices=["series", "channel_unidentified"])
    p.add_argument("--series", type=int, default=None)
    p.add_argument("--channel", type=int, default=None)
    p.add_argument("--budget", type=int, default=None)
    p = v2sub.add_parser("plan")
    p.add_argument("--order", type=int, required=True)
    p.add_argument("--file", required=True)
    p = v2sub.add_parser("apply")
    p.add_argument("--order", type=int, required=True)
    p = v2sub.add_parser("report")
    p.add_argument("--order", type=int, required=True)
    p = v2sub.add_parser("kill")
    p.add_argument("--order", type=int, required=True)
    p = v2sub.add_parser("undo")
    p.add_argument("--order", type=int, default=None)
    p.add_argument("--file", type=int, default=None)
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
    elif args.cmd == "simple":
        out = _simple(args)
    elif args.cmd in ("find", "file", "lookup", "series", "crossid",
                      "history", "fix-batch", "record-get", "record-set",
                      "aliases", "alias-forget", "park", "unpark",
                      "scan", "scan-status", "mode"):
        out = _v6(args)
    elif args.cmd == "v2":
        out = _v2(args)
    else:  # pragma: no cover
        out = {"ok": False, "message": "unknown command"}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
