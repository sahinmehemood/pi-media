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
    def __init__(self, mid=42, user=None, document=None, video=None, reply_to=None):
        self.id = mid
        self.from_user = user
        self.chat = SimpleNamespace(id=user.id if user else 0)
        self.document = document
        self.video = video
        self.reply_to_message = reply_to
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
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

    async def get_me(self):
        return self.me

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

    sys.modules.update({
        "Backend": backend, "Backend.db": db, "Backend.config": config,
        "Backend.pyrofork": pyrofork, "Backend.pyrofork.bot": botmod,
        "Backend.helper": helper, "Backend.helper.encrypt": encrypt,
        "Backend.helper.settings_manager": sm, "Backend.logger": logger_mod,
    })
    return SimpleNamespace(db_state=state, captured=captured, botmod=botmod,
                           settings=current, Client=Client)


STUBS = install_stubs()
dl = load_module("dl_link_under_test", ROOT / "Backend/pyrofork/plugins/dl_link.py")


def reset():
    STUBS.db_state["by_user"].clear()
    STUBS.captured.clear()
    STUBS.botmod.Userbot = FakeUserbot()
    STUBS.settings.subscription = False
    STUBS.settings.base_url = "https://example.test"
    dl._peer_warmed = False
    dl._command_registered = False


def test_handlers_registered():
    names = [fn.__name__ for _f, _g, fn in STUBS.Client.handlers]
    assert names == ["dl_auto_link", "dl_command"], names


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


def test_dl_added_to_command_menu_once():
    async def main():
        reset()
        client = FakeClient()
        msg = FakeMessage(user=FakeUser(OWNER), document=FakeMedia())
        await dl.dl_auto_link(client, msg)
        await dl.dl_auto_link(client, FakeMessage(user=FakeUser(OWNER), document=FakeMedia()))
        assert len(client.commands_set) == 1
        merged = [c.command for c in client.commands_set[0]]
        assert merged == ["start", "set", "dl"], merged
    run(main())


def test_dl_menu_skips_when_already_present():
    async def main():
        reset()
        client = FakeClient(commands=[BotCommand("start", "s"), BotCommand("dl", "d")])
        msg = FakeMessage(user=FakeUser(OWNER), document=FakeMedia())
        await dl.dl_auto_link(client, msg)
        assert client.commands_set == []
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


def test_userbot_missing_is_explained():
    async def main():
        reset()
        STUBS.botmod.Userbot = None
        msg = FakeMessage(user=FakeUser(OWNER), document=FakeMedia())
        await dl.dl_auto_link(FakeClient(), msg)
        assert len(msg.replies) == 1 and "no Userbot is configured" in msg.replies[0]
    run(main())


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} dl_link tests passed")
