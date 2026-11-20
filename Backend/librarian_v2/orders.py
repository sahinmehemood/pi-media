"""Librarian v2 Work Orders — the bot's only capability (SPEC §6).

The bot holds no general mutation power. Inside a live Work Order it may
plan and apply caption fixes for exactly the order's scope; every apply
re-checks scope, identity, budget and plan hash server-side. All external
effects go through injected callables so the whole flow is testable with
the harness fakes:

    editor(chat_id, msg_id, new_caption)  — the Media Manager bot edit
    reader(chat_id, msg_id) -> caption    — live caption read (identity)
    verify(file_row, new_caption) -> bool — fix landed in the library
    parse_caption(caption) -> bits|None   — the app's own parser
    pace(channel_id)                      — apply pacing (v1 engine values)

Flood waits surface from `editor` as an exception with a `retry_after`
attribute and become job snoozes (SPEC §7).
"""

import time

from . import identify as identify_mod
from . import jobs as jobs_mod


class OrderError(Exception):
    pass


class OrderService:
    def __init__(self, store, identifier, editor, reader, verify,
                 parse_caption, pace=None, specials_policy="guard",
                 dry_run=True):
        self.store = store
        self.identifier = identifier
        self.editor = editor
        self.reader = reader
        self.verify = verify
        self.parse_caption = parse_caption
        self.pace = pace or (lambda channel_id: None)
        self.specials_policy = specials_policy
        self.dry_run = dry_run

    # -- opening ------------------------------------------------------------
    def open_order(self, scope_kind, now, series_id=None, channel_id=None,
                   max_files=500, budget_applies=500, auto_floor=0.95,
                   approval_floor=0.80, opened_by="bot", ttl_seconds=86400):
        if scope_kind == "series" and not series_id:
            raise OrderError("series scope requires series_id")
        if scope_kind == "channel_unidentified" and not channel_id:
            raise OrderError("channel scope requires channel_id")
        oid = self.store.create_order(
            scope_kind, now, series_id=series_id, channel_id=channel_id,
            max_files=max_files, budget_applies=budget_applies,
            auto_floor=auto_floor, approval_floor=approval_floor,
            opened_by=opened_by, expires_at=now + ttl_seconds)
        self.store.set_order_state(oid, "planning", now)
        return oid

    # -- planning -------------------------------------------------------------
    def _in_scope(self, order, file_row, series_id):
        if order["scope_kind"] == "series":
            return series_id == order["series_id"]
        return (file_row["source_channel_id"] == order["source_channel_id"]
                and file_row["series_id"] is None)

    def build_plan(self, order_id, items, now):
        """items: [{media_file_id, series_id, bits, new_caption,
        base_score, breakdown}] — the bot's proposals, validated and
        tiered by the server. Returns (plan_id, summary)."""
        order = self.store.get_order(order_id)
        if order is None:
            raise OrderError(f"no such order {order_id}")
        if order["state"] not in ("planning", "planned"):
            raise OrderError(f"order {order_id} is {order['state']}")
        if len(items) > order["max_files"]:
            raise OrderError("plan exceeds order max_files")

        plan_items = []
        for it in items:
            f = self.store.get_file(it["media_file_id"])
            refusal = None
            episode = None
            score = float(it.get("base_score") or 0.0)
            breakdown = dict(it.get("breakdown") or {})
            if f is None:
                refusal = "no such file"
            elif not self._in_scope(order, f, it.get("series_id")):
                refusal = "out_of_scope"
            else:
                episode, _method = self.identifier.slot_file(
                    it["series_id"], it.get("bits") or {})
                if episode is None:
                    refusal = "no_episode_slot"
                else:
                    score = self.identifier.finalise_score(
                        score, it["series_id"], it.get("bits") or {})
                    breakdown["final"] = score
                    if self._specials_blocked(it.get("bits") or {}, episode):
                        refusal = "specials_guard"
                    else:
                        parsed = self.parse_caption(it["new_caption"])
                        if not self._caption_matches(parsed, episode):
                            refusal = "caption_parse_mismatch"
            if refusal:
                tier = "refused"
            elif score >= order["auto_floor"]:
                tier = "auto"
            elif score >= order["approval_floor"]:
                tier = "approval"
            else:
                tier, refusal = "refused", "below_approval_floor"
            plan_items.append({
                "media_file_id": it["media_file_id"],
                "episode_id": episode["id"] if episode else None,
                "new_caption": it["new_caption"],
                "evidence_score": score,
                "score_breakdown": breakdown,
                "tier": tier,
                "refusal_reason": refusal,
                "_series_id": it.get("series_id"),
                "_bits": it.get("bits") or {},
                "_stem": it.get("stem"),
            })
        # The artefact hash covers only outcome-changing fields (store).
        plan_id = self.store.create_plan(order_id, plan_items, now)
        # Persist per-item series/bits alongside for the apply handler:
        # plan_item has no columns for them, so they ride in score_breakdown.
        with self.store.tx():
            for row, src in zip(self.store.list_plan_items(plan_id),
                                plan_items):
                import json as _json
                bd = _json.loads(row["score_breakdown"])
                bd["_series_id"] = src["_series_id"]
                bd["_bits"] = src["_bits"]
                bd["_stem"] = src.get("_stem")
                self.store.conn.execute(
                    "UPDATE plan_item SET score_breakdown=? WHERE id=?",
                    (_json.dumps(bd), row["id"]))
        self.store.set_plan_state(plan_id, "locked")
        self.store.set_order_state(order_id, "planned", now)
        summary = {"auto": 0, "approval": 0, "refused": 0}
        for pi in plan_items:
            summary[pi["tier"]] += 1
        return plan_id, summary

    def _specials_blocked(self, bits, episode):
        if self.specials_policy != "guard":
            return False
        # A special-marked file (bits.special, as the ledger carries it)
        # must land in season 0, never in a regular season (SPEC §6).
        return bool(bits.get("special") is not None
                    and episode["season_number"] != 0)

    @staticmethod
    def _caption_matches(parsed, episode):
        if not parsed:
            return False
        return (parsed.get("season") == episode["season_number"]
                and parsed.get("episode") == episode["episode_number"])

    # -- approval ---------------------------------------------------------------
    def approve_plan(self, order_id, plan_id, now):
        plan = self.store.get_plan(plan_id)
        if plan is None or plan["work_order_id"] != order_id:
            raise OrderError("plan does not belong to this order")
        self.store.set_plan_state(plan_id, "approved")

    # -- apply --------------------------------------------------------------------
    def apply_plan(self, order_id, now):
        order = self.store.get_order(order_id)
        if order is None or order["state"] != "planned":
            raise OrderError("order is not in planned state")
        if self.dry_run:
            raise OrderError("dry_run: applies are disabled")
        plan = self.store.latest_plan_for_order(order_id)
        items = self.store.list_plan_items(plan["id"])
        live = [i for i in items if i["tier"] != "refused"]
        if any(i["tier"] == "approval" for i in live) \
                and plan["state"] != "approved":
            raise OrderError("plan has approval-tier items and is not"
                             " approved")
        # Re-verify the artefact: what applies is what was approved.
        digest = self.store.plan_hash(
            [{"media_file_id": i["media_file_id"],
              "episode_id": i["episode_id"],
              "new_caption": i["new_caption"]} for i in items])
        if digest != plan["plan_hash"]:
            self.store.set_plan_state(plan["id"], "void")
            raise OrderError("plan hash mismatch — plan voided")
        for item in live:
            jobs_mod.enqueue(self.store, "apply_caption", {
                "plan_id": plan["id"], "plan_item_id": item["id"],
                "file_id": item["media_file_id"], "order_id": order_id,
            }, now)
        self.store.set_plan_state(plan["id"], "applied")
        self.store.set_order_state(order_id, "applying", now)
        return len(live)

    def make_apply_handler(self):
        def handler(payload, job):
            self._apply_one(payload, job)
        return handler

    def _apply_one(self, payload, job):
        import json as _json
        now = int(time.time())
        store = self.store
        item = store.conn.execute("SELECT * FROM plan_item WHERE id=?",
                                  (payload["plan_item_id"],)).fetchone()
        f = store.get_file(payload["file_id"])
        order = store.get_order(payload["order_id"])
        if order is None or order["state"] in ("killed", "expired"):
            raise jobs_mod.DeadError("order no longer live")
        if order["applies_used"] >= order["budget_applies"]:
            store.set_order_state(order["id"], "paused", now)
            raise jobs_mod.DeadError("order budget exhausted — paused")
        breakdown = _json.loads(item["score_breakdown"])
        series_id = breakdown.get("_series_id")
        before = f["current_caption"]
        # Idempotency: a previous attempt already landed this fix.
        existing = store.conn.execute(
            "SELECT id FROM caption_revision WHERE media_file_id=?"
            " AND work_order_id=? AND after_caption=? AND reverted_at"
            " IS NULL", (f["id"], order["id"], item["new_caption"])).fetchone()
        if existing:
            return
        # Live identity check — fails closed.
        live_caption = self.reader(f["chat_id"], f["msg_id"])
        if live_caption != before:
            with store.tx():
                store.conn.execute(
                    "UPDATE media_file SET match_status='conflicted',"
                    " updated_at=? WHERE id=?", (now, f["id"]))
            raise jobs_mod.DeadError("identity drift: live caption differs")
        self.pace(f["source_channel_id"])
        try:
            self.editor(f["chat_id"], f["msg_id"], item["new_caption"])
        except Exception as exc:
            retry_after = getattr(exc, "retry_after", None)
            if retry_after is not None:
                raise jobs_mod.SnoozeError(retry_after, "flood wait")
            raise
        if not self.verify(f, item["new_caption"]):
            # Roll back the caption we just wrote; nothing is recorded as
            # fixed, and the failure is a dead letter with the reason.
            try:
                self.editor(f["chat_id"], f["msg_id"], before)
            except Exception:
                pass
            raise jobs_mod.DeadError("verify failed — caption rolled back")
        slot_taken = store.conn.execute(
            "SELECT id FROM media_file WHERE episode_id=? AND is_primary=1"
            " AND id!=?", (item["episode_id"], f["id"])).fetchone()
        store.add_revision(f["id"], before, item["new_caption"], "bot",
                           order["id"], now)
        store.set_file_caption(f["id"], item["new_caption"], now)
        store.assign_file(f["id"], series_id, item["episode_id"],
                          0 if slot_taken else 1, "work_order",
                          item["evidence_score"], now)
        store.add_order_applies(order["id"], 1, now)
        if item["episode_id"]:
            store.set_order_cursor(order["id"], item["episode_id"], now)
        # Amendment 2: a successful fix teaches the stem->series alias, so
        # the bot's research becomes a permanent server-side shortcut.
        stem = breakdown.get("_stem")
        if stem and series_id:
            series = store.get_series(series_id)
            if series:
                store.add_alias(series_id, identify_mod.normalise(stem),
                                series["kind"])

    # -- progress (the bot's live view — SPEC §8 amendment 1) ----------------------
    def progress(self, order_id):
        order = self.store.get_order(order_id)
        if order is None:
            raise OrderError(f"no such order {order_id}")
        plan = self.store.latest_plan_for_order(order_id)
        planned = applied = failed = refused = 0
        if plan:
            items = self.store.list_plan_items(plan["id"])
            planned = sum(1 for i in items if i["tier"] != "refused")
            refused = sum(1 for i in items if i["tier"] == "refused")
            applied = self.store.conn.execute(
                "SELECT COUNT(*) AS n FROM caption_revision WHERE"
                " work_order_id=? AND reverted_at IS NULL",
                (order_id,)).fetchone()["n"]
            failed = self.store.conn.execute(
                "SELECT COUNT(*) AS n FROM job WHERE kind='apply_caption'"
                " AND state='dead' AND json_extract(payload,'$.order_id')=?",
                (order_id,)).fetchone()["n"]
        remaining = max(0, planned - applied - failed)
        pct = round(100.0 * applied / planned, 1) if planned else 0.0
        return {"order_id": order_id, "state": order["state"],
                "planned": planned, "applied": applied, "verified": applied,
                "failed": failed, "refused": refused, "remaining": remaining,
                "percent": pct, "budget_used": order["applies_used"],
                "budget_total": order["budget_applies"],
                "cursor_episode_id": order["cursor_episode_id"]}

    def maybe_finish_order(self, order_id, now):
        p = self.progress(order_id)
        order = self.store.get_order(order_id)
        if order["state"] == "applying" and p["remaining"] == 0:
            self.store.set_order_state(order_id, "done", now)
            return True
        return False

    # -- undo ------------------------------------------------------------------------
    def undo_file(self, file_id, now):
        rev = self.store.latest_revision_for_file(file_id)
        if rev is None:
            return False
        f = self.store.get_file(file_id)
        self.pace(f["source_channel_id"])
        self.editor(f["chat_id"], f["msg_id"], rev["before_caption"])
        self.store.set_file_caption(file_id, rev["before_caption"], now)
        self.store.unassign_file(file_id, now)
        self.store.mark_revision_reverted(rev["id"], now)
        return True

    def undo_order(self, order_id, now):
        revs = self.store.conn.execute(
            "SELECT media_file_id FROM caption_revision WHERE"
            " work_order_id=? AND reverted_at IS NULL ORDER BY id DESC",
            (order_id,)).fetchall()
        done = 0
        for row in revs:
            if self.undo_file(row["media_file_id"], now):
                done += 1
        return done
