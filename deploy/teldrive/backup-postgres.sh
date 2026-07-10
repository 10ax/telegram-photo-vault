#!/usr/bin/env bash
# Back up the teldrive Postgres DB (the source of truth for your file tree).
# Cron example (daily 03:00, keeps 14):
#   0 3 * * * /path/to/deploy/teldrive/backup-postgres.sh >> /var/log/teldrive-backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")"

BACKUP_DIR="${1:-./backups}"
KEEP="${KEEP:-14}"
mkdir -p "$BACKUP_DIR"

# shellcheck disable=SC1091
set -a; [ -f .env ] && . ./.env; set +a
USER="${POSTGRES_USER:-teldrive}"
DB="${POSTGRES_DB:-teldrive}"
TS="$(date +%Y%m%d_%H%M%S)"
OUT="$BACKUP_DIR/teldrive_${TS}.sql.gz"

docker compose exec -T postgres pg_dump -U "$USER" -d "$DB" | gzip > "$OUT"

# Prune old backups, keep the newest $KEEP.
ls -1t "$BACKUP_DIR"/teldrive_*.sql.gz 2>/dev/null | tail -n "+$((KEEP + 1))" | xargs -r rm -f
echo "backup -> $OUT ($(du -h "$OUT" | cut -f1))"

# Restore (manual):
#   gunzip -c teldrive_YYYYMMDD_HHMMSS.sql.gz | docker compose exec -T postgres psql -U teldrive -d teldrive
