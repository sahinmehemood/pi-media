"""Simple Librarian tests (Yan's model): the bot verifies and writes
the caption; this module only applies it. Fake motor collections +
fake Telegram, no app imports."""

import asyncio
import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "librarian_simple", ROOT / "Backend/helper/librarian_simple.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ls = _load()
ANI = -1004395609351


def _match(doc, query):
    for k, v in query.items():
        cur = doc
        for part in k.split("."):
            cur = (cur or {}).get(part) if isinstance(cur, dict) else None
        if isinstance(v, dict) and "$in" in v:
            if cur not in v["$in"]:
                return False
        elif isinstance(v, dict) and "$gte" in v:
            if not (cur is not None and cur >= v["$gte"]):
                return False
        elif cur != v:
            return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs
        self._limit = None

    def sort(self, key, direction=1):
        self._docs = sorted(self._docs, key=lambda d: d.get(key) or 0,
                            reverse=direction < 0)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def __aiter__(self):
        docs = self._docs[: self._limit] if self._limit else self._docs
        self._it = iter(docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class FakeColl:
    _seq = [0]

    def __init__(self, docs=None):
        self.docs = list(docs or [])
        for d in self.docs:
            if "_id" not in d:
                FakeColl._seq[0] += 1
                d["_id"] = FakeColl._seq[0]

    def find(self, query):
        return FakeCursor([d for d in self.docs if _match(d, query)])

    async def find_one(self, query, sort=None):
        hits = [d for d in self.docs if _match(d, query)]
        if sort:
            hits.sort(key=lambda d: d.get(sort[0][0]) or 0,
                      reverse=sort[0][1] < 0)
        return hits[0] if hits else None

    async def count_documents(self, query):
        return len([d for d in self.docs if _match(d, query)])

    async def insert_one(self, doc):
        doc = dict(doc)
        if "_id" not in doc:
            FakeColl._seq[0] += 1
            doc["_id"] = FakeColl._seq[0]
        self.docs.append(doc)

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _match(d, query):
                d.update(update.get("$set") or {})
                return
        if upsert:
            doc = dict(query)
            doc.update(update.get("$set") or {})
            self.docs.append(doc)


def _deps(**over):
    state = {"captions": {(ANI, 101): "PW - E77 [E-SUB] 4K.mkv"},
             "index": {}, "taught": []}
    ledger = FakeColl([{
        "key": "k1", "status": "open", "scan_chat_id": ANI,
        "scan_msg_id": 101, "caption": "PW - E77 [E-SUB] 4K.mkv",
        "bits": {"stem": "pw", "season": None, "episode": 77,
                 "special": None, "year": None},
        "reason_code": "no-match"}])
    fixes = FakeColl()

    async def read_caption(chat, msg):
        return state["captions"].get((chat, msg))

    async def edit_caption(chat, msg, caption):
        state["captions"][(chat, msg)] = caption

    async def indexed_ids(chat, msg):
        return state["index"].get((chat, msg))

    async def teach_alias(channel, stem, caption):
        state["taught"].append((channel, stem, caption))

    settings = SimpleNamespace(librarian_enabled=True,
                               librarian_dry_run=False,
                               librarian_edit_budget_per_day=50)
    deps = ls.Deps(read_caption, edit_caption, indexed_ids, ledger,
                   FakeColl(), fixes, teach_alias, settings)
    deps._state = state
    for k, v in over.items():
        setattr(deps, k, v)
    return deps


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


NEW = ("Perfect World (2021) S01E77 4K\n"
       "<a href=\"https://www.imdb.com/title/tt14986786/\">IMDB</a>")
OLD = "PW - E77 [E-SUB] 4K.mkv"


def test_waiting_lists_located_files():
    deps = _deps()
    out = _run(ls.waiting(deps))
    assert out["count"] == 1
    f = out["files"][0]
    assert f["chat_id"] == ANI and f["msg_id"] == 101 and f["located"]
    assert f["stem"] == "pw" and f["episode"] == 77
    out = _run(ls.waiting(deps, stem="other"))
    assert out["count"] == 0


def test_fix_happy_path():
    deps = _deps()
    deps._state["index"][(ANI, 101)] = ("tt14986786", None)
    out = _run(ls.fix(deps, ANI, 101, NEW, OLD, key="k1", stem="pw"))
    assert out["ok"] and out["indexed"]
    assert deps._state["captions"][(ANI, 101)] == NEW
    assert deps.ledger.docs[0]["status"] == "fixed"
    assert deps._state["taught"] and deps._state["taught"][0][1] == "pw"
    assert deps.fixes.docs and deps.fixes.docs[0]["after"] == NEW


def test_fix_refuses_when_caption_changed():
    deps = _deps()
    deps._state["captions"][(ANI, 101)] = "someone else's edit"
    out = _run(ls.fix(deps, ANI, 101, NEW, OLD, key="k1"))
    assert not out["ok"] and "changed by someone else" in out["message"]
    assert deps._state["captions"][(ANI, 101)] == "someone else's edit"


def test_fix_dry_run_edits_nothing():
    deps = _deps()
    deps.settings.librarian_dry_run = True
    out = _run(ls.fix(deps, ANI, 101, NEW, OLD, key="k1"))
    assert out["ok"] and out["dry_run"]
    assert deps._state["captions"][(ANI, 101)] == OLD


def test_fix_budget_is_a_hard_stop():
    deps = _deps()
    deps.settings.librarian_edit_budget_per_day = 1
    deps.fixes.docs.append({"ts": time.time(), "reverted": False})
    out = _run(ls.fix(deps, ANI, 101, NEW, OLD, key="k1"))
    assert not out["ok"] and "budget" in out["message"]


def test_fix_flood_wait_is_plain():
    deps = _deps()

    async def flood(chat, msg, caption):
        err = RuntimeError("flood")
        err.retry_after = 30
        raise err
    deps.edit_caption = flood
    out = _run(ls.fix(deps, ANI, 101, NEW, OLD, key="k1"))
    assert not out["ok"] and out["flood_wait"] == 30


def test_fix_unconfirmed_index_is_honest():
    deps = _deps()  # index stays empty
    out = _run(ls.fix(deps, ANI, 101, NEW, OLD, key="k1"))
    assert out["ok"] and out["indexed"] is False
    assert "not confirmed" in out["message"]


def test_undo_restores_exactly():
    deps = _deps()
    deps._state["index"][(ANI, 101)] = ("tt14986786", None)
    _run(ls.fix(deps, ANI, 101, NEW, OLD, key="k1", stem="pw"))
    out = _run(ls.undo(deps, ANI, 101))
    assert out["ok"] and out["restored"] == OLD
    assert deps._state["captions"][(ANI, 101)] == OLD
    assert deps.ledger.docs[0]["status"] == "open"


def test_counts_are_straight():
    deps = _deps()
    deps.ledger.docs.append({"key": "k2", "status": "fixed"})
    out = _run(ls.counts(deps))
    assert out == {"ok": True, "waiting": 1, "fixed": 1, "fixed_today": 0}


def test_recent_fixes_newest_first():
    deps = _deps()
    deps.fixes.docs.append({"chat_id": ANI, "msg_id": 1, "before": "a",
                            "after": "b", "ts": 100.0, "indexed": True,
                            "reverted": False})
    deps.fixes.docs.append({"chat_id": ANI, "msg_id": 2, "before": "c",
                            "after": "d", "ts": 200.0, "indexed": False,
                            "reverted": True})
    out = _run(ls.recent_fixes(deps))
    assert [f["msg_id"] for f in out] == [2, 1]
    assert out[0]["reverted"] and out[0]["when"]


def test_disabled_refuses():
    deps = _deps()
    deps.settings.librarian_enabled = False
    out = _run(ls.fix(deps, ANI, 101, NEW, OLD, key="k1"))
    assert not out["ok"] and "off" in out["message"]
