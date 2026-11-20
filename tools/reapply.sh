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
#   5. Byte-compares every PLUGIN_FILES entry and every file in
#      tools/seam_manifest.txt between the target and this tree —
#      the release proof that a pristine apply reproduces this tree
#      exactly (Librarian DESIGN §4).
#
# Exit code 0 means the plugin applies cleanly to that upstream ref,
# all tests pass, and the byte-compare is clean. Run it after every
# official update; if the patch no longer applies, upstream touched a
# seam — see docs/stream-buffer/DESIGN.md §6.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:?usage: reapply.sh <path-to-official-clone> [git-ref]}"
REF="${2:-master}"

PLUGIN_FILES=(
  Backend/helper/librarian_store.py
  Backend/helper/stream_buffer.py
  Backend/helper/buffered_streamer.py
  Backend/helper/stream_source.py
  Backend/helper/prewarm.py
  Backend/helper/update_manager.py
  Backend/pyrofork/plugins/dl_link.py
  Backend/pyrofork/plugins/prewarm_hooks.py
  Backend/fastapi/templates/dl_index.html
  tests/_loader.py
  tests/run_all.py
  tests/test_stream_buffer_core.py
  tests/test_buffered_streamer.py
  tests/test_static_seams.py
  tests/test_dl_link.py
  tests/test_wave2_engine.py
  tests/test_wave2_prewarm.py
  tests/test_wave2_update_manager.py
  tests/test_librarian_store.py
  tools/chief-profile/SOUL.md
  tools/chief-profile/AGENTS.md
  tools/chief-profile/memories/MEMORY.md
  tools/chief-profile/memories/USER.md
  tools/chief-profile/skills/routing-and-delegation/SKILL.md
  tools/chief-profile/skills/status-and-digest/SKILL.md
  tools/generalist-profile/atlas/SOUL.md
  tools/generalist-profile/atlas/AGENTS.md
  tools/generalist-profile/atlas/memories/MEMORY.md
  tools/generalist-profile/atlas/memories/USER.md
  tools/generalist-profile/servy/SOUL.md
  tools/generalist-profile/servy/AGENTS.md
  tools/generalist-profile/servy/memories/MEMORY.md
  tools/generalist-profile/servy/memories/USER.md
  docs/stream-buffer/DESIGN.md
  docs/dl-link.md
  docs/stream-buffer/README.md
  docs/wave-2/DESIGN.md
  docs/website-rebuild/DESIGN.md
  tools/reapply.sh
  tools/seam_manifest.txt
  patches/integrate.patch
  .github/workflows/upstream-check.yml
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

echo "==> Byte-comparing plugin + seam files against $SRC"
SEAM_FILES=()
while IFS= read -r line; do
  case "$line" in ''|'#'*) continue ;; esac
  SEAM_FILES+=("$line")
done < "$SRC/tools/seam_manifest.txt"
MISMATCH=0
for f in "${PLUGIN_FILES[@]}" "${SEAM_FILES[@]}"; do
  if ! cmp --silent "$SRC/$f" "$TARGET/$f"; then
    echo "MISMATCH: $f"
    MISMATCH=1
  fi
done
if [ "$MISMATCH" -ne 0 ]; then
  echo "==> FAIL: pristine apply does not reproduce the working tree"
  exit 1
fi

echo "==> OK: streaming buffer plugin applied to $REF, all tests pass, byte-compare clean"
