#!/bin/bash
# Daily backup of terrra-europa data + secrets to local + S3.
# Run from cron. Uses EC2 instance role for S3 auth.
set -euo pipefail

ROOT="$HOME/terrra-europa"
BACKUP_DIR="$HOME/backups"
S3_PREFIX="s3://terrra-europa/back"
REGION="eu-central-1"
LOCAL_KEEP_DAYS=14

mkdir -p "$BACKUP_DIR"
TS=$(date -u +"%Y-%m-%dT%H-%M-%SZ")
ARCHIVE="$BACKUP_DIR/terrra-europa-$TS.tar.gz"

# Tar data + secrets + .env. Quiet about missing files only — fail otherwise.
cd "$ROOT"
tar -czf "$ARCHIVE" \
  data \
  secrets \
  .env

# Upload to S3 (server-side encrypted by default)
aws s3 cp "$ARCHIVE" "$S3_PREFIX/$(basename "$ARCHIVE")" --region "$REGION" --only-show-errors

# Local prune
find "$BACKUP_DIR" -name "terrra-europa-*.tar.gz" -mtime +"$LOCAL_KEEP_DAYS" -delete

echo "[$(date -u +%FT%TZ)] backed up $(basename "$ARCHIVE") ($(stat -c %s "$ARCHIVE") bytes)"
