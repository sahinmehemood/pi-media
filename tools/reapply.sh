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
  Backend/helper/stream_source.py
  Backend/helper/prewarm.py
  Backend/helper/update_manager.py
  Backend/helper/hermes_bot.py
  Backend/helper/librarian.py
  Backend/pyrofork/plugins/dl_link.py
  Backend/pyrofork/plugins/prewarm_hooks.py
  Backend/fastapi/templates/dl_index.html
  Backend/fastapi/templates/librarian.html
  Backend/fastapi/templates/librarian/_proposals.html
  Backend/fastapi/templates/librarian/_activity.html
  Backend/fastapi/templates/librarian/_flagged.html
  Backend/fastapi/templates/librarian/_aliases.html
  tests/_loader.py
  tests/run_all.py
  tests/test_stream_buffer_core.py
  tests/test_buffered_streamer.py
  tests/test_static_seams.py
  tests/test_dl_link.py
  tests/test_wave2_engine.py
  tests/test_wave2_prewarm.py
  tests/test_wave2_update_manager.py
  tests/test_hermes_bot.py
  tests/test_librarian.py
  docs/librarian/DESIGN.md
  docs/librarian/DESIGN-v4.md
  docs/librarian/DESIGN-v5.md
  tools/librarian_cli.py
  tools/media-manager-profile/SOUL.md
  tools/media-manager-profile/AGENTS.md
  tools/media-manager-profile/memories/MEMORY.md
  tools/media-manager-profile/memories/USER.md
  tools/media-manager-profile/skills/librarian/SKILL.md
  tools/media-manager-profile/skills/provider-research/SKILL.md
  tools/media-manager-profile/skills/release-naming/SKILL.md
  tools/media-manager-profile/skills/movie-naming/SKILL.md
  tools/media-manager-profile/skills/tv-naming/SKILL.md
  tools/media-manager-profile/skills/anime-naming/SKILL.md
  tools/media-manager-profile/skills/donghua-naming/SKILL.md
  tools/media-manager-profile/skills/cartoon-naming/SKILL.md
  tools/media-manager-profile/skills/drama-naming/SKILL.md
  tools/media-manager-profile/skills/telegram-uploaders/SKILL.md
  tools/media-manager-profile/skills/packet-triage/SKILL.md
  tools/media-manager-profile/skills/specials-and-arcs/SKILL.md
  tools/media-manager-profile/skills/quality-and-editions/SKILL.md
  tools/media-manager-profile/skills/gate-and-apply/SKILL.md
  tools/media-manager-profile/skills/alias-and-memory-discipline/SKILL.md
  tools/media-manager-profile/skills/screenshot-evidence/SKILL.md
  tools/media-manager-profile/skills/self-check-eval/SKILL.md
  tools/media-manager-profile/skills/failure-playbooks/SKILL.md
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

echo "==> OK: streaming buffer plugin applied to $REF and all tests pass"
