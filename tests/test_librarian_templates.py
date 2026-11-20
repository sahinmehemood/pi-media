"""Phase 4 Workstream C5 — template tests (phase4-plan §4.6, §9.5).

Covers the two C5 template surfaces:

- ``Backend/fastapi/templates/librarian.html`` — the Fix history
  section: seeded rows render action / state / IST time / before &
  after captions with a "dry run" label on dry-run rows; NO undo
  action is rendered (undo does not exist until Phase 5/6, plan
  C-1); captions render under Jinja autoescaping — no ``|safe`` on
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
    # No undo action is rendered — undo does not exist until
    # Phase 5/6 (plan C-1); a dead button would be a false affordance.
    assert "undo" not in section.lower()
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
