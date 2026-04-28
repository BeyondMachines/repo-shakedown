#!/usr/bin/env bash
#
# scan.sh — Run a Strix security scan locally
#
# Usage:
#   ./scripts/scan.sh <target_repo_url> [scan_mode]
#
# Examples:
#   ./scripts/scan.sh https://github.com/org/repo
#   ./scripts/scan.sh https://github.com/org/repo quick
#   ./scripts/scan.sh ./local-app-directory deep
#
set -euo pipefail

# ── Colors ──────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# ── Parse args ──────────────────────────────────────────────────
TARGET="${1:-}"
SCAN_MODE="${2:-quick}"

if [ -z "$TARGET" ]; then
  echo -e "${RED}Error: target is required${NC}"
  echo ""
  echo "Usage: $0 <target_repo_or_url> [scan_mode]"
  echo ""
  echo "  target       GitHub repo URL, local path, or web app URL"
  echo "  scan_mode    quick | standard | deep  (default: quick)"
  echo ""
  echo "Examples:"
  echo "  $0 https://github.com/org/repo"
  echo "  $0 ./my-app quick"
  echo "  $0 https://my-app.com deep"
  exit 1
fi

# ── Load .env if present ────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

if [ -f "$REPO_ROOT/.env" ]; then
  echo -e "${CYAN}Loading .env...${NC}"
  set -a
  source "$REPO_ROOT/.env"
  set +a
fi

# ── Validate prerequisites ──────────────────────────────────────
echo -e "${CYAN}Checking prerequisites...${NC}"

# Docker
if ! command -v docker &>/dev/null; then
  echo -e "${RED}Error: Docker is not installed or not in PATH${NC}"
  echo "Strix requires Docker to run its sandbox. Install: https://docs.docker.com/get-docker/"
  exit 1
fi

# Auto-detect Docker socket if DOCKER_HOST isn't set
# (Strix's Python docker library doesn't find macOS sockets automatically)
if [ -z "${DOCKER_HOST:-}" ]; then
  for sock in \
    "$HOME/.docker/run/docker.sock" \
    "$HOME/.colima/default/docker.sock" \
    "$HOME/.rd/docker.sock" \
    "/var/run/docker.sock"; do
    if [ -S "$sock" ]; then
      export DOCKER_HOST="unix://$sock"
      echo -e "${CYAN}Auto-detected Docker socket: $sock${NC}"
      break
    fi
  done
fi

if ! docker info &>/dev/null 2>&1; then
  echo -e "${RED}Error: Docker daemon is not running${NC}"
  echo "Start Docker Desktop or the Docker service, then try again."
  exit 1
fi

# Strix
if ! command -v strix &>/dev/null; then
  echo -e "${YELLOW}Strix not found. Installing...${NC}"
  curl -sSL https://strix.ai/install | bash
  export PATH="$HOME/.local/bin:$PATH"
fi

# Gemini API key
if [ -z "${GEMINI_API_KEY:-}" ]; then
  echo -e "${RED}Error: GEMINI_API_KEY is not set${NC}"
  echo "Get one at https://aistudio.google.com/apikey"
  echo "Then: export GEMINI_API_KEY=your-key"
  exit 1
fi

# ── Configure Strix for Gemini ──────────────────────────────────
export STRIX_LLM="${STRIX_LLM:-gemini/gemini-2.5-pro}"
export LLM_API_KEY="$GEMINI_API_KEY"

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Strix Security Scan${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════${NC}"
echo -e "  Target:     ${CYAN}$TARGET${NC}"
echo -e "  Scan mode:  ${CYAN}$SCAN_MODE${NC}"
echo -e "  LLM:        ${CYAN}$STRIX_LLM${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════${NC}"
echo ""

# ── Clone if GitHub URL (ensures we scan the EXACT repo) ───────
LOCAL_TARGET="$TARGET"
CLONE_DIR=""

if echo "$TARGET" | grep -qE '^https?://github\.com/'; then
  CLONE_DIR=$(mktemp -d)
  REPO_NAME=$(basename "$TARGET" .git)
  echo -e "${CYAN}Cloning $TARGET to ensure exact repo is scanned...${NC}"
  git clone --depth 1 "$TARGET" "$CLONE_DIR/$REPO_NAME"
  LOCAL_TARGET="$CLONE_DIR/$REPO_NAME"
  echo -e "${GREEN}Cloned to $LOCAL_TARGET${NC}"
fi

# ── Run the scan ────────────────────────────────────────────────
set +e
strix \
  --non-interactive \
  --target "$LOCAL_TARGET" \
  --scan-mode "$SCAN_MODE"
EXIT_CODE=$?
set -e

# ── Cleanup clone ───────────────────────────────────────────────
if [ -n "$CLONE_DIR" ]; then
  rm -rf "$CLONE_DIR"
fi

# ── Report location ────────────────────────────────────────────
echo ""
if [ $EXIT_CODE -ne 0 ]; then
  echo -e "${YELLOW}Scan completed with exit code $EXIT_CODE (vulnerabilities may have been found)${NC}"
else
  echo -e "${GREEN}Scan completed successfully (no vulnerabilities found)${NC}"
fi

# Find the latest run directory
RUN_DIR=$(ls -td strix_runs/*/ 2>/dev/null | head -1)
if [ -n "$RUN_DIR" ]; then
  echo -e "Report directory: ${CYAN}$RUN_DIR${NC}"

  REPORT_FILE=$(find "$RUN_DIR" -name "penetration_test_report.md" | head -1)
  if [ -n "$REPORT_FILE" ]; then
    echo -e "Full report:      ${CYAN}$REPORT_FILE${NC}"
  fi
fi

# ── Optional: upload to S3 + send webhook ───────────────────────
if [ -n "${S3_BUCKET:-}" ] && [ -n "${AWS_ACCESS_KEY_ID:-}" ]; then
  echo ""
  echo -e "${CYAN}Uploading report to S3 and sending webhook...${NC}"
  "$SCRIPT_DIR/upload-and-notify.sh" "$TARGET" "$SCAN_MODE"
else
  echo ""
  echo -e "${YELLOW}Tip: Set S3_BUCKET and AWS credentials in .env to auto-upload reports${NC}"
  echo -e "${YELLOW}Tip: Set WEBHOOK_URL in .env to get notifications${NC}"
fi

exit $EXIT_CODE