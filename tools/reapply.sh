#!/usr/bin/env bash
# Reapply the streaming buffer plugin onto a fresh official checkout.
#
# Usage: tools/reapply.sh <path-to-official-clone> [git-ref]
#
# What it does:
#   1. Checks out [git-ref] (default: master) in the target clone and
#      discards any previous plugin application there.
#   2. Copies the plugin's new files (engine, adapter, tests, docs).
#   3. Applies patches/integrate.patch — every edit to upstream files
#      (integration seams + donation removal), nothing else.
#   4. Runs the plugin test suite inside the target.
#
# Exit code 0 means the plugin applies cleanly to that upstream ref and
# all tests pass. Run it after every official update; if the patch no
# longer applies, upstream touched a seam — see docs/stream-buffer/DESIGN.md §6.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:?usage: reapply.sh <path-to-official-clone> [git-ref]}"
REF="${2:-master}"

PLUGIN_FILES=(
  Backend/helper/stream_buffer.py
  Backend/helper/buffered_streamer.py
  tests/_loader.py
  tests/run_all.py
  tests/test_stream_buffer_core.py
  tests/test_buffered_streamer.py
  tests/test_static_seams.py
  docs/stream-buffer/DESIGN.md
  docs/stream-buffer/README.md
  tools/reapply.sh
  patches/integrate.patch
)

echo "==> Target: $TARGET @ $REF"
git -C "$TARGET" checkout --quiet "$REF"
git -C "$TARGET" checkout -- . 
# Remove any previous application (tracked edits are reset above;
# plugin files are untracked in the target, so delete them explicitly).
for f in "${PLUGIN_FILES[@]}"; do
  rm -f "$TARGET/$f"
done

echo "==> Copying plugin files"
for f in "${PLUGIN_FILES[@]}"; do
  mkdir -p "$TARGET/$(dirname "$f")"
  cp "$SRC/$f" "$TARGET/$f"
done

echo "==> Applying seam patch"
git -C "$TARGET" apply --check "$SRC/patches/integrate.patch"
git -C "$TARGET" apply "$SRC/patches/integrate.patch"

echo "==> Running plugin tests"
(cd "$TARGET" && python3 tests/run_all.py)

echo "==> OK: streaming buffer plugin applied to $REF and all tests pass"
