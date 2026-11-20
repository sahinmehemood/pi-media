#!/usr/bin/env python3
"""Librarian CLI — the Media Manager bot's only interface to the app
(DESIGN §9). A thin wrapper over the plugin router's API: stdlib
only, the response body is printed verbatim, and the exit code is
0 on HTTP 200, 2 on any HTTP non-200, 3 on transport failure or a
local usage error.

Configuration (environment):
  LIBRARIAN_API_TOKEN  the pairing token (plaintext; app-side only
                       its SHA-256 is stored)
  LIBRARIAN_API_URL    the app's base URL (default
                       http://127.0.0.1:8000)

Structured payloads cross as one UTF-8 JSON document in a file,
named by --json-file <path> ('-' = stdin) — the pinned Phase 3
encoding (§9.5 E1/E2). There is no --json string flag.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 120  # seconds per request (DESIGN §9)


class TransportError(Exception):
    pass


def _base_url():
    return (os.environ.get("LIBRARIAN_API_URL") or "http://127.0.0.1:8000").rstrip("/")


def _http(method, path, payload=None):
    """One request. Returns (status, body_text); raises
    TransportError on any transport failure. The test seam."""
    token = os.environ.get("LIBRARIAN_API_TOKEN") or ""
    url = _base_url() + path
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise TransportError(f"{type(exc).__name__}: {exc}")


def _read_json_file(path):
    """Parse a --json-file argument ('-' = stdin) into an object."""
    try:
        if path == "-":
            text = sys.stdin.read()
        else:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
    except OSError as exc:
        raise TransportError(f"cannot read --json-file {path}: {exc}")
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise TransportError(f"--json-file {path} is not valid JSON: {exc}")
    if not isinstance(value, dict):
        raise TransportError(f"--json-file {path} must contain a JSON object")
    return value


def _query(params):
    clean = {k: v for k, v in params.items() if v is not None}
    return ("?" + urllib.parse.urlencode(clean)) if clean else ""


class _Usage(Exception):
    pass


def _parse_args(argv, positionals, flags, value_flags):
    """Minimal argv parser: returns (positional list, opts dict).
    flags are boolean switches; value_flags take one value each."""
    pos, opts = [], {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in flags:
            opts[arg] = True
            i += 1
        elif arg in value_flags:
            if i + 1 >= len(argv):
                raise _Usage(f"{arg} requires a value")
            opts[arg] = argv[i + 1]
            i += 2
        elif arg.startswith("--"):
            raise _Usage(f"unknown flag: {arg}")
        else:
            pos.append(arg)
            i += 1
    if positionals is not None and len(pos) != positionals:
        raise _Usage(f"expected {positionals} argument(s), got {len(pos)}")
    return pos, opts


def _dispatch(argv):
    """Returns (method, path, payload) for one command line."""
    if not argv:
        raise _Usage("no command given")
    cmd, rest = argv[0], argv[1:]

    if cmd in ("status", "mode", "scan-status"):
        _parse_args(rest, 0, set(), set())
        return "GET", f"/api/librarian/{cmd}", None

    if cmd == "waiting":
        _, opts = _parse_args(rest, 0, {"--grouped"},
                              {"--channel", "--stem", "--limit"})
        params = {"channel": opts.get("--channel"), "stem": opts.get("--stem"),
                  "limit": opts.get("--limit"),
                  "grouped": "true" if opts.get("--grouped") else None}
        return "GET", "/api/librarian/waiting" + _query(params), None

    if cmd == "find":
        pos, _ = _parse_args(rest, 1, set(), set())
        return "GET", "/api/librarian/find" + _query({"text": pos[0]}), None

    if cmd in ("file", "probe"):
        pos, _ = _parse_args(rest, 2, set(), set())
        try:
            chat, msg = int(pos[0]), int(pos[1])
        except ValueError:
            raise _Usage(f"{cmd} requires integer <chat> and <msg>")
        return "GET", f"/api/librarian/{cmd}" + _query({"chat": chat, "msg": msg}), None

    if cmd == "history":
        _, opts = _parse_args(rest, 0, {"--today"},
                              {"--stem", "--batch", "--limit"})
        params = {"stem": opts.get("--stem"), "batch": opts.get("--batch"),
                  "limit": opts.get("--limit"),
                  "today": "true" if opts.get("--today") else None}
        return "GET", "/api/librarian/history" + _query(params), None

    if cmd == "lookup":
        pos, opts = _parse_args(rest, 1, set(), {"--type"})
        payload = {"name": pos[0]}
        if opts.get("--type") is not None:
            payload["media_type"] = opts["--type"]
        return "POST", "/api/librarian/lookup", payload

    if cmd in ("series", "crossid"):
        pos, _ = _parse_args(rest, 1, set(), set())
        return "POST", f"/api/librarian/{cmd}", {"ref": pos[0]}

    if cmd == "record-get":
        pos, opts = _parse_args(rest, None, set(), {"--channel", "--stem"})
        if opts.get("--channel") is not None or opts.get("--stem") is not None:
            if pos:
                raise _Usage("record-get: use either a ref or --channel/--stem, not both")
            if not (opts.get("--channel") and opts.get("--stem")):
                raise _Usage("record-get: --channel and --stem must be given together")
            return "POST", "/api/librarian/record-get", {
                "channel": opts["--channel"], "stem": opts["--stem"]}
        if len(pos) != 1:
            raise _Usage("record-get requires exactly one <ref> argument")
        return "POST", "/api/librarian/record-get", {"ref": pos[0]}

    if cmd == "record-set":
        _, opts = _parse_args(rest, 0, set(), {"--json-file"})
        if "--json-file" not in opts:
            raise _Usage("record-set requires --json-file <path|->")
        return "POST", "/api/librarian/record-set", _read_json_file(opts["--json-file"])

    if cmd == "aliases":
        _, opts = _parse_args(rest, 0, set(), {"--channel"})
        return "GET", "/api/librarian/aliases" + _query(
            {"channel": opts.get("--channel")}), None

    if cmd == "alias-set":
        pos, _ = _parse_args(rest, 3, set(), set())
        return "POST", "/api/librarian/alias-set", {
            "channel": pos[0], "stem": pos[1], "record": pos[2]}

    if cmd == "alias-forget":
        pos, _ = _parse_args(rest, 2, set(), set())
        return "POST", "/api/librarian/alias-forget", {
            "channel": pos[0], "stem": pos[1]}

    if cmd in ("park", "escalate"):
        pos, opts = _parse_args(rest, 1, set(), {"--reason"})
        if opts.get("--reason") is None:
            raise _Usage(f"{cmd} requires --reason <text>")
        return "POST", f"/api/librarian/{cmd}", {
            "key": pos[0], "reason": opts["--reason"]}

    if cmd == "unpark":
        pos, _ = _parse_args(rest, 1, set(), set())
        return "POST", "/api/librarian/unpark", {"key": pos[0]}

    if cmd in ("fix", "fix-batch"):
        _, opts = _parse_args(rest, 0, set(), {"--json-file"})
        if "--json-file" not in opts:
            raise _Usage(f"{cmd} requires --json-file <path|->")
        return "POST", f"/api/librarian/{cmd}", _read_json_file(opts["--json-file"])

    if cmd == "undo":
        pos, opts = _parse_args(rest, 2, set(), {"--request-id"})
        try:
            chat, msg = int(pos[0]), int(pos[1])
        except ValueError:
            raise _Usage("undo requires integer <chat> and <msg>")
        payload = {"chat_id": chat, "msg_id": msg}
        if opts.get("--request-id") is not None:
            payload["request_id"] = opts["--request-id"]
        return "POST", "/api/librarian/undo", payload

    if cmd == "scan":
        _, opts = _parse_args(rest, 0, set(), {"--channel"})
        payload = {}
        if opts.get("--channel") is not None:
            payload["channel"] = opts["--channel"]
        return "POST", "/api/librarian/scan", payload

    raise _Usage(f"unknown command: {cmd}")


USAGE = """usage: librarian_cli.py <command> [arguments]

  status | mode | scan-status
  waiting [--channel <chat>] [--stem <stem>] [--grouped] [--limit <n>]
  find <text>
  file <chat> <msg> | probe <chat> <msg>
  history [--stem <stem>] [--today] [--batch <id>] [--limit <n>]
  lookup <name> [--type <media_type>]
  series <provider:id> | crossid <provider:id>
  record-get <ref> | record-get --channel <chat> --stem <stem>
  record-set --json-file <path|->
  aliases [--channel <chat>]
  alias-set <channel> <stem> <record> | alias-forget <channel> <stem>
  park <key> --reason <text> | unpark <key> | escalate <key> --reason <text>
  fix --json-file <path|-> | fix-batch --json-file <path|->
  undo <chat> <msg> [--request-id <uuid>]
  scan [--channel <chat>]
"""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not (os.environ.get("LIBRARIAN_API_TOKEN") or ""):
        print("LIBRARIAN_API_TOKEN is not set", file=sys.stderr)
        return 3
    try:
        method, path, payload = _dispatch(argv)
    except _Usage as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 3
    except TransportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    try:
        status, body = _http(method, path, payload)
    except TransportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    sys.stdout.write(body)  # verbatim — never re-serialised (§9.1)
    return 0 if status == 200 else 2


if __name__ == "__main__":
    raise SystemExit(main())
