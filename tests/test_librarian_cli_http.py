"""Phase 5 Workstream E-3 — Security Low 3 closure (phase5-plan
DISP-11, §6.3, §10.4): the Librarian CLI's REAL transport, exercised
over a REAL socket against the REAL plugin router.

Why this file exists: every other CLI test replaces the CLI's
transport function with a fake (tests/test_librarian_api.py, the
``_run_cli`` helper), so the real transport — urllib request
construction, the Authorization header, the timeout, and main()'s
exit-code mapping (tools/librarian_cli.py:37-55, 251-275) — never
executed against a real HTTP surface. That generous-fake shape is
the failure mode that masked production bugs in an earlier phase.
Here the transport function is NEVER replaced, assigned, or
wrapped: each test asserts its identity against the function object
loaded from tools/librarian_cli.py before and after the run, and
the CLI is driven only through ``main()`` with the environment
pointed at a local server.

Server choice (§10.4, recorded as required): the plan's first
choice is uvicorn serving the FastAPI app. Checked at build time
in the suite environment: ``uvicorn``, ``fastapi`` and ``starlette``
are ALL unavailable (the Phase 3/4 harness stubs fastapi precisely
because it is absent), so there is no ASGI app to serve and
TestClient is not a substitute (§10.4). The sanctioned fallback is
used: a stdlib ``http.server.ThreadingHTTPServer`` bound to a real
socket, whose handler dispatches each wire request to the REAL
route handler from the REAL ``librarian_routes`` module (loaded by
the real loader, with the Phase 3/4 harness's fake DB / settings at
the real module boundaries — the doubles that harness already
maintains) and serialises the handler's real response onto the
wire. The transport under test (the CLI's urllib stack) and the
router handlers are both production code; only the HTTP server
plumbing is stdlib harness, which is the point of the fallback:
a real socket + real HTTP.

Security S5-11 constraints, all honoured here: the server binds
127.0.0.1 only, on an ephemeral port; the pairing token is the
harness's synthetic test token (minted against the fake settings —
never a real token); LIBRARIAN_API_URL / LIBRARIAN_API_TOKEN are
set explicitly in-process for every invocation, never inherited
from the ambient environment (and restored afterwards); the
constructed URL's host is asserted loopback before the first call;
the closed-port failure case uses a loopback address.

Scan note: §10.4's scan case asserts the §5.2 test-mode refusal
shape. Workstream D's route has landed (D-2), so the scan test
below asserts ``refused_precondition`` ONLY — the stub-era
either-state acceptance was discharged in D-2's commit, as the
Senior Review of E-3 required.

Undo deferral history (recorded here at C-2, closing the gap the
Senior Review of E-3 named: the deferral was noted in QA's E-3
report but not in this docstring): §6.3 made E-3's coverage of
``undo --target-request-id`` conditional — "if OQ-1's CLI change
lands in Workstream C, E-3's coverage includes it as a dispatch
case through the real transport". E-3 landed (c699e85) BEFORE
Workstream C: the CLI had no ``--target-request-id`` flag yet
(DISP-8 was still awaiting Yan's OQ-1 ratification), so the case
could not be written then and was deferred to C-2, the commit
that adds the flag. The undo case below is that deferred case.
The undo route at this tree is still the ``not_implemented``
stub (the real route lands with C-1), so — exactly as this
file's scan case did before D-2 — the case pins the wire
behaviour (POST dispatch, auth header, payload byte-shape
including ``target_request_id``) and accepts the stub's response
state; tightening it to the real route's shapes is C-1's
follow-up, mirroring the scan case's D-2 tightening.
"""

import asyncio
import io
import json
import os
import socket
import sys
import tempfile
import threading
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_librarian_api as api  # noqa: E402  (installs the harness)
import test_librarian_engine as eng  # noqa: E402  (engine + its fakes)

cli = api.cli
# The one real transport function object, captured at import. Every
# CLI invocation in this file asserts the module still holds THIS
# object — the runtime proof that the transport was never stubbed.
_REAL_HTTP = cli._http

TOKEN = api.TEST_TOKEN  # synthetic, against the harness fake settings
CHAT = eng.CHAT


# ------------------------------------------------------------------
# Real-socket HTTP server dispatching to the real route handlers
# ------------------------------------------------------------------

def _dispatch_http(method, raw_path, headers, body_bytes):
    """One wire request -> the real router handler -> (status, bytes)."""
    parsed = urllib.parse.urlparse(raw_path)
    query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
    handler = api.ROUTE_MAP.get((method, parsed.path))
    if handler is None:
        return 404, json.dumps({"error": "no such route"}).encode("utf-8")
    raw_body = body_bytes.decode("utf-8") if body_bytes else None
    request = api.FakeRequest(headers=headers, query=query, raw_body=raw_body)
    try:
        resp = asyncio.run(handler(request))
    except api.HTTPException as exc:
        return exc.status_code, json.dumps(
            {"error": str(exc.detail)}).encode("utf-8")
    return resp.status_code, json.dumps(resp.content).encode("utf-8")


class _Server:
    """ThreadingHTTPServer on 127.0.0.1, ephemeral port, recording
    every request (headers as received) and every response body."""

    def __init__(self):
        self.records = []
        records = self.records

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence server logging
                pass

            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                status, payload = _dispatch_http(
                    method, self.path, dict(self.headers.items()), body)
                records.append({
                    "method": method, "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "content_type": self.headers.get("Content-Type"),
                    "request_body": body,
                    "status": status, "response_body": payload,
                })
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()
        return False


# ------------------------------------------------------------------
# CLI driver — environment set explicitly, transport never touched
# ------------------------------------------------------------------

def _run_cli(argv, base_url, token):
    assert cli._http is _REAL_HTTP, "transport must be the real function"
    parsed = urllib.parse.urlparse(base_url)
    assert parsed.scheme == "http", parsed
    assert parsed.hostname in ("127.0.0.1", "::1"), (
        f"S5-11: refusing a non-loopback LIBRARIAN_API_URL: {base_url}")
    old_url = os.environ.get("LIBRARIAN_API_URL")
    old_token = os.environ.get("LIBRARIAN_API_TOKEN")
    os.environ["LIBRARIAN_API_URL"] = base_url
    os.environ["LIBRARIAN_API_TOKEN"] = token
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
    finally:
        if old_url is None:
            os.environ.pop("LIBRARIAN_API_URL", None)
        else:
            os.environ["LIBRARIAN_API_URL"] = old_url
        if old_token is None:
            os.environ.pop("LIBRARIAN_API_TOKEN", None)
        else:
            os.environ["LIBRARIAN_API_TOKEN"] = old_token
    assert cli._http is _REAL_HTTP, "transport changed during the run"
    return code, out.getvalue(), err.getvalue()


def _closed_port_url():
    """A loopback URL whose port is closed (bound, read, released)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


# ------------------------------------------------------------------
# The transport itself is the production function (identity proof)
# ------------------------------------------------------------------

def test_http_transport_is_real_unstubbed_http():
    assert cli._http is _REAL_HTTP
    # The function the CLI calls is the one defined in the real file
    # and it drives urllib's urlopen — not a harness substitute.
    assert cli._http.__code__.co_filename.endswith(
        str(Path("tools") / "librarian_cli.py"))
    assert "urlopen" in cli._http.__code__.co_names
    assert cli.TIMEOUT == 120


# ------------------------------------------------------------------
# status — GET, token header as received, verbatim body
# ------------------------------------------------------------------

def test_http_status_get_token_header_and_verbatim_body():
    eng._reset()
    api._enable(librarian_mode="ask", librarian_edit_budget_per_day=10,
                librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    api._seed_ledger([api._ledger_row(msg_id=1),
                      api._ledger_row(msg_id=2, status="parked")])
    with _Server() as server:
        code, out, err = _run_cli(["status"], server.url, TOKEN)
        assert code == 0, (code, err)
        assert len(server.records) == 1
        rec = server.records[0]
        assert rec["method"] == "GET"
        assert rec["path"] == "/api/librarian/status"
        # The header exactly as the server received it off the wire.
        assert rec["authorization"] == f"Bearer {TOKEN}"
        assert rec["status"] == 200
        # Byte-identical: stdout is the server's response body,
        # never re-serialised by the CLI (§9.1).
        assert out.encode("utf-8") == rec["response_body"]
        body = json.loads(out)
        assert set(body.keys()) == {"enabled", "mode", "test_mode",
                                    "writes_enabled", "budget", "channels",
                                    "channel_types", "counts", "userbot",
                                    "bot"}
        assert body["enabled"] is True and body["mode"] == "ask"
        assert body["counts"] == {"waiting": 1, "parked": 1,
                                  "escalated": 0, "fixed": 0}


# ------------------------------------------------------------------
# fix --json-file (test mode) — the full stack runs over the wire
# ------------------------------------------------------------------

def test_http_fix_json_file_test_mode_creates_dry_run_row():
    eng._reset()
    api._enable(librarian_channels=["-100111"],
                librarian_channel_types={"-100111": "tv"})
    eng._settings()  # test mode on (the Phase 4 harness posture)
    eng._seed_ledger()
    msg = eng._message()
    eng._clients({(CHAT, 1): msg}, {(CHAT, 1): msg})
    payload = eng._payload()
    with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(payload, fh)
        path = fh.name
    try:
        with _Server() as server:
            code, out, err = _run_cli(
                ["fix", "--json-file", path], server.url, TOKEN)
            assert code == 0, (code, err, out)
            assert len(server.records) == 1
            rec = server.records[0]
            assert rec["method"] == "POST"
            assert rec["path"] == "/api/librarian/fix"
            assert rec["authorization"] == f"Bearer {TOKEN}"
            assert rec["content_type"] == "application/json"
            # The payload crossed the wire byte-exactly as the CLI
            # serialised the --json-file document.
            assert json.loads(rec["request_body"].decode("utf-8")) == payload
            assert out.encode("utf-8") == rec["response_body"]
            body = json.loads(out)
            assert body["state"] == "dry_run_complete"
            assert body["dry_run"] is True
    finally:
        os.unlink(path)
    # The full stack ran: the dry-run row exists in the fake DB.
    docs = api.fake_db.dbs["tracking"]["librarian_fixes"].docs
    assert len(docs) == 1
    row = docs[0]
    assert row["request_id"] == payload["request_id"]
    assert row["state"] == "dry_run_complete"
    assert row["dry_run"] is True and row["budget_counted"] is False


# ------------------------------------------------------------------
# Bad token — HTTP 401 over the wire maps to exit 2
# ------------------------------------------------------------------

def test_http_bad_token_exit_2():
    eng._reset()
    api._enable()
    with _Server() as server:
        code, out, err = _run_cli(["status"], server.url, "wrong-token")
        assert code == 2, (code, out, err)
        assert len(server.records) == 1
        rec = server.records[0]
        assert rec["authorization"] == "Bearer wrong-token"
        assert rec["status"] == 401
        assert out.encode("utf-8") == rec["response_body"]
        assert json.loads(out) == {"error": "missing or invalid token"}


# ------------------------------------------------------------------
# Transport failure — closed loopback port maps to exit 3
# ------------------------------------------------------------------

def test_http_closed_port_exit_3():
    eng._reset()
    api._enable()
    url = _closed_port_url()
    code, out, err = _run_cli(["status"], url, TOKEN)
    assert code == 3, (code, out, err)
    assert out == ""
    assert "error:" in err  # the failure is reported on stderr


# ------------------------------------------------------------------
# scan — POST dispatch through the real transport (see docstring:
# the §5.2 refusal-shape tightening lands with Workstream D)
# ------------------------------------------------------------------

def test_http_scan_dispatch_over_real_transport():
    eng._reset()
    api._enable(librarian_channels=["-100111"])
    eng._settings()  # test mode on
    with _Server() as server:
        code, out, err = _run_cli(["scan"], server.url, TOKEN)
        assert code == 0, (code, err, out)
        assert len(server.records) == 1
        rec = server.records[0]
        assert rec["method"] == "POST"
        assert rec["path"] == "/api/librarian/scan"
        assert rec["authorization"] == f"Bearer {TOKEN}"
        assert json.loads(rec["request_body"].decode("utf-8")) == {}
        assert out.encode("utf-8") == rec["response_body"]
        body = json.loads(out)
        # Workstream D landed (D-2): the scan route is real, and in
        # test mode it refuses with refused_precondition ONLY — the
        # either-state acceptance from the stub era is discharged.
        assert body["state"] == "refused_precondition", body
        assert "test mode" in body["error"]


# ------------------------------------------------------------------
# undo --target-request-id — POST dispatch through the real
# transport (the E-3 case deferred to C-2; see the docstring's
# deferral history). The undo route is still the not_implemented
# stub at this tree, so the case pins the wire behaviour and
# accepts the stub's response state, exactly as the scan case
# did before D-2.
# ------------------------------------------------------------------

def test_http_undo_target_request_id_dispatch_over_real_transport():
    eng._reset()
    api._enable()
    with _Server() as server:
        code, out, err = _run_cli(
            ["undo", str(CHAT), "55", "--request-id", "u-1",
             "--target-request-id", "fix-1"], server.url, TOKEN)
        assert code == 0, (code, err, out)
        assert len(server.records) == 1
        rec = server.records[0]
        assert rec["method"] == "POST"
        assert rec["path"] == "/api/librarian/undo"
        assert rec["authorization"] == f"Bearer {TOKEN}"
        assert rec["content_type"] == "application/json"
        # Payload byte-shape over the wire: the undo's own key and
        # the target fix's key cross as distinct fields (DISP-8).
        assert json.loads(rec["request_body"].decode("utf-8")) == {
            "chat_id": CHAT, "msg_id": 55,
            "request_id": "u-1", "target_request_id": "fix-1"}
        assert out.encode("utf-8") == rec["response_body"]
        # The stub-era response state, accepted as the scan case
        # accepted it before D-2; C-1 tightens this when the real
        # undo route lands.
        assert json.loads(out) == {"state": "not_implemented"}
