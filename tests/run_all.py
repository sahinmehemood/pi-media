"""Run the whole buffer-plugin test suite with plain python3 (stdlib only).

Usage: python3 tests/run_all.py   (also compatible with pytest discovery)
"""

import importlib
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

MODULES = ["test_stream_buffer_core", "test_buffered_streamer", "test_static_seams",
           "test_dl_link", "test_wave2_engine", "test_wave2_prewarm",
           "test_wave2_update_manager", "test_librarian_store",
           "test_librarian_gateway", "test_librarian_api",
           "test_librarian_dbname", "test_librarian_templates",
           "test_librarian_writer", "test_librarian_engine"]


def main() -> int:
    passed = failed = 0
    failures = []
    for name in MODULES:
        mod = importlib.import_module(name)
        for attr in sorted(dir(mod)):
            if not attr.startswith("test_"):
                continue
            fn = getattr(mod, attr)
            if not callable(fn):
                continue
            try:
                fn()
                passed += 1
                print(f"PASS {name}.{attr}")
            except Exception:
                failed += 1
                failures.append(f"{name}.{attr}")
                print(f"FAIL {name}.{attr}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
