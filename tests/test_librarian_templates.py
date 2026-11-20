"""Phase 4 Workstream C5 — template tests (phase4-plan §4.6, §9.5).

Covers the two C5 template surfaces:

- ``Backend/fastapi/templates/librarian.html`` — the Fix history
  section: seeded rows render action / state / IST time / before &
  after captions with a "dry run" label on dry-run rows; an Undo
  action is rendered only for a fixed, non-dry-run, action=="edit"
  row (Phase 5 C-3a, §4.1 eligibility, Tech Lead ruling (b));
  captions render under Jinja autoescaping — no ``|safe`` on
  any caption field (security A-2(iv)).
- ``Backend/fastapi/templates/settings.html`` — the Librarian
  card's Writer bot block (security A-6 / W-5 point 6 / W-7):
  username + fingerprint + set/not-set only, a password input wired
  to the dedicated writer-token action
  (``POST``/``DELETE /admin/librarian/writer-token``, plan §4.1.3),
  the BotFather rotation truth as help text, and NO writer key in
  ``saveSettings()``'s payload. Sentinel render: a sentinel writer
  token placed in the settings context appears nowhere in the
  rendered HTML (W-5.6).

Render tests use jinja2 when importable (the app env has it; the
plain-python suite env may not — the Phase 3 pattern: static
assertions always run, render assertions run when jinja2 is
available, with autoescape selected exactly as FastAPI's
Jinja2Templates selects it, so the double is production-shaped).
The page-handler context contract these renders use: the handler
(Backend C, librarian_routes.py) adds ``fix_history`` — the
``list_history(limit=50)`` rows projected with a preformatted IST
``time_ist`` string per row.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB_HTML = ROOT / "Backend/fastapi/templates/librarian.html"
SETTINGS_HTML = ROOT / "Backend/fastapi/templates/settings.html"

WRITER_SENTINEL = "999999:WRITER-TOKEN-SENTINEL-xxxxxxxxxxxx"
WRITER_FINGERPRINT = "a1b2c3d4e5f6"
WRITER_USERNAME = "mediamanagerbot"


def _jinja_env(template_name, source):
    import jinja2

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(
            str(ROOT / "Backend/fastapi/templates")),
        autoescape=jinja2.select_autoescape(["html", "xml"]),
    )
    env.loader = jinja2.DictLoader({
        "base.html": "{% block content %}{% endblock %}",
        template_name: source,
    })
    return env


def _page_ctx(**over):
    ctx = {
        "request": None, "theme": {}, "themes": [], "current_theme": "x",
        "current_user": "u",
        "counts": {"waiting": 0, "parked": 0, "escalated": 0, "fixed": 0},
        "ops_today_total": 0, "ops_today_fixed": 0,
        "agent_last_seen": None, "librarian_enabled": True,
        "groups": [], "page": 1, "total_pages": 1,
        "escalated": [], "records": [], "aliases": [], "channels": [],
    }
    ctx.update(over)
    return ctx


def _fix_row(**kw):
    row = {
        "request_id": "req-0001",
        "action": "edit", "state": "dry_run_complete", "dry_run": True,
        "time_ist": "20 Nov 2026, 19:05 IST", "stem": "some show",
        "old_caption": "Old caption tt1111111",
        "new_caption": "New caption tt1234567",
        "old_caption_text": "Old caption tt1111111",
        "new_caption_text": "New caption tt1234567",
        "error": None, "created_at": "2026-11-20T13:35:00+00:00",
    }
    row.update(kw)
    return row


def _settings_ctx(settings):
    return {
        "request": None, "theme": {}, "themes": [], "current_theme": "x",
        "current_user": "u", "userbot_configured": False,
        "formatter_presets": [], "buffer_health": {}, "prewarm_health": {},
        "settings": settings,
    }


def _try_render(template_name, source, ctx):
    try:
        import jinja2  # noqa: F401
    except ImportError:
        print(f"NOTE C5: jinja2 unavailable in suite env — {template_name} "
              "render assertions deferred to the staging exercise; "
              "static assertions already ran.")
        return None
    return _jinja_env(template_name, source).get_template(
        template_name).render(**ctx)


# ------------------------------------------------------------------
# librarian.html — Fix history section (§4.6, §9.5 "C5 renders")
# ------------------------------------------------------------------

def test_C5_fix_history_section_static_shape():
    text = LIB_HTML.read_text(encoding="utf-8")
    start = text.index("Fix history")
    end = text.index("Series records")
    section = text[start:end]
    # Renders the §4.6 fields from the fix_history context rows.
    for needle in ("fix_history", "f.action", "f.state", "f.dry_run",
                   "dry run", "f.old_caption", "f.new_caption"):
        assert needle in section, needle
    # Phase 5 C-3a: an Undo button is rendered in the section, gated
    # by the §4.1 / ruling (b) condition — fixed, non-dry-run, edit
    # only — keyed by the row's request_id, reusing .lib-btn verbatim.
    assert 'data-a="undo"' in section
    assert 'data-k="{{ f.request_id }}"' in section
    assert ">Undo</button>" in section
    assert 'class="lib-btn"' in section
    assert ('f.state == "fixed" and f.dry_run == false '
            'and f.action == "edit"') in section
    # Empty state when no history rows are in context.
    assert "No fixes recorded yet." in section


def test_C5_fix_history_render_rows_and_labels():
    source = LIB_HTML.read_text(encoding="utf-8")
    rows = [
        _fix_row(),
        _fix_row(state="refused_drift", error="caption changed",
                 time_ist="20 Nov 2026, 19:06 IST", stem="other show"),
        _fix_row(state="fixed", dry_run=False, action="edit",
                 time_ist="20 Nov 2026, 19:07 IST"),
    ]
    html = _try_render("librarian.html", source,
                       _page_ctx(fix_history=rows))
    if html is None:
        return
    assert "Fix history" in html
    assert "dry_run_complete" in html and "refused_drift" in html
    assert "20 Nov 2026, 19:05 IST" in html
    assert "Old caption tt1111111" in html
    assert "New caption tt1234567" in html
    assert "caption changed" in html
    # Exactly the two dry-run rows carry the label; the real row does not
    # (label count == dry-run row count).
    assert html.count("dry run") == 2, html.count("dry run")
    # Absent fix_history -> the section renders its empty state.
    html = _try_render("librarian.html", source, _page_ctx())
    assert "No fixes recorded yet." in html


def test_C5_fix_history_undo_render_eligible_row():
    """Phase 5 C-3a (i): a fixed, non-dry-run, action=="edit" row
    renders Undo with data-k equal to its request_id."""
    source = LIB_HTML.read_text(encoding="utf-8")
    rows = [_fix_row(state="fixed", dry_run=False, action="edit",
                     request_id="req-undo-1")]
    html = _try_render("librarian.html", source,
                       _page_ctx(fix_history=rows))
    if html is None:
        return
    assert ('<button type="button" class="lib-btn" data-a="undo" '
            'data-k="req-undo-1">Undo</button>') in html
    assert html.count('data-a="undo"') == 1


def test_C5_fix_history_undo_not_rendered_for_non_edit_action():
    """Phase 5 C-3a (ii) — ruling (b) pin: a fixed, non-dry-run row
    whose action is not "edit" renders NO Undo."""
    source = LIB_HTML.read_text(encoding="utf-8")
    for action in ("repost", "delete", "skip"):
        rows = [_fix_row(state="fixed", dry_run=False, action=action,
                         request_id=f"req-{action}-1")]
        html = _try_render("librarian.html", source,
                           _page_ctx(fix_history=rows))
        if html is None:
            return
        assert 'data-a="undo"' not in html, action
        assert ">Undo</button>" not in html, action


def test_C5_fix_history_undo_not_rendered_for_dry_run_or_non_fixed():
    """Phase 5 C-3a (iii): dry-run rows and non-fixed rows render
    NO Undo, even when the action is "edit"."""
    source = LIB_HTML.read_text(encoding="utf-8")
    ineligible = [
        # Dry-run: state fixed but dry_run True.
        _fix_row(state="fixed", dry_run=True, action="edit",
                 request_id="req-dry-1"),
        # Non-fixed, non-dry-run edit rows.
        _fix_row(state="dry_run_complete", dry_run=False, action="edit",
                 request_id="req-dry-complete-1"),
        _fix_row(state="refused_drift", dry_run=False, action="edit",
                 request_id="req-refused-1", error="caption changed"),
        _fix_row(state="failed", dry_run=False, action="edit",
                 request_id="req-failed-1", error="boom"),
    ]
    for row in ineligible:
        html = _try_render("librarian.html", source,
                           _page_ctx(fix_history=[row]))
        if html is None:
            return
        assert 'data-a="undo"' not in html, row["request_id"]
        assert ">Undo</button>" not in html, row["request_id"]
    # Mixed history: exactly the one eligible row renders Undo,
    # keyed by its own request_id.
    eligible = _fix_row(state="fixed", dry_run=False, action="edit",
                        request_id="req-eligible-1")
    html = _try_render("librarian.html", source,
                       _page_ctx(fix_history=ineligible + [eligible]))
    if html is None:
        return
    assert html.count('data-a="undo"') == 1
    assert 'data-k="req-eligible-1"' in html


def test_C5_undo_js_branch_contract():
    """Phase 5 C-3a JS: inside the existing delegated click handler,
    the undo branch alerts on ANY returned state != "undone" (and
    re-enables the button); reload happens only past that branch,
    i.e. on state == "undone" (§4.4: refusals are not swallowed)."""
    text = LIB_HTML.read_text(encoding="utf-8")
    assert "action === 'undo' && data.state !== 'undone'" in text
    undo_at = text.index("action === 'undo'")
    reload_at = text.index("location.reload();", undo_at)
    branch = text[undo_at:reload_at]
    assert "alert(data.error ||" in branch
    assert "b.disabled = false" in branch
    assert "return" in branch
    # No new endpoint / handler: the single existing delegated
    # listener and the single action endpoint remain the only ones.
    assert text.count("addEventListener('click'") == 1
    assert text.count("/admin/librarian/action") == 1


def test_C5_captions_escaped_no_safe():
    text = LIB_HTML.read_text(encoding="utf-8")
    # A-2(iv): no |safe anywhere in the page, on any field.
    assert "|safe" not in text
    assert "librarian_writer_token" not in text
    evil = 'Evil <script>alert(1)</script> caption'
    html = _try_render(
        "librarian.html", text,
        _page_ctx(fix_history=[_fix_row(old_caption_text=evil,
                                        old_caption=evil)]))
    if html is None:
        return
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_C5_page_size_with_history_seeded():
    """§9.5: the Phase 3 page-size measurement re-run with the
    history section seeded — operative basis (content block
    < 51,200 bytes) asserted; the number is printed for the
    three-number record (content / total / base-alone, the total
    and base numbers recorded at the gate from the real render,
    as in Phase 3)."""
    source = LIB_HTML.read_text(encoding="utf-8")
    cap_old = ("Some Show S01E01 1080p WEB-DL x264 [Group] tt1234567 "
               "https://example.invalid/title")
    cap_new = ("Some Show (2024) S01E01 1080p WEB-DL x264 [Group] "
               "tt1234567 https://example.invalid/title")
    fix_rows = [_fix_row(old_caption=cap_old, new_caption=cap_new,
                         old_caption_text=cap_old,
                         new_caption_text=cap_new) for _ in range(50)]
    groups = []
    for stem in ("stem-alpha", "stem-beta", "stem-gamma"):
        files = [{"file_name": f"Seeded Show S01E{i:02d} 1080p.mkv",
                  "identity": {"size": "100B"}, "origin": "live-fail",
                  "key": f"-100111:{i}"} for i in range(1, 18)]
        groups.append({"stem": stem, "count": len(files),
                       "channel_name": "-100111", "record": None,
                       "episode_tokens": ["S01E01", "S01E17"],
                       "files": files})
    escalated = [
        {"key": "-100111:90", "file_name": "Esc.mkv",
         "channel_name": "-100111", "escalation_reason": "check me"},
        {"key": "-100111:91", "file_name": "Esc2.mkv",
         "channel_name": "-100111", "escalation_reason": "check me too"},
    ]
    html = _try_render("librarian.html", source, _page_ctx(
        groups=groups, escalated=escalated, fix_history=fix_rows,
        counts={"waiting": 50, "parked": 0, "escalated": 2, "fixed": 0}))
    if html is None:
        return
    size = len(html.encode("utf-8"))
    print(f"C5 page-size re-measurement: content block {size} bytes "
          f"(50 waiting files + 2 escalated + 50 fix-history rows)")
    assert size < 51200, size


# ------------------------------------------------------------------
# librarian.html — Channels section (Yan, 2026-11-20: channel types
# retired page-wide — the bot infers a channel's nature from its
# NAME; this section shows name + ID only, no stored type)
# ------------------------------------------------------------------

def test_librarian_channels_section_no_types():
    text = LIB_HTML.read_text(encoding="utf-8")
    start = text.index("Channels")
    section = text[start:]
    # Rows render the channel name and ID — and no type value.
    assert "c.channel_name" in section
    assert "c.channel_id" in section
    assert "c.type" not in section
    # The retired ground-truth wording is gone; the replacement
    # states the name-inference model and keeps the Settings pointer.
    assert "ground truth" not in section
    assert "reads each channel's name to understand" in section
    assert "Edit the allowlist in" in section
    assert "Settings → Librarian" in section
    # Empty state unchanged.
    assert "No channels are allowlisted yet." in section


# ------------------------------------------------------------------
# settings.html — Writer bot block (§4.6, §9.4 W-5 point 6, A-6, W-7)
# ------------------------------------------------------------------

def test_C5_settings_writer_block_static():
    text = SETTINGS_HTML.read_text(encoding="utf-8")
    # Dedicated action wiring (plan §4.1.3 contract): POST + DELETE on
    # /admin/librarian/writer-token, from its own JS functions.
    assert "fetch('/admin/librarian/writer-token'" in text
    assert "method: 'POST'" in text and "method: 'DELETE'" in text
    assert "setLibrarianWriterToken" in text
    assert "clearLibrarianWriterToken" in text
    # The token entry is a password input, never a prefilled text field.
    assert 'type="password" id="librarian-writer-token"' in text
    # W-7 rotation truth as help text, verbatim substance.
    assert ("Replacing the token here does not revoke the old one "
            "— only BotFather /revoke does.") in text
    # The template never outputs the stored token value: the only
    # librarian_writer_token* expressions allowed are the fingerprint
    # and username display keys (and the password input's id, which is
    # an HTML id, not a value).
    assert not re.search(r"\{\{[^}]*librarian_writer_token(?![\w])", text)
    assert not re.search(r"\{\{[^}]*librarian_writer_token \}\}", text)
    # Status-line wording pin (§4.6): set -> "Writer bot: @<username>
    # (…<fingerprint>)"; not set -> "Writer bot: Not set".
    assert "Writer bot: @{{ lib_writer_name }} (…{{ lib_writer_fp }})" in text
    assert "Writer bot: Not set" in text


def test_C5_writer_keys_absent_from_save_payload():
    """A-6 / W-2: the writer UI must not invite the generic save —
    saveSettings()'s payload literal gains NO writer key (§4.6
    static test)."""
    text = SETTINGS_HTML.read_text(encoding="utf-8")
    start = text.index("const payload = {")
    end = text.index("};", start)
    payload = text[start:end]
    for key in ("librarian_writer_token", "librarian_writer_token_fingerprint",
                "librarian_writer_bot_id", "librarian_writer_bot_username",
                "writer"):
        assert key not in payload, key
    # The pre-existing librarian keys still save (no collateral damage).
    for key in ("librarian_enabled", "librarian_mode",
                "librarian_channels", "librarian_channel_types"):
        assert key in payload, key


def test_C5_settings_writer_render_sentinel():
    """W-5 point 6 sentinel: render the settings page with a sentinel
    writer token in the settings context — the token string appears
    nowhere in the HTML; username + fingerprint + set state render."""
    source = SETTINGS_HTML.read_text(encoding="utf-8")
    settings = {
        "channel_titles": {}, "database_list": [],
        "librarian_writer_token": WRITER_SENTINEL,
        "librarian_writer_token_fingerprint": WRITER_FINGERPRINT,
        "librarian_writer_bot_username": WRITER_USERNAME,
        "librarian_writer_bot_id": 424242,
    }
    html = _try_render("settings.html", source, _settings_ctx(settings))
    if html is None:
        return
    assert WRITER_SENTINEL not in html
    assert f"Writer bot: @{WRITER_USERNAME} (…{WRITER_FINGERPRINT})" in html
    assert "only BotFather /revoke does" in html
    # Not-set state: writer keys absent from the context entirely.
    html = _try_render("settings.html", source,
                       _settings_ctx({"channel_titles": {},
                                      "database_list": []}))
    assert "Writer bot: Not set" in html
    assert WRITER_SENTINEL not in html


# ------------------------------------------------------------------
# settings.html — Librarian channels tick-box picker (Yan, 2026-11-20:
# channel types retired — the bot infers a channel's nature from its
# NAME; the allowlist is a closed picker of the app's source channels
# plus the skip channel, shown by name; the save clears the stored
# types map so no phantom ground truth lingers)
# ------------------------------------------------------------------

def test_librarian_channels_picker_static():
    text = SETTINGS_HTML.read_text(encoding="utf-8")
    # Picker container + tick toggles, pre-ticked from the allowlist.
    assert 'id="librarian_channels_items"' in text
    assert 'class="librarian-allow-toggle"' in text
    assert "collectLibrarianChannels" in text
    assert "rebuildLibrarianChannels" in text
    # Closed universe: sources are the app's source-channel lists plus
    # the skip channel; names come from the channel_titles map — the
    # same data source every other channel list on the page uses.
    for key in ("settings.auth_channels", "settings.manual_channels",
                "settings.global_search_channels", "settings.skip_channel",
                "settings.channel_titles"):
        assert key in text, key
    # No types UI anywhere: no type selects/vocabulary, no merged-row
    # machinery, and no add-by-ID row (the universe is closed).
    for gone in ("lct-type", "lct-channel", "LIBRARIAN_CHANNEL_TYPES",
                 "collectLibrarianChannelTypes", "librarian_channels_new",
                 "addLibrarianChannelRow"):
        assert gone not in text, gone
    # Help line carries Yan's model: tick + name inference.
    assert "tick the channels the bot may work in" in text
    assert "reads each channel's" in text
    # Save payload: ticked IDs via the collector, and the stored types
    # map cleared explicitly (an absent key would be left untouched by
    # the settings backend, so the empty map must be posted).
    start = text.index("const payload = {")
    end = text.index("};", start)
    payload = text[start:end]
    assert "librarian_channels:" in payload
    assert "collectLibrarianChannels()" in payload
    assert re.search(r"librarian_channel_types:\s*\{\}", payload)


def test_librarian_channels_picker_render():
    """Render: source channels + skip render by name; exactly the
    allowlisted ones are ticked; no type select renders."""
    source = SETTINGS_HTML.read_text(encoding="utf-8")
    settings = {
        "channel_titles": {
            "-100111": "Donghua Dumping", "-100222": "Movie Dumping",
            "-100333": "Series Dumping", "-100444": "Ani Dumping",
            "-100999": "Skip Bin",
        },
        "database_list": [],
        "auth_channels": ["-100111", "-100222"],
        "manual_channels": ["-100333"],
        "global_search_channels": ["-100444"],
        "skip_channel": "-100999",
        "librarian_channels": ["-100111", "-100999"],
        "librarian_channel_types": {"-100111": "donghua"},
    }
    html = _try_render("settings.html", source, _settings_ctx(settings))
    if html is None:
        return
    seg_start = html.index('id="librarian_channels_items"')
    seg = html[seg_start:html.index('<hr class="divider">', seg_start)]
    for name in ("Donghua Dumping", "Movie Dumping", "Series Dumping",
                 "Ani Dumping", "Skip Bin"):
        assert name in seg, name
    assert seg.count('class="librarian-allow-toggle"') == 5
    assert seg.count("checked") == 2  # only the two allowlisted rows
    assert "<select" not in seg
    assert "lct-" not in html


# ------------------------------------------------------------------
# Page undo surface completion (Phase 5 C-3b, plan §4.4; OQ-1(a) +
# OQ-1b(a) as ratified; Security S5-7) — the REAL
# ``librarian_page_action`` handler from the REAL
# ``Backend/fastapi/librarian_routes.py``, loaded by file path and
# driven through its recorded route, per the F2 precedent (real
# route + real template render; the template half is the C-3a
# section above). Doubles sit only at the module boundaries the
# handler itself calls, with the real functions' shapes:
#
# - ``librarian_store.get_fix_by_request_id(request_id)`` —
#   librarian_store.py: an async find_one by ``request_id``
#   returning the stored row (a copy) or None.
# - ``librarian_engine.undo_one(payload)`` — plan §4.1's pinned
#   signature: ``{chat_id, msg_id, request_id, target_request_id?}``
#   in, the undo operation's persisted row (the §6 fix-row shape,
#   so the route's ``_fix_item`` projection applies) or a
#   step-1-class ``{request_id, state, error}`` refusal out. The
#   double echoes the payload's ``request_id`` as the persisted
#   row's — exactly what the real engine's stored-result rule does
#   — and records every call as the assertion surface.
# - ``SettingsManager.current()`` — a settings object carrying the
#   real property names/formulas for the three librarian switches
#   the surface reads (settings_manager.py defaults: enabled
#   False, test_mode True, writes_enabled False).
#
# ``sys.modules`` entries touched here are saved and restored
# around every surface, so this section composes with the api
# suite's harness in either run order.
# ------------------------------------------------------------------

import asyncio as _asyncio
import contextlib as _contextlib
import copy as _copy
import importlib.util as _ilu
import sys as _sys
import types as _types

_ROUTES_PATH = ROOT / "Backend/fastapi/librarian_routes.py"
_MISSING = object()


class _C3bHTTPException(Exception):
    def __init__(self, status_code, detail=None, headers=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _C3bJSONResponse:
    def __init__(self, content=None, status_code=200, media_type=None):
        self.content = content
        self.status_code = status_code


class _C3bRouter:
    def __init__(self, *args, **kwargs):
        self.routes = []
        self.on_startup = list(kwargs.get("on_startup") or [])

    def _add(self, method, path, dependencies=None):
        def deco(fn):
            self.routes.append((method, path, fn))
            return fn
        return deco

    def get(self, path, dependencies=None, **kw):
        return self._add("GET", path, dependencies)

    def post(self, path, dependencies=None, **kw):
        return self._add("POST", path, dependencies)

    def delete(self, path, dependencies=None, **kw):
        return self._add("DELETE", path, dependencies)

    def route_map(self):
        return {(m, p): fn for m, p, fn in self.routes}


class _C3bTemplates:
    def __init__(self, directory=None):
        self.directory = directory

    def TemplateResponse(self, name, context=None, *args, **kwargs):
        return _C3bJSONResponse(content=f"<template:{name}>")


class _C3bRequest:
    """The starlette surface the page action uses: session +
    async json()."""

    def __init__(self, body=None, session=None):
        self.headers = {}
        self.query_params = {}
        self.session = dict(session or {})
        self.cookies = {}
        self._body = body

    async def json(self):
        return _copy.deepcopy(self._body)


class _C3bSettings:
    def __init__(self, data=None):
        self._d = dict(data or {})

    @property
    def librarian_enabled(self):
        return bool(self._d.get("librarian_enabled", False))

    @property
    def librarian_test_mode(self):
        return bool(self._d.get("librarian_test_mode", True))

    @property
    def librarian_writes_enabled(self):
        return bool(self._d.get("librarian_writes_enabled", False))


class _C3bSettingsManager:
    _current = _C3bSettings()

    @classmethod
    def current(cls):
        return cls._current


class _C3bUndoEngine:
    """undo_one double with §4.1's pinned signature/semantics:
    returns the persisted undo row under the payload's own
    request_id (stored-result rule), or a scripted result."""

    def __init__(self):
        self.calls = []
        self.scripted = None  # dict, or callable(payload) -> dict

    async def undo_one(self, payload):
        self.calls.append(_copy.deepcopy(payload))
        if self.scripted is not None:
            if callable(self.scripted):
                return self.scripted(payload)
            return _copy.deepcopy(self.scripted)
        loc = {"chat_id": payload["chat_id"], "msg_id": payload["msg_id"]}
        return {
            "request_id": payload["request_id"], "batch_id": None,
            "source_locator": dict(loc), "current_locator": dict(loc),
            "action": "undo_edit", "state": "undone",
            "index_confirmed": True, "dry_run": False,
            "budget_counted": False, "error": None,
            "old_caption": "New caption tt1234567",
            "new_caption": "Old caption tt1111111",
            "expected_ids": {}, "series_record_id": None,
            "created_at": "2026-11-20T14:00:00+00:00",
        }


def _c3b_target_row(**kw):
    row = {
        "request_id": "fix-req-1", "batch_id": None,
        "source_locator": {"chat_id": -100111, "msg_id": 55},
        "current_locator": {"chat_id": -100111, "msg_id": 55},
        "action": "edit", "state": "fixed", "dry_run": False,
        "budget_counted": True, "index_confirmed": True, "error": None,
        "old_caption": "Old caption tt1111111",
        "new_caption": "New caption tt1234567",
        "old_caption_text": "Old caption tt1111111",
        "new_caption_text": "New caption tt1234567",
        "expected_ids": {"tmdb": 1234567}, "series_record_id": None,
        "created_at": "2026-11-20T13:35:00+00:00",
    }
    row.update(kw)
    return row


@_contextlib.contextmanager
def _c3b_surface(settings_data=None, target_rows=(), engine=None):
    """Install the boundary doubles, load the REAL routes module,
    and yield (routes, store_rows, engine). Restores sys.modules
    exactly on exit."""
    engine = engine if engine is not None else _C3bUndoEngine()
    store_rows = {r["request_id"]: r for r in target_rows}

    async def get_fix_by_request_id(request_id):
        row = store_rows.get(request_id)
        return _copy.deepcopy(row) if row is not None else None

    _C3bSettingsManager._current = _C3bSettings(settings_data)

    fastapi = _types.ModuleType("fastapi")
    fastapi.APIRouter = _C3bRouter
    fastapi.Depends = lambda dependency=None: ("Depends", dependency)
    fastapi.HTTPException = _C3bHTTPException
    fastapi.Request = _C3bRequest
    fa_responses = _types.ModuleType("fastapi.responses")
    fa_responses.JSONResponse = _C3bJSONResponse
    fa_templating = _types.ModuleType("fastapi.templating")
    fa_templating.Jinja2Templates = _C3bTemplates
    fastapi.responses = fa_responses
    fastapi.templating = fa_templating

    backend = _types.ModuleType("Backend")
    backend_fastapi = _types.ModuleType("Backend.fastapi")
    security_pkg = _types.ModuleType("Backend.fastapi.security")
    credentials = _types.ModuleType("Backend.fastapi.security.credentials")

    async def require_auth(request):
        if not request.session.get("authenticated"):
            raise _C3bHTTPException(status_code=401,
                                    detail="Not authenticated")
        return True

    credentials.require_auth = require_auth
    credentials.get_current_user = (
        lambda request: "admin" if request.session.get("authenticated")
        else None)
    security_pkg.credentials = credentials
    backend_fastapi.security = security_pkg

    helper = _types.ModuleType("Backend.helper")
    settings_manager = _types.ModuleType(
        "Backend.helper.settings_manager")
    settings_manager.SettingsManager = _C3bSettingsManager
    settings_manager.Settings = _C3bSettings
    store_mod = _types.ModuleType("Backend.helper.librarian_store")
    store_mod.get_fix_by_request_id = get_fix_by_request_id
    engine_mod = _types.ModuleType("Backend.helper.librarian_engine")
    engine_mod.undo_one = engine.undo_one
    helper.settings_manager = settings_manager
    helper.librarian_store = store_mod
    helper.librarian_engine = engine_mod
    backend.fastapi = backend_fastapi
    backend.helper = helper

    modules = {
        "fastapi": fastapi,
        "fastapi.responses": fa_responses,
        "fastapi.templating": fa_templating,
        "Backend": backend,
        "Backend.fastapi": backend_fastapi,
        "Backend.fastapi.security": security_pkg,
        "Backend.fastapi.security.credentials": credentials,
        "Backend.helper": helper,
        "Backend.helper.settings_manager": settings_manager,
        "Backend.helper.librarian_store": store_mod,
        "Backend.helper.librarian_engine": engine_mod,
    }
    saved = {name: _sys.modules.get(name, _MISSING) for name in modules}
    saved["librarian_routes_c3b_under_test"] = _sys.modules.get(
        "librarian_routes_c3b_under_test", _MISSING)
    _sys.modules.update(modules)
    try:
        spec = _ilu.spec_from_file_location(
            "librarian_routes_c3b_under_test", str(_ROUTES_PATH))
        routes = _ilu.module_from_spec(spec)
        _sys.modules[spec.name] = routes
        spec.loader.exec_module(routes)
        yield routes, store_rows, engine
    finally:
        for name, prev in saved.items():
            if prev is _MISSING:
                _sys.modules.pop(name, None)
            else:
                _sys.modules[name] = prev


def _c3b_act(routes, body):
    handler = routes.router.route_map()[("POST", "/admin/librarian/action")]
    req = _C3bRequest(body=body, session={"authenticated": True})
    return _asyncio.run(handler(req))


# Enabled, test mode OFF, writes flag OFF (the §8 gate asymmetry:
# undo must succeed without librarian_writes_enabled).
_C3B_UNDO_SETTINGS = {"librarian_enabled": True,
                      "librarian_test_mode": False,
                      "librarian_writes_enabled": False}


def test_C3b_page_undo_mints_own_id_and_targets_fix():
    """§4.4 / OQ-1(a): the page action passes BOTH ids to the
    engine — a freshly minted own key (secrets.token_hex, >= 16
    bytes of entropy, hex, within the §9 ID-field limits, never
    the target's id, never reused) and the target fix's
    request_id — with the post taken from the target row's
    current_locator; the persisted undo row's request_id in the
    response IS the minted id (S5-7: persisted/auditable)."""
    with _c3b_surface(_C3B_UNDO_SETTINGS,
                      target_rows=[_c3b_target_row()]) as (routes, _s, eng):
        resp = _c3b_act(routes, {"action": "undo", "key": "fix-req-1",
                                 "reason": ""})
        assert resp.status_code == 200, resp.content
        assert len(eng.calls) == 1
        payload = eng.calls[0]
        assert payload["target_request_id"] == "fix-req-1"
        assert payload["chat_id"] == -100111 and payload["msg_id"] == 55
        minted = payload["request_id"]
        assert minted != "fix-req-1"
        assert len(bytes.fromhex(minted)) >= 16  # S5-7 entropy pin
        assert len(minted) <= 64
        assert resp.content["state"] == "undone"
        assert resp.content["request_id"] == minted
        assert resp.content["action"] == "undo_edit"
        assert resp.content["budget_counted"] is False
        # A second click mints a FRESH id — two undos of one fix
        # are distinct operations (DISP-8's rejected-reading pin).
        resp2 = _c3b_act(routes, {"action": "undo", "key": "fix-req-1"})
        assert resp2.content["state"] == "undone"
        assert eng.calls[1]["request_id"] != minted


def test_C3b_page_undo_uses_current_locator():
    """§8/DISP-7: undo targets the post's current_locator, not
    the source locator, when the two differ."""
    row = _c3b_target_row(
        current_locator={"chat_id": -100222, "msg_id": 77})
    with _c3b_surface(_C3B_UNDO_SETTINGS,
                      target_rows=[row]) as (routes, _s, eng):
        resp = _c3b_act(routes, {"action": "undo", "key": "fix-req-1"})
        assert resp.content["state"] == "undone"
        assert eng.calls[0]["chat_id"] == -100222
        assert eng.calls[0]["msg_id"] == 77


def test_C3b_page_undo_test_mode_refused_no_engine_call():
    """§4.4 gate parity with the route: test mode on -> the
    §4.1 refusal verbatim, and the engine is never called (no
    row, nothing minted-and-persisted, nothing written)."""
    settings = dict(_C3B_UNDO_SETTINGS, librarian_test_mode=True)
    with _c3b_surface(settings,
                      target_rows=[_c3b_target_row()]) as (routes, _s, eng):
        resp = _c3b_act(routes, {"action": "undo", "key": "fix-req-1"})
        assert resp.status_code == 200
        assert resp.content["state"] == "refused_precondition"
        assert resp.content["error"] == (
            "undo mutates Telegram — test mode is on")
        assert eng.calls == []


def test_C3b_page_undo_disabled_and_unknown_action():
    """The pre-existing page gates stand for undo: disabled ->
    403 disabled; an unknown action -> 400 before any gate."""
    settings = dict(_C3B_UNDO_SETTINGS, librarian_enabled=False)
    with _c3b_surface(settings,
                      target_rows=[_c3b_target_row()]) as (routes, _s, eng):
        resp = _c3b_act(routes, {"action": "undo", "key": "fix-req-1"})
        assert resp.status_code == 403
        assert resp.content["state"] == "disabled"
        assert eng.calls == []
    with _c3b_surface(_C3B_UNDO_SETTINGS) as (routes, _s, eng):
        resp = _c3b_act(routes, {"action": "bogus", "key": "fix-req-1"})
        assert resp.status_code == 400
        assert eng.calls == []


def test_C3b_page_undo_unknown_or_locatorless_target():
    """A target the store cannot resolve is a plain-words
    not_found (HTTP 200, the per-operation shape) and the engine
    is never called — the surface cannot invent a post."""
    with _c3b_surface(_C3B_UNDO_SETTINGS) as (routes, _s, eng):
        resp = _c3b_act(routes, {"action": "undo", "key": "no-such-fix"})
        assert resp.status_code == 200
        assert resp.content["state"] == "not_found"
        assert "no-such-fix" in resp.content["error"]
        assert eng.calls == []
    row = _c3b_target_row(current_locator=None, source_locator=None)
    with _c3b_surface(_C3B_UNDO_SETTINGS,
                      target_rows=[row]) as (routes, _s, eng):
        resp = _c3b_act(routes, {"action": "undo", "key": "fix-req-1"})
        assert resp.status_code == 200
        assert resp.content["state"] == "not_found"
        assert eng.calls == []


def test_C3b_page_undo_invalid_key_400():
    """The target must be a §9-shaped ID field: missing, empty,
    or off-charset keys 400 before any store or engine call."""
    with _c3b_surface(_C3B_UNDO_SETTINGS,
                      target_rows=[_c3b_target_row()]) as (routes, _s, eng):
        for body in ({"action": "undo"},
                     {"action": "undo", "key": ""},
                     {"action": "undo", "key": "bad key!"},
                     {"action": "undo", "key": "x" * 65}):
            resp = _c3b_act(routes, body)
            assert resp.status_code == 400, body
        assert eng.calls == []


def test_C3b_page_undo_engine_outcomes_pass_through():
    """Engine outcomes reach the page verbatim in the fix-result
    shape, carrying the minted request_id: a drift refusal and a
    partial undo both surface their state + plain-words error
    (the template's JS branch alerts on any state != 'undone' —
    a partial is never reported as an undo)."""
    cases = [
        ({"state": "refused_drift",
          "error": "the live caption no longer matches the fix "
                   "being undone"},
         "refused_drift"),
        ({"state": "undo_partial",
          "error": "caption restored; index removal failed"},
         "undo_partial"),
    ]
    for refusal, state in cases:
        engine = _C3bUndoEngine()

        def script(payload, _r=refusal):
            return {"request_id": payload["request_id"], **_r}

        engine.scripted = script
        with _c3b_surface(_C3B_UNDO_SETTINGS,
                          target_rows=[_c3b_target_row()],
                          engine=engine) as (routes, _s, eng):
            resp = _c3b_act(routes, {"action": "undo",
                                     "key": "fix-req-1"})
            assert resp.status_code == 200
            assert resp.content["state"] == state
            assert resp.content["error"] == refusal["error"]
            assert resp.content["request_id"] == eng.calls[0]["request_id"]


def test_C3b_minting_pin_static():
    """S5-7 fencing, statically: the ONLY token_hex mint in the
    routes module is inside the page-undo helper (>= 16 bytes);
    the bot-API undo route mints nothing — it never receives an
    app-invented ID."""
    text = _ROUTES_PATH.read_text(encoding="utf-8")
    # Exactly one mint statement in the module (the docstring's
    # prose mention of the call is not a second mint).
    assert text.count("request_id = secrets.token_hex(16)") == 1
    helper = text[text.index("async def _page_undo"):
                  text.index("@router.post(\"/admin/librarian/action\")")]
    assert "request_id = secrets.token_hex(16)" in helper
    assert "target_request_id" in helper
    assert "librarian_test_mode" in helper
    # No writes-flag / allowlist / budget gate in the undo path.
    for forbidden in ("librarian_writes_enabled", "librarian_channels",
                      "count_budget_used_today"):
        assert forbidden not in helper, forbidden
    bot_undo = text[text.index("async def librarian_undo"):
                    text.index("async def librarian_scan")]
    assert "token_hex" not in bot_undo
    assert "token_urlsafe" not in bot_undo
