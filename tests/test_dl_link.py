"""DM file-to-link plugin tests: auth gating, link payload, /dl command.

The plugin module is loaded by path with pyrogram and the Backend
package stubbed, so the suite stays stdlib-only like the rest.
"""

import sys
import types
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_module, run

ROOT = Path(__file__).resolve().parent.parent
OWNER = 111


class _Filter:
    def __and__(self, other):
        return self

    def __or__(self, other):
        return self


class FakeUser:
    def __init__(self, uid, name="Yan"):
        self.id = uid
        self.first_name = name
        self.username = "yan"


class FakeMedia:
    def __init__(self, file_name="TDH - S3 E27.mkv", file_size=989_000_000,
                 file_unique_id="UNIQ1"):
        self.file_name = file_name
        self.file_size = file_size
        self.file_unique_id = file_unique_id


class FakeMessage:
    def __init__(self, mid=42, user=None, document=None, video=None, reply_to=None,
                 forward_origin=None, forward_from_chat=None,
                 forward_from_message_id=None):
        self.id = mid
        self.from_user = user
        self.chat = SimpleNamespace(id=user.id if user else 0)
        self.document = document
        self.video = video
        self.reply_to_message = reply_to
        self.forward_origin = forward_origin
        self.forward_from_chat = forward_from_chat
        self.forward_from_message_id = forward_from_message_id
        self.replies = []
        self.documents = []
        self.command = None

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        return None

    async def reply_document(self, data, **kwargs):
        self.documents.append((data, kwargs))
        return None


class BotCommand:
    def __init__(self, command, description):
        self.command = command
        self.description = description


class FakeUserbot:
    def __init__(self, fail=False, history=None):
        self.calls = []
        self.fail = fail
        self.history = history or []

    async def get_chat(self, username):
        self.calls.append(("get_chat", username))
        if self.fail:
            raise RuntimeError("boom")
        return SimpleNamespace(id=999)

    async def get_users(self, username):
        self.calls.append(("get_users", username))
        if self.fail:
            raise RuntimeError("boom")
        return SimpleNamespace(id=999)

    async def get_chat_history(self, chat_id, limit=100):
        self.calls.append(("get_chat_history", chat_id))
        for m in self.history:
            yield m


class FakeClient:
    def __init__(self, bot_id=999, commands=None):
        self.me = SimpleNamespace(id=bot_id, username="reonthebot")
        self.commands = commands if commands is not None else [
            BotCommand("start", "Start the bot"),
            BotCommand("set", "Manually add IMDb metadata"),
        ]
        self.commands_set = []
        self.copies = []
        self.copy_msg_id = 9001
        self.copy_error = None
        self.member_status = "administrator"
        self.member_error = None

    async def get_me(self):
        return self.me

    async def copy_message(self, chat_id, from_chat_id, message_id):
        self.copies.append((chat_id, from_chat_id, message_id))
        if self.copy_error is not None:
            raise self.copy_error
        return SimpleNamespace(id=self.copy_msg_id)

    async def get_chat_member(self, chat_id, user_id):
        if self.member_error is not None:
            raise self.member_error
        return SimpleNamespace(status=self.member_status)

    async def get_bot_commands(self):
        return list(self.commands)

    async def set_bot_commands(self, commands):
        self.commands_set.append(list(commands))
        self.commands = list(commands)


def install_stubs():
    """Install pyrogram + Backend stubs; return the mutable stub handles."""
    pyro = types.ModuleType("pyrogram")

    class Client:
        handlers = []

        @classmethod
        def on_message(cls, filters=None, group=0):
            def deco(fn):
                cls.handlers.append((filters, group, fn))
                return fn
            return deco

    pyro.Client = Client
    pyro.enums = SimpleNamespace(ParseMode=SimpleNamespace(HTML="html"))
    pyro.filters = SimpleNamespace(
        private=_Filter(), document=_Filter(), video=_Filter(),
        command=lambda *a, **k: _Filter(),
    )
    pyro_types = types.ModuleType("pyrogram.types")
    pyro_types.Message = object
    pyro_types.BotCommand = BotCommand
    sys.modules["pyrogram"] = pyro
    sys.modules["pyrogram.types"] = pyro_types

    backend = types.ModuleType("Backend")
    db = types.ModuleType("Backend.db")
    state = {"by_user": {}}

    async def add_api_token(name, user_id):
        doc = {"token": f"TOK-{user_id}", "name": name, "user_id": user_id}
        state["by_user"][user_id] = doc
        return doc

    async def get_api_token_by_user(user_id):
        return state["by_user"].get(user_id)

    db.add_api_token = add_api_token
    db.get_api_token_by_user = get_api_token_by_user
    backend.db = db

    config = types.ModuleType("Backend.config")
    config.Telegram = SimpleNamespace(OWNER_ID=OWNER)

    pyrofork = types.ModuleType("Backend.pyrofork")
    botmod = types.ModuleType("Backend.pyrofork.bot")
    botmod.Userbot = FakeUserbot()  # configured by default
    botmod.USERBOT_CLIENT_INDEX = -1

    helper = types.ModuleType("Backend.helper")
    encrypt = types.ModuleType("Backend.helper.encrypt")
    captured = []

    async def encode_string(data):
        captured.append(data)
        return "ENC123"

    encrypt.encode_string = encode_string

    sm = types.ModuleType("Backend.helper.settings_manager")
    current = SimpleNamespace(subscription=False, base_url="https://example.test")

    class SettingsManager:
        @staticmethod
        def current():
            return current

    sm.SettingsManager = SettingsManager

    logger_mod = types.ModuleType("Backend.logger")
    logger_mod.LOGGER = SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )

    #----- Link Doctor stubs: file-id lookup, probe streamer, buffer state
    doctor = SimpleNamespace(file_ids_error=None, probe_mode="ok")
    pyro_helper = types.ModuleType("Backend.helper.pyro")

    async def get_file_ids(client, chat_id, message_id):
        if doctor.file_ids_error is not None:
            raise doctor.file_ids_error
        return SimpleNamespace(file_size=989_000_000, dc_id=5,
                               mime_type="video/x-matroska")

    pyro_helper.get_file_ids = get_file_ids

    bs_mod = types.ModuleType("Backend.helper.buffered_streamer")

    class FakeBufferedStreamer:
        def __init__(self, client, index):
            self.client = client
            self.index = index

        async def prefetch_stream(self, **kwargs):
            async def gen():
                if doctor.probe_mode == "hang":
                    import asyncio
                    await asyncio.sleep(60)
                if doctor.probe_mode == "error":
                    raise RuntimeError("dc unreachable")
                yield b"x" * 1024
            return gen()

    bs_mod.BufferedByteStreamer = FakeBufferedStreamer

    sb_mod = types.ModuleType("Backend.helper.stream_buffer")
    sb_mod.get_policy = lambda: SimpleNamespace(
        profile="balanced", workers=4, per_client_cap=4, cache_mb=48)
    sb_mod.METRICS = SimpleNamespace(
        snapshot=lambda: {"floods_seen": 0, "stall_timeouts": 0})

    sys.modules.update({
        "Backend": backend, "Backend.db": db, "Backend.config": config,
        "Backend.pyrofork": pyrofork, "Backend.pyrofork.bot": botmod,
        "Backend.helper": helper, "Backend.helper.encrypt": encrypt,
        "Backend.helper.settings_manager": sm, "Backend.logger": logger_mod,
        "Backend.helper.pyro": pyro_helper,
        "Backend.helper.buffered_streamer": bs_mod,
        "Backend.helper.stream_buffer": sb_mod,
    })
    return SimpleNamespace(db_state=state, captured=captured, botmod=botmod,
                           settings=current, Client=Client, doctor=doctor)


STUBS = install_stubs()
dl = load_module("dl_link_under_test", ROOT / "Backend/pyrofork/plugins/dl_link.py")


_PRISTINE_DEDUPE = (dl._dedupe_get, dl._dedupe_put)
_PRISTINE_COLL = dl._dedupe_coll


def reset():
    STUBS.db_state["by_user"].clear()
    STUBS.captured.clear()
    STUBS.botmod.Userbot = FakeUserbot()
    dl._dedupe_get, dl._dedupe_put = _PRISTINE_DEDUPE
    dl._dedupe_coll = _PRISTINE_COLL
    for win in dl._batch_windows.values():
        task = win.get("task")
        if task is not None and not task.done():
            task.cancel()
    dl._batch_windows.clear()
    dl.BATCH_QUIET_S = 3.0
    if hasattr(STUBS.settings, "dl_channel_id"):
        del STUBS.settings.dl_channel_id
    STUBS.settings.subscription = False
    STUBS.settings.base_url = "https://example.test"
    STUBS.doctor.file_ids_error = None
    STUBS.doctor.probe_mode = "ok"
    dl._peer_warmed = False
    dl.PROBE_TIMEOUT_S = 20.0


def test_handlers_registered():
    names = [fn.__name__ for _f, _g, fn in STUBS.Client.handlers]
    assert names == ["dl_auto_link", "dl_command", "dl_check_command"], names


def test_pure_helpers():
    assert dl.link_payload(999, 42) == {"chat_id": 999, "msg_id": 42, "global": True}
    assert dl.build_link("https://x.test", "TOK", "ENC", "a b.mkv") == \
        "https://x.test/dl/TOK/ENC/a%20b.mkv"
    assert dl.human_size(500) == "500 B"
    assert dl.human_size(989_000_000).endswith("MB")


def test_owner_document_gets_link():
    async def main():
        reset()
        msg = FakeMessage(user=FakeUser(OWNER), document=FakeMedia())
        await dl.dl_auto_link(FakeClient(), msg)
        assert len(msg.replies) == 1
        text = msg.replies[0]
        assert "https://example.test/dl/TOK-111/ENC123/TDH%20-%20S3%20E27.mkv" in text, text
        assert STUBS.captured == [{"chat_id": 999, "msg_id": 42, "global": True}]
    run(main())


def test_stranger_gets_silence_in_owner_mode():
    async def main():
        reset()
        msg = FakeMessage(user=FakeUser(555), document=FakeMedia())
        await dl.dl_auto_link(FakeClient(), msg)
        assert msg.replies == []
    run(main())


def test_subscriber_with_token_gets_link():
    async def main():
        reset()
        STUBS.settings.subscription = True
        STUBS.db_state["by_user"][555] = {"token": "TOK-SUB"}
        msg = FakeMessage(user=FakeUser(555), document=FakeMedia())
        await dl.dl_auto_link(FakeClient(), msg)
        assert len(msg.replies) == 1 and "/dl/TOK-SUB/" in msg.replies[0]
    run(main())


def test_subscriber_without_token_gets_silence():
    async def main():
        reset()
        STUBS.settings.subscription = True
        msg = FakeMessage(user=FakeUser(555), document=FakeMedia())
        await dl.dl_auto_link(FakeClient(), msg)
        assert msg.replies == []
    run(main())


def test_dl_command_needs_a_replied_file():
    async def main():
        reset()
        msg = FakeMessage(user=FakeUser(OWNER))
        await dl.dl_command(FakeClient(), msg)
        assert len(msg.replies) == 1 and "Reply to a file" in msg.replies[0]
    run(main())


def test_dl_command_on_replied_video():
    async def main():
        reset()
        target = FakeMessage(mid=77, user=FakeUser(OWNER),
                             video=FakeMedia(file_name=None, file_size=10))
        msg = FakeMessage(mid=78, user=FakeUser(OWNER), reply_to=target)
        await dl.dl_command(FakeClient(), msg)
        assert len(msg.replies) == 1
        assert "/dl/TOK-111/ENC123/file_77" in msg.replies[0]
        assert STUBS.captured == [{"chat_id": 999, "msg_id": 77, "global": True}]
    run(main())


def test_peer_warmup_by_username_once():
    async def main():
        reset()
        client = FakeClient()
        for _ in range(2):
            msg = FakeMessage(user=FakeUser(OWNER), document=FakeMedia())
            await dl.dl_auto_link(client, msg)
            assert len(msg.replies) == 1
        warm = [c for c in STUBS.botmod.Userbot.calls if c[0] in ("get_chat", "get_users")]
        assert warm == [("get_chat", "reonthebot")], warm
    run(main())


def test_peer_warmup_failure_still_links():
    async def main():
        reset()
        STUBS.botmod.Userbot = FakeUserbot(fail=True)
        msg = FakeMessage(user=FakeUser(OWNER), document=FakeMedia())
        await dl.dl_auto_link(FakeClient(), msg)
        assert len(msg.replies) == 1 and "/dl/TOK-111/" in msg.replies[0]
        methods = [m for m, _u in STUBS.botmod.Userbot.calls
                   if m in ("get_chat", "get_users")]
        assert methods == ["get_chat", "get_users"], methods
    run(main())


def test_msg_id_resolved_in_userbot_view_by_unique_id():
    async def main():
        reset()
        # Same file, but the userbot's DM numbers it 15, not 42.
        twin = FakeMessage(mid=15, user=FakeUser(OWNER), document=FakeMedia())
        STUBS.botmod.Userbot = FakeUserbot(history=[twin])
        msg = FakeMessage(user=FakeUser(OWNER), document=FakeMedia())
        await dl.dl_auto_link(FakeClient(), msg)
        assert len(msg.replies) == 1
        assert STUBS.captured == [{"chat_id": 999, "msg_id": 15, "global": True}], \
            STUBS.captured
    run(main())


def test_msg_id_resolved_by_name_and_size_fallback():
    async def main():
        reset()
        twin = FakeMessage(mid=16, user=FakeUser(OWNER),
                           document=FakeMedia(file_unique_id="OTHER"))
        STUBS.botmod.Userbot = FakeUserbot(history=[twin])
        msg = FakeMessage(user=FakeUser(OWNER), document=FakeMedia())
        await dl.dl_auto_link(FakeClient(), msg)
        assert STUBS.captured == [{"chat_id": 999, "msg_id": 16, "global": True}], \
            STUBS.captured
    run(main())


def test_msg_id_falls_back_to_bot_side_id():
    async def main():
        reset()
        other = FakeMessage(mid=17, user=FakeUser(OWNER),
                            document=FakeMedia(file_name="x.mkv", file_size=1,
                                                file_unique_id="ZZZ"))
        STUBS.botmod.Userbot = FakeUserbot(history=[other])
        msg = FakeMessage(user=FakeUser(OWNER), document=FakeMedia())
        await dl.dl_auto_link(FakeClient(), msg)
        assert STUBS.captured == [{"chat_id": 999, "msg_id": 42, "global": True}], \
            STUBS.captured
    run(main())


def _check_pair(history=None):
    target = FakeMessage(mid=42, user=FakeUser(OWNER), document=FakeMedia())
    msg = FakeMessage(mid=43, user=FakeUser(OWNER), reply_to=target)
    if history is not None:
        STUBS.botmod.Userbot = FakeUserbot(history=history)
    return msg


def test_check_usage_without_reply():
    async def main():
        reset()
        msg = FakeMessage(user=FakeUser(OWNER))
        await dl.dl_check_command(FakeClient(), msg)
        assert len(msg.replies) == 1 and "pre-flight" in msg.replies[0]
    run(main())


def test_check_stranger_gets_silence():
    async def main():
        reset()
        target = FakeMessage(mid=42, user=FakeUser(555), document=FakeMedia())
        msg = FakeMessage(mid=43, user=FakeUser(555), reply_to=target)
        await dl.dl_check_command(FakeClient(), msg)
        assert msg.replies == []
    run(main())


def test_check_healthy_report():
    async def main():
        reset()
        twin = FakeMessage(mid=15, user=FakeUser(OWNER), document=FakeMedia())
        msg = _check_pair(history=[twin])
        await dl.dl_check_command(FakeClient(), msg)
        assert len(msg.replies) == 1
        text = msg.replies[0]
        for needle in ("id 15", "DC5", "Probe", "Buffer: balanced", "healthy"):
            assert needle in text, (needle, text)
    run(main())


def test_check_userbot_missing_reports_broken():
    async def main():
        reset()
        msg = _check_pair()
        STUBS.botmod.Userbot = None
        await dl.dl_check_command(FakeClient(), msg)
        assert len(msg.replies) == 1
        assert "not configured" in msg.replies[0] and "broken" in msg.replies[0]
    run(main())


def test_check_probe_timeout_reports_broken():
    async def main():
        reset()
        STUBS.doctor.probe_mode = "hang"
        dl.PROBE_TIMEOUT_S = 0.2
        msg = _check_pair(history=[])
        await dl.dl_check_command(FakeClient(), msg)
        assert len(msg.replies) == 1
        assert "no data within" in msg.replies[0] and "broken" in msg.replies[0]
    run(main())


def test_check_file_lookup_failure_reports_broken():
    async def main():
        reset()
        STUBS.doctor.file_ids_error = RuntimeError("dead reference")
        msg = _check_pair(history=[])
        await dl.dl_check_command(FakeClient(), msg)
        assert len(msg.replies) == 1
        assert "File properties" in msg.replies[0] and "broken" in msg.replies[0]
    run(main())


def test_userbot_missing_is_explained():
    async def main():
        reset()
        STUBS.botmod.Userbot = None
        msg = FakeMessage(user=FakeUser(OWNER), document=FakeMedia())
        await dl.dl_auto_link(FakeClient(), msg)
        assert len(msg.replies) == 1 and "no Userbot is configured" in msg.replies[0]
    run(main())


#----- Pool mode (DL Channel)

DL_ID = -1001234567890


def _pool_on(dedupe_doc=None):
    """Enable pool mode with an in-memory dedupe store; return the store."""
    STUBS.settings.dl_channel_id = DL_ID
    store = {}

    async def fake_get(unique_id):
        return dedupe_doc if dedupe_doc is not None else store.get(unique_id)

    async def fake_put(unique_id, msg_id, name=None, size=None):
        store[unique_id] = {"msg_id": msg_id}

    dl._dedupe_get, dl._dedupe_put = fake_get, fake_put
    return store


def test_pool_payload_shape():
    assert dl.pool_payload(DL_ID, 55) == {"chat_id": 1234567890, "msg_id": 55}
    assert dl.pool_payload(1234567890, 55) == {"chat_id": 1234567890, "msg_id": 55}


def test_pool_mode_copies_and_links_via_pool():
    async def main():
        reset()
        store = _pool_on()
        client = FakeClient()
        msg = FakeMessage(user=FakeUser(OWNER), document=FakeMedia())
        await dl.dl_auto_link(client, msg)
        assert client.copies == [(DL_ID, OWNER, 42)], client.copies
        assert store == {"UNIQ1": {"msg_id": 9001}}, store
        assert STUBS.captured == [{"chat_id": 1234567890, "msg_id": 9001}], STUBS.captured
        assert len(msg.replies) == 1
        text = msg.replies[0]
        assert "/dl/TOK-111/ENC123/" in text and "bot pool" in text, text
        assert "userbot not used" in text
        # The userbot was never touched: no peer warm-up, no history scan.
        assert STUBS.botmod.Userbot.calls == [], STUBS.botmod.Userbot.calls
    run(main())


def test_pool_mode_dedupe_hit_skips_copy():
    async def main():
        reset()
        _pool_on(dedupe_doc={"msg_id": 777})
        client = FakeClient()
        msg = FakeMessage(user=FakeUser(OWNER), document=FakeMedia())
        await dl.dl_auto_link(client, msg)
        assert client.copies == []
        assert STUBS.captured == [{"chat_id": 1234567890, "msg_id": 777}]
        assert "Already in" in msg.replies[0]
    run(main())


def test_pool_mode_copy_failure_never_falls_back():
    async def main():
        reset()
        _pool_on()
        client = FakeClient()
        client.copy_error = RuntimeError("CHAT_ADMIN_REQUIRED")
        msg = FakeMessage(user=FakeUser(OWNER), document=FakeMedia())
        await dl.dl_auto_link(client, msg)
        assert len(msg.replies) == 1
        text = msg.replies[0]
        assert "Bot Admin Manager" in text and "not</b> used" in text, text
        assert STUBS.captured == []  # no link of either kind was issued
    run(main())


def test_pool_mode_works_without_userbot():
    async def main():
        reset()
        _pool_on()
        STUBS.botmod.Userbot = None
        msg = FakeMessage(user=FakeUser(OWNER), document=FakeMedia())
        await dl.dl_auto_link(FakeClient(), msg)
        assert len(msg.replies) == 1 and "/dl/TOK-111/ENC123/" in msg.replies[0]
    run(main())


def test_forward_from_channel_replies_id_only():
    async def main():
        reset()
        origin = SimpleNamespace(
            chat=SimpleNamespace(id=DL_ID, type="channel", title="DL Pool"),
            message_id=5)
        msg = FakeMessage(user=FakeUser(OWNER), document=FakeMedia(),
                          forward_origin=origin)
        client = FakeClient()
        await dl.dl_auto_link(client, msg)
        assert len(msg.replies) == 1
        text = msg.replies[0]
        assert "Channel ID" in text and str(DL_ID) in text, text
        assert STUBS.captured == [] and client.copies == []
    run(main())


def test_forward_from_other_channel_links_when_pool_on():
    async def main():
        reset()
        _pool_on()
        origin = SimpleNamespace(
            chat=SimpleNamespace(id=-1009998887776, type="channel", title="Donghua"),
            message_id=5)
        msg = FakeMessage(user=FakeUser(OWNER), document=FakeMedia(),
                          forward_origin=origin)
        client = FakeClient()
        await dl.dl_auto_link(client, msg)
        # A normal link request: copied into the DL Channel, pool payload.
        assert client.copies == [(DL_ID, OWNER, 42)], client.copies
        assert STUBS.captured == [{"chat_id": 1234567890, "msg_id": 9001}]
        assert "Channel ID" not in msg.replies[0]
    run(main())


def test_forward_from_dl_channel_links_original_post():
    async def main():
        reset()
        store = _pool_on()
        origin = SimpleNamespace(
            chat=SimpleNamespace(id=DL_ID, type="channel", title="DL Pool"),
            message_id=321)
        msg = FakeMessage(user=FakeUser(OWNER), document=FakeMedia(),
                          forward_origin=origin)
        client = FakeClient()
        await dl.dl_auto_link(client, msg)
        assert client.copies == []  # already pooled: no copy
        assert STUBS.captured == [{"chat_id": 1234567890, "msg_id": 321}], STUBS.captured
        assert store == {"UNIQ1": {"msg_id": 321}}, store
        assert "Already in your DL Channel" in msg.replies[0]
    run(main())


def test_forward_from_dl_channel_without_origin_id_shows_helper():
    async def main():
        reset()
        _pool_on()
        origin = SimpleNamespace(
            chat=SimpleNamespace(id=DL_ID, type="channel", title="DL Pool"),
            message_id=None)
        msg = FakeMessage(user=FakeUser(OWNER), document=FakeMedia(),
                          forward_origin=origin)
        await dl.dl_auto_link(FakeClient(), msg)
        assert len(msg.replies) == 1
        assert "configured DL Channel" in msg.replies[0], msg.replies[0]
        assert STUBS.captured == []
    run(main())


def test_forward_from_user_still_links():
    async def main():
        reset()
        origin = SimpleNamespace(
            chat=SimpleNamespace(id=555, type="private", title=None),
            message_id=5)
        msg = FakeMessage(user=FakeUser(OWNER), document=FakeMedia(),
                          forward_origin=origin)
        await dl.dl_auto_link(FakeClient(), msg)
        assert STUBS.captured == [{"chat_id": 999, "msg_id": 42, "global": True}]
    run(main())


def test_check_pool_mode_healthy():
    async def main():
        reset()
        _pool_on(dedupe_doc={"msg_id": 777})
        msg = _check_pair()
        await dl.dl_check_command(FakeClient(), msg)
        assert len(msg.replies) == 1
        text = msg.replies[0]
        assert "DL Channel" in text and "already pooled as post 777" in text, text
        assert "File:" in text and "Probe:" in text and "Pool: 1 bot" in text, text
        assert "healthy" in text
        assert "✓ Userbot" not in text
    run(main())


def test_check_pool_mode_not_admin_is_degraded():
    async def main():
        reset()
        _pool_on()
        client = FakeClient()
        client.member_status = "member"
        msg = _check_pair()
        await dl.dl_check_command(client, msg)
        text = msg.replies[0]
        assert "not an admin" in text and "not pooled yet" in text, text
        assert "degraded" in text
    run(main())


def test_check_pool_mode_channel_unreachable_is_broken():
    async def main():
        reset()
        _pool_on()
        client = FakeClient()
        client.member_error = RuntimeError("boom")
        msg = _check_pair()
        await dl.dl_check_command(client, msg)
        text = msg.replies[0]
        assert "cannot access the DL Channel" in text and "broken" in text, text
    run(main())


#----- Wave 2 Phase C: batch pooling, DL Index, lifecycle

import asyncio as _asyncio
import time as _time


class FakeColl:
    """In-memory dl_link_map collection (motor-shaped)."""

    def __init__(self, docs=None):
        self.docs = {d["_id"]: dict(d) for d in (docs or [])}
        self.updates = []

    async def find_one(self, query):
        doc = self.docs.get(query.get("_id"))
        return dict(doc) if doc else None

    def find(self, query):
        async def gen():
            for doc in list(self.docs.values()):
                yield dict(doc)
        return gen()

    async def update_one(self, query, update, upsert=False):
        self.updates.append((query, update))
        doc = self.docs.setdefault(query["_id"], {"_id": query["_id"]})
        for key, value in (update.get("$set") or {}).items():
            doc[key] = value
        for key, value in (update.get("$setOnInsert") or {}).items():
            doc.setdefault(key, value)

    async def delete_one(self, query):
        self.docs.pop(query.get("_id"), None)


def test_build_m3u_format():
    out = dl.build_m3u([("A B.mkv", "http://x/1"), ("C\nD.mkv", "http://x/2")])
    assert out.startswith("#EXTM3U\n")
    assert "#EXTINF:-1,A B.mkv\nhttp://x/1\n" in out
    assert "#EXTINF:-1,C D.mkv\nhttp://x/2\n" in out  # newline in name flattened


def test_batch_two_files_one_combined_reply_and_m3u():
    async def main():
        reset()
        _pool_on()
        dl.BATCH_QUIET_S = 0.05
        client = FakeClient()
        m1 = FakeMessage(mid=1, user=FakeUser(OWNER),
                         document=FakeMedia("Ep 1.mkv", 100, "U1"))
        m2 = FakeMessage(mid=2, user=FakeUser(OWNER),
                         document=FakeMedia("Ep 2.mkv", 200, "U2"))
        await dl.dl_auto_link(client, m1)
        # First file: instant single reply, exactly like a single file
        assert len(m1.replies) == 1 and "Ep 1.mkv" in m1.replies[0]
        await dl.dl_auto_link(client, m2)
        assert m2.replies == []  # held for the batch
        await _asyncio.sleep(0.3)
        # One combined reply on the first held message, with both links
        assert len(m2.replies) == 1, m2.replies
        text = m2.replies[0]
        assert "Ep 1.mkv" in text and "Ep 2.mkv" in text, text
        assert len(m2.documents) == 1
        data, kwargs = m2.documents[0]
        assert kwargs.get("file_name") == "dl-batch.m3u"
        body = data.getvalue().decode()
        assert body.startswith("#EXTM3U") and body.count("#EXTINF") == 2
        assert len(client.copies) == 2
    run(main())


def test_batch_single_file_no_followup():
    async def main():
        reset()
        _pool_on()
        dl.BATCH_QUIET_S = 0.05
        client = FakeClient()
        m1 = FakeMessage(mid=1, user=FakeUser(OWNER), document=FakeMedia())
        await dl.dl_auto_link(client, m1)
        await _asyncio.sleep(0.25)
        assert len(m1.replies) == 1 and m1.documents == []
    run(main())


def test_dedupe_put_records_index_metadata():
    async def main():
        reset()
        coll = FakeColl()
        dl._dedupe_coll = lambda: coll
        await dl._dedupe_put("U1", 5, "Name.mkv", 123)
        doc = coll.docs["U1"]
        assert doc["msg_id"] == 5 and doc["name"] == "Name.mkv" and doc["size"] == 123
        assert doc["pinned"] is False and doc["pooled_at"] > 0
        # Re-put (backfill) must not reset pooled_at or pinned
        doc["pinned"] = True
        stamp = doc["pooled_at"]
        await dl._dedupe_put("U1", 5, "Name.mkv", 123)
        assert coll.docs["U1"]["pinned"] is True
        assert coll.docs["U1"]["pooled_at"] == stamp
    run(main())


def _index_coll():
    now = _time.time()
    return FakeColl([
        {"_id": "UA", "msg_id": 11, "name": "Alpha.mkv", "size": 100,
         "pooled_at": now - 100, "pinned": True},
        {"_id": "UB", "msg_id": 12, "ts": now - 50},  # legacy doc, no metadata
        {"_id": "UC", "msg_id": 13, "name": "Beta Show.mkv", "size": 300,
         "pooled_at": now - 10, "pinned": False},
    ])


def test_index_docs_shape_sort_and_filter():
    async def main():
        reset()
        _pool_on()
        coll = _index_coll()
        dl._dedupe_coll = lambda: coll
        docs = await dl._index_docs()
        assert [d["msg_id"] for d in docs] == [13, 12, 11]  # newest first
        assert docs[1]["name"] == "DL post 12" and docs[1]["pinned"] is False
        assert docs[0]["pinned"] is False and docs[2]["pinned"] is True
        hits = await dl._index_docs("beta")
        assert [d["msg_id"] for d in hits] == [13]
    run(main())


def test_dl_list_command_replies_with_links():
    async def main():
        reset()
        _pool_on()
        dl._dedupe_coll = lambda: _index_coll()
        msg = FakeMessage(user=FakeUser(OWNER))
        msg.command = ["dl", "list"]
        await dl.dl_command(FakeClient(), msg)
        assert len(msg.replies) == 1
        text = msg.replies[0]
        assert "DL Index" in text and "Alpha.mkv" in text and "📌" in text, text
        assert "/dl/TOK-111/ENC123/" in text
    run(main())


def test_dl_pin_and_unpin_by_reply():
    async def main():
        reset()
        store = _pool_on(dedupe_doc={"msg_id": 9001, "name": "TDH - S3 E27.mkv"})
        coll = FakeColl([{"_id": "UNIQ1", "msg_id": 9001}])
        dl._dedupe_coll = lambda: coll
        target = FakeMessage(mid=7, user=FakeUser(OWNER), document=FakeMedia())
        msg = FakeMessage(user=FakeUser(OWNER), reply_to=target)
        msg.command = ["dl", "pin"]
        await dl.dl_command(FakeClient(), msg)
        assert coll.docs["UNIQ1"]["pinned"] is True
        assert "Pinned" in msg.replies[0]
        msg2 = FakeMessage(user=FakeUser(OWNER), reply_to=target)
        msg2.command = ["dl", "unpin"]
        await dl.dl_command(FakeClient(), msg2)
        assert coll.docs["UNIQ1"]["pinned"] is False
    run(main())


def test_expired_msg_ids_rules():
    now = 1_000_000_000.0
    docs = [
        {"msg_id": 1, "pooled_at": now - 40 * 86400},                    # old -> expired
        {"msg_id": 2, "pooled_at": now - 40 * 86400, "pinned": True},    # pinned -> kept
        {"msg_id": 3, "pooled_at": now - 5 * 86400},                     # fresh -> kept
        {"msg_id": 4, "ts": now - 90 * 86400},                           # legacy ts -> expired
    ]
    assert dl.expired_msg_ids(docs, 30, now) == [1, 4]
    assert dl.expired_msg_ids(docs, 0, now) == []  # 0 = never


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} dl_link tests passed")
