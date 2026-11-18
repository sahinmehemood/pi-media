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
                "buffer_stall_timeout"):
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
                       "buffer_cache_mb", "buffer_stall_timeout",
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
    assert "diff --git a/Backend/helper/pyro.py" in patch


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
