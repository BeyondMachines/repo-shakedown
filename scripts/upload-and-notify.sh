#!/usr/bin/env bash
#
# upload-and-notify.sh — Package Strix results, upload to S3, notify webhook
#
# Usage (called by scan.sh or standalone):
#   ./scripts/upload-and-notify.sh <target> [scan_mode]
#
# Required env vars: S3_BUCKET, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
# Optional env var:  WEBHOOK_URL, AWS_REGION
#
set -euo pipefail

TARGET="${1:?Usage: $0 <target> [scan_mode]}"
SCAN_MODE="${2:-default}"

AWS_REGION="${AWS_REGION:-us-east-1}"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H-%M-%SZ")

# Derive a safe name from the target
SAFE_NAME=$(echo "$TARGET" | sed 's|https://github.com/||; s|https://||; s|http://||' | tr '/' '-' | tr ':' '-')

# ── Find the latest Strix run ──────────────────────────────────
RUN_DIR=$(ls -td strix_runs/*/ 2>/dev/null | head -1)

if [ -z "$RUN_DIR" ]; then
  echo "Error: No strix_runs directory found. Run a scan first."
  exit 1
fi

echo "Packaging results from: $RUN_DIR"

# ── Package ─────────────────────────────────────────────────────
ARCHIVE_NAME="${SAFE_NAME}_${TIMESTAMP}.tar.gz"
tar -czf "$ARCHIVE_NAME" -C strix_runs .

echo "Archive created: $ARCHIVE_NAME ($(du -h "$ARCHIVE_NAME" | cut -f1))"

# ── Upload to S3 ───────────────────────────────────────────────
if [ -z "${S3_BUCKET:-}" ]; then
  echo "Warning: S3_BUCKET not set, skipping upload"
  S3_URI="(not uploaded)"
else
  S3_KEY="strix-reports/${SAFE_NAME}/${TIMESTAMP}.tar.gz"
  S3_URI="s3://${S3_BUCKET}/${S3_KEY}"

  echo "Uploading to $S3_URI ..."
  aws s3 cp "$ARCHIVE_NAME" "$S3_URI" --region "$AWS_REGION"
  echo "Upload complete."
fi

# ── Extract summary ─────────────────────────────────────────────
REPORT_FILE=$(find "$RUN_DIR" -name "penetration_test_report.md" | head -1)
if [ -n "$REPORT_FILE" ]; then
  SUMMARY=$(head -80 "$REPORT_FILE")
else
  SUMMARY="No penetration_test_report.md found in run directory."
fi

# ── Send webhook ────────────────────────────────────────────────
if [ -z "${WEBHOOK_URL:-}" ]; then
  echo "Warning: WEBHOOK_URL not set, skipping notification"
else
  echo "Sending webhook notification..."

  # JSON-encode the summary
  SUMMARY_JSON=$(echo "$SUMMARY" | jq -Rs .)

  PAYLOAD=$(cat <<EOF
{
  "scanner": "strix",
  "target": "$TARGET",
  "scan_mode": "$SCAN_MODE",
  "timestamp": "$TIMESTAMP",
  "s3_report": "$S3_URI",
  "triggered_by": "local",
  "findings_summary": $SUMMARY_JSON
}
EOF
)

  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "$WEBHOOK_URL" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD")

  if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
    echo "Webhook sent successfully (HTTP $HTTP_CODE)"
  else
    echo "Warning: Webhook returned HTTP $HTTP_CODE"
  fi
fi

# ── Cleanup ─────────────────────────────────────────────────────
echo ""
echo "Done. Archive: $ARCHIVE_NAME"
echo "S3:      $S3_URI"