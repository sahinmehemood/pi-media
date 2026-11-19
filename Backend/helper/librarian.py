"""Hermes Librarian — Phase 1: reject ledger, run clustering, the
deterministic resolution funnel (L0-L2) and the server-side verification
gate.

Phase 1 makes NO edits and applies NOTHING: it records every file the
metadata pipeline rejects (the skip pile, live failures, scan failures,
suspect matches), clusters them into runs, and produces gate-verified
proposals with approval tiers for a human to review. The apply engine,
approval digests and the Media Manager brain (L3) are later phases.

Design: docs/librarian/DESIGN.md (v3). Hard rules honoured here:
- the Hermes/Media Manager bot is never involved in streaming;
- the alias map is only READ (it is taught solely by human-confirmed
  decisions, which happen in later phases);
- personal titles and manual channels are never proposed for changes.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Optional

from Backend.logger import LOGGER

# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in tests/test_librarian.py)
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)")
_EP_SE_RE = re.compile(r"\bS(\d{1,3})E(\d{1,4})(?:\s*[-~]\s*E?(\d{1,4}))?\b", re.I)
_EP_SP_RE = re.compile(r"\bSP(\d{1,3})\s*[-_. ]*E(\d{1,4})\b", re.I)
_EP_E_RE = re.compile(r"\b(?:EP|E)(\d{1,4})\b", re.I)
_ABS_RE = re.compile(r"^(.+?)[\s._-]+(\d{3,4})$")
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_NOISE_TOKENS = {
    "esub", "e-sub", "sub", "subbed", "dub", "dubbed", "eng", "english",
    "hindi", "multi", "web", "webdl", "web-dl", "bluray", "hdrip", "dvdrip",
    "x264", "x265", "hevc", "h264", "h265", "aac", "ddp5", "atmos", "hdr",
    "4k", "uhd", "fhd", "hd", "sd", "480p", "720p", "1080p", "1440p",
    "2160p", "540p", "360p", "240p", "mkv", "mp4", "avi", "srt",
}


def _clean_stem_text(text: str) -> str:
    text = _TAG_RE.sub(" ", text or "")
    text = text.replace("_", " ").replace(".", " ")
    parts = []
    for tok in re.split(r"[\s\-]+", text):
        t = tok.strip().lower()
        if not t or t in _NOISE_TOKENS:
            continue
        parts.append(t)
    return " ".join(parts)


def name_bits(caption: str = "", file_name: str = "") -> dict:
    """Extract (stem, season, episode, special, year) from a caption or
    file name. The stem is the clustering key: the text before the
    episode token, cleaned of quality/codec/group noise."""
    raw = (caption or "").strip().split("\n")[0].strip() or (file_name or "")
    name = re.sub(r"\.(mkv|mp4|avi|mov|webm|ts)$", "", raw, flags=re.I)
    name = re.sub(r"\.part\d+$", "", name, flags=re.I)

    year = None
    ym = _YEAR_RE.search(name)
    if ym:
        year = int(ym.group(1))

    season = episode = special = None
    cut = None
    m = _EP_SP_RE.search(name)
    if m:
        special, episode, cut = int(m.group(1)), int(m.group(2)), m.start()
    else:
        m = _EP_SE_RE.search(name)
        if m:
            season, episode, cut = int(m.group(1)), int(m.group(2)), m.start()
        else:
            m = _EP_E_RE.search(name)
            if m:
                episode, cut = int(m.group(1)), m.start()
            else:
                m = _ABS_RE.match(name.strip())
                if m:
                    episode, cut = int(m.group(2)), m.start(2)

    head = name[:cut] if cut is not None else name
    stem = _clean_stem_text(head)
    if special is not None:
        # SP<n> arc markers belong to the stem: SP1 and SP3 files of the
        # same show are different runs with overlapping episode numbers.
        stem = f"{stem} sp{special}".strip()
    if year and str(year) in stem.split():
        stem = " ".join(t for t in stem.split() if t != str(year))
    return {
        "stem": stem,
        "season": season,
        "episode": episode,
        "special": special,
        "year": year,
        "raw": raw,
    }


def reason_code_for(reason_text: str) -> str:
    text = (reason_text or "").lower()
    if "multi-part" in text or "split" in text:
        return "multipart"
    if "combined" in text and "season" in text:
        return "combined-pack"
    if "multiple seasons" in text:
        return "multi-season"
    if "no video quality" in text:
        return "no-quality"
    if "no title" in text:
        return "no-title"
    if "could not be parsed" in text:
        return "parse-failure"
    if "could not match" in text:
        return "no-match"
    if "more than one title" in text:
        return "suspect-multi-index"
    if "slot" in text:
        return "suspect-slot-collision"
    return "unknown"


def initials_of(title: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", title or "")
    return "".join(w[0] for w in words if w).lower()


def stem_title_match(stem: str, title: str) -> bool:
    """True when an abbreviation stem plausibly names `title`: either the
    stem's first token equals the title's word initials (BTTH = Battle
    Through The Heavens), or the whole stem equals the cleaned title."""
    if not stem or not title:
        return False
    first = stem.split()[0]
    if len(first) >= 2 and initials_of(title) == first:
        return True
    return _clean_stem_text(title) == stem


def cluster_items(items: list[dict]) -> list[dict]:
    """Group ledger items into runs: episodic items cluster by
    (chat_id, stem); movies (no episode and no season) stay single."""
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for it in items:
        bits = it.get("bits") or name_bits(it.get("caption", ""), it.get("file_name", ""))
        chat = it.get("chat_id")
        if bits.get("episode") is None and bits.get("season") is None:
            key = ("single", it.get("key"))
        else:
            key = ("run", chat, bits.get("stem") or "")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(it)

    runs = []
    for key in order:
        members = groups[key]
        bits0 = members[0].get("bits") or name_bits(
            members[0].get("caption", ""), members[0].get("file_name", ""))
        episodes = sorted({(m.get("bits") or {}).get("episode") for m in members
                           if (m.get("bits") or {}).get("episode") is not None})
        missing = []
        if len(episodes) > 1:
            missing = [e for e in range(episodes[0], episodes[-1] + 1) if e not in episodes]
        chat = members[0].get("chat_id")
        stem = bits0.get("stem") or ""
        runs.append({
            "run_id": f"{chat if chat is not None else 'unknown'}|{stem or members[0].get('key')}",
            "chat_id": chat,
            "stem": stem,
            "season": bits0.get("season"),
            "special": bits0.get("special"),
            "year": bits0.get("year"),
            "episodes": episodes,
            "missing_episodes": missing,
            "item_keys": [m.get("key") for m in members],
            "items": members,
        })
    return runs


def assign_tier(layer: Optional[str], gate: Optional[dict]) -> str:
    """Tier A: gate-verified with no unresolved checks, from a stamped
    ID (L0) or a taught alias (L1). Tier B: gate-verified but from
    deterministic search/coherence (L2) or with skipped checks.
    Tier C: unverifiable or no proposal — never auto-touched."""
    if not layer or not gate or not gate.get("passed"):
        return "C"
    skipped = [c for c in gate.get("checks", []) if c.get("ok") is None]
    if layer in ("L0", "L1") and not skipped:
        return "A"
    return "B"


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ---------------------------------------------------------------------------
# Ledger store (tracking DB)
# ---------------------------------------------------------------------------

def _tracking():
    try:
        from Backend import db
        return (getattr(db, "dbs", None) or {}).get("tracking")
    except Exception:
        return None


def _ledger_coll():
    tracking = _tracking()
    return tracking["reject_ledger"] if tracking is not None else None


def _runs_coll():
    tracking = _tracking()
    return tracking["curation_runs"] if tracking is not None else None


def _alias_coll():
    tracking = _tracking()
    return tracking["alias_map"] if tracking is not None else None


async def record_reject(fields: dict) -> bool:
    """Upsert one ledger entry keyed by `key` (file_unique_id, or a
    tg:<chat>:<msg> key for suspects without one). Repeat sightings of
    the same file update the entry instead of duplicating it — the
    ledger never re-inflates the way the skip pile did."""
    coll = _ledger_coll()
    if coll is None or not fields.get("key"):
        LOGGER.warning("[Librarian] ledger unavailable or key missing; record skipped")
        return False
    now = time.time()
    mutable = {
        "caption": fields.get("caption", ""),
        "file_name": fields.get("file_name", ""),
        "size": fields.get("size", 0),
        "height": fields.get("height"),
        "reason": fields.get("reason", ""),
        "reason_code": fields.get("reason_code", "unknown"),
        "updated_at": now,
    }
    if fields.get("status"):
        mutable["status"] = fields["status"]
    if fields.get("current_match") is not None:
        mutable["current_match"] = fields["current_match"]
    on_insert = {
        "key": fields["key"],
        "file_unique_id": fields.get("file_unique_id"),
        "chat_id": fields.get("chat_id"),
        "msg_id": fields.get("msg_id"),
        "origin": fields.get("origin", "live-fail"),
        "origin_hint": fields.get("origin_hint", ""),
        "status": fields.get("status") or "open",
        "parse": _jsonable(fields.get("parse")),
        "bits": fields.get("bits"),
        "first_seen": now,
    }
    update = {"$set": mutable, "$setOnInsert": on_insert,
              "$push": {"history": {"ts": now, "event": fields.get("origin", "seen")}}}
    if fields.get("skip_msg_id"):
        update["$addToSet"] = {"skip_msg_ids": fields["skip_msg_id"]}
    try:
        await coll.update_one({"_id": fields["key"]}, update, upsert=True)
        return True
    except Exception as e:
        LOGGER.warning(f"[Librarian] ledger write failed: {e}")
        return False


def _media_fields(message) -> dict:
    media = None
    kind = ""
    for attr, k in (("video", "video"), ("document", "document"),
                    ("audio", "audio"), ("animation", "animation"),
                    ("photo", "photo")):
        media = getattr(message, attr, None)
        if media:
            kind = k
            break
    if not media:
        return {}
    if isinstance(media, list):
        media = media[-1]
    return {
        "file_unique_id": getattr(media, "file_unique_id", None),
        "file_name": getattr(media, "file_name", "") or "",
        "size": getattr(media, "file_size", 0) or 0,
        "height": getattr(media, "height", None) if kind == "video" else None,
        "kind": kind,
    }


def _failure_analysis(caption: str, file_name: str) -> tuple[str, Optional[dict]]:
    target = (caption or "").strip().split("\n")[0] or (file_name or "")
    try:
        from Backend.helper.metadata.parse import analyze_metadata_failure, parse_media_name
        reason = analyze_metadata_failure(target)
        try:
            parsed = _jsonable(parse_media_name(target))
        except Exception:
            parsed = None
        return reason, parsed
    except Exception:
        return "", None


async def record_from_message(message, origin: str) -> None:
    """Ledger hook for the skip-channel router. Best-effort by design:
    a ledger failure must never break ingest, so nothing raises."""
    try:
        from Backend.helper.settings_manager import SettingsManager
        if not SettingsManager.current().librarian_enabled:
            return
        fields = _media_fields(message)
        if not fields.get("file_unique_id"):
            return
        caption = getattr(message, "caption", "") or ""
        reason, parsed = _failure_analysis(caption, fields["file_name"])
        await record_reject({
            "key": fields["file_unique_id"],
            "file_unique_id": fields["file_unique_id"],
            "chat_id": getattr(getattr(message, "chat", None), "id", None),
            "msg_id": getattr(message, "id", None),
            "origin": origin,
            "caption": caption,
            "file_name": fields["file_name"],
            "size": fields["size"],
            "height": fields["height"],
            "reason": reason,
            "reason_code": reason_code_for(reason),
            "parse": parsed,
            "bits": name_bits(caption, fields["file_name"]),
        })
    except Exception as e:
        LOGGER.warning(f"[Librarian] record_from_message failed: {e}")


async def ledger_counts() -> dict:
    coll = _ledger_coll()
    if coll is None:
        return {"available": False}
    out = {"available": True, "total": 0, "by_status": {}, "by_reason": {}, "by_origin": {}}
    try:
        out["total"] = await coll.count_documents({})
        for field, bucket in (("status", "by_status"), ("reason_code", "by_reason"),
                              ("origin", "by_origin")):
            async for row in coll.aggregate([
                    {"$group": {"_id": f"${field}", "n": {"$sum": 1}}},
                    {"$sort": {"n": -1}}]):
                out[bucket][row.get("_id") or "unknown"] = row.get("n", 0)
    except Exception as e:
        LOGGER.warning(f"[Librarian] counts failed: {e}")
    runs = _runs_coll()
    if runs is not None:
        try:
            out["runs"] = await runs.count_documents({})
            out["runs_by_tier"] = {}
            async for row in runs.aggregate([
                    {"$group": {"_id": "$tier", "n": {"$sum": 1}}}]):
                out["runs_by_tier"][row.get("_id") or "?"] = row.get("n", 0)
        except Exception:
            pass
    return out


async def list_ledger(status: str = "", reason: str = "", limit: int = 100) -> list[dict]:
    coll = _ledger_coll()
    if coll is None:
        return []
    query = {}
    if status:
        query["status"] = status
    if reason:
        query["reason_code"] = reason
    out = []
    try:
        cursor = coll.find(query).sort("updated_at", -1).limit(max(1, min(500, limit)))
        async for doc in cursor:
            doc = dict(doc)
            doc.pop("history", None)
            doc.pop("parse", None)
            out.append(_jsonable(doc))
    except Exception as e:
        LOGGER.warning(f"[Librarian] list failed: {e}")
    return out


async def list_runs(limit: int = 100) -> list[dict]:
    coll = _runs_coll()
    if coll is None:
        return []
    out = []
    try:
        cursor = coll.find({}).sort("updated_at", -1).limit(max(1, min(200, limit)))
        async for doc in cursor:
            doc = dict(doc)
            doc.pop("items", None)
            out.append(_jsonable(doc))
    except Exception as e:
        LOGGER.warning(f"[Librarian] runs list failed: {e}")
    return out


async def lookup_alias(chat_id, stem: str) -> Optional[dict]:
    coll = _alias_coll()
    if coll is None or chat_id is None or not stem:
        return None
    try:
        return await coll.find_one({"_id": f"{chat_id}:{stem}"})
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Resolution funnel L0-L2 + verification gate
# ---------------------------------------------------------------------------

def _year_of(value) -> Optional[int]:
    if value is None:
        return None
    m = re.search(r"\d{4}", str(value))
    return int(m.group(0)) if m else None


def _proposal_from_candidate(cand: dict, layer: str) -> dict:
    return {
        "layer": layer,
        "provider": cand.get("source"),
        "media_type": cand.get("media_type"),
        "title": cand.get("title") or "",
        "year": _year_of(cand.get("year")),
        "tmdb_id": cand.get("tmdb_id"),
        "imdb_id": cand.get("imdb_id"),
        "tvdb_id": cand.get("tvdb_id"),
        "kitsu_id": cand.get("kitsu_id"),
    }


async def _l0(run: dict) -> Optional[dict]:
    """L0: a caption/filename already carrying an ID ref resolves by ID."""
    from Backend.helper.metadata.common import extract_default_id
    from Backend.helper.metadata.entry import search_any_candidates
    for item in run["items"]:
        for text in (item.get("caption", ""), item.get("file_name", "")):
            if text and extract_default_id(text):
                try:
                    cands = await search_any_candidates(text)
                except Exception:
                    cands = []
                for cand in cands:
                    if cand.get("title"):
                        return _proposal_from_candidate(cand, "L0")
    return None


async def _l2_search(run: dict) -> Optional[dict]:
    """L2a: deterministic provider search on the parsed title guess —
    only when the stem reads as real words, not an abbreviation."""
    from Backend.helper.metadata.common import score_candidate
    from Backend.helper.metadata.entry import search_any_candidates
    stem = run.get("stem") or ""
    words = stem.split()
    if not words or (len(words) == 1 and len(words[0]) <= 6 and words[0].isalpha()):
        return None  # abbreviation stem — search cannot help; coherence/L3 territory
    try:
        cands = await search_any_candidates(stem, run.get("year"))
    except Exception:
        return None
    best, best_score = None, 0.0
    for cand in cands:
        if not cand.get("title"):
            continue
        score = score_candidate(stem, run.get("year"), cand["title"],
                                _year_of(cand.get("year")) or 0)
        if score > best_score:
            best, best_score = cand, score
    if best is not None and best_score >= 0.6:
        proposal = _proposal_from_candidate(best, "L2")
        proposal["score"] = round(best_score, 3)
        return proposal
    return None


def _norm_chat(value) -> Optional[int]:
    try:
        raw = str(int(value))
    except (TypeError, ValueError):
        return None
    return int(raw.replace("-100", "", 1)) if raw.startswith("-100") else int(raw)


async def _channel_title_index() -> dict:
    """{normalized chat id: {normalized title: title record}} built from
    the library itself — which titles each channel already carries."""
    from Backend import db
    from Backend.helper.encrypt import decode_string
    index: dict[int, dict[str, dict]] = {}
    dbs = getattr(db, "dbs", None) or {}
    for name, handle in dbs.items():
        if name == "tracking" or handle is None:
            continue
        for coll_name, mtype in (("movie", "movie"), ("tv", "tv")):
            try:
                cursor = handle[coll_name].find(
                    {}, {"title": 1, "tmdb_id": 1, "imdb_id": 1, "tvdb_id": 1,
                         "kitsu_id": 1, "release_year": 1, "year": 1,
                         "telegram": 1, "seasons": 1})
                async for doc in cursor:
                    title = doc.get("title") or ""
                    if not title:
                        continue
                    entries = list(doc.get("telegram") or [])
                    for season in doc.get("seasons") or []:
                        for ep in season.get("episodes") or []:
                            entries.extend(ep.get("telegram") or [])
                    record = {
                        "title": title, "media_type": mtype,
                        "tmdb_id": doc.get("tmdb_id"), "imdb_id": doc.get("imdb_id"),
                        "tvdb_id": doc.get("tvdb_id"), "kitsu_id": doc.get("kitsu_id"),
                        "year": _year_of(doc.get("release_year") or doc.get("year")),
                    }
                    for entry in entries:
                        enc = entry.get("id")
                        if not enc:
                            continue
                        try:
                            decoded = await decode_string(enc)
                        except Exception:
                            continue
                        chat = _norm_chat((decoded or {}).get("chat_id"))
                        if chat is None:
                            continue
                        index.setdefault(chat, {})[_clean_stem_text(title)] = record
            except Exception as e:
                LOGGER.warning(f"[Librarian] title index scan failed ({name}.{coll_name}): {e}")
    return index


async def _l2_coherence(run: dict, title_index: dict) -> Optional[dict]:
    """L2b: channel coherence — the library already carries a title
    whose initials spell the abbreviation stem (the BTTH problem).
    Scoped to the run's own channel when its origin is known; backlog
    items with unknown origin search every channel but still require
    the matching TITLE to be unique. Ambiguity escalates."""
    chat = _norm_chat(run.get("chat_id"))
    if chat is not None:
        pools = [title_index.get(chat) or {}]
    else:
        pools = list(title_index.values())
    by_title: dict[str, dict] = {}
    for titles in pools:
        for rec in titles.values():
            if stem_title_match(run.get("stem") or "", rec["title"]):
                by_title[rec["title"]] = rec
    if len(by_title) != 1:
        return None
    proposal = dict(next(iter(by_title.values())))
    proposal["layer"] = "L2"
    proposal["provider"] = "library"
    return proposal


async def verify_proposal(proposal: dict, run: dict) -> dict:
    """The server-side verification gate. Checks that cannot be run are
    recorded as skipped (ok=None) — never silently passed."""
    from Backend.helper.metadata.providers import tmdb as _tmdb
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": ok, "detail": detail})

    tmdb_id = proposal.get("tmdb_id")
    try:
        tmdb_id = int(tmdb_id) if tmdb_id is not None else None
        if tmdb_id is not None and tmdb_id < 0:
            tmdb_id = None  # personal title — never proposed, belt & braces
    except (TypeError, ValueError):
        tmdb_id = None
    media_type = proposal.get("media_type") or "movie"

    # 1. The claimed ID exists.
    if tmdb_id:
        try:
            detail = await _tmdb.details(media_type, tmdb_id)
            add("id_exists", bool(detail), f"TMDB {media_type} {tmdb_id}")
        except Exception as e:
            add("id_exists", None, f"TMDB lookup failed: {e}")
    elif proposal.get("imdb_id") or proposal.get("tvdb_id") or proposal.get("kitsu_id"):
        add("id_exists", True, "provider-backed candidate (non-TMDB id)")
    else:
        add("id_exists", False, "proposal carries no provider id")

    # 2. Numbering sanity + episode existence (sampled: first/mid/last).
    episodes = [e for e in (run.get("episodes") or []) if e is not None]
    season = run.get("season")
    if media_type == "tv" and episodes:
        bad = [e for e in episodes if e < 1 or e > 2000]
        add("numbering_sanity", not bad,
            "episode numbers in range" if not bad else f"implausible episodes: {bad[:3]}")
        if run.get("special") is not None:
            add("episode_exists", None, "special (SP) numbering — mapping is an L3 decision")
        elif season is None:
            add("episode_exists", None, "absolute numbering — mapping not checked in Phase 1")
        elif tmdb_id:
            sample = sorted({episodes[0], episodes[len(episodes) // 2], episodes[-1]})
            failed = []
            for ep in sample:
                try:
                    got = await _tmdb.episode_details(tmdb_id, season, ep)
                    if not got:
                        failed.append(ep)
                except Exception:
                    failed.append(ep)
            add("episode_exists", not failed,
                f"S{season} sampled episodes {sample} exist" if not failed
                else f"S{season} episodes missing on TMDB: {failed}")
        else:
            add("episode_exists", None, "no TMDB id — episode existence unchecked")
    elif media_type == "tv":
        add("numbering_sanity", None, "no episode numbers in run")

    # 3. Cross-ID consistency where both ids exist.
    imdb_id = proposal.get("imdb_id")
    if tmdb_id and imdb_id:
        try:
            ext = await _tmdb.external_imdb_id(media_type, tmdb_id)
            if ext:
                add("cross_id", ext == imdb_id,
                    f"TMDB maps to {ext}" if ext != imdb_id else "TMDB/IMDb agree")
            else:
                add("cross_id", None, "TMDB has no IMDb mapping for this title")
        except Exception as e:
            add("cross_id", None, f"cross-id lookup failed: {e}")

    passed = all(c["ok"] is not False for c in checks) and any(
        c["ok"] for c in checks)
    return {"passed": bool(passed), "checks": checks}


async def resolve_run(run: dict, title_index: dict) -> dict:
    """Run one run through L0 -> L1 -> L2 and the gate. Returns the run
    outcome (also persisted by run_funnel)."""
    proposal = await _l0(run)
    if proposal is None:
        alias = await lookup_alias(run.get("chat_id"), run.get("stem") or "")
        if alias and (alias.get("tmdb_id") or alias.get("imdb_id")):
            proposal = {
                "layer": "L1", "provider": "alias",
                "media_type": alias.get("media_type"),
                "title": alias.get("title") or "",
                "year": alias.get("year"),
                "tmdb_id": alias.get("tmdb_id"), "imdb_id": alias.get("imdb_id"),
                "tvdb_id": alias.get("tvdb_id"), "kitsu_id": alias.get("kitsu_id"),
            }
    if proposal is None:
        proposal = await _l2_search(run)
    if proposal is None:
        proposal = await _l2_coherence(run, title_index)
    if proposal is None:
        return {"proposal": None, "gate": None, "tier": "C"}
    gate = await verify_proposal(proposal, run)
    return {"proposal": proposal, "gate": gate,
            "tier": assign_tier(proposal.get("layer"), gate)}


# ---------------------------------------------------------------------------
# Actions: backlog sweep, suspect audit, funnel — background-task state
# ---------------------------------------------------------------------------

ACTION_STATE = {
    "running": False, "kind": "", "started_at": None,
    "finished_at": None, "result": None, "error": None,
}


async def run_backlog_sweep() -> dict:
    """Harvest the skip pile into the ledger: one entry per UNIQUE file
    (all copy message ids recorded), origin marked backlog-sweep. Read
    -only on Telegram — nothing is deleted or moved."""
    from Backend.helper.settings_manager import SettingsManager
    settings = SettingsManager.current()
    skip = settings.skip_channel
    if not skip:
        return {"ok": False, "message": "No skip channel configured."}
    import Backend.pyrofork.bot as botmod
    userbot = getattr(botmod, "Userbot", None)
    if userbot is None:
        return {"ok": False, "message": "Userbot is not available."}
    skip_id = int(skip) if str(skip).lstrip("-").isdigit() else skip
    seen: dict[str, dict] = {}
    total = 0
    async for message in userbot.get_chat_history(skip_id):
        total += 1
        fields = _media_fields(message)
        if not fields.get("file_unique_id"):
            continue
        entry = seen.setdefault(fields["file_unique_id"], {"fields": fields, "message": message, "copies": []})
        entry["copies"].append(message.id)
        if total % 200 == 0:
            await asyncio.sleep(0.05)
    recorded = 0
    for unique_id, entry in seen.items():
        message = entry["message"]
        caption = getattr(message, "caption", "") or ""
        reason, parsed = _failure_analysis(caption, entry["fields"]["file_name"])
        hint = ""
        m = re.search(r"@([A-Za-z0-9_]{4,})", caption or "")
        if m:
            hint = f"@{m.group(1)}"
        ok = await record_reject({
            "key": unique_id, "file_unique_id": unique_id,
            "chat_id": None, "msg_id": None, "origin": "backlog-sweep",
            "origin_hint": hint, "caption": caption,
            "file_name": entry["fields"]["file_name"],
            "size": entry["fields"]["size"], "height": entry["fields"]["height"],
            "reason": reason, "reason_code": reason_code_for(reason),
            "parse": parsed, "bits": name_bits(caption, entry["fields"]["file_name"]),
            "skip_msg_id": entry["copies"][0] if entry["copies"] else None,
        })
        if ok:
            recorded += 1
        # Record every copy id on the entry (dedupe evidence + Phase 2 cleanup).
        coll = _ledger_coll()
        if coll is not None and len(entry["copies"]) > 1:
            try:
                await coll.update_one({"_id": unique_id},
                                      {"$addToSet": {"skip_msg_ids": {"$each": entry["copies"]}}})
            except Exception:
                pass
    return {"ok": True, "messages_read": total, "unique_files": len(seen),
            "recorded": recorded}


async def run_suspect_audit() -> dict:
    """DB-only suspect checks over the indexed library: (1) the same
    Telegram message indexed under more than one title; (2) a quality
    slot holding entries whose sizes diverge wildly. Nothing changes —
    suspects are ledger entries for review."""
    from Backend import db
    from Backend.helper.encrypt import decode_string
    dbs = getattr(db, "dbs", None) or {}
    locations: dict[tuple, set] = {}
    slot_sizes: dict[tuple, list] = {}
    for name, handle in dbs.items():
        if name == "tracking" or handle is None:
            continue
        for coll_name, mtype in (("movie", "movie"), ("tv", "tv")):
            try:
                cursor = handle[coll_name].find(
                    {}, {"title": 1, "telegram": 1, "seasons": 1})
                async for doc in cursor:
                    title = doc.get("title") or "?"
                    slots = [(None, None, doc.get("telegram") or [])]
                    for season in doc.get("seasons") or []:
                        for ep in season.get("episodes") or []:
                            slots.append((season.get("season"), ep.get("episode"),
                                          ep.get("telegram") or []))
                    for s_no, e_no, entries in slots:
                        for entry in entries:
                            enc = entry.get("id")
                            if not enc:
                                continue
                            try:
                                decoded = await decode_string(enc)
                            except Exception:
                                continue
                            chat = _norm_chat((decoded or {}).get("chat_id"))
                            msg = (decoded or {}).get("msg_id")
                            if chat is None or msg is None:
                                continue
                            locations.setdefault((chat, msg), set()).add(
                                (mtype, title, s_no, e_no))
                            size = entry.get("size") or ""
                            slot_sizes.setdefault((mtype, title, s_no, e_no,
                                                   entry.get("quality")), []).append(
                                (chat, msg, size))
            except Exception as e:
                LOGGER.warning(f"[Librarian] audit scan failed ({name}.{coll_name}): {e}")

    recorded = 0
    found = {"multi_index": 0, "slot_collision": 0}
    for (chat, msg), titles in locations.items():
        distinct = {(t[0], t[1]) for t in titles}
        if len(distinct) < 2:
            continue
        found["multi_index"] += 1
        names = ", ".join(sorted(t[1] for t in distinct))
        ok = await record_reject({
            "key": f"tg:{chat}:{msg}", "file_unique_id": None,
            "chat_id": chat, "msg_id": msg, "origin": "suspect-audit",
            "caption": "", "file_name": "",
            "reason": f"Indexed under more than one title: {names}",
            "reason_code": "suspect-multi-index",
            "current_match": {"titles": sorted(t[1] for t in distinct)},
            "bits": name_bits("", ""),
        })
        recorded += 1 if ok else 0

    for (mtype, title, s_no, e_no, quality), entries in slot_sizes.items():
        if len(entries) < 2:
            continue
        def _bytes(size_str):
            try:
                return float(str(size_str).split()[0]) * {
                    "B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3,
                }.get(str(size_str).split()[-1].upper(), 1)
            except Exception:
                return 0
        sizes = [_bytes(s) for _, _, s in entries]
        sizes = [s for s in sizes if s > 0]
        if len(sizes) >= 2 and max(sizes) > 2.5 * min(sizes):
            found["slot_collision"] += 1
            chat, msg, _ = entries[0]
            ok = await record_reject({
                "key": f"tg:{chat}:{msg}", "file_unique_id": None,
                "chat_id": chat, "msg_id": msg, "origin": "suspect-audit",
                "caption": "", "file_name": "",
                "reason": (f"Quality slot {quality} for '{title}' "
                           f"S{s_no}E{e_no} holds {len(entries)} files with "
                           f"divergent sizes — possibly a wrong match."),
                "reason_code": "suspect-slot-collision",
                "current_match": {"title": title, "media_type": mtype},
                "bits": name_bits("", ""),
            })
            recorded += 1 if ok else 0
    return {"ok": True, "suspects_recorded": recorded, "found": found,
            "messages_indexed": len(locations)}


async def run_funnel(limit_runs: int = 40) -> dict:
    """Cluster open ledger items into runs and resolve each through
    L0-L2 + the gate. Persists run outcomes; items gain run_id/status.
    Proposals only — Phase 1 applies nothing."""
    coll = _ledger_coll()
    runs_coll = _runs_coll()
    if coll is None or runs_coll is None:
        return {"ok": False, "message": "Ledger unavailable."}
    items = []
    try:
        cursor = coll.find({"status": {"$in": ["open", "unresolved"]}}).limit(2000)
        async for doc in cursor:
            doc = dict(doc)
            doc["key"] = doc.get("_id")
            items.append(doc)
    except Exception as e:
        return {"ok": False, "message": f"Ledger read failed: {e}"}
    if not items:
        return {"ok": True, "runs": 0, "message": "No open ledger items."}

    title_index = await _channel_title_index()
    runs = cluster_items(items)[:max(1, limit_runs)]
    summary = {"ok": True, "runs": 0, "tier_a": 0, "tier_b": 0, "tier_c": 0}
    now = time.time()
    for run in runs:
        outcome = await resolve_run(run, title_index)
        tier = outcome["tier"]
        doc = {
            "chat_id": run["chat_id"], "stem": run["stem"],
            "season": run["season"], "special": run["special"],
            "year": run["year"], "episodes": run["episodes"],
            "missing_episodes": run["missing_episodes"],
            "item_keys": run["item_keys"],
            "item_count": len(run["item_keys"]),
            "proposal": outcome["proposal"], "gate": outcome["gate"],
            "tier": tier, "status": "proposed" if outcome["proposal"] else "unresolved",
            "updated_at": now,
        }
        try:
            await runs_coll.update_one(
                {"_id": run["run_id"]},
                {"$set": doc, "$setOnInsert": {"created_at": now}}, upsert=True)
            await coll.update_many(
                {"_id": {"$in": run["item_keys"]}},
                {"$set": {"run_id": run["run_id"],
                          "status": "proposed" if outcome["proposal"] else "unresolved"}})
        except Exception as e:
            LOGGER.warning(f"[Librarian] run persist failed: {e}")
        summary["runs"] += 1
        summary[{"A": "tier_a", "B": "tier_b", "C": "tier_c"}[tier]] += 1
    return summary


async def _run_action(kind: str) -> None:
    ACTION_STATE.update({"running": True, "kind": kind,
                         "started_at": time.time(), "finished_at": None,
                         "result": None, "error": None})
    try:
        if kind == "sweep":
            result = await run_backlog_sweep()
        elif kind == "audit":
            result = await run_suspect_audit()
        elif kind == "funnel":
            result = await run_funnel()
        else:
            result = {"ok": False, "message": f"Unknown action: {kind}"}
        ACTION_STATE["result"] = result
    except Exception as e:
        LOGGER.error(f"[Librarian] action {kind} failed: {e}")
        ACTION_STATE["error"] = str(e)
    finally:
        ACTION_STATE["running"] = False
        ACTION_STATE["finished_at"] = time.time()


def start_action(kind: str) -> dict:
    if ACTION_STATE["running"]:
        return {"status": "error", "message": f"Already running: {ACTION_STATE['kind']}."}
    asyncio.get_event_loop().create_task(_run_action(kind))
    return {"status": "success", "message": f"{kind} started."}
