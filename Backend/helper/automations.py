"""Automations engine (Librarian L3).

Scheduled jobs for the Librarian, in the Sonarr/Jellyfin tasks
pattern: every job has an enable toggle, a schedule preset, Run now,
and a visible history — all managed from the Librarian page.

Design notes:
- No external scheduler dependency: a 30-second ticker in the app
  process checks due jobs. Schedules are interval presets, so a job
  that was due while the app was down simply runs once when it
  returns (missed runs coalesce — the right behaviour on a phone).
- Job definitions live in code (JOB_DEFS); Yan's choices (enabled,
  schedule, last-run state) live in `tracking.automations`; every
  run is appended to `tracking.automation_runs`.
- Librarian work runs through librarian.start_action, so a job can
  never overlap a manual scan/sweep — if the librarian is busy, the
  run is recorded as skipped.
"""

from __future__ import annotations

import asyncio
import time

from Backend.logger import LOGGER

SCHEDULES: dict[str, int] = {
    "off": 0,
    "hourly": 3600,
    "6h": 6 * 3600,
    "daily": 24 * 3600,
    "weekly": 7 * 24 * 3600,
}

SCHEDULE_LABELS: list[tuple[str, str]] = [
    ("off", "No schedule"),
    ("hourly", "Every hour"),
    ("6h", "Every 6 hours"),
    ("daily", "Daily"),
    ("weekly", "Weekly"),
]

JOB_DEFS: list[dict] = [
    {"id": "harvest", "kind": "action", "action": "sweep",
     "title": "Harvest the skip pile",
     "description": "Re-check the files sitting in the skip channel "
                    "and record any the ledger missed.",
     "default_enabled": False, "default_schedule": "off"},
    {"id": "audit", "kind": "action", "action": "audit",
     "title": "Audit indexed files",
     "description": "Re-verify a sample of already-indexed files "
                    "against the providers and flag suspects.",
     "default_enabled": False, "default_schedule": "off"},
    {"id": "stats", "kind": "stats",
     "title": "Refresh page numbers",
     "description": "Recompute the Librarian page counts so the "
                    "page always opens instantly.",
     "default_enabled": True, "default_schedule": "hourly"},
    {"id": "preidentify", "kind": "preidentify",
     "title": "Pre-identify waiting files",
     "description": "The app's own first pass: attaches candidate "
                    "identities (taught aliases first, then provider "
                    "search) so the bot starts from evidence instead "
                    "of a blank page. Candidates never edit anything "
                    "by themselves.",
     "default_enabled": False, "default_schedule": "off"},
    {"id": "scan_all", "kind": "scan_all",
     "title": "Scan all channels (new posts only)",
     "description": "Incremental scan of every Librarian channel — "
                    "walks only posts newer than each channel's last "
                    "scan, so repeat scans stay fast.",
     "default_enabled": False, "default_schedule": "off"},
]

_RUNNING: set[str] = set()
_TICKER_STARTED = False


def job_def(job_id: str) -> dict | None:
    for d in JOB_DEFS:
        if d["id"] == job_id:
            return d
    return None


# ---------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------
def _tracking():
    try:
        from Backend import db
        return (getattr(db, "dbs", None) or {}).get("tracking")
    except Exception:
        return None


def _cfg_coll():
    tdb = _tracking()
    return tdb["automations"] if tdb is not None else None


def _runs_coll():
    tdb = _tracking()
    return tdb["automation_runs"] if tdb is not None else None


async def get_config(job_id: str) -> dict:
    """Yan's choices for one job, merged over the definition's
    defaults: {enabled, schedule, anchor, last_run, last_status,
    last_summary}."""
    d = job_def(job_id) or {}
    cfg = {"enabled": bool(d.get("default_enabled", False)),
           "schedule": d.get("default_schedule", "off"),
           "anchor": 0.0, "last_run": 0.0, "last_status": "",
           "last_summary": ""}
    coll = _cfg_coll()
    if coll is not None:
        try:
            doc = await coll.find_one({"_id": job_id})
        except Exception:
            doc = None
        if doc:
            for key in cfg:
                if key in doc:
                    cfg[key] = doc[key]
    if cfg["schedule"] not in SCHEDULES:
        cfg["schedule"] = "off"
    return cfg


async def set_config(job_id: str, enabled=None, schedule=None) -> dict:
    """Persist a toggle/schedule change. Changing the schedule
    re-anchors it: the next run is one interval from now."""
    if job_def(job_id) is None:
        return {"ok": False, "message": "Unknown job."}
    coll = _cfg_coll()
    if coll is None:
        return {"ok": False, "message": "Tracking DB unavailable."}
    fields: dict = {"updated_at": time.time()}
    if enabled is not None:
        fields["enabled"] = bool(enabled)
    if schedule is not None:
        if schedule not in SCHEDULES:
            return {"ok": False, "message": "Unknown schedule."}
        fields["schedule"] = schedule
        fields["anchor"] = time.time()
    await coll.update_one({"_id": job_id}, {"$set": fields}, upsert=True)
    return {"ok": True, "config": await get_config(job_id)}


def next_run_ts(cfg: dict, now: float | None = None) -> float | None:
    """When this job next runs (epoch), or None when it never will.
    A timestamp in the past means the job is due — missed runs
    coalesce into one."""
    if not cfg.get("enabled"):
        return None
    interval = SCHEDULES.get(cfg.get("schedule") or "off", 0)
    if interval <= 0:
        return None
    base = cfg.get("last_run") or cfg.get("anchor") or 0.0
    now = time.time() if now is None else now
    if not base:
        return now + interval
    return base + interval


async def _record_run(job_id: str, trigger: str, status: str,
                      summary: str, started: float) -> None:
    coll = _runs_coll()
    if coll is not None:
        try:
            await coll.insert_one({
                "job_id": job_id, "trigger": trigger, "status": status,
                "summary": summary, "started_at": started,
                "finished_at": time.time()})
        except Exception:
            pass
    cfg_coll = _cfg_coll()
    if cfg_coll is not None:
        try:
            await cfg_coll.update_one({"_id": job_id}, {"$set": {
                "last_run": started, "last_status": status,
                "last_summary": summary}}, upsert=True)
        except Exception:
            pass


async def list_runs(job_id: str, limit: int = 8) -> list[dict]:
    coll = _runs_coll()
    if coll is None:
        return []
    out = []
    try:
        cursor = coll.find({"job_id": job_id}).sort(
            "started_at", -1).limit(max(1, min(50, limit)))
        async for doc in cursor:
            doc = dict(doc)
            doc.pop("_id", None)
            out.append(doc)
    except Exception:
        return []
    return out


async def jobs_view() -> list[dict]:
    """Everything the Automations section renders: definition +
    config + next run + recent history, per job."""
    out = []
    for d in JOB_DEFS:
        cfg = await get_config(d["id"])
        out.append({
            "id": d["id"], "title": d["title"],
            "description": d["description"],
            "enabled": cfg["enabled"], "schedule": cfg["schedule"],
            "next_run": next_run_ts(cfg),
            "last_run": cfg["last_run"], "last_status": cfg["last_status"],
            "last_summary": cfg["last_summary"],
            "running": d["id"] in _RUNNING,
            "runs": await list_runs(d["id"], limit=6),
        })
    return out


# ---------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------
async def _execute(d: dict) -> tuple[str, str]:
    """Run one job's work; returns (status, summary). Never raises."""
    from Backend.helper import librarian
    try:
        from Backend.helper.settings_manager import SettingsManager
        if not SettingsManager.current().librarian_enabled:
            return "skipped", "The Librarian is off in Settings."
    except Exception:
        pass
    if d["kind"] == "stats":
        librarian.invalidate_counts()
        counts = await librarian.panel_counts()
        return "ok", (f"{counts.get('waiting', 0)} waiting, "
                      f"{counts.get('fixed', 0)} fixed")
    if d["kind"] == "preidentify":
        res = await librarian.run_preidentify()
        if not res.get("ok"):
            return "error", str(res.get("message") or "Sweep failed.")[:300]
        return "ok", (f"{res.get('checked', 0)} checked, "
                      f"{res.get('attached', 0)} candidates attached")
    if d["kind"] == "scan_all":
        from Backend.helper.settings_manager import SettingsManager
        settings = SettingsManager.current()
        channels = settings.librarian_channels or settings.auth_channels
        if librarian.ACTION_STATE.get("running"):
            return "skipped", "The Librarian was busy with another job."
        done = 0
        total_files = 0
        for channel in channels:
            res = await librarian.run_channel_scan(channel)
            if res.get("ok"):
                done += 1
                total_files += int(res.get("files") or 0)
        return "ok", (f"{done}/{len(channels)} channels scanned, "
                      f"{total_files} files recorded")
    # Librarian actions share one single-flight runner with the page.
    if librarian.ACTION_STATE.get("running"):
        return "skipped", "The Librarian was busy with another job."
    res = librarian.start_action(d["action"])
    if not res.get("ok", True) and res.get("status") == "error":
        return "skipped", str(res.get("message") or "Could not start.")
    # Wait for the background action to finish (it updates
    # ACTION_STATE); generous cap so a stuck walk can't hang a run.
    for _ in range(900):
        await asyncio.sleep(2)
        if not librarian.ACTION_STATE.get("running"):
            break
    state = librarian.ACTION_STATE
    if state.get("error"):
        return "error", str(state["error"])[:300]
    result = state.get("result") or {}
    if isinstance(result, dict):
        summary = ", ".join(f"{k}: {v}" for k, v in list(result.items())[:4]
                            if k != "ok")
        return "ok", summary or "Done."
    return "ok", "Done."


async def run_job(job_id: str, trigger: str = "manual") -> dict:
    d = job_def(job_id)
    if d is None:
        return {"ok": False, "message": "Unknown job."}
    if job_id in _RUNNING:
        return {"ok": False, "message": "That job is already running."}
    _RUNNING.add(job_id)
    started = time.time()
    try:
        status, summary = await _execute(d)
    except Exception as e:  # a job must never take the ticker down
        status, summary = "error", str(e)[:300]
    finally:
        _RUNNING.discard(job_id)
    await _record_run(job_id, trigger, status, summary, started)
    return {"ok": status == "ok", "status": status, "summary": summary}


def run_now(job_id: str) -> dict:
    """Fire-and-forget Run now (the page must not hang on long jobs)."""
    if job_def(job_id) is None:
        return {"ok": False, "message": "Unknown job."}
    if job_id in _RUNNING:
        return {"ok": False, "message": "That job is already running."}
    asyncio.get_event_loop().create_task(run_job(job_id, "manual"))
    return {"ok": True, "message": "Job started — check its history shortly."}


# ---------------------------------------------------------------------
# Ticker
# ---------------------------------------------------------------------
async def _ticker() -> None:
    while True:
        try:
            # Librarian v2 job driving (SPEC seam): a no-op unless v2 is
            # enabled; never allowed to take the ticker down.
            try:
                from Backend.librarian_v2 import api as _v2api
                if _v2api.enabled():
                    _v2api.get_runtime().tick()
            except Exception:
                pass
            for d in JOB_DEFS:
                cfg = await get_config(d["id"])
                nxt = next_run_ts(cfg)
                if nxt is not None and nxt <= time.time() \
                        and d["id"] not in _RUNNING:
                    asyncio.get_event_loop().create_task(
                        run_job(d["id"], "schedule"))
        except Exception as e:
            LOGGER.warning(f"[Automations] tick failed: {e}")
        await asyncio.sleep(30)


def start() -> None:
    """Start the ticker once (called from the app's startup hook)."""
    global _TICKER_STARTED
    if _TICKER_STARTED:
        return
    _TICKER_STARTED = True
    asyncio.get_event_loop().create_task(_ticker())
    LOGGER.info("[Automations] scheduler started "
                f"({len(JOB_DEFS)} jobs registered)")
