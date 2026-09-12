#!/usr/bin/env bash
# ==============================================================================
# Handelsimperium Automated Database Backup Script
# Generates compressed PostgreSQL dumps and enforces a 7-day retention policy.
# ==============================================================================
set -euo pipefail

BACKUP_DIR="/var/backups/handelsimperium"
ENV_FILE="/opt/handelsimperium/.env"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="${BACKUP_DIR}/dump_handelsimperium_${TIMESTAMP}.sql.gz"

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Starting Handelsimperium PostgreSQL database backup..."

# Source environment variables if .env exists
if [ -f "$ENV_FILE" ]; then
    # Export only valid shell assignment lines
    set -a
    # shellcheck disable=SC1090
    source <(grep -E '^[A-Za-z_]+=' "$ENV_FILE")
    set +a
fi

DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-handelsimperium_user}"
DB_NAME="${DB_NAME:-handelsimperium}"
export PGPASSWORD="${DB_PASS:-imperium_secret_2026}"

# Ensure backup destination exists
mkdir -p "$BACKUP_DIR"

# Execute pg_dump and pipe through gzip
echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Dumping database '${DB_NAME}' from ${DB_HOST}:${DB_PORT} as '${DB_USER}'..."
pg_dump -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$DB_NAME" | gzip -9 > "$BACKUP_FILE"

# Verify backup creation and non-zero size
if [ -s "$BACKUP_FILE" ]; then
    BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Backup successful: ${BACKUP_FILE} (${BACKUP_SIZE})"
else
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] ERROR: Backup file ${BACKUP_FILE} is empty or missing!" >&2
    exit 1
fi

# Retention management: prune backups older than 7 days
echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Applying 7-day retention policy on ${BACKUP_DIR}..."
DELETED_COUNT=$(find "$BACKUP_DIR" -type f -name "dump_handelsimperium_*.sql.gz" -mtime +7 -print -delete | wc -l)
echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Purged ${DELETED_COUNT} obsolete backup(s)."

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Backup routine finished successfully."
