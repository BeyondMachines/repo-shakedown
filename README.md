# repo-shakedown

Orchestrates [Strix](https://github.com/usestrix/strix) penetration testing using [pit-boss](https://github.com/BeyondMachines/pit-boss) findings as targeting intelligence. Takes the "back room" of the Bada Bing security suite from passive analysis to active validation.

**Part of the Bada Bing security pipeline:**

| Tool | Role |
|------|------|
| [pr-bouncer](https://github.com/BeyondMachines/pr-bouncer) | Checks every PR at the door |
| [pit-boss](https://github.com/BeyondMachines/pit-boss) | Reviews the floor, flags trouble |
| **repo-shakedown** | Takes flagged repos to the back room — Strix-powered pentesting |
 
---

## How It Works

```
pit-boss candidates.json (weekly, stored on S3)
        │
   ┌────┴────┐
   │ prepare  │  Downloads candidates.json, clones repos, generates focused
   └────┬────┘  Strix instruction files. Skips repos already scanned this month.
        │       One task per qualifying repo → tasks.json
        │
   ┌────┴────┐
   │  scan   │  Picks the next pending task, runs Strix with pit-boss-guided
   └────┬────┘  instructions. One repo per invocation.
        │
   ┌────┴────┐
   │ report  │  LLM summarizes findings → saved locally + uploaded to S3.
   └─────────┘  Optional: Slack notification + Jira ticket.
                Marks repo as scanned this month in S3-backed tracking file.
```

### The Intelligence Bridge

pit-boss already knows *what* to look for and *where*. Instead of giving Strix a vague "scan this repo", repo-shakedown generates a focused instruction file per repo that includes:

- LLM-generated scan narrative and urgency rating
- Specific attack instructions for identified vulnerability patterns
- Priority files with the most findings
- Known existing issues with severity
- Existing technical debt notes
- Risk assessment if findings are ignored

This keeps Strix focused and produces results directly actionable by human pentesters.

---

## Using as a GitHub Action

The simplest way to run repo-shakedown is as a reusable composite action. Copy the example workflow into your org's repo:

```yaml
# .github/workflows/security-scan.yml
name: Security Scan

on:
  schedule:
    - cron: "0 */6 * * *"   # every 6 hours
  workflow_dispatch:

jobs:
  scan:
    runs-on: ubuntu-latest
    timeout-minutes: 300
    steps:
      - uses: beyondmachines/repo-shakedown@v1
        with:
          # AWS / S3 — used for pit-boss data, report uploads, and monthly tracking
          aws_access_key_id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws_secret_access_key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          s3_bucket: ${{ secrets.S3_BUCKET }}

          # LLM — set whichever key matches your provider
          gemini_api_key: ${{ secrets.GEMINI_API_KEY }}

          # Notifications (optional)
          enable_slack: 'true'
          slack_webhook_url: ${{ secrets.SLACK_WEBHOOK_URL }}
          enable_jira: 'true'
          jira_base_url: ${{ secrets.JIRA_BASE_URL }}
          jira_email: ${{ secrets.JIRA_EMAIL }}
          jira_api_token: ${{ secrets.JIRA_API_TOKEN }}

          # GitHub PAT for scanning private repos (optional)
          repo_clone_pat: ${{ secrets.REPO_CLONE_PAT }}
```

Each action run scans **exactly one repo** from the current pit-boss recommendations, then stops. Repos already scanned in the current calendar month are automatically skipped (tracked in `{s3_reports_prefix}/scanned_repos.json`). Run the action on a schedule and it will work through all recommendations over the course of the week.

See [`.github/workflows/example-caller.yml`](.github/workflows/example-caller.yml) for the full example with all available inputs documented.

### Action Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `pitboss_s3_prefix` | No | auto-detect | S3 prefix for candidates.json files. Defaults to `pitboss-snapshots/YYYY-MM/` |
| `pitboss_json` | No | — | Local path to candidates.json (for testing without S3) |
| `repos_dir` | No | `./repos` | Directory for cloned target repos |
| `auto_clone` | No | `true` | Clone missing repos automatically |
| `reprocess` | No | `false` | Re-ingest pit-boss snapshots already processed |
| `llm` | No | `gemini/gemini-2.5-pro` | LLM model for scanning and summarisation |
| `gemini_api_key` | * | — | API key for `gemini/*` models |
| `openai_api_key` | * | — | API key for `openai/*` models |
| `anthropic_api_key` | * | — | API key for `anthropic/*` models |
| `llm_api_key` | * | — | Generic fallback API key |
| `aws_access_key_id` | No | — | AWS credentials for S3 |
| `aws_secret_access_key` | No | — | AWS credentials for S3 |
| `aws_region` | No | `us-east-1` | AWS region |
| `s3_bucket` | No | — | S3 bucket for pit-boss data, reports, and monthly tracking |
| `s3_reports_prefix` | No | `shakedown-reports/` | S3 prefix for report uploads and tracking file |
| `enable_slack` | No | `false` | Send Slack notification on completion |
| `slack_webhook_url` | No | — | Slack incoming webhook URL |
| `enable_jira` | No | `false` | Create a Jira ticket for each scan result |
| `jira_base_url` | No | — | Jira instance URL |
| `jira_project_key` | No | `SEC` | Jira project key |
| `jira_email` | No | — | Jira auth email |
| `jira_api_token` | No | — | Jira API token |
| `repo_clone_pat` | No | — | GitHub PAT for cloning private target repos |

\* Set whichever key matches your chosen LLM provider.

---

## Quick Start (Local)

### Prerequisites

- Docker (running) — required by Strix
- Strix installed: `curl -sSL https://strix.ai/install | bash`
- An API key for your chosen LLM provider
- AWS credentials configured (for S3 access)

### Install

```bash
git clone https://github.com/BeyondMachines/repo-shakedown.git
cd repo-shakedown
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your values
```

### Local Run (all-in-one)

The `run` command scans **all** pending repos in one shot. Use `run-one` for CI or when you want to scan a single repo and stop.

```bash
# Scan all candidates from a specific week
python repo_shakedown.py run \
  --s3-prefix shakedown/2026-04-d01-07/ \
  --repos-dir ./repos \
  --auto-clone

# Scan with a specific LLM
python repo_shakedown.py run \
  --s3-prefix shakedown/2026-04-d01-07/ \
  --repos-dir ./repos \
  --auto-clone \
  --llm "anthropic/claude-sonnet-4-6"

# Re-scan a week that was already processed
python repo_shakedown.py run \
  --s3-prefix shakedown/2026-04-d01-07/ \
  --repos-dir ./repos \
  --auto-clone \
  --reprocess
```

### Step-by-Step (cron / manual)

```bash
# 1. Build tasks from pit-boss output (clones missing repos, applies monthly dedup)
python repo_shakedown.py prepare \
  --s3-prefix shakedown/2026-04-d01-07/ \
  --repos-dir ./repos \
  --auto-clone

# 2. Check the queue
python repo_shakedown.py status

# 3. Run one scan (call repeatedly or schedule on cron)
python repo_shakedown.py scan

# 4. Generate reports for completed scans
python repo_shakedown.py report
```

---

## Monthly Deduplication

repo-shakedown tracks which repos have been scanned in the current calendar month. If pit-boss recommends the same repo in two weekly snapshots within the same month, the second recommendation is silently skipped during `prepare`.

Tracking is stored in:
- **S3** (preferred): `s3://{S3_BUCKET}/{S3_REPORTS_PREFIX}/scanned_repos.json`
- **Local fallback**: `{SHAKEDOWN_WORK_DIR}/scanned_repos.json` (when `S3_BUCKET` is not set)

The file is read once per process and cached in memory. Structure:

```json
{
  "2026-04": ["org/repo-a", "org/repo-b"],
  "2026-05": ["org/repo-c"]
}
```

Old month entries persist and are never pruned — they serve as a permanent audit log. Use `--reprocess` to force a repo back into the queue even if it was scanned this month.

---

## S3 Data Layout

pit-boss stores its output on S3 in weekly folders:

```
s3://{S3_BUCKET}/
├── shakedown/
│   ├── 2026-03-d20-26/
│   │   └── candidates.json
│   └── 2026-04-d01-07/
│       └── candidates.json
│
└── shakedown-reports/                              ← S3_REPORTS_PREFIX
    ├── scanned_repos.json                          ← monthly dedup tracking
    ├── VATBox__ConcurRent__1744123456_0_report.md
    └── VATBox__inspect-manager__1744123457_1_report.md
```

Pass `--s3-prefix shakedown/` to pick up all weeks, or `--s3-prefix shakedown/2026-04-d01-07/` to pin to one week.

---

## LLM Configuration

The model is resolved in this order:

1. `--llm` CLI flag (highest priority)
2. `STRIX_LLM` environment variable
3. Default: `gemini/gemini-2.5-pro`

The tool uses LiteLLM's `provider/model` format. The summarizer for report generation supports both google-genai (for `gemini/*` models) and litellm (for everything else).

| Provider | Model string | API key env var |
|----------|-------------|-----------------|
| Google Gemini | `gemini/gemini-2.5-pro` | `GEMINI_API_KEY` |
| OpenAI | `openai/gpt-4o` | `OPENAI_API_KEY` |
| Anthropic | `anthropic/claude-sonnet-4-6` | `ANTHROPIC_API_KEY` |
| Vertex AI | `vertex_ai/gemini-2.5-pro-preview` | `gcloud auth` (no key) |
| AWS Bedrock | `bedrock/anthropic.claude-...` | AWS credentials |
| Ollama | `ollama/llama4` | None (local) |
| Any provider | any LiteLLM model string | `LLM_API_KEY` |

Set `SUMMARIZER_LLM` separately if you want the report summarizer to use a different (cheaper) model than Strix uses for scanning.

---

## Strix Reasoning Effort

Strix thinking depth is controlled via `STRIX_REASONING_EFFORT`:

| Value | Thinking effort | When used |
|-------|----------------|-----------|
| `high` | Full reasoning (default) | All repos unless overridden |
| `medium` | Reduced reasoning | Repos pit-boss marks as `quick` |
| `quick` | Minimal reasoning | Fast CI-style passes |

pit-boss sets `suggested_scan_mode` per repo (`default` or `deep`). Both map to `high` effort. Override the default for all tasks by setting `STRIX_REASONING_EFFORT` in `.env`.

---

## CLI Reference

### Global flag

| Flag | Description |
|------|-------------|
| `--llm MODEL` | Override LLM for this run (e.g. `anthropic/claude-sonnet-4-6`) |

### `run-one`

**CI / GitHub Action mode.** Runs prepare (with monthly dedup) → scans exactly one repo → reports. Designed to be called once per action run or cron tick. Exits 0 cleanly when there are no pending tasks (all repos already scanned this month).

```bash
python repo_shakedown.py run-one \
  --s3-prefix shakedown/2026-04-d01-07/ \
  --repos-dir ./repos \
  --auto-clone
```

| Flag | Required | Description |
|------|----------|-------------|
| `--pitboss-json` | * | Path(s) to local candidates.json file(s) |
| `--s3-prefix` | * | S3 prefix to scan for candidates.json files |
| `--repos-dir` | Yes | Directory to store cloned repositories |
| `--auto-clone` | No | Clone missing repos automatically |
| `--reprocess` | No | Ignore tracking — reprocess already-seen files |

\* One of `--pitboss-json` or `--s3-prefix` is required (mutually exclusive).

### `run`

**All-in-one local mode.** Runs prepare → scans **all** pending repos → reports in sequence. Monthly dedup still applies during prepare.

Same flags as `run-one`.

### `prepare`

Downloads pit-boss data and builds the task queue. Does not run scans. Applies both snapshot-level dedup (`processed_sources.json`) and monthly repo-level dedup (`scanned_repos.json`).

```bash
python repo_shakedown.py prepare \
  --s3-prefix shakedown/2026-04-d01-07/ \
  --repos-dir ./repos \
  --auto-clone
```

### `scan`

Picks the next pending task and runs Strix. Calls `report` automatically on completion. Designed to be called on a cron schedule.

| Flag | Default | Description |
|------|---------|-------------|
| `--force-reset` | off | Reset stuck `running` tasks back to `pending` |

### `report`

Generates LLM-powered triage reports for completed scans that haven't been reported yet. Saves locally and uploads to S3 if `S3_REPORTS_PREFIX` is set. Sends Slack + Jira notifications if configured, otherwise prints to console. Marks each reported repo as scanned this month.

### `status`

Shows current queue status with risk scores, reasoning effort, and LLM used per task.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `S3_BUCKET` | Yes (for S3 mode) | S3 bucket for pit-boss data, report uploads, and monthly tracking |
| `STRIX_LLM` | No | LLM model (default: `gemini/gemini-2.5-pro`) |
| `GEMINI_API_KEY` | * | API key for `gemini/*` models |
| `OPENAI_API_KEY` | * | API key for `openai/*` models |
| `ANTHROPIC_API_KEY` | * | API key for `anthropic/*` models |
| `LLM_API_KEY` | * | Generic fallback API key |
| `SUMMARIZER_LLM` | No | Separate model for report summariser |
| `STRIX_REASONING_EFFORT` | No | `high`, `medium`, or `quick` (default: `high`) |
| `S3_REPORTS_PREFIX` | No | S3 prefix for report uploads and `scanned_repos.json` (e.g. `shakedown-reports/`) |
| `SHAKEDOWN_WORK_DIR` | No | Working directory (default: `./shakedown-work`) |
| `SLACK_WEBHOOK_URL` | No | Slack incoming webhook |
| `JIRA_BASE_URL` | No | Jira instance URL |
| `JIRA_PROJECT_KEY` | No | Jira project key (default: `SEC`) |
| `JIRA_EMAIL` | No | Jira auth email |
| `JIRA_API_TOKEN` | No | Jira API token |

\* Set whichever key matches your chosen LLM provider.

AWS credentials are picked up from the standard boto3 chain: `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_DEFAULT_REGION`, an AWS profile, or an instance role.

---

## Repo Cloning

For local use, repos are cloned via `git clone` with HTTPS URLs (`https://github.com/org/repo.git`). Configure credentials before running:

```bash
# SSH key (recommended for local use)
gh auth login

# HTTPS token (required in CI / GitHub Actions)
git config --global \
  url."https://x-access-token:${GITHUB_TOKEN}@github.com/".insteadOf \
  "https://github.com/"
```

In the GitHub Action, pass a PAT via the `repo_clone_pat` input — the action configures git automatically.

---

## Notification Fallbacks

The tool is fully usable without any external integrations:

| Integration | If configured | If not configured |
|-------------|--------------|-------------------|
| S3 reports | Uploads report to S3 | Saved locally only |
| Monthly tracking | Stored in S3 (`scanned_repos.json`) | Stored locally in `shakedown-work/` |
| Slack | Sends webhook notification | Prints formatted report to console |
| Jira | Creates ticket with findings | Prints ticket title/priority to console |
| LLM summariser | AI-powered triage report | Basic deterministic summary |

---

## Task Lifecycle

```
pending → running → done
                  → failed
```

Each `scan` invocation processes exactly one task. `run-one` ensures only one task is picked per action run. `run` loops until the queue is empty. After a task reaches `done`, the repo is recorded in `scanned_repos.json` so it is skipped for the rest of the calendar month.

---

## Recommended Schedule

| Mode | Cron | Purpose |
|------|------|---------|
| GitHub Action (`run-one`) | `0 */6 * * *` | Scan one repo every 6 hours, cycling through all monthly recommendations |
| Self-hosted prepare | `0 7 * * 1` | Build task queue Monday morning |
| Self-hosted scan | `0 */4 * * *` | Process one task every 4 hours |

---

## Output Structure

```
shakedown-work/
├── tasks.json                              ← Task queue with status, effort, LLM used
├── processed_sources.json                  ← Tracks which candidates.json files were ingested
├── scanned_repos.json                      ← Monthly repo dedup (fallback when no S3)
├── s3-downloads/                           ← Cached S3 downloads
├── instructions/
│   ├── VATBox__ConcurRent__1744123456_0.md ← Generated Strix instruction files
│   └── VATBox__inspect-manager__...md
├── results/
│   ├── VATBox__ConcurRent__1744123456_0/   ← Copied Strix outputs
│   └── ...
└── reports/
    ├── VATBox__ConcurRent__1744123456_0_report.md  ← Triage reports
    └── ...
```

---

## License

MIT
