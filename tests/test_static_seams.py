"""Static seam tests: donation removal and integration points stay intact.

These read files as text (no app imports) so they also serve as the
reapply check: if an upstream update breaks a seam, these fail loudly.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8", errors="ignore")


def test_donation_removed():
    for rel in (
        "Backend/fastapi/routes/stremio_routes.py",
        "Backend/fastapi/templates/base.html",
        "README.md",
        "docs/index.html",
        "docs/setup.html",
    ):
        text = read(rel)
        assert "donate.weebzonex" not in text, rel
        assert "_donation" not in text, rel


def test_streamer_factory_seam():
    text = read("Backend/fastapi/routes/stream_routes.py")
    assert "BufferedByteStreamer(tg_client, index)" in text
    assert "BufferedByteStreamer(botmod.Userbot, USERBOT_CLIENT_INDEX)" in text


def test_settings_defaults_and_reinit_seam():
    text = read("Backend/helper/settings_manager.py")
    for key in ("buffer_enabled", "buffer_profile", "buffer_adaptive",
                "buffer_readahead_mb", "buffer_workers", "buffer_cache_mb",
                "buffer_disk_cache_mb", "buffer_seek_burst",
                "buffer_stall_timeout", "flood_alert_enabled"):
        assert f'"{key}"' in text, key
    assert "refresh_policy(new)" in text


def test_settings_api_validation_seam():
    text = read("Backend/fastapi/routes/api_routes.py")
    assert '"buffer_enabled", "buffer_adaptive"' in text
    assert "_buffer_ranges" in text
    assert "buffer_profile must be conservative, balanced, aggressive or custom" in text


def test_settings_page_card():
    text = read("Backend/fastapi/templates/settings.html")
    for element_id in ("buffer_enabled", "buffer_profile", "buffer_adaptive",
                       "buffer_readahead_mb", "buffer_workers",
                       "buffer_cache_mb", "buffer_disk_cache_mb",
                       "buffer_seek_burst", "buffer_stall_timeout",
                       "flood_alert_enabled",
                       "buffer-fields", "buffer-estimate"):
        assert f'id="{element_id}"' in text, element_id
    assert "buffer_health" in text


def test_js_presets_match_engine_profiles():
    import re
    import sys
    sys.path.insert(0, str(ROOT / "tests"))
    from _loader import load_stream_buffer

    sb = load_stream_buffer()
    html = read("Backend/fastapi/templates/settings.html")
    block = re.search(r"BUFFER_PRESETS = \{(.*?)\};", html, re.S).group(1)
    for name, prof in sb.PROFILES.items():
        m = re.search(name + r":\s*\{\s*readahead:\s*(\d+),\s*workers:\s*(\d+),\s*cache:\s*(\d+)", block)
        assert m, name
        js = tuple(int(x) for x in m.groups())
        engine = (prof["readahead_mb"], prof["workers"], prof["cache_mb"])
        assert js == engine, (name, js, engine)


def test_dl_link_plugin_is_seamless():
    text = read("Backend/pyrofork/plugins/dl_link.py")
    assert "dl_auto_link" in text and "dl_command" in text
    # The plugin never touches the command list itself: upstream's
    # setup_bot_commands() owns (and wipes) it at every startup.
    assert "set_bot_commands" not in text
    patch = read("patches/integrate.patch")
    assert "diff --git a/Backend/pyrofork" not in patch  # no seam edits there
    # The /dl menu entry lives in upstream's commands list instead.
    pyro = read("Backend/helper/pyro.py")
    assert 'BotCommand("dl"' in pyro
    assert 'BotCommand("check"' in pyro
    assert "diff --git a/Backend/helper/pyro.py" in patch


def test_dl_channel_seams():
    sm = read("Backend/helper/settings_manager.py")
    assert '"dl_channel_id"' in sm and "def dl_channel_id" in sm
    assert '"dl_channel_id")' in sm  # title sync in _all_channel_ids
    api = read("Backend/fastapi/routes/api_routes.py")
    assert 'payload["dl_channel_id"], "DL channel"' in api
    assert '"DL": _norm_ids' in api  # cross-field clash check
    assert 'add(s.dl_channel_id, "dl")' in api  # Bot Admin Manager list
    html = read("Backend/fastapi/templates/settings.html")
    assert 'id="dl_channel_id"' in html and 'id="dl_channel_id_clear"' in html
    assert "dl_channel_id:" in html  # saveSettings payload
    patch = read("patches/integrate.patch")
    assert "dl_channel_id" in patch


def test_wave2_phase_a_seams():
    # Source labels: resolver module + both enrichments + both templates
    assert (ROOT / "Backend/helper/stream_source.py").exists()
    api = read("Backend/fastapi/routes/api_routes.py")
    assert "classify_source" in api and "buffer_disk_cache_mb" in api
    routes = read("Backend/fastapi/routes/stream_routes.py")
    assert "classify_source" in routes
    database = read("Backend/helper/database.py")
    assert '"chat_id": 1' in database  # analytics projection carries the id
    admin = read("Backend/fastapi/templates/admin_dashboard.html")
    assert "source_label" in admin and "<th>Source</th>" in admin
    dash = read("Backend/fastapi/templates/dashboard.html")
    assert "source_label" in dash
    # Flood alert: engine registry + plugin sender
    assert "set_flood_sender" in read("Backend/helper/stream_buffer.py")
    assert "set_flood_sender" in read("Backend/pyrofork/plugins/dl_link.py")
    # Disk tier lives in the engine core
    assert "class DiskTier" in read("Backend/helper/stream_buffer.py")


def test_wave2_phase_b_seams():
    # Core + hooks plugin exist; hooks register the progress listener
    assert (ROOT / "Backend/helper/prewarm.py").exists()
    hooks = read("Backend/pyrofork/plugins/prewarm_hooks.py")
    assert "set_progress_listener" in hooks and "offer_pooled" in hooks
    assert "group=12" in hooks  # ingest handler runs after the receiver
    # Adapter: progress marks + pre-warm analytics skip
    adapter = read("Backend/helper/buffered_streamer.py")
    assert "notify_progress" in adapter and 'get("prewarm")' in adapter
    engine = read("Backend/helper/stream_buffer.py")
    assert "PROGRESS_MARKS" in engine and "set_progress_listener" in engine
    # Settings end-to-end: defaults, API validation, page card, dl hook
    sm = read("Backend/helper/settings_manager.py")
    for key in ("prewarm_enabled", "prewarm_profile", "prewarm_mb_per_file",
                "prewarm_episodes_ahead", "prewarm_trigger_pct",
                "prewarm_daily_budget_mb", "prewarm_only_charging"):
        assert f'"{key}"' in sm, key
    api = read("Backend/fastapi/routes/api_routes.py")
    assert "prewarm_profile" in api and "prewarm_daily_budget_mb" in api
    page = read("Backend/fastapi/templates/settings.html")
    for cid in ("prewarm_enabled", "prewarm_profile", "prewarm_trigger_pct",
                "prewarm_episodes_ahead", "prewarm_mb_per_file",
                "prewarm_daily_budget_mb", "prewarm_only_charging"):
        assert f'id="{cid}"' in page, cid
    assert "prewarm_health" in read("Backend/fastapi/routes/template_routes.py")
    dl = read("Backend/pyrofork/plugins/dl_link.py")
    assert "offer_pooled" in dl


def test_startup_policy_init_seam():
    text = read("Backend/__main__.py")
    assert "refresh_policy(SettingsManager.current().to_dict())" in text


def test_template_context_seam():
    text = read("Backend/fastapi/routes/template_routes.py")
    assert '"buffer_health": buffer_health' in text


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} static seam tests passed")
