"""Regression tests: no donation or promotional content may reach users.

Source of the requirement: the fork removes donation requests and unwanted
self-promotion. This suite scans the user-facing application (templates,
addon routes, docs, README) so a later upstream merge cannot silently
re-introduce them.

Deliberately narrow: it asserts on the *promotional* patterns only. License
text, upstream attribution (github.com/weebzone/Telegram-Stremio) and
functional links are expected to remain and are asserted present.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
RESULTS = []

#----- Patterns that are unambiguously donation / self-promotion ----------
PROMO = [
    (r"donate\.weebzonex\.workers\.dev", "donation URL"),
    (r"_donation\s*\(", "donation helper"),
    (r"def\s+_donation", "donation function"),
    (r"Support the project", "support-the-project link"),
    (r"Support the developer", "support-the-developer text"),
    (r"❤️?\s*Support", "heart support label"),
    (r"buymeacoffee", "buy-me-a-coffee"),
    (r"patreon\.com", "patreon"),
]

#----- Things that MUST survive: licence + required attribution ------------
ATTRIBUTION = [
    (r"weebzone/Telegram-Stremio", "upstream repository reference"),
    (r"GNU GENERAL PUBLIC LICENSE", "GPL licence header"),
]

SCAN_DIRS = ["Backend", "docs", "tests"]
SCAN_FILES = ["README.md", "LICENSE", "pyproject.toml", "requirements.txt"]
TEXT_SUFFIXES = {".py", ".html", ".md", ".js", ".css", ".txt", ".toml", ".json", ".yaml", ".yml"}


def _files():
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p.suffix not in TEXT_SUFFIXES:
                continue
            if "__pycache__" in str(p):
                continue
            # This suite contains the pattern strings themselves; excluding it
            # keeps the scan honest about the application, not about the test.
            if p.resolve() == pathlib.Path(__file__).resolve():
                continue
            yield p
    for f in SCAN_FILES:
        p = ROOT / f
        if p.exists():
            yield p


def test(name):
    def deco(fn):
        try:
            fn()
            RESULTS.append((True, name, ""))
        except AssertionError as e:
            RESULTS.append((False, name, str(e)))
        except Exception as e:  # noqa: BLE001
            RESULTS.append((False, name, f"{type(e).__name__}: {e}"))
        return fn
    return deco


@test("no donation or promotional pattern in user-facing code")
def _():
    hits = []
    for p in _files():
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        for pat, label in PROMO:
            if re.search(pat, text, re.IGNORECASE):
                hits.append(f"{p.relative_to(ROOT)}: {label}")
    assert not hits, "promotional content found:\n  " + "\n  ".join(hits[:20])


@test("no donation helper is exported from the addon routes")
def _():
    p = ROOT / "Backend/fastapi/routes/stremio_routes.py"
    text = p.read_text()
    assert "_donation" not in text, "stremio_routes still defines/uses _donation"
    # The stream list must still be a valid response shape.
    assert '"streams"' in text, "stream response shape changed unexpectedly"


@test("licence and upstream attribution are preserved")
def _():
    licence = ROOT / "LICENSE"
    assert licence.exists(), "LICENSE missing"
    head = licence.read_text(errors="ignore")[:4000]
    for pat, label in ATTRIBUTION:
        if label.startswith("GPL"):
            assert re.search(pat, head), f"{label} missing from LICENSE"
    readme = (ROOT / "README.md").read_text(errors="ignore")
    assert re.search(r"weebzone/Telegram-Stremio", readme), "upstream attribution missing from README"


@test("addon still returns a well-formed stream list when empty")
def _():
    p = ROOT / "Backend/fastapi/routes/stremio_routes.py"
    text = p.read_text()
    # The empty-stream branch must return an empty list, not a promo card.
    assert 'return {"streams": []}' in text, "empty-stream branch does not return an empty list"


def main():
    passed = sum(1 for ok, _n, _e in RESULTS if ok)
    total = len(RESULTS)
    for ok, name, err in RESULTS:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            print(f"       -> {err}")
    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
