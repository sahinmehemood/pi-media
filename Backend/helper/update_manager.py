"""Update Manager core (Wave 2 Phase D) — fork-aware upstream updates.

The live install is a fork of weebzone/Telegram-Stremio: upstream files
carry a seam patch (patches/integrate.patch) plus self-contained plugin
files. Updating therefore means: re-apply the fork onto the new
upstream in the STAGING clone (~/telegram-stremio-own), run the full
suite there, and only then move the live repo to the tested tree.

State machine (persisted in .update-manager.json in the app root,
outside git):
    idle -> checking -> checked -> staging -> tested (green|red)
    tested(green) -> deploying -> deployed
    deployed -> rolling_back -> rolled_back
Any step can land in "error" with a message; Check can be re-run from
almost anywhere. Deploy is guarded: it refuses unless the last stage
was green AND the live repo has not moved since the check (a phase
deploy by hand invalidates the staged update — re-check instead).

Deliberately imports nothing from Backend: it must stay runnable and
testable standalone. All git work goes through _run() so tests can
substitute a fake. config.env / *.session are untracked and are never
touched by any operation here (git reset --hard only affects tracked
files).
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
STAGING_ROOT = APP_ROOT.parent / "telegram-stremio-own"
STATE_FILE = APP_ROOT / ".update-manager.json"
RESTART_LOG = APP_ROOT / ".update-manager-restart.log"
UPSTREAM_URL = "https://github.com/weebzone/Telegram-Stremio"
UPSTREAM_REF = "upstream/master"
STAGED_BRANCH = "staged-update"
SERVICE_PATH = "/data/data/com.termux/files/usr/var/service/telegram-stremio"

TEST_SUMMARY_RE = re.compile(r"(\d+) passed, (\d+) failed")


# ── pure helpers (unit-tested) ─────────────────────────────────────

def intersect_risk(changed_files, seam_files):
    """Upstream-changed files that the fork also touches = breakage risk."""
    return sorted(set(changed_files) & set(seam_files))


def parse_test_summary(output: str):
    """Last 'N passed, M failed' line in captured output, or None."""
    found = TEST_SUMMARY_RE.findall(output or "")
    if not found:
        return None
    passed, failed = found[-1]
    return {"passed": int(passed), "failed": int(failed)}


def empty_state() -> dict:
    return {
        "phase": "idle",
        "message": "",
        "updated_at": 0,
        "live_head": "",
        "base": "",
        "upstream_head": "",
        "commits_ahead": 0,
        "commits": [],
        "changed_files": [],
        "risky_files": [],
        "stage": None,        # {"green": bool, "steps": [...], "tests": {...}|None}
        "previous_head": "",  # rollback point recorded at deploy time
        "deployed_head": "",
    }


def can_deploy(state: dict) -> tuple[bool, str]:
    if state.get("phase") != "tested":
        return False, "Nothing staged — run Check, then Stage & Test first."
    stage = state.get("stage") or {}
    if not stage.get("green"):
        return False, "The staged update is not green — deploying is blocked."
    return True, ""


def can_rollback(state: dict) -> tuple[bool, str]:
    if not state.get("previous_head"):
        return False, "No rollback point recorded (deploy records one)."
    return True, ""


# ── state persistence ──────────────────────────────────────────────

def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text())
        state = empty_state()
        state.update({k: v for k, v in data.items() if k in state})
        return state
    except Exception:
        return empty_state()


def save_state(state: dict) -> dict:
    state["updated_at"] = int(time.time())
    try:
        STATE_FILE.write_text(json.dumps(state, indent=1))
    except Exception:
        pass
    return state


def _set_phase(state: dict, phase: str, message: str = "") -> dict:
    state["phase"] = phase
    state["message"] = message
    return save_state(state)


def _note(state: dict, message: str) -> dict:
    """Record a guard refusal WITHOUT changing the phase — a mistimed
    button tap must not erase a good check/stage/deploy report."""
    state["message"] = message
    return save_state(state)


# ── command runner (single seam for tests) ─────────────────────────

def _run(cmd: list[str], cwd: Path, timeout: int = 120) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(cmd)}"
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


def _git(cwd: Path, *args: str, timeout: int = 120) -> tuple[int, str]:
    return _run(["git", *args], cwd, timeout)


def _head(cwd: Path) -> str:
    rc, out = _git(cwd, "rev-parse", "HEAD")
    return out.strip().splitlines()[0] if rc == 0 and out.strip() else ""


def _ensure_upstream_remote() -> tuple[int, str]:
    rc, out = _git(STAGING_ROOT, "remote")
    if rc != 0:
        return rc, out
    if "upstream" not in out.split():
        return _git(STAGING_ROOT, "remote", "add", "upstream", UPSTREAM_URL)
    return 0, ""


# ── operations ─────────────────────────────────────────────────────

def do_check() -> dict:
    state = load_state()
    _set_phase(state, "checking", "Fetching upstream and comparing…")
    try:
        if not STAGING_ROOT.is_dir():
            return _set_phase(state, "error",
                              f"Staging clone missing: {STAGING_ROOT}")
        live_head = _head(APP_ROOT)
        if not live_head:
            return _set_phase(state, "error", "Cannot read the live repo HEAD.")
        rc, out = _ensure_upstream_remote()
        if rc != 0:
            return _set_phase(state, "error", f"git remote failed: {out.strip()[-200:]}")
        # Make sure the staging clone knows the live head (local fetch).
        # Capture its sha immediately: the upstream fetch below would
        # overwrite FETCH_HEAD.
        _git(STAGING_ROOT, "fetch", str(APP_ROOT), "HEAD", timeout=60)
        live_ref = _head_at(STAGING_ROOT, "FETCH_HEAD") or live_head
        rc, out = _git(STAGING_ROOT, "fetch", "upstream", "master", timeout=300)
        if rc != 0:
            return _set_phase(state, "error",
                              f"Upstream fetch failed: {out.strip()[-200:]}")
        upstream_head = _head_at(STAGING_ROOT, UPSTREAM_REF)
        rc, out = _git(STAGING_ROOT, "merge-base", live_ref, UPSTREAM_REF)
        base = out.strip() if rc == 0 else ""
        if not upstream_head or not base:
            return _set_phase(state, "error", "Could not resolve upstream/base commits.")
        rc, out = _git(STAGING_ROOT, "rev-list", "--count", f"{base}..{UPSTREAM_REF}")
        ahead = int(out.strip()) if rc == 0 and out.strip().isdigit() else 0
        _, changed = _git(STAGING_ROOT, "diff", "--name-only", f"{base}..{UPSTREAM_REF}")
        _, seams = _git(STAGING_ROOT, "diff", "--name-only", f"{base}..{live_ref}")
        _, log = _git(STAGING_ROOT, "log", "--oneline", f"{base}..{UPSTREAM_REF}")
        changed_files = [l for l in changed.splitlines() if l.strip()]
        seam_files = [l for l in seams.splitlines() if l.strip()]
        state.update({
            "live_head": live_head,
            "base": base,
            "upstream_head": upstream_head,
            "commits_ahead": ahead,
            "commits": [l for l in log.splitlines() if l.strip()][:50],
            "changed_files": changed_files,
            "risky_files": intersect_risk(changed_files, seam_files),
            "stage": None,
        })
        msg = (f"Up to date — no upstream commits beyond your base."
               if ahead == 0 else
               f"{ahead} upstream commit(s) available; "
               f"{len(state['risky_files'])} touch fork seam files.")
        return _set_phase(state, "checked", msg)
    except Exception as e:
        return _set_phase(state, "error", f"Check failed: {type(e).__name__}: {e}")


def _head_at(cwd: Path, ref: str) -> str:
    rc, out = _git(cwd, "rev-parse", ref)
    return out.strip().splitlines()[0] if rc == 0 and out.strip() else ""


def do_stage() -> dict:
    state = load_state()
    if state.get("phase") not in ("checked", "tested"):
        return _note(state, "Run Check first — staging needs a known upstream head.")
    if state.get("commits_ahead", 0) == 0:
        return _note(state, "Already up to date — nothing to stage.")
    _set_phase(state, "staging", "Applying the fork onto upstream in the staging clone…")
    steps = []
    try:
        reapply = APP_ROOT / "tools" / "reapply.sh"
        rc, out = _run(["bash", str(reapply), str(STAGING_ROOT), UPSTREAM_REF],
                       APP_ROOT, timeout=900)
        steps.append({"name": "Reapply fork + run test suite", "ok": rc == 0,
                      "detail": "\n".join(out.strip().splitlines()[-6:])})
        tests = parse_test_summary(out)
        green = rc == 0
        if green:
            rc2, out2 = _run(["python3", "-m", "compileall", "-q", "Backend"],
                             STAGING_ROOT, timeout=300)
            steps.append({"name": "Compile pass (Backend)", "ok": rc2 == 0,
                          "detail": out2.strip()[-300:]})
            green = rc2 == 0
        if green:
            rc3, out3 = _git(STAGING_ROOT, "add", "-A")
            rc4, out4 = _run(
                ["git", "-c", "user.name=Update Manager",
                 "-c", "user.email=update-manager@localhost",
                 "commit", "-q", "-m",
                 f"staged-update: upstream {state['upstream_head'][:10]}"],
                STAGING_ROOT, timeout=120)
            # commit on a scratch branch pointing at the tested tree
            rc5, out5 = _git(STAGING_ROOT, "branch", "-f", STAGED_BRANCH, "HEAD")
            steps.append({"name": "Record tested tree", "ok": rc4 == 0 and rc5 == 0,
                          "detail": (out4 + out5).strip()[-200:]})
            green = rc4 == 0 and rc5 == 0
        state["stage"] = {"green": green, "steps": steps, "tests": tests,
                          "upstream_head": state["upstream_head"]}
        msg = ("Green — the fork applies cleanly and all tests pass on the "
               "new upstream. Deploy is armed." if green else
               "Red — the fork does not apply cleanly to the new upstream. "
               "The live install was NOT touched; see the step details.")
        return _set_phase(state, "tested", msg)
    except Exception as e:
        return _set_phase(state, "error", f"Stage failed: {type(e).__name__}: {e}")


def restart_service(delay_s: int = 2) -> None:
    """Restart the runit service from a detached session so the restart
    survives this process being killed by it. Verified pattern: setsid +
    start_new_session, output to a log file, never a pipe held by us."""
    try:
        log = open(RESTART_LOG, "ab")
        subprocess.Popen(
            ["setsid", "sh", "-c",
             f"sleep {delay_s}; sv restart {SERVICE_PATH}"],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def do_deploy() -> dict:
    state = load_state()
    ok, why = can_deploy(state)
    if not ok:
        return _note(state, why)
    if _head(APP_ROOT) != state.get("live_head"):
        state["stage"] = None
        state["phase"] = "checked"
        return _note(
            state,
            "The live repo moved since the check (a manual deploy?). "
            "Re-run Check and Stage before deploying.")
    staged = _head_at(STAGING_ROOT, STAGED_BRANCH)
    if not staged:
        return _note(state, "Staged tree is missing — re-run Stage & Test.")
    _set_phase(state, "deploying", "Moving the live repo to the tested tree…")
    state["previous_head"] = state["live_head"]  # rollback point
    save_state(state)
    rc, out = _git(APP_ROOT, "fetch", str(STAGING_ROOT), STAGED_BRANCH, timeout=120)
    if rc == 0:
        rc, out = _git(APP_ROOT, "reset", "--hard", "FETCH_HEAD", timeout=120)
    if rc != 0:
        return _set_phase(state, "error",
                          f"Deploy checkout failed: {out.strip()[-200:]} "
                          f"(rollback point {state['previous_head'][:10]} recorded)")
    state["deployed_head"] = staged
    _set_phase(state, "deployed",
               f"Deployed upstream {state['upstream_head'][:10]} — restarting. "
               f"Rollback point: {state['previous_head'][:10]}. "
               "Note: GitHub's own branch still points at the previous build; "
               "it gets synced on the next regular deploy.")
    restart_service()
    return state


def do_rollback() -> dict:
    state = load_state()
    ok, why = can_rollback(state)
    if not ok:
        return _note(state, why)
    target = state["previous_head"]
    _set_phase(state, "rolling_back", f"Rolling back to {target[:10]}…")
    rc, out = _git(APP_ROOT, "reset", "--hard", target, timeout=120)
    if rc != 0:
        return _set_phase(state, "error", f"Rollback failed: {out.strip()[-200:]}")
    state["live_head"] = target
    state["deployed_head"] = target
    state["previous_head"] = ""
    state["stage"] = None
    _set_phase(state, "rolled_back",
               f"Rolled back to {target[:10]} — restarting.")
    restart_service()
    return state


def status_snapshot() -> dict:
    state = load_state()
    ok_d, _ = can_deploy(state)
    ok_r, _ = can_rollback(state)
    return {
        **state,
        "live_head_now": _head(APP_ROOT),
        "staging_present": STAGING_ROOT.is_dir(),
        "deploy_armed": ok_d,
        "rollback_available": ok_r,
    }
