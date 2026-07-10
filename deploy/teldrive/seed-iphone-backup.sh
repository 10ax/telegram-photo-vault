#!/usr/bin/env bash
# Seed the iPhone backup folder into the shared teldrive drive.
#
#   ./seed-iphone-backup.sh [SOURCE_DIR] [RCLONE_REMOTE:PATH]
#
# Prereqs:
#   - rclone remote of type "teldrive" configured (see README: `rclone config`,
#     pointing at http://<host>:8080 with an access token from the teldrive UI).
#   - Optional: jdupes for local de-duplication (teldrive does not content-dedupe).
#
# Speed: teldrive round-robins across the bots you add in its UI, so parallel
# --transfers use different bot accounts and bypass the non-premium single-account
# throttle. Match --transfers to your bot count (up to ~8).
set -euo pipefail

SRC="${1:-$HOME/Pictures/iPhone backup}"
REMOTE="${2:-teldrive:Family/Wife-iPhone}"
TRANSFERS="${TRANSFERS:-8}"

[ -d "$SRC" ] || { echo "Source not found: $SRC" >&2; exit 1; }

# 1) De-duplicate locally (hardlinks identical files so rclone uploads each once).
if command -v jdupes >/dev/null 2>&1; then
  echo ">> De-duplicating '$SRC' with jdupes…"
  jdupes -r -L "$SRC"
else
  echo ">> jdupes not installed — skipping local de-dupe."
  echo "   (install jdupes to avoid uploading duplicate copies)"
fi

# 2) Upload — resumable, parallel, rate-limited to be a good Telegram citizen.
echo ">> Uploading '$SRC' -> '$REMOTE' (transfers=$TRANSFERS)…"
rclone copy "$SRC" "$REMOTE" \
  --progress \
  --transfers "$TRANSFERS" \
  --checkers "$((TRANSFERS * 2))" \
  --tpslimit "$TRANSFERS" \
  --retries 5 \
  --low-level-retries 10 \
  --ignore-existing

echo ">> Done. Verify in the teldrive web UI."
