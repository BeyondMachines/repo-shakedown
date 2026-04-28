#!/usr/bin/env python3
"""
repo-shakedown — Orchestrates Strix penetration testing from pit-boss findings.

Commands:
  run-one  (CI/action) — prepare + scan one repo + report (skip if scanned this month)
  run      (local)     — all-in-one: prepare + clone repos + scan all + report
  prepare  (weekly)    — reads pit-boss candidates.json, builds task queue
  scan     (every 4h)  — picks the next pending task, runs Strix headless
  report               — generates reports for completed scans
  status               — shows queue status

Usage:
    # CI / GitHub Action: scan one repo, then stop (monthly dedup applies)
    python repo_shakedown.py run-one \
        --s3-prefix shakedown/2026-04-d01-07/ \
        --repos-dir ./repos \
        --auto-clone

    # All-in-one local run (recommended for manual use)
    python repo_shakedown.py run \
        --s3-prefix shakedown/2026-04-d01-07/ \
        --repos-dir ./repos \
        --auto-clone

    # Build tasks only (no scanning)
    python repo_shakedown.py prepare \
        --s3-prefix shakedown/2026-04-d01-07/ \
        --repos-dir ./repos \
        --auto-clone

    # Run the next pending scan
    python repo_shakedown.py scan

    # Run with a specific LLM override
    python repo_shakedown.py scan --llm "anthropic/claude-sonnet-4-6"

    # Force-reset a stuck "running" task
    python repo_shakedown.py scan --force-reset

    # Report on completed scans
    python repo_shakedown.py report

    # Check queue
    python repo_shakedown.py status

LLM configuration (checked in this order):
    1. --llm CLI flag
    2. STRIX_LLM environment variable
    3. Default: gemini/gemini-2.5-pro

LLM API key (set whichever matches your provider):
    GEMINI_API_KEY       — for gemini/* models
    LLM_API_KEY          — generic (works for most providers via LiteLLM)
    OPENAI_API_KEY       — for openai/* models
    ANTHROPIC_API_KEY    — for anthropic/* models

Required env vars:
    S3_BUCKET                — S3 bucket name (pit-boss data, report uploads, monthly tracking)

Optional env vars:
    STRIX_REASONING_EFFORT   — high, medium, quick (default: high)
    S3_REPORTS_PREFIX        — S3 prefix for report uploads and scan tracking (e.g. shakedown-reports/)
    SHAKEDOWN_WORK_DIR       — Working directory (default: ./shakedown-work)
    SUMMARIZER_LLM           — Model for the report summarizer (default: same as STRIX_LLM)
    SLACK_WEBHOOK_URL        — Slack incoming webhook (optional)
    JIRA_BASE_URL            — Jira instance URL (optional)
    JIRA_PROJECT_KEY         — Jira project key (default: SEC)
    JIRA_EMAIL               — Jira auth email (optional)
    JIRA_API_TOKEN           — Jira API token (optional)

Monthly deduplication:
    scanned_repos.json is stored in S3 (at S3_REPORTS_PREFIX/scanned_repos.json) when
    S3_BUCKET is configured, otherwise falls back to SHAKEDOWN_WORK_DIR/scanned_repos.json.
    repos already scanned in the current calendar month are skipped by prepare/run-one.

Repo cloning:
    Uses git clone with HTTPS URLs (https://github.com/org/repo.git).
    For private repos, ensure your git credentials are configured (e.g. via SSH keys or a credential helper).
"""

import argparse
import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

# Load .env from repo root (local dev) or current directory
_env_candidates = [
    Path(__file__).resolve().parent.parent / ".env",  # repo root
    Path(__file__).resolve().parent / ".env",          # script dir
    Path.cwd() / ".env",                               # current dir
]
for _env_file in _env_candidates:
    if _env_file.exists():
        load_dotenv(_env_file)
        break

# ── Configuration ────────────────────────────────────────────────

WORK_DIR = Path(os.environ.get("SHAKEDOWN_WORK_DIR", "./shakedown-work"))
TASKS_FILE = WORK_DIR / "tasks.json"
INSTRUCTIONS_DIR = WORK_DIR / "instructions"
RESULTS_DIR = WORK_DIR / "results"
REPORTS_DIR = WORK_DIR / "reports"

DEFAULT_LLM = "gemini/gemini-2.5-pro"
REASONING_EFFORT = os.environ.get("STRIX_REASONING_EFFORT", "high")

S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_REPORTS_PREFIX = os.environ.get("S3_REPORTS_PREFIX", "")
PROCESSED_FILE = WORK_DIR / "processed_sources.json"

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "")
JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "SEC")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")

SCANNED_REPOS_LOCAL = WORK_DIR / "scanned_repos.json"
_scanned_repos_cache: Optional[Dict[str, List[str]]] = None


def resolve_llm(cli_llm: Optional[str] = None) -> str:
    """Resolve LLM model string. Priority: CLI flag > env > default."""
    if cli_llm:
        return cli_llm
    return os.environ.get("STRIX_LLM", DEFAULT_LLM)


def resolve_summarizer_llm(cli_llm: Optional[str] = None) -> str:
    """
    Resolve the model used for the Gemini summarizer (report generation).
    Falls back to SUMMARIZER_LLM env, then the strix LLM, then default.
    """
    env_summarizer = os.environ.get("SUMMARIZER_LLM", "")
    if env_summarizer:
        return env_summarizer
    return resolve_llm(cli_llm)


def resolve_api_key(llm_model: str) -> str:
    """
    Resolve the API key for the given LLM model string.
    Checks provider-specific env vars first, then generic LLM_API_KEY.
    """
    provider = llm_model.split("/")[0].lower() if "/" in llm_model else ""

    if provider == "gemini":
        return os.environ.get("GEMINI_API_KEY", os.environ.get("LLM_API_KEY", ""))
    elif provider == "openai":
        return os.environ.get("OPENAI_API_KEY", os.environ.get("LLM_API_KEY", ""))
    elif provider == "anthropic":
        return os.environ.get("ANTHROPIC_API_KEY", os.environ.get("LLM_API_KEY", ""))
    elif provider in ("vertex_ai", "bedrock", "azure"):
        # These use cloud auth, not API keys
        return os.environ.get("LLM_API_KEY", "")
    else:
        return os.environ.get("LLM_API_KEY", "")


# ── Repo Cloning ─────────────────────────────────────────────────

def normalize_reasoning_effort(suggested_scan_mode: str) -> str:
    """Map pit-boss suggested_scan_mode to STRIX_REASONING_EFFORT value."""
    if suggested_scan_mode == "quick":
        return "medium"
    # "default", "deep", or anything else → highest effort
    return "high"


def clone_repo(repo: str, repos_dir: Path) -> Optional[Path]:
    """
    Clone a GitHub repo using git.
    repo is 'org/repo' format (from candidates.json 'repo' field).
    Returns local Path on success, None on failure.
    """
    repo_name = repo.split("/")[-1]
    dest = repos_dir / repo_name
    if dest.exists():
        return dest

    repos_dir.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        ["git", "clone", "--depth", "1", f"https://github.com/{repo}.git", str(dest)],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return dest
    print(f"  ❌ Clone failed for {repo}: {result.stderr.strip()}")
    return None


# ── Task Queue ───────────────────────────────────────────────────

def load_tasks() -> List[Dict]:
    if TASKS_FILE.exists():
        return json.loads(TASKS_FILE.read_text())
    return []


def save_tasks(tasks: List[Dict]):
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TASKS_FILE.write_text(json.dumps(tasks, indent=2))


def find_next_pending(tasks: List[Dict]) -> Optional[Dict]:
    for t in tasks:
        if t["status"] == "pending":
            return t
    return None


def update_task_status(tasks: List[Dict], task_id: str, status: str, **extra):
    for t in tasks:
        if t["id"] == task_id:
            t["status"] = status
            t["updated_at"] = datetime.now(timezone.utc).isoformat()
            t.update(extra)
            break
    save_tasks(tasks)


# ── Source Tracking (avoid reprocessing) ──────────────────────

def load_processed_sources() -> Dict[str, str]:
    """
    Returns dict of source_key → timestamp for files already processed.
    source_key is an S3 key or a local file hash.
    """
    if PROCESSED_FILE.exists():
        return json.loads(PROCESSED_FILE.read_text())
    return {}


def save_processed_sources(processed: Dict[str, str]):
    PROCESSED_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROCESSED_FILE.write_text(json.dumps(processed, indent=2))


def mark_source_processed(source_key: str):
    processed = load_processed_sources()
    processed[source_key] = datetime.now(timezone.utc).isoformat()
    save_processed_sources(processed)


def is_source_processed(source_key: str) -> bool:
    return source_key in load_processed_sources()


def _local_file_key(filepath: Path) -> str:
    """Generate a stable key for a local file based on path + size + mtime."""
    stat = filepath.stat()
    return f"local::{filepath.resolve()}::size={stat.st_size}::mtime={int(stat.st_mtime)}"


# ── Monthly Scanned-Repo Tracking ────────────────────────────

def _get_month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _get_scanned_repos_s3_key() -> str:
    if S3_REPORTS_PREFIX:
        return f"{S3_REPORTS_PREFIX.rstrip('/')}/scanned_repos.json"
    return "shakedown/scanned_repos.json"


def _load_scanned_repos() -> Dict[str, List[str]]:
    """Load monthly scanned-repo index. Reads from S3 if configured, else local file.
    Result is cached in-process so S3 is hit at most once per run."""
    global _scanned_repos_cache
    if _scanned_repos_cache is not None:
        return _scanned_repos_cache

    if S3_BUCKET:
        try:
            import boto3
            s3 = boto3.client("s3")
            key = _get_scanned_repos_s3_key()
            obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
            _scanned_repos_cache = json.loads(obj["Body"].read().decode())
            return _scanned_repos_cache
        except Exception as e:
            err = str(e)
            if "NoSuchKey" not in err and "404" not in err and "does not exist" not in err:
                print(f"  ⚠️  Could not load scanned_repos from S3: {e}")
            _scanned_repos_cache = {}
            return _scanned_repos_cache

    if SCANNED_REPOS_LOCAL.exists():
        _scanned_repos_cache = json.loads(SCANNED_REPOS_LOCAL.read_text())
    else:
        _scanned_repos_cache = {}
    return _scanned_repos_cache


def _save_scanned_repos(data: Dict[str, List[str]]):
    """Persist monthly scanned-repo index to S3 (preferred) or local file."""
    global _scanned_repos_cache
    _scanned_repos_cache = data
    payload = json.dumps(data, indent=2).encode()

    if S3_BUCKET:
        try:
            import boto3
            s3 = boto3.client("s3")
            key = _get_scanned_repos_s3_key()
            s3.put_object(Bucket=S3_BUCKET, Key=key, Body=payload,
                          ContentType="application/json")
            print(f"  ☁️  Monthly tracking saved: s3://{S3_BUCKET}/{key}")
            return
        except Exception as e:
            print(f"  ⚠️  Could not save scanned_repos to S3: {e}")

    SCANNED_REPOS_LOCAL.parent.mkdir(parents=True, exist_ok=True)
    SCANNED_REPOS_LOCAL.write_bytes(payload)
    print(f"  💾 Monthly tracking saved: {SCANNED_REPOS_LOCAL}")


def _is_repo_scanned_this_month(repo: str) -> bool:
    return repo in _load_scanned_repos().get(_get_month_key(), [])


def _mark_repo_scanned_this_month(repo: str):
    data = _load_scanned_repos()
    monthly = data.setdefault(_get_month_key(), [])
    if repo not in monthly:
        monthly.append(repo)
        _save_scanned_repos(data)


# ── S3 Loading for Prepare ───────────────────────────────────

def load_pitboss_files_from_s3(s3_prefix: str) -> List[Dict]:
    """
    Download all pit-boss JSON files under an S3 prefix.
    Returns list of dicts: {s3_key, data, local_path}.
    Skips files already processed.
    """
    try:
        import boto3
    except ImportError:
        print("  ❌ boto3 not installed. pip install boto3")
        print("     Or use --pitboss-json with a local file instead.")
        return []

    bucket = S3_BUCKET
    s3 = boto3.client("s3")

    print(f"  Listing s3://{bucket}/{s3_prefix} ...")
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=s3_prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".json"):
                keys.append(obj["Key"])

    print(f"  Found {len(keys)} JSON files")

    results = []
    download_dir = WORK_DIR / "s3-downloads"
    download_dir.mkdir(parents=True, exist_ok=True)

    for key in sorted(keys):
        if is_source_processed(f"s3::{key}"):
            print(f"  ⏭️  Already processed: {key}")
            continue

        local_path = download_dir / key.replace("/", "__")
        try:
            s3.download_file(bucket, key, str(local_path))
            data = json.loads(local_path.read_text())
            results.append({
                "s3_key": key,
                "source_key": f"s3::{key}",
                "data": data,
                "local_path": local_path,
            })
            print(f"  ✓ Downloaded: {key}")
        except Exception as e:
            print(f"  ⚠️  Failed to download {key}: {e}")

    return results


def load_pitboss_files_local(file_paths: List[str]) -> List[Dict]:
    """
    Load one or more local pit-boss JSON files.
    Returns list of dicts: {source_key, data, local_path}.
    Skips files already processed.
    """
    results = []
    for fp in file_paths:
        path = Path(fp)
        if not path.exists():
            print(f"  ⚠️  File not found: {path}")
            continue

        source_key = _local_file_key(path)
        if is_source_processed(source_key):
            print(f"  ⏭️  Already processed: {path.name}")
            continue

        try:
            data = json.loads(path.read_text())
            results.append({
                "source_key": source_key,
                "data": data,
                "local_path": path,
            })
            print(f"  ✓ Loaded: {path}")
        except Exception as e:
            print(f"  ⚠️  Failed to parse {path}: {e}")

    return results


# ── Phase 1: Prepare ─────────────────────────────────────────────

def extract_tasks_from_pitboss(
    pitboss_data: Dict,
    repos_dir: Path,
    auto_clone: bool = False,
    threshold: int = 5,
) -> List[Dict]:
    """
    Parse pit-boss repo_risk output and produce one task per repo above threshold.
    """
    tasks = []
    repo_risk = pitboss_data.get("repo_risk", {})

    for i, (repo, repo_entry) in enumerate(repo_risk.items()):
        if not repo:
            continue

        max_risk = repo_entry.get("max_risk", 0)
        max_existing = repo_entry.get("max_existing_risk", 0)
        total_criticals = repo_entry.get("new_critical_count", 0)
        override_count = repo_entry.get("override_count", 0)

        if max_risk < threshold:
            continue

        repo_url = f"https://github.com/{repo}"

        # Derive scan depth from risk score
        if max_risk >= 7:
            scan_mode = "deep"
        elif max_risk >= 4:
            scan_mode = "default"
        else:
            scan_mode = "quick"

        # Resolve local repo path — try several naming conventions
        repo_name = repo.split("/")[-1] if "/" in repo else repo
        repo_path = repos_dir / repo_name
        if not repo_path.exists():
            repo_path = repos_dir / repo.replace("/", "__")
        if not repo_path.exists():
            repo_path = repos_dir / repo.replace("/", "-")
        if not repo_path.exists():
            if auto_clone:
                cloned = clone_repo(repo, repos_dir)
                if not cloned:
                    print(f"  ❌ Failed to clone {repo} — skipping")
                    continue
                repo_path = cloned
                print(f"  🔁 Cloned {repo} → {repo_path}")
            else:
                print(f"  ⚠️  Repo not found at {repos_dir}/{repo_name} — skipping {repo}")
                continue

        reasoning_effort = normalize_reasoning_effort(scan_mode)
        instruction_content = generate_instruction_file(repo, repo_entry)

        task_id = f"{repo.replace('/', '__')}__{int(time.time())}_{i}"
        instruction_path = INSTRUCTIONS_DIR / f"{task_id}.md"
        instruction_path.parent.mkdir(parents=True, exist_ok=True)
        instruction_path.write_text(instruction_content)

        tasks.append({
            "id": task_id,
            "repo": repo,
            "repo_url": repo_url,
            "repo_path": str(repo_path.resolve()),
            "instruction_file": str(instruction_path.resolve()),
            "reasoning_effort": reasoning_effort,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "max_risk": max_risk,
            "max_existing_risk": max_existing,
            "critical_count": total_criticals,
            "override_count": override_count,
            "strix_run_dir": None,
            "strix_exit_code": None,
            "report_file": None,
        })

    return tasks


def generate_instruction_file(repo: str, repo_entry: Dict) -> str:
    """
    Generate a focused Strix instruction file from pit-boss repo_risk data.
    """
    lines = []

    max_risk = repo_entry.get("max_risk", 0)
    max_existing = repo_entry.get("max_existing_risk", 0)
    total_prs = repo_entry.get("total_prs", 0)
    new_critical = repo_entry.get("new_critical_count", 0)

    lines.append(f"# Penetration Test Instructions — {repo}")
    lines.append("")
    lines.append("## Risk Summary")
    lines.append("")
    lines.append(f"- Max new risk score: {max_risk}/10")
    lines.append(f"- Max existing risk score: {max_existing}/10")
    lines.append(f"- New critical issues: {new_critical}")
    lines.append(f"- PRs reviewed: {total_prs}")
    lines.append("")

    # Recent new issues flagged during PR reviews
    top_new = repo_entry.get("top_new_issues", [])
    if top_new:
        lines.append("## Recent New Issues (from PR reviews)")
        lines.append("")
        lines.append("These vulnerabilities were introduced in recent PRs — investigate further:")
        lines.append("")
        for issue in top_new:
            title = issue.get("title", issue) if isinstance(issue, dict) else issue
            severity = issue.get("severity", "") if isinstance(issue, dict) else ""
            lines.append(f"- {title}" + (f" ({severity})" if severity else ""))
        lines.append("")

    # Existing issues
    top_existing = repo_entry.get("top_existing_issues", [])
    if top_existing:
        lines.append("## Known Existing Issues")
        lines.append("")
        lines.append("These issues were already present — confirm they are still unresolved:")
        lines.append("")
        for issue in top_existing:
            title = issue.get("title", issue) if isinstance(issue, dict) else issue
            severity = issue.get("severity", "") if isinstance(issue, dict) else ""
            lines.append(f"- {title}" + (f" ({severity})" if severity else ""))
        lines.append("")

    # Existing code issues with file info
    existing_code = repo_entry.get("existing_code_issues", [])
    if existing_code:
        lines.append("## Existing Code Issues")
        lines.append("")
        lines.append("| File | Title | Severity |")
        lines.append("|------|-------|----------|")
        for issue in existing_code:
            f = issue.get("file", "unknown") if isinstance(issue, dict) else "unknown"
            title = issue.get("title", "") if isinstance(issue, dict) else str(issue)
            severity = issue.get("severity", "") if isinstance(issue, dict) else ""
            lines.append(f"| `{f}` | {title} | {severity} |")
        lines.append("")

    # Recommendations from pit-boss
    recommendations = repo_entry.get("recommendations", [])
    if recommendations:
        lines.append("## Recommendations")
        lines.append("")
        for r in recommendations:
            lines.append(f"- {r}")
        lines.append("")

    # General instructions
    lines.append("## General Instructions")
    lines.append("")
    lines.append("- This is a source-code review scan. The repository is cloned locally.")
    lines.append("- Focus on finding exploitable vulnerabilities, not cosmetic issues.")
    lines.append("- For each finding, describe a realistic attack scenario.")
    lines.append("- Prioritize findings that could lead to data breach, privilege "
                 "escalation, or service disruption.")
    lines.append("- If you find a vulnerability, attempt to create a proof-of-concept.")
    lines.append("- Rate each finding: CRITICAL, HIGH, MEDIUM, LOW.")
    lines.append("- Note any security controls that are well-implemented "
                 "('good catches' for the team).")

    return "\n".join(lines)


def cmd_prepare(args):
    """Phase 1: Read pit-boss JSON(s), generate task queue."""
    print("=" * 60)
    print("  repo-shakedown — Prepare scan tasks")
    print("=" * 60)

    auto_clone = getattr(args, "auto_clone", False)
    repos_dir = Path(args.repos_dir)

    if auto_clone:
        repos_dir.mkdir(parents=True, exist_ok=True)
    elif not repos_dir.exists():
        print(f"❌ Repos directory not found: {repos_dir}")
        print("   Pass --auto-clone to clone repos automatically.")
        return 1

    for d in [WORK_DIR, INSTRUCTIONS_DIR, RESULTS_DIR, REPORTS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    # Handle --reprocess: clear tracking so all files are re-ingested
    if args.reprocess:
        if PROCESSED_FILE.exists():
            PROCESSED_FILE.unlink()
        print("  🔄 Reprocess mode — ignoring previous tracking")

    # ── Load pit-boss data from local files or S3 ────────────
    pitboss_files = []

    if args.pitboss_json:
        # Local file mode — one or more files
        paths = args.pitboss_json if isinstance(args.pitboss_json, list) else [args.pitboss_json]
        print(f"\n📥 Loading from local files ...")
        pitboss_files = load_pitboss_files_local(paths)

    elif args.s3_prefix:
        # S3 mode — download all JSONs under prefix
        print(f"\n📥 Loading from S3: s3://{S3_BUCKET}/{args.s3_prefix}")
        pitboss_files = load_pitboss_files_from_s3(args.s3_prefix)

    else:
        print("❌ Provide either --pitboss-json or --s3-prefix")
        return 1

    if not pitboss_files:
        print("\n⚠️  No new pit-boss files to process.")
        return 0

    # ── Process each file ────────────────────────────────────
    existing_tasks = load_tasks()
    existing_repos = {t["repo"] for t in existing_tasks if t["status"] == "pending"}
    total_added = 0

    for pf in pitboss_files:
        data = pf["data"]
        source_key = pf["source_key"]
        source_name = pf.get("s3_key", pf["local_path"])

        threshold = getattr(args, "threshold", 5)
        repo_risk = data.get("repo_risk", {})
        above = sum(1 for v in repo_risk.values() if v.get("max_risk", 0) >= threshold)
        print(f"\n📄 Processing: {source_name}")
        print(f"   Repos in snapshot: {len(repo_risk)}, "
              f"Above threshold ({threshold}/10): {above}")

        new_tasks = extract_tasks_from_pitboss(data, repos_dir, auto_clone=auto_clone,
                                               threshold=threshold)
        added = 0
        for task in new_tasks:
            if task["repo"] in existing_repos:
                print(f"  ⏭️  {task['repo']} — already has a pending task")
                continue
            if _is_repo_scanned_this_month(task["repo"]):
                print(f"  ⏭️  {task['repo']} — already scanned this month ({_get_month_key()})")
                continue
            # Tag task with its source for traceability
            task["source_key"] = source_key
            task["source_name"] = str(source_name)
            existing_tasks.append(task)
            existing_repos.add(task["repo"])
            added += 1
            print(f"  ✅ {task['repo']} "
                  f"(risk={task['max_risk']}, effort={task['reasoning_effort']})")

        # Mark this source as processed
        mark_source_processed(source_key)
        total_added += added
        print(f"   → {added} tasks from this file")

    save_tasks(existing_tasks)

    pending = sum(1 for t in existing_tasks if t["status"] == "pending")
    processed = load_processed_sources()
    print(f"\n📋 Summary:")
    print(f"   New tasks added:    {total_added}")
    print(f"   Total pending:      {pending}")
    print(f"   Sources processed:  {len(processed)} (lifetime)")
    print(f"   Queue file:         {TASKS_FILE}")
    print(f"   Tracking file:      {PROCESSED_FILE}")

    print(f"\n{'=' * 60}")
    print(f"  Run `python repo_shakedown.py scan` to scan one task.")
    print(f"  Run `python repo_shakedown.py run ...` to scan all tasks.")
    print(f"{'=' * 60}")
    return 0


# ── Phase 2: Scan ────────────────────────────────────────────────

def run_strix(task: Dict, llm_model: str) -> int:
    """
    Invoke Strix CLI in headless mode.
    Returns exit code: 0 = clean, 2 = vulns found.
    """
    repo_path = task["repo_path"]
    instruction_file = task["instruction_file"]
    reasoning_effort = task.get("reasoning_effort", REASONING_EFFORT)

    env = os.environ.copy()
    env["STRIX_LLM"] = llm_model
    env["STRIX_REASONING_EFFORT"] = reasoning_effort

    # Set the right API key env var for the provider
    api_key = resolve_api_key(llm_model)
    if api_key:
        env["LLM_API_KEY"] = api_key

    cmd = [
        "strix",
        "-n",
        "--target", repo_path,
        "--instruction-file", instruction_file,
    ]

    print(f"\n🔍 Running Strix:")
    print(f"   Command:  {' '.join(cmd)}")
    print(f"   Target:   {repo_path}")
    print(f"   Effort:   {reasoning_effort}")
    print(f"   LLM:      {llm_model}")
    print("")

    try:
        result = subprocess.run(
            cmd, env=env,
            capture_output=False,
            timeout=14400,
        )
        return result.returncode
    except subprocess.TimeoutExpired:
        print("  ⚠️  Strix scan timed out (4 hour limit)")
        return -1
    except FileNotFoundError:
        print("  ❌ Strix CLI not found. Install: curl -sSL https://strix.ai/install | bash")
        return -2


def find_strix_run_dir(task: Dict) -> Optional[Path]:
    """Find the most recent Strix output directory matching this repo."""
    strix_runs = Path("strix_runs")
    if not strix_runs.exists():
        return None

    repo_name = task["repo"].split("/")[-1].lower()
    candidates = [
        d for d in strix_runs.iterdir()
        if d.is_dir() and repo_name in d.name.lower()
    ]

    if not candidates:
        all_dirs = sorted(strix_runs.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True)
        candidates = all_dirs[:1]

    if candidates:
        return max(candidates, key=lambda d: d.stat().st_mtime)
    return None


def cmd_scan(args):
    """Phase 2: Pick next pending task, run Strix, report."""
    llm_model = resolve_llm(args.llm)

    print("=" * 60)
    print("  repo-shakedown — Scan")
    print(f"  LLM: {llm_model}")
    print("=" * 60)

    tasks = load_tasks()
    if not tasks:
        print("\n📋 No tasks in queue. Run `prepare` first.")
        return 0

    if args.force_reset:
        stuck = [t for t in tasks if t["status"] == "running"]
        for t in stuck:
            update_task_status(tasks, t["id"], "pending")
            print(f"  🔄 Reset stuck task: {t['repo']}")
        tasks = load_tasks()

    task = find_next_pending(tasks)
    if not task:
        pending = sum(1 for t in tasks if t["status"] == "pending")
        done = sum(1 for t in tasks if t["status"] == "done")
        failed = sum(1 for t in tasks if t["status"] == "failed")
        print(f"\n📋 Queue: {pending} pending, {done} done, {failed} failed")
        print("   No pending tasks.")
        return 0

    update_task_status(tasks, task["id"], "running")
    print(f"\n🎯 Scanning: {task['repo']}")
    print(f"   Risk: {task['max_risk']}/10 (new), {task['max_existing_risk']}/10 (existing)")
    print(f"   Criticals: {task['critical_count']}, Overrides: {task['override_count']}")
    print(f"   Effort: {task.get('reasoning_effort', REASONING_EFFORT)}, "
          f"Urgency: {task.get('llm_urgency', 'N/A')}")

    start_time = time.time()
    exit_code = run_strix(task, llm_model)
    duration = time.time() - start_time

    run_dir = find_strix_run_dir(task)
    strix_run_path = str(run_dir) if run_dir else None

    if exit_code in (0, 2):
        status = "done"
        vulns_found = exit_code == 2
        print(f"\n✅ Scan completed in {duration / 60:.1f} minutes")
        if vulns_found:
            print("   ⚠️  Vulnerabilities found!")
    else:
        status = "failed"
        vulns_found = False
        print(f"\n❌ Scan failed (exit code: {exit_code})")

    tasks = load_tasks()
    update_task_status(
        tasks, task["id"], status,
        strix_exit_code=exit_code,
        strix_run_dir=strix_run_path,
        duration_seconds=round(duration),
        vulns_found=vulns_found,
        llm_used=llm_model,
    )

    # Copy results
    if run_dir and run_dir.exists():
        dest = RESULTS_DIR / task["id"]
        dest.mkdir(parents=True, exist_ok=True)
        for f in run_dir.iterdir():
            if f.is_file():
                (dest / f.name).write_bytes(f.read_bytes())
        print(f"   Results: {dest}")

    # Report
    if status == "done":
        _report_single_task(task, run_dir, args.llm)

    tasks = load_tasks()
    remaining = sum(1 for t in tasks if t["status"] == "pending")
    print(f"\n📋 Remaining pending: {remaining}")
    return 0 if status == "done" else 1


# ── Phase 3: Report ──────────────────────────────────────────────

def _summarize_with_llm(strix_output: str, task: Dict, cli_llm: Optional[str] = None) -> Optional[str]:
    """
    Use an LLM to produce a human-readable triage summary.
    Tries google-genai for gemini models, falls back to litellm for others.
    """
    summarizer_model = resolve_summarizer_llm(cli_llm)
    api_key = resolve_api_key(summarizer_model)

    if not api_key and not summarizer_model.startswith(("vertex_ai/", "bedrock/")):
        print(f"  ⚠️  No API key found for {summarizer_model} — skipping AI summary")
        return None

    prompt = textwrap.dedent(f"""\
    You are a senior penetration tester reviewing automated scan results
    for repository: {task['repo']}.

    The scan was guided by these pit-boss findings:
    - Max NEW risk score: {task['max_risk']}/10
    - Max EXISTING risk score: {task['max_existing_risk']}/10
    - Critical issues flagged: {task['critical_count']}
    - Overridden decisions: {task['override_count']}

    Below is the Strix penetration testing output. Produce a triage report
    for human pentesters that:

    1. Lists each vulnerability found, rated CRITICAL/HIGH/MEDIUM/LOW
    2. For each, describes a realistic exploitation scenario
    3. Notes which findings confirm the pit-boss flags vs. new discoveries
    4. Recommends specific manual tests the human pentester should perform
    5. Highlights any "good catches" — security controls that held up

    Keep it concise and actionable.

    STRIX OUTPUT:
    {strix_output[:12000]}
    """)

    provider = summarizer_model.split("/")[0].lower() if "/" in summarizer_model else ""

    # Try google-genai for gemini models
    if provider == "gemini":
        return _summarize_google_genai(prompt, summarizer_model, api_key)

    # Try litellm for everything else
    return _summarize_litellm(prompt, summarizer_model, api_key)


def _summarize_google_genai(prompt: str, model: str, api_key: str) -> Optional[str]:
    """Summarize using google-genai SDK."""
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        # Strip the "gemini/" prefix for the google-genai SDK
        model_name = model.replace("gemini/", "", 1) if model.startswith("gemini/") else model

        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=4096,
            ),
        )
        return response.text
    except ImportError:
        print("  ⚠️  google-genai not installed. pip install google-genai")
        return None
    except Exception as e:
        print(f"  ⚠️  Gemini summarization failed: {e}")
        return None


def _summarize_litellm(prompt: str, model: str, api_key: str) -> Optional[str]:
    """Summarize using litellm (works with any provider)."""
    try:
        import litellm

        if api_key:
            os.environ["LLM_API_KEY"] = api_key

        response = litellm.completion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=4096,
        )
        return response.choices[0].message.content
    except ImportError:
        print("  ⚠️  litellm not installed. pip install litellm")
        return None
    except Exception as e:
        print(f"  ⚠️  LiteLLM summarization failed: {e}")
        return None


def _format_report_for_print(task: Dict, summary: str) -> str:
    """Format the report for terminal output."""
    border = "─" * 60
    return textwrap.dedent(f"""\
    ┌{border}┐
    │ SHAKEDOWN REPORT: {task['repo']:<40} │
    ├{border}┤
    │ Risk: {task['max_risk']}/10 (new) {task['max_existing_risk']}/10 (existing){'':>16}│
    │ Criticals: {task['critical_count']:<6} Overrides: {task['override_count']:<6}{'':>22}│
    │ Duration: {task.get('duration_seconds', 0) // 60} min{'':>40}│
    │ Vulns found: {'YES ⚠️' if task.get('vulns_found') else 'No'}{'':>37}│
    └{border}┘

    {summary}
    """)


def _send_slack_notification(task: Dict, summary: str):
    """Send findings to Slack. Prints to console if webhook not configured."""
    if not SLACK_WEBHOOK_URL:
        print("\n  📨 Slack webhook not configured — printing report to console:")
        print(_format_report_for_print(task, summary))
        return

    try:
        import urllib.request

        truncated = summary[:2800] if len(summary) > 2800 else summary
        vulns_emoji = "🚨" if task.get("vulns_found") else "✅"

        payload = {
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"{vulns_emoji} Shakedown: {task['repo']}"
                    }
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Risk:* {task['max_risk']}/10"},
                        {"type": "mrkdwn", "text": f"*Mode:* {task.get('scan_mode', 'standard')}"},
                        {"type": "mrkdwn", "text": f"*Criticals:* {task['critical_count']}"},
                        {"type": "mrkdwn", "text": f"*Duration:* {task.get('duration_seconds', 0) // 60}m"},
                    ]
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"```{truncated}```"}
                }
            ]
        }

        req = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        print("  📨 Slack notification sent")

    except Exception as e:
        print(f"  ⚠️  Slack notification failed: {e}")
        print("  Falling back to console output:")
        print(_format_report_for_print(task, summary))


def _create_jira_ticket(task: Dict, summary: str):
    """Create a Jira ticket. Prints to console if Jira not configured."""
    if not all([JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN]):
        if task.get("vulns_found"):
            print("\n  🎫 Jira not configured — printing ticket content:")
            print(f"     Title: [Shakedown] {task['repo']} — Risk {task['max_risk']}/10")
            print(f"     Priority: {'High' if task.get('vulns_found') else 'Medium'}")
            print(f"     Labels: security-vuln-found, repo-shakedown, automated")
            print(f"     Body: (see report file)")
        return

    try:
        import urllib.request
        import base64

        priority = "High" if task.get("vulns_found") else "Medium"
        label = "security-vuln-found" if task.get("vulns_found") else "security-review"

        payload = {
            "fields": {
                "project": {"key": JIRA_PROJECT_KEY},
                "summary": f"[Shakedown] {task['repo']} — Risk {task['max_risk']}/10",
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": summary[:30000]}]
                        }
                    ]
                },
                "issuetype": {"name": "Task"},
                "priority": {"name": priority},
                "labels": [label, "repo-shakedown", "automated"],
            }
        }

        auth = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
        url = f"{JIRA_BASE_URL.rstrip('/')}/rest/api/3/issue"

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {auth}",
            },
        )
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read())
        print(f"  🎫 Jira ticket created: {result.get('key', '?')}")

    except Exception as e:
        print(f"  ⚠️  Jira ticket creation failed: {e}")
        print(f"     Title would be: [Shakedown] {task['repo']} — Risk {task['max_risk']}/10")


def _report_single_task(task: Dict, run_dir: Optional[Path], cli_llm: Optional[str] = None):
    """Generate report and send notifications for a completed scan."""
    print(f"\n📝 Generating report for {task['repo']} ...")

    strix_output = ""
    if run_dir and run_dir.exists():
        events_file = run_dir / "events.jsonl"
        if events_file.exists():
            strix_output = events_file.read_text()
        else:
            for f in sorted(run_dir.iterdir()):
                if f.suffix in (".txt", ".md", ".json", ".jsonl"):
                    strix_output += f"\n--- {f.name} ---\n"
                    strix_output += f.read_text()[:5000]

    if not strix_output:
        strix_output = "(No Strix output captured)"

    # AI-powered summary
    summary = _summarize_with_llm(strix_output, task, cli_llm)

    if not summary:
        summary = textwrap.dedent(f"""\
        ## Shakedown Report: {task['repo']}

        Scan completed {'with vulnerabilities found' if task.get('vulns_found') else 'clean'}.
        - Max NEW risk: {task['max_risk']}/10
        - Max EXISTING risk: {task['max_existing_risk']}/10
        - Critical issues flagged: {task['critical_count']}
        - Scan mode: {task.get('scan_mode', 'standard')}
        - Duration: {task.get('duration_seconds', 0) // 60} minutes
        - Strix output: {task.get('strix_run_dir', 'N/A')}

        Manual review recommended.
        """)

    # Save report locally
    report_path = REPORTS_DIR / f"{task['id']}_report.md"
    report_path.write_text(summary)
    print(f"   Report saved: {report_path}")

    # Upload to S3 if configured
    _upload_report_to_s3(report_path, task)

    tasks = load_tasks()
    update_task_status(tasks, task["id"], task.get("status", "done"),
                       report_file=str(report_path))

    # Notifications — graceful fallback to print
    _send_slack_notification(task, summary)
    _create_jira_ticket(task, summary)

    # Record repo as scanned this month so it is skipped if pit-boss recommends it again
    _mark_repo_scanned_this_month(task["repo"])


def _upload_report_to_s3(report_path: Path, task: Dict):
    """Upload report to S3. Silent no-op if S3_REPORTS_PREFIX is not configured."""
    if not S3_REPORTS_PREFIX:
        return
    if not S3_BUCKET:
        print("  ⚠️  S3_REPORTS_PREFIX is set but S3_BUCKET is empty — skipping upload")
        return
    try:
        import boto3
        s3 = boto3.client("s3")
        key = f"{S3_REPORTS_PREFIX.rstrip('/')}/{report_path.name}"
        s3.upload_file(str(report_path), S3_BUCKET, key)
        print(f"  ☁️  Report uploaded: s3://{S3_BUCKET}/{key}")
    except ImportError:
        print("  ⚠️  boto3 not installed — skipping S3 upload. pip install boto3")
    except Exception as e:
        print(f"  ⚠️  S3 report upload failed: {e}")


def cmd_report(args):
    """Report on completed scans that haven't been reported yet."""
    print("=" * 60)
    print("  repo-shakedown — Report")
    print("=" * 60)

    tasks = load_tasks()
    unreported = [
        t for t in tasks
        if t["status"] == "done" and not t.get("report_file")
    ]

    if not unreported:
        print("\n📋 No unreported completed scans.")
        return 0

    for task in unreported:
        run_dir = Path(task["strix_run_dir"]) if task.get("strix_run_dir") else None
        _report_single_task(task, run_dir, args.llm)

    print(f"\n  Reported on {len(unreported)} scans.")
    return 0


def cmd_status(args):
    """Show current queue status."""
    tasks = load_tasks()
    if not tasks:
        print("📋 No tasks in queue.")
        return 0

    statuses = {"pending": 0, "running": 0, "done": 0, "failed": 0}
    for t in tasks:
        s = t.get("status", "unknown")
        statuses[s] = statuses.get(s, 0) + 1

    print(f"\n📋 Task Queue Status")
    print(f"   Pending:  {statuses['pending']}")
    print(f"   Running:  {statuses['running']}")
    print(f"   Done:     {statuses['done']}")
    print(f"   Failed:   {statuses['failed']}")
    print("")

    for t in tasks:
        icon = {"pending": "⏳", "running": "🔄", "done": "✅", "failed": "❌"}.get(t["status"], "?")
        vuln = " 🚨" if t.get("vulns_found") else ""
        llm_note = f" [{t['llm_used']}]" if t.get("llm_used") else ""
        effort = t.get("reasoning_effort", "?")
        urgency = f" [{t['llm_urgency']}]" if t.get("llm_urgency") else ""
        print(f"   {icon} {t['repo']} — risk {t['max_risk']}/10, "
              f"effort={effort}{urgency}{vuln}{llm_note}")

    return 0


# ── CI: scan exactly one repo per action run ─────────────────────

def cmd_run_one(args):
    """CI mode: prepare from pit-boss → scan exactly one pending repo → report.

    Monthly dedup is applied during prepare: repos already scanned in the current
    calendar month are skipped.  The action exits 0 whether a scan ran or not
    (no pending tasks is not an error; it just means everything is up to date).
    """
    print("=" * 60)
    print("  repo-shakedown — Run One (prepare + scan one)")
    print("=" * 60)

    rc = cmd_prepare(args)
    if rc != 0:
        return rc

    tasks = load_tasks()
    if not find_next_pending(tasks):
        print("\n✅ No pending tasks — all recommended repos already scanned this month.")
        return 0

    scan_args = argparse.Namespace(llm=args.llm, force_reset=False)
    return cmd_scan(scan_args)


# ── All-in-one local run ─────────────────────────────────────────

def cmd_run(args):
    """All-in-one: prepare (with optional clone) → scan all → report."""
    print("=" * 60)
    print("  repo-shakedown — Run (prepare + scan + report)")
    print("=" * 60)

    # Phase 1: prepare (includes gh preflight if --auto-clone)
    rc = cmd_prepare(args)
    if rc != 0:
        return rc

    # Phase 2: scan all pending tasks in sequence
    scan_args = argparse.Namespace(llm=args.llm, force_reset=False)
    scanned = 0
    failed = 0
    while True:
        tasks = load_tasks()
        if not find_next_pending(tasks):
            break
        rc = cmd_scan(scan_args)
        scanned += 1
        if rc != 0:
            failed += 1

    print(f"\n  Scanned {scanned} repo(s) — {failed} failed.")

    # Phase 3: report all completed scans (includes S3 upload if configured)
    report_args = argparse.Namespace(llm=args.llm)
    cmd_report(report_args)

    return 0 if failed == 0 else 1


# ── CLI ──────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="repo-shakedown: Pit-boss → Strix orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
        LLM resolution order:
          1. --llm flag on the command line
          2. STRIX_LLM environment variable
          3. Default: gemini/gemini-2.5-pro

        Supported LLM formats (via LiteLLM):
          gemini/gemini-2.5-pro         (GEMINI_API_KEY)
          openai/gpt-5                  (OPENAI_API_KEY)
          anthropic/claude-sonnet-4-6   (ANTHROPIC_API_KEY)
          vertex_ai/gemini-3-pro        (gcloud auth)
          bedrock/anthropic.claude-...  (AWS auth)
          ollama/llama4                 (local, no key)
        """),
    )

    # Global --llm flag available to all subcommands
    p.add_argument("--llm", type=str, default=None,
                   help="LLM model for Strix and summarizer "
                        "(e.g. gemini/gemini-2.5-pro, openai/gpt-5)")

    sub = p.add_subparsers(dest="command", required=True)

    # ── run-one: CI / GitHub Action mode ───────────────────────
    run_one_p = sub.add_parser(
        "run-one",
        help="CI mode: prepare + scan one repo + report (monthly dedup applied)",
    )
    run_one_source = run_one_p.add_mutually_exclusive_group(required=True)
    run_one_source.add_argument("--pitboss-json", nargs="+",
                                help="Path(s) to local candidates.json file(s)")
    run_one_source.add_argument("--s3-prefix", type=str,
                                help="S3 prefix for candidates.json files")
    run_one_p.add_argument("--repos-dir", required=True,
                           help="Directory to store cloned repositories")
    run_one_p.add_argument("--auto-clone", action="store_true",
                           help="Clone missing repos automatically using git clone")
    run_one_p.add_argument("--reprocess", action="store_true",
                           help="Ignore tracking — reprocess all files")
    run_one_p.add_argument("--threshold", type=int, default=5,
                           help="Min max_risk score to include a repo (default: 5)")

    # ── run: all-in-one for local use ──────────────────────────
    run_p = sub.add_parser("run",
                           help="All-in-one: prepare + clone + scan all + report")
    run_source = run_p.add_mutually_exclusive_group(required=True)
    run_source.add_argument("--pitboss-json", nargs="+",
                            help="Path(s) to local candidates.json file(s)")
    run_source.add_argument("--s3-prefix", type=str,
                            help="S3 prefix for candidates.json files "
                                 "(e.g. shakedown/2026-04-d01-07/)")
    run_p.add_argument("--repos-dir", required=True,
                       help="Directory to store cloned repositories")
    run_p.add_argument("--auto-clone", action="store_true",
                       help="Clone missing repos automatically using git clone")
    run_p.add_argument("--reprocess", action="store_true",
                       help="Ignore tracking — reprocess all files")
    run_p.add_argument("--threshold", type=int, default=5,
                       help="Min max_risk score to include a repo (default: 5)")

    # ── prepare: build task queue only ─────────────────────────
    prep = sub.add_parser("prepare", help="Build scan tasks from candidates.json")
    prep_source = prep.add_mutually_exclusive_group(required=True)
    prep_source.add_argument("--pitboss-json", nargs="+",
                             help="Path(s) to local candidates.json file(s)")
    prep_source.add_argument("--s3-prefix", type=str,
                             help="S3 prefix for candidates.json files "
                                  "(e.g. shakedown/2026-04-d01-07/)")
    prep.add_argument("--repos-dir", required=True,
                      help="Directory containing cloned repositories")
    prep.add_argument("--auto-clone", action="store_true",
                      help="Clone missing repos automatically using git clone")
    prep.add_argument("--reprocess", action="store_true",
                      help="Ignore tracking — reprocess all files")
    prep.add_argument("--threshold", type=int, default=5,
                      help="Min max_risk score to include a repo (default: 5)")

    # ── scan, report, status ────────────────────────────────────
    scan = sub.add_parser("scan", help="Run next pending scan")
    scan.add_argument("--force-reset", action="store_true",
                      help="Reset stuck 'running' tasks to 'pending'")

    sub.add_parser("report", help="Generate reports for completed scans")
    sub.add_parser("status", help="Show queue status")

    args = p.parse_args()

    if args.command == "run-one":
        return cmd_run_one(args)
    elif args.command == "run":
        return cmd_run(args)
    elif args.command == "prepare":
        return cmd_prepare(args)
    elif args.command == "scan":
        return cmd_scan(args)
    elif args.command == "report":
        return cmd_report(args)
    elif args.command == "status":
        return cmd_status(args)


if __name__ == "__main__":
    sys.exit(main())