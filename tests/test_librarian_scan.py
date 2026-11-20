"""Phase 5 Workstream D tests — `scan` route + seam 9 (phase5-plan
§5, §10.5; D-ACC-1..3).

Two surfaces, both driven through production code:

- Seam 9: the REAL ``ScanManager._process_message`` (loaded by file
  path) with module-boundary doubles whose shapes mirror the real
  modules — ``metadata`` / ``route_to_skip_channel`` are counting
  doubles with the real signatures (scan_manager.py's import list),
  and the coordination check runs against the REAL
  ``librarian_store.find_active_fix`` over the Phase 3/4 harness's
  fake DB (test_librarian_api's inventory, reused — never a second,
  kinder fake). The locator-form trap (DISP-3 item 4) is pinned:
  fix rows store the full -100… form, and the test starts from a
  -100… channel string exactly as ``_run``/``_scan_channel`` do
  (``int(ch)`` at scan_manager.py's channel loop).
- The scan route: the REAL ``librarian_scan`` handler from the real
  ``librarian_routes`` module (via the test_librarian_api harness),
  with ``get_scan_manager`` / ``get_scan_client`` replaced at the
  real ``api_routes`` boundary — the exact names the route imports
  (api_routes.py:2189-2197) — by a counting manager double with the
  real ``start(client, channels, mode="scan")`` signature and the
  real ``get_status()`` key set. D-ACC-2's resume case is the one
  exception: it drives the REAL ScanManager (see that test).
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_module, run  # noqa: E402
import test_librarian_api as api  # noqa: E402  (installs the harness)

store = api.store

CHAT_STR = "-1003966209940"
CHAT = -1003966209940

# ------------------------------------------------------------------
# Counting doubles at scan_manager.py's real import boundary
# ------------------------------------------------------------------

CALLS = {"metadata": [], "funnel": [], "stamp": [], "insert": []}


async def _metadata_double(title, channel, msg_id, override_id=None):
    CALLS["metadata"].append((title, channel, msg_id, override_id))
    return {"group_key": None, "encoded_string": "enc:fake"}


async def _funnel_double(client, message, origin="live-fail"):
    CALLS["funnel"].append((message.id, origin))


async def _stamp_double(message, metadata_info):
    CALLS["stamp"].append(message.id)


async def _encode_double(payload):
    return "enc:fake"


async def _decode_double(value):
    return {}


async def _thumb_double(metadata_info, message, encoded, client):
    return None


async def _ingest_subtitle_double(name, channel, msg_id):
    return False


def _install_scan_manager_boundary():
    """Give the real scan_manager's imports production-shaped
    doubles. Only names scan_manager.py actually imports are set,
    each verified against its import list (scan_manager.py:9-16)."""
    pg_errors = sys.modules["pyrogram.errors"]
    if not hasattr(pg_errors, "ChannelPrivate"):
        pg_errors.ChannelPrivate = type("ChannelPrivate", (Exception,), {})
    if not hasattr(pg_errors, "ChatAdminRequired"):
        pg_errors.ChatAdminRequired = type("ChatAdminRequired", (Exception,), {})

    metadata_mod = sys.modules["Backend.helper.metadata"]
    metadata_mod.caption_with_links = lambda message: message.caption or ""
    metadata_mod.extract_default_id = lambda message: None
    metadata_mod.has_raw_ref = lambda caption: False
    metadata_mod.metadata = _metadata_double

    pyro_mod = sys.modules["Backend.helper.pyro"]
    pyro_mod.apply_video_thumb_to_metadata = _thumb_double
    pyro_mod.finalize_media_name = lambda title, grouped: title

    encrypt_mod = sys.modules["Backend.helper.encrypt"]
    encrypt_mod.encode_string = _encode_double
    encrypt_mod.decode_string = _decode_double

    manual_add_mod = sys.modules["Backend.helper.manual_add"]
    manual_add_mod.stamp_caption_with_id = _stamp_double

    split_mod = sys.modules["Backend.helper.split_files"]
    split_mod.parse_split_info = lambda candidate: None

    subtitles_mod = sys.modules["Backend.helper.subtitles"]
    subtitles_mod.ingest_subtitle = _ingest_subtitle_double

    # skip_channel: swap in the counting double only for the load —
    # scan_manager binds the two names at import, so restoring the
    # previous module entry afterwards cannot unbind them.
    previous = sys.modules.get("Backend.helper.skip_channel")
    skip_mod = types.ModuleType("Backend.helper.skip_channel")
    skip_mod.is_skip_channel = lambda message: False
    skip_mod.route_to_skip_channel = _funnel_double
    sys.modules["Backend.helper.skip_channel"] = skip_mod
    try:
        mod = load_module("scan_manager_under_test",
                          "Backend/helper/scan_manager.py")
    finally:
        if previous is not None:
            sys.modules["Backend.helper.skip_channel"] = previous
        else:
            del sys.modules["Backend.helper.skip_channel"]
    return mod


scan_manager_mod = _install_scan_manager_boundary()
RealScanManager = scan_manager_mod.ScanManager


class FakeScanDB:
    """The Database surface _process_message touches: ``dbs`` +
    ``current_db_index`` for the dup check (0 storage DBs -> the
    check finds nothing, exactly as an empty library does) and
    ``insert_media`` with database.py's real call shape."""

    def __init__(self):
        self.dbs = {}
        self.current_db_index = 0

    async def insert_media(self, metadata_info, channel=None, msg_id=None,
                           size=None, name=None, raw_size=None, status=None):
        CALLS["insert"].append((channel, msg_id))
        return "inserted-id"


def _scan_message(msg_id=7):
    media = types.SimpleNamespace(
        file_name="Some Show S01E01 1080p.mkv", file_size=1234)
    return types.SimpleNamespace(
        id=msg_id, chat=types.SimpleNamespace(id=CHAT), empty=False,
        caption="Some Show S01E01 1080p.mkv", video=media, document=None)


def _reset_scan():
    api._reset_api()
    for key in CALLS:
        CALLS[key] = []


def _seed_fix(state="prepared", chat_id=CHAT, msg_id=7):
    row = api._fix_row(
        state=state,
        source_locator={"chat_id": chat_id, "msg_id": msg_id},
        current_locator={"chat_id": chat_id, "msg_id": msg_id})
    api.fake_db.dbs["tracking"]["librarian_fixes"].docs.append(row)
    return row


def _fresh_manager():
    mgr = RealScanManager()
    mgr.bind_db(FakeScanDB())
    return mgr


# ------------------------------------------------------------------
# D-ACC-3 — seam 9
# ------------------------------------------------------------------

def test_seam9_skips_post_with_active_fix_from_full_form_channel():
    _reset_scan()
    _seed_fix(state="prepared")

    async def go():
        mgr = _fresh_manager()
        # The scan was started from the -100… channel string; _run
        # converts with int(ch) — that int IS the full form.
        chat_id = int(CHAT_STR)
        await mgr._process_message(object(), _scan_message(7), chat_id)
        assert mgr.state["counters"]["skipped_meta"] == 1
        assert CALLS["metadata"] == [] and CALLS["funnel"] == []
        assert CALLS["insert"] == []

    run(go())


def test_seam9_stripped_form_never_matches():
    # The locator-form trap, documented as a test (plan §10.3's
    # shape): the store matches the full form only.
    _reset_scan()
    _seed_fix(state="prepared")

    async def go():
        stripped = int(str(CHAT).replace("-100", ""))
        assert await store.find_active_fix(stripped, 7) is None
        assert await store.find_active_fix(CHAT, 7) is not None

    run(go())


def test_seam9_no_active_row_processes_as_today():
    _reset_scan()

    async def go():
        mgr = _fresh_manager()
        await mgr._process_message(object(), _scan_message(7), int(CHAT_STR))
        assert len(CALLS["metadata"]) == 1
        assert CALLS["insert"] == [(3966209940, 7)]
        assert mgr.state["counters"]["indexed"] == 1
        assert mgr.state["counters"]["skipped_meta"] == 0

    run(go())


def test_seam9_terminal_row_does_not_skip():
    _reset_scan()
    _seed_fix(state="fixed")

    async def go():
        mgr = _fresh_manager()
        await mgr._process_message(object(), _scan_message(7), int(CHAT_STR))
        assert len(CALLS["metadata"]) == 1
        assert mgr.state["counters"]["indexed"] == 1

    run(go())


def test_seam9_store_failure_falls_through():
    _reset_scan()
    _seed_fix(state="prepared")
    original = store.find_active_fix

    async def boom(chat_id, msg_id):
        raise RuntimeError("store down")

    store.find_active_fix = boom
    try:
        async def go():
            mgr = _fresh_manager()
            await mgr._process_message(object(), _scan_message(7), int(CHAT_STR))
            # Scanning proceeded exactly as without the check.
            assert len(CALLS["metadata"]) == 1
            assert mgr.state["counters"]["indexed"] == 1

        run(go())
    finally:
        store.find_active_fix = original
