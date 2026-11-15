#!/usr/bin/env bash
# ============================================================================
# pi-media feature re-patch — run this after every new upstream release.
#
# Usage:   bash patches/reapply.sh
#
# It fetches the latest official Telegram-Stremio, starts a fresh branch from
# it, copies the two drop-in modules and the regression suite over from the
# previous `final` branch, and prints the exact merge checklist for the six
# integration touch points. The checklist diffs are the merge guide — apply
# each hunk against the NEW upstream file (upstream may have changed the same
# lines; never copy the old file wholesale except for the two drop-ins).
# ============================================================================
set -euo pipefail

UPSTREAM_REMOTE=${UPSTREAM_REMOTE:-official}
UPSTREAM_REF=${UPSTREAM_REF:-master}
PREV=${PREV_FINAL:-final}
NEW=${NEW_BRANCH:-final-next}

#----- 0. Preconditions
git rev-parse --verify "$PREV" >/dev/null 2>&1 \
  || { echo "Previous feature branch '$PREV' not found — it holds the code to re-apply."; exit 1; }

#----- 1. Fresh branch on the newest upstream release
git fetch "$UPSTREAM_REMOTE" 2>/dev/null || {
  echo "Fetching from '$UPSTREAM_REMOTE' failed. Add the upstream remote first:"; echo "  git remote add $UPSTREAM_REMOTE https://github.com/weebzone/Telegram-Stremio.git"; exit 1;
}
git rev-parse --verify --quiet "$UPSTREAM_REMOTE/$UPSTREAM_REF" >/dev/null \
  || { echo "Ref $UPSTREAM_REMOTE/$UPSTREAM_REF not found after fetch."; exit 1; }
git checkout -B "$NEW" "$UPSTREAM_REMOTE/$UPSTREAM_REF"
echo ">> Base: $NEW = $(git rev-parse --short "$UPSTREAM_REMOTE/$UPSTREAM_REF") $(git log -1 --format=%s "$UPSTREAM_REMOTE/$UPSTREAM_REF")"

#----- 2. Drop-in modules + regression suite (whole-file copy is safe here:
#         they depend only on pyrogram.errors, Backend.logger and settings key
#         names — none of which upstream changes lightly)
git checkout "$PREV" -- Backend/helper/stream_buffer.py Backend/helper/flood_wait.py
git checkout "$PREV" -- tests/
echo ">> Copied drop-in modules and tests from $PREV"

#----- 3. Donation removal: deletions re-apply mechanically. Show exactly what
#         the old branch deleted so the same lines can be re-deleted.
echo
echo "==============================================================="
echo " INTEGRATION CHECKLIST — merge each hunk, don't copy files"
echo "==============================================================="
echo
echo "--- Feature 1 touch points (buffer integration) ---"
git diff --stat "$UPSTREAM_REMOTE/$UPSTREAM_REF" "$PREV" -- \
  Backend/helper/custom_dl.py \
  Backend/fastapi/routes/stream_routes.py \
  Backend/__main__.py \
  Backend/helper/settings_manager.py \
  Backend/fastapi/routes/api_routes.py \
  Backend/fastapi/templates/settings.html
echo
echo "--- Feature 2 deletion list (re-delete these from upstream) ---"
git diff "$UPSTREAM_REMOTE/$UPSTREAM_REF" "$PREV" -- \
  Backend/fastapi/routes/stremio_routes.py \
  Backend/fastapi/templates/base.html README.md docs/index.html docs/setup.html \
  | grep -E "^[-+]def _donation|^[-+].*donate\.|^[-+]return \{\"streams\":|^[-+]streams\.append" || true
echo
echo ">> Full per-file diffs: git diff $UPSTREAM_REMOTE/$UPSTREAM_REF $PREV -- <file>"
echo ">> Full guide: patches/README.md"
echo
echo ">> Next steps: walk the checklist, commit in the 6-commit series,"
echo ">> run: for f in tests/test_*.py; do python3.14 \"\$f\"; done"
echo ">> then: git checkout -B final && git branch -D $NEW (fast-forward)"
