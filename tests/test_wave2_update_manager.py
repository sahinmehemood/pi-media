"""Wave 2 Phase D tests: Update Manager pure helpers, state guards,
and refusal paths (no git is ever invoked on a refusal)."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load_module, run

um = load_module("update_manager_under_test", "Backend/helper/update_manager.py")


def _with_tmp_state(state):
    """Point the manager at a temp state file seeded with `state`."""
    tmp = Path(tempfile.mkdtemp()) / "state.json"
    um.STATE_FILE = tmp
    um.save_state({**um.empty_state(), **state})
    return tmp


def test_intersect_risk():
    assert um.intersect_risk(["a.py", "b.py", "c.py"], ["b.py", "d.py"]) == ["b.py"]
    assert um.intersect_risk(["a.py"], ["b.py"]) == []
    assert um.intersect_risk([], ["b.py"]) == []


def test_parse_test_summary():
    out = "junk\n108 passed, 0 failed\nmore"
    assert um.parse_test_summary(out) == {"passed": 108, "failed": 0}
    out2 = "10 passed, 1 failed\n20 passed, 0 failed"
    assert um.parse_test_summary(out2) == {"passed": 20, "failed": 0}  # last wins
    assert um.parse_test_summary("no summary here") is None
    assert um.parse_test_summary("") is None


def test_deploy_guards():
    ok, why = um.can_deploy(um.empty_state())
    assert not ok and "Nothing staged" in why
    red = {**um.empty_state(), "phase": "tested", "stage": {"green": False}}
    ok, why = um.can_deploy(red)
    assert not ok and "not green" in why
    green = {**um.empty_state(), "phase": "tested", "stage": {"green": True}}
    ok, why = um.can_deploy(green)
    assert ok and why == ""


def test_rollback_guards():
    ok, why = um.can_rollback(um.empty_state())
    assert not ok and "No rollback point" in why
    ok, _ = um.can_rollback({**um.empty_state(), "previous_head": "abc123"})
    assert ok


def test_state_roundtrip():
    _with_tmp_state({"phase": "checked", "commits_ahead": 3})
    loaded = um.load_state()
    assert loaded["phase"] == "checked" and loaded["commits_ahead"] == 3
    assert loaded["previous_head"] == ""  # defaults filled


def test_refusals_never_touch_git():
    calls = []
    orig_run = um._run
    um._run = lambda cmd, cwd, timeout=120: calls.append(cmd) or (0, "")
    try:
        _with_tmp_state({"phase": "idle"})
        out = um.do_deploy()
        assert "Nothing staged" in out["message"] and out["phase"] == "idle"
        out = um.do_stage()
        assert "Run Check first" in out["message"] and out["phase"] == "idle"
        out = um.do_rollback()
        assert "No rollback point" in out["message"] and out["phase"] == "idle"
        assert calls == [], f"refusals must not run commands, got {calls}"
    finally:
        um._run = orig_run


def test_note_preserves_phase():
    _with_tmp_state({"phase": "deployed", "previous_head": "deadbeef"})
    out = um.do_stage()  # wrong phase for staging; must not clobber
    assert out["phase"] == "deployed" and out["previous_head"] == "deadbeef"


if __name__ == "__main__":
    run(globals())
