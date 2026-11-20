"""Phase 4 Workstream C1 tests — the writer-token security seam
(DESIGN §8 step 6 / §9; phase4-plan §4.1; security W-1…W-8, C-2, C-3).

Reuses the Phase 3 suite's harness (``test_librarian_api``): the REAL
``librarian_routes``, ``api_routes`` settings functions and ``backup``
against the same production-shaped fakes. The writer action's
Telegram validation (``_resolve_bot_identity``) is stubbed at the
exact attribute the route lazy-imports — the real helper performs a
live ``getMe`` and is never exercised against the network here.

Covered: W-2/W-4 payload non-settability for all four writer keys;
W-3 set-time validation (format, getMe failure, streaming/pool
identity collision, one atomic update carrying exactly four keys);
W-5 redaction points 1 (settings response), 2 (settings_page context,
the Low 1 scoped fix), 3 (backup export/import, incl. the C-3
last-seen line), 5/6 (bot surface + admin surfaces carry no token);
W-6 ``status.bot`` derivation both ways; W-7 clear semantics.
"""

import asyncio
import hashlib
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_librarian_api as api  # noqa: E402  (installs the harness)

from _loader import load_module, run  # noqa: E402


def _run(coro):
    """asyncio.run returning the result (the harness's run() discards
    it; the Phase 3 tests that need a value use asyncio.run directly)."""
    return asyncio.run(coro)

ROOT = Path(__file__).resolve().parent.parent

routes = api.routes
ROUTE_MAP = api.ROUTE_MAP
FakeRequest = api.FakeRequest
FakeSettingsManager = api.FakeSettingsManager
HTTPException = api.HTTPException

# A format-valid bot-token shape (own_bots TOKEN_RE: ^\d{5,}:[\w-]{30,}$)
# whose literal value must never appear in any response, export, or
# rendered context — the W-5 sentinel.
WRITER_SENTINEL = "123456789:AAEwriter-sentinel_token-material-0123456789abcd"
WRITER_BOT = {"user_id": 424242001, "username": "fixture_writer_bot",
              "name": "Fixture Writer"}


def _reset():
    api._reset_api()
    api.api_routes._resolve_bot_identity = _resolver_ok
    api.STUBS["bot"].StreamBot = None
    api.STUBS["bot"].multi_clients = {}


async def _resolver_ok(token):
    return dict(WRITER_BOT)


async def _resolver_none(token):
    return None


def _session_req(body=None, method_session=True):
    return FakeRequest(body=body, session={"authenticated": True})


def _set_writer_action(body):
    handler = ROUTE_MAP[("POST", "/admin/librarian/writer-token")]
    return _run(handler(_session_req(body=body)))


def _status_bot():
    resp = api._call("GET", "/api/librarian/status", api._authed_req())
    assert resp.status_code == 200
    return resp.content["bot"]


# ------------------------------------------------------------------
# W-2 — the action is session-authed (require_auth), like the pairing
# action; no pairing-token route exists for it.
# ------------------------------------------------------------------

def test_W2_writer_routes_are_session_authed():
    _reset()
    for method, path in (("POST", "/admin/librarian/writer-token"),
                         ("DELETE", "/admin/librarian/writer-token")):
        handler = ROUTE_MAP[(method, path)]
        defaults = handler.__defaults__ or ()
        deps = [d for d in defaults if isinstance(d, api._Depends)]
        assert deps and deps[0].dependency is api.STUBS["credentials"].require_auth, (method, path)
    # No pairing-authed (bot API) route touches the writer token.
    for (method, path) in ROUTE_MAP:
        if path.startswith("/api/librarian/"):
            assert "writer" not in path, path


# ------------------------------------------------------------------
# W-2 / W-4 — generic settings payload: non-empty -> 400 naming the
# action; empty -> dropped (leave-unchanged). All four writer keys.
# ------------------------------------------------------------------

def test_W2_settings_payload_rejects_writer_keys():
    _reset()

    async def go():
        for key, value in (("librarian_writer_token", WRITER_SENTINEL),
                           ("librarian_writer_token_fingerprint", "0123456789ab"),
                           ("librarian_writer_bot_id", 424242001),
                           ("librarian_writer_bot_username", "fixture_writer_bot")):
            FakeSettingsManager.update_calls = []
            try:
                await api.api_routes.update_settings_api({key: value})
                raise AssertionError(f"expected 400 for {key}")
            except HTTPException as e:
                assert e.status_code == 400, key
                assert "writer-token action" in e.detail, (key, e.detail)
            assert FakeSettingsManager.update_calls == [], key
        # Empty values are dropped, the save proceeds, nothing changes.
        for key in ("librarian_writer_token", "librarian_writer_token_fingerprint",
                    "librarian_writer_bot_id", "librarian_writer_bot_username"):
            FakeSettingsManager.update_calls = []
            await api.api_routes.update_settings_api({key: ""})
            assert FakeSettingsManager.update_calls, key
            assert key not in FakeSettingsManager.update_calls[-1], key

    run(go())


# ------------------------------------------------------------------
# W-3 — set-time validation and identity binding
# ------------------------------------------------------------------

def test_W3_set_rejects_bad_format_and_unverifiable():
    _reset()
    api._enable()
    # Empty / missing token.
    resp = _set_writer_action({"token": "   "})
    assert resp.status_code == 400 and FakeSettingsManager.update_calls == []
    resp = _set_writer_action({})
    assert resp.status_code == 400 and FakeSettingsManager.update_calls == []
    # Format-invalid (fails the tree's own TOKEN_RE shape).
    resp = _set_writer_action({"token": "not-a-bot-token"})
    assert resp.status_code == 400 and FakeSettingsManager.update_calls == []
    # getMe cannot verify (invalid / unreachable / non-bot -> None).
    api.api_routes._resolve_bot_identity = _resolver_none
    resp = _set_writer_action({"token": WRITER_SENTINEL})
    assert resp.status_code == 400
    assert "nothing was saved" in resp.content["error"]
    assert FakeSettingsManager.update_calls == []
    assert FakeSettingsManager.current().librarian_writer_token == ""


def test_W3_set_rejects_streaming_and_pool_identities():
    _reset()
    api._enable()
    # Streaming bot collision.
    api.STUBS["bot"].StreamBot = types.SimpleNamespace(
        me=types.SimpleNamespace(id=WRITER_BOT["user_id"]))
    resp = _set_writer_action({"token": WRITER_SENTINEL})
    assert resp.status_code == 400, resp.content
    assert FakeSettingsManager.update_calls == []
    # Pool bot collision (streaming identity now different).
    api.STUBS["bot"].StreamBot = types.SimpleNamespace(
        me=types.SimpleNamespace(id=111))
    api.STUBS["bot"].multi_clients = {
        0: types.SimpleNamespace(me=types.SimpleNamespace(id=WRITER_BOT["user_id"])),
    }
    resp = _set_writer_action({"token": WRITER_SENTINEL})
    assert resp.status_code == 400, resp.content
    assert FakeSettingsManager.update_calls == []


def test_W3_set_happy_path_one_atomic_update():
    _reset()
    api._enable()
    resp = _set_writer_action({"token": "  " + WRITER_SENTINEL + "  "})
    assert resp.status_code == 200, resp.content
    fingerprint = hashlib.sha256(WRITER_SENTINEL.encode()).hexdigest()[:12]
    # Exactly one update() carrying exactly the four keys (W-3).
    assert FakeSettingsManager.update_calls == [{
        "librarian_writer_token": WRITER_SENTINEL,
        "librarian_writer_token_fingerprint": fingerprint,
        "librarian_writer_bot_id": WRITER_BOT["user_id"],
        "librarian_writer_bot_username": WRITER_BOT["username"],
    }]
    # Response confirms identity, never the token (W-5 point 5/6 shape).
    assert resp.content == {"set": True, "username": WRITER_BOT["username"],
                            "fingerprint": fingerprint}
    assert WRITER_SENTINEL not in json.dumps(resp.content)
    # And status.bot flips to present (W-6).
    assert _status_bot() == "present"


def test_W7_clear_empties_all_four_keys():
    _reset()
    api._enable()
    _set_writer_action({"token": WRITER_SENTINEL})
    assert _status_bot() == "present"
    FakeSettingsManager.update_calls = []
    handler = ROUTE_MAP[("DELETE", "/admin/librarian/writer-token")]
    resp = _run(handler(_session_req()))
    assert resp.status_code == 200 and resp.content == {"set": False}
    assert FakeSettingsManager.update_calls == [{
        "librarian_writer_token": "",
        "librarian_writer_token_fingerprint": "",
        "librarian_writer_bot_id": 0,
        "librarian_writer_bot_username": "",
    }]
    assert _status_bot() == "absent"


# ------------------------------------------------------------------
# W-6 — status.bot derivation, both ways (purely local conjunction)
# ------------------------------------------------------------------

def test_W6_status_bot_derivation():
    _reset()
    api._enable()
    assert _status_bot() == "absent"                       # nothing stored
    api._set_settings(librarian_writer_token=WRITER_SENTINEL,
                      librarian_writer_bot_id=0)
    assert _status_bot() == "absent"                       # token, no id
    api._set_settings(librarian_writer_token="",
                      librarian_writer_bot_id=WRITER_BOT["user_id"])
    assert _status_bot() == "absent"                       # id, no token
    api._set_settings(librarian_writer_token=WRITER_SENTINEL)
    assert _status_bot() == "present"                      # both


# ------------------------------------------------------------------
# W-5 point 1 — settings API response blanking
# ------------------------------------------------------------------

def test_W5_settings_response_blanks_token():
    _reset()
    api._set_settings(librarian_writer_token=WRITER_SENTINEL,
                      librarian_writer_token_fingerprint="0123456789ab",
                      librarian_writer_bot_id=WRITER_BOT["user_id"],
                      librarian_writer_bot_username=WRITER_BOT["username"])

    async def go():
        result = await api.api_routes.get_settings_api()
        blob = json.dumps(result)
        assert WRITER_SENTINEL not in blob
        settings = result["settings"]
        assert settings["librarian_writer_token"] == ""
        assert settings["librarian_writer_token_set"] is True
        # Fingerprint + identity remain — the display path (W-4).
        assert settings["librarian_writer_token_fingerprint"] == "0123456789ab"
        assert settings["librarian_writer_bot_username"] == WRITER_BOT["username"]

    run(go())


# ------------------------------------------------------------------
# W-5 point 3 + C-3 — backup export/import exclusions, both directions
# ------------------------------------------------------------------

def test_W5_backup_export_omits_writer_keys_and_last_seen():
    _reset()
    api._set_settings(librarian_writer_token=WRITER_SENTINEL,
                      librarian_writer_token_fingerprint="0123456789ab",
                      librarian_agent_last_seen="2026-11-20T00:00:00+00:00")

    async def go():
        result = await api.backup.export_config()
        blob = json.dumps(result)
        assert WRITER_SENTINEL not in blob
        assert "librarian_writer_token" not in result["settings"]
        assert "librarian_writer_token_fingerprint" not in result["settings"]
        assert "librarian_agent_last_seen" not in result["settings"]

    run(go())


def test_W5_backup_import_cannot_set_writer_keys_or_last_seen():
    _reset()
    api._set_settings(librarian_writer_token="original-writer-token")

    async def go():
        payload = {"app": "telegram-stremio",
                   "settings": {"librarian_writer_token": WRITER_SENTINEL,
                                "librarian_writer_token_fingerprint": "0123456789ab",
                                "librarian_agent_last_seen": "2020-01-01T00:00:00",
                                "librarian_mode": "ask"}}
        await api.backup.import_config(payload)
        assert FakeSettingsManager.update_calls, "update() should have been called"
        for call in FakeSettingsManager.update_calls:
            assert "librarian_writer_token" not in call
            assert "librarian_writer_token_fingerprint" not in call
            assert "librarian_agent_last_seen" not in call
        assert FakeSettingsManager.current().librarian_writer_token == "original-writer-token"

    run(go())


# ------------------------------------------------------------------
# W-5 point 2 / C-2 (Low 1 scoped fix) — settings_page() blanks the
# writer token, the pairing hash, and the session secret BEFORE the
# dict enters the Jinja context.
# ------------------------------------------------------------------

def _load_template_routes():
    # template_routes' import surface beyond the Phase 3 harness:
    # faithful minimal additions, each mirroring the real module's
    # shape (never called by settings_page except as noted).
    bot = api.STUBS["bot"]
    if not hasattr(bot, "work_loads_summary"):
        bot.work_loads_summary = lambda: {}
    creds = api.STUBS["credentials"]
    if not hasattr(creds, "revoke_session"):
        creds.revoke_session = lambda request: None
        creds.verify_credentials = lambda u, p: False
    sys.modules.setdefault("Backend.config", api._permissive("Backend.config"))
    if "Backend.helper.bounded" not in sys.modules:
        sys.modules["Backend.helper.bounded"] = api._permissive("Backend.helper.bounded")
    stream_buffer = sys.modules["Backend.helper.stream_buffer"]
    if not hasattr(stream_buffer, "snapshot"):
        stream_buffer.snapshot = lambda: {}
    # FakeTemplates stands in for Jinja2Templates; template_routes
    # touches templates.env.globals at import (real Jinja2Templates
    # surface), so give the fake that attribute.
    if not hasattr(api.FakeTemplates, "env"):
        api.FakeTemplates.env = types.SimpleNamespace(globals={})
    return load_module("template_routes_under_test",
                       "Backend/fastapi/routes/template_routes.py")


def test_W5_settings_page_context_blanks_secrets():
    _reset()
    template_routes = _load_template_routes()
    hash_sentinel = "hash" * 16
    secret_sentinel = "session-secret-sentinel-value"
    api._set_settings(**{"librarian_writer_token": WRITER_SENTINEL,
                         "librarian_api_token_hash": hash_sentinel,
                         "session_secret": secret_sentinel})
    template_routes.templates.calls = []
    _run(template_routes.settings_page(_session_req()))
    assert template_routes.templates.calls, "settings_page rendered nothing"
    _name, ctx = template_routes.templates.calls[-1]
    settings = ctx["settings"]
    assert settings["librarian_writer_token"] == ""
    assert settings["librarian_api_token_hash"] == ""
    assert settings["session_secret"] == ""
    blob = json.dumps(ctx, default=str)
    assert WRITER_SENTINEL not in blob
    assert hash_sentinel not in blob
    assert secret_sentinel not in blob
    # The stored values themselves are untouched — only the context copy.
    current = FakeSettingsManager.current()
    assert current.librarian_writer_token == WRITER_SENTINEL
    assert current.librarian_api_token_hash == hash_sentinel


# ------------------------------------------------------------------
# F2 (Phase 4 gate follow-up) — the settings PAGE showed "Pairing
# token: Not set" while a token WAS set. The JSON API computes
# librarian_api_token_hash_set from the real value before blanking
# (R1); the page route blanked the hash WITHOUT setting the flag, so
# the template's fallback read the blanked hash and rendered
# "Not set". The page route must mirror the API: flag computed from
# the stored value first, hash still never in context or HTML (C-2).
# ------------------------------------------------------------------

def _render_settings(ctx):
    """Render the REAL settings.html with a captured route context,
    via the templates suite's env (stub base). Returns None where
    jinja2 is unavailable — the context assertions below always run;
    render assertions run in envs that have jinja2 (app/staging)."""
    import test_librarian_templates as tpl

    source = tpl.SETTINGS_HTML.read_text(encoding="utf-8")
    return tpl._try_render("settings.html", source, ctx)


def test_F2_settings_page_context_sets_pairing_flag():
    _reset()
    template_routes = _load_template_routes()
    hash_sentinel = "hash" * 16
    api._set_settings(**{"librarian_api_token_hash": hash_sentinel})
    template_routes.templates.calls = []
    _run(template_routes.settings_page(_session_req()))
    assert template_routes.templates.calls, "settings_page rendered nothing"
    _name, ctx = template_routes.templates.calls[-1]
    settings = ctx["settings"]
    # The flag mirrors the API: computed from the stored value...
    assert settings["librarian_api_token_hash_set"] is True
    # ...while the hash itself is still blanked and nowhere in ctx.
    assert settings["librarian_api_token_hash"] == ""
    assert hash_sentinel not in json.dumps(ctx, default=str)
    # The stored value itself is untouched — only the context copy.
    assert FakeSettingsManager.current().librarian_api_token_hash == hash_sentinel
    # Rendered label logic yields "Set"; the hash is nowhere in HTML.
    html = _render_settings(ctx)
    if html is not None:
        assert hash_sentinel not in html
        assert 'id="librarian-token-status">Set<' in html

    # Negative direction: no stored hash -> flag False, "Not set".
    _reset()
    api._set_settings(**{"librarian_api_token_hash": ""})
    template_routes.templates.calls = []
    _run(template_routes.settings_page(_session_req()))
    assert template_routes.templates.calls, "settings_page rendered nothing"
    _name, ctx = template_routes.templates.calls[-1]
    assert ctx["settings"]["librarian_api_token_hash_set"] is False
    html = _render_settings(ctx)
    if html is not None:
        assert 'id="librarian-token-status">Not set<' in html
