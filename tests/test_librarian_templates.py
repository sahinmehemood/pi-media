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
