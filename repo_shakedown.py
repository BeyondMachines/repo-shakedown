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
import csv
import zipfile
from urllib.parse import quote

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

S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_REPORTS_PREFIX = os.environ.get("S3_REPORTS_PREFIX", "")
PROCESSED_FILE = WORK_DIR / "processed_sources.json"

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "")
JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "SEC")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
JIRA_EPIC_KEY = os.environ.get("JIRA_EPIC_KEY", "")


SCANNED_REPOS_LOCAL = WORK_DIR / "scanned_repos.json"
_scanned_repos_cache: Optional[Dict[str, List[str]]] = None



def resolve_llm(cli_llm: Optional[str] = None) -> str:
    """Resolve LLM model string. Priority: CLI flag > env > default."""
    if cli_llm:
        return cli_llm
    return os.environ.get("STRIX_LLM", DEFAULT_LLM)



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

def _classify_pitboss_file(data: Dict) -> str:
    """
    Identify which kind of pit-boss file this is by structure, not filename.
    Returns 'candidates', 'snapshot', or 'unknown'.

    - candidates: top-level 'repos' array (output of ShakedownCandidateBuilder)
    - snapshot: top-level 'repo_risk' dict + 'pr_records_compact'
                (output of ReviewCorrelator.to_snapshot)
    - other 'repo_risk'-shaped files (monthly analyses, aggregates) are
      treated as snapshots — they have the same per-repo shape.
    """
    if not isinstance(data, dict):
        return "unknown"
    if "repos" in data and "generated_by" in data:
        return "candidates"
    if "repo_risk" in data:
        return "snapshot"
    return "unknown"


def _extract_week_label(pf: Dict) -> Optional[str]:
    """
    Pull the week label out of a pit-boss file dict (as returned by
    load_pitboss_files_from_s3 / load_pitboss_files_local).

    Snapshots store 'week_label' in their data. Candidates files store the
    label in 'period' OR encode it in the S3 key like
    'shakedown/2026-04-d28-30/candidates.json'.
    Returns None if no label can be determined.
    """
    data = pf.get("data", {})

    # Snapshot — stored explicitly
    label = data.get("week_label")
    if label:
        return label

    # Candidates — sometimes in 'period'
    label = data.get("period")
    if label:
        return label

    # Fallback: parse from S3 key path
    s3_key = pf.get("s3_key", "")
    if s3_key:
        parts = s3_key.split("/")
        # Expect e.g. 'shakedown/2026-04-d28-30/candidates.json'
        if len(parts) >= 2:
            return parts[-2]

    return None


def _pair_files_by_label(
    pitboss_files: List[Dict],
) -> Dict[str, Dict[str, Optional[Dict]]]:
    """
    Group loaded pit-boss files by week label, pairing each snapshot with its
    matching candidates file.

    Returns:
        { '2026-04-d28-30': {'snapshot': <pf>, 'candidates': <pf>}, ... }

    A label may have only a snapshot, only a candidates file, or both.
    Files we can't classify or label are dropped with a warning.
    """
    paired: Dict[str, Dict[str, Optional[Dict]]] = {}

    for i, pf in enumerate(pitboss_files):
        kind = _classify_pitboss_file(pf.get("data", {}))
        if kind == "unknown":
            print(f"  ⚠️  Unrecognized pit-boss file shape: "
                  f"{pf.get('s3_key', pf.get('local_path'))}")
            continue

        label = _extract_week_label(pf)
        if not label:
            # No label available (e.g. local file, or empty 'period' field).
            # Synthesize one. Pairing is meaningless without a real label,
            # but we still want to process the file's contents.
            label = f"_unlabeled_{kind}_{i}"

        bucket = paired.setdefault(label, {"snapshot": None, "candidates": None})

        # If we somehow get two of the same kind for one label, prefer the
        # one with more data (candidates with more repos, snapshot with more
        # PR records). Defensive — should not normally happen.
        existing = bucket[kind]
        if existing is None:
            bucket[kind] = pf
        else:
            cur_repos = len(pf["data"].get("repos", [])) \
                or len(pf["data"].get("repo_risk", {}))
            old_repos = len(existing["data"].get("repos", [])) \
                or len(existing["data"].get("repo_risk", {}))
            if cur_repos > old_repos:
                bucket[kind] = pf

    return paired


def _merge_repo_data(
    repo: str,
    snapshot_repo: Optional[Dict],
    candidate_repo: Optional[Dict],
    snapshot_pr_records: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """
    Merge a single repo's data from snapshot and candidates files.

    Rules:
      - Snapshot wins on PR-level granular data (top_*, existing_code_issues,
        recommendations, breaking_changes, tool_findings, pr_records).
      - Candidates wins on prioritization (priority_score, suggested_scan_mode,
        qualified_by, reasons).
      - Candidates wins on targeting (scan_guidance, critical_issue_titles).
      - LLM fields (llm_*) only present when Gemini ran for this repo.
      - When a repo appears in only one file, fields from the other are absent
        and downstream code (instruction generation) must handle their absence.

    Either snapshot_repo or candidate_repo may be None — at least one must be
    provided.
    """
    if not snapshot_repo and not candidate_repo:
        return {}

    merged: Dict[str, Any] = {"repo": repo}

    # ── Snapshot-derived fields (PR-level granularity) ───────
    if snapshot_repo:
        merged.update({
            "max_risk": snapshot_repo.get("max_risk", 0),
            "max_existing_risk": snapshot_repo.get("max_existing_risk", 0),
            "avg_risk": snapshot_repo.get("avg_risk", 0),
            "avg_existing_risk": snapshot_repo.get("avg_existing_risk", 0),
            "new_critical_count": snapshot_repo.get("new_critical_count", 0),
            "existing_critical_count": snapshot_repo.get("existing_critical_count", 0),
            "critical_count": snapshot_repo.get("critical_count", 0),
            "override_count": snapshot_repo.get("override_count", 0),
            "fix_count": snapshot_repo.get("fix_count", 0),
            "persist_count": snapshot_repo.get("persist_count", 0),
            "total_prs": snapshot_repo.get("total_prs", 0),
            "total_scans": snapshot_repo.get("total_scans", 0),
            "top_new_issues": snapshot_repo.get("top_new_issues", []),
            "top_existing_issues": snapshot_repo.get("top_existing_issues", []),
            "top_issues": snapshot_repo.get("top_issues", []),
            "existing_code_issues": snapshot_repo.get("existing_code_issues", []),
            "recommendations": snapshot_repo.get("recommendations", []),
            "breaking_changes": snapshot_repo.get("breaking_changes", []),
            "tool_findings": snapshot_repo.get("tool_findings", {}),
        })

    # ── Candidate-derived fields (priority + targeting + LLM) ────
    if candidate_repo:
        # Candidates uses '*_score' suffix — normalize to match snapshot
        if "max_risk_score" in candidate_repo:
            merged["max_risk"] = max(
                merged.get("max_risk", 0),
                candidate_repo["max_risk_score"],
            )
        if "max_existing_risk_score" in candidate_repo:
            merged["max_existing_risk"] = max(
                merged.get("max_existing_risk", 0),
                candidate_repo["max_existing_risk_score"],
            )

        # Counts — take max if both sources have them (candidates is canonical)
        for key in ("new_critical_count", "existing_critical_count",
                    "override_count", "fix_count", "persist_count"):
            if key in candidate_repo:
                merged[key] = max(merged.get(key, 0), candidate_repo[key])

        merged.update({
            "priority_score": candidate_repo.get("priority_score", 0),
            "suggested_scan_mode": candidate_repo.get("suggested_scan_mode",
                                                      "default"),
            "qualified_by": candidate_repo.get("qualified_by", {}),
            "reasons": candidate_repo.get("reasons", []),
            "critical_issue_titles": candidate_repo.get(
                "critical_issue_titles", []),
            "scan_guidance": candidate_repo.get("scan_guidance", {}),
            "repo_url": candidate_repo.get("repo_url",
                                           f"https://github.com/{repo}"),
        })

        # LLM enrichment — only present when Gemini ran for this repo.
        # Use .get() with None default so absence is detectable downstream.
        for key in ("llm_urgency", "llm_narrative", "llm_focus_areas",
                    "llm_priority_files", "llm_scan_instructions",
                    "llm_existing_debt_notes", "llm_risk_if_ignored"):
            if key in candidate_repo:
                merged[key] = candidate_repo[key]

    # ── PR-level evidence from the snapshot ─────────────────
    if snapshot_pr_records:
        merged["pr_records"] = [
            p for p in snapshot_pr_records
            if p.get("repo") == repo
        ]
    else:
        merged["pr_records"] = []

    # Default values for fields not provided by either source
    merged.setdefault("repo_url", f"https://github.com/{repo}")
    merged.setdefault("priority_score", 0)
    merged.setdefault("suggested_scan_mode", "default")
    merged.setdefault("reasons", [])

    return merged


def build_merged_repo_index(
    pitboss_files: List[Dict],
) -> Dict[str, Dict[str, Any]]:
    """
    Top-level merge entry point.

    Pairs all loaded pit-boss files by week label, then merges per-repo across
    snapshot and candidates within each label. Returns a single
    {repo -> merged_data} dict.

    When a repo appears in multiple weeks, the latest week's data wins for
    targeting fields, but counts/criticals are summed across weeks.
    """
    paired = _pair_files_by_label(pitboss_files)

    if not paired:
        return {}

    # Sort labels chronologically so that "latest wins" for targeting fields.
    # Labels look like '2026-04-d28-30' or '2026-04' — string sort works.
    sorted_labels = sorted(paired.keys())

    merged_repos: Dict[str, Dict[str, Any]] = {}

    for label in sorted_labels:
        bucket = paired[label]
        snap_pf = bucket.get("snapshot")
        cand_pf = bucket.get("candidates")

        snapshot_repo_risk = (snap_pf or {}).get(
            "data", {}).get("repo_risk", {})
        snapshot_pr_records = (snap_pf or {}).get(
            "data", {}).get("pr_records_compact", [])
        candidates_repos = {
            c["repo"]: c
            for c in (cand_pf or {}).get("data", {}).get("repos", [])
            if c.get("repo")
        }

        # Union of all repos present in either source for this label
        all_repos = set(snapshot_repo_risk.keys()) | set(candidates_repos.keys())

        for repo in all_repos:
            week_merged = _merge_repo_data(
                repo,
                snapshot_repo=snapshot_repo_risk.get(repo),
                candidate_repo=candidates_repos.get(repo),
                snapshot_pr_records=snapshot_pr_records,
            )
            if not week_merged:
                continue

            # Combine across weeks for the same repo
            existing = merged_repos.get(repo)
            if not existing:
                merged_repos[repo] = week_merged
                continue

            # Sum counts; take max of risk scores; latest week's
            # targeting/LLM fields win (we got there via sorted iteration)
            for count_key in ("new_critical_count", "existing_critical_count",
                              "override_count", "fix_count", "persist_count",
                              "total_prs", "total_scans"):
                existing[count_key] = (
                    existing.get(count_key, 0)
                    + week_merged.get(count_key, 0)
                )
            for max_key in ("max_risk", "max_existing_risk", "priority_score"):
                existing[max_key] = max(
                    existing.get(max_key, 0),
                    week_merged.get(max_key, 0),
                )

            # Targeting/LLM fields: latest week wins. Only overwrite if
            # the new week has data for that field.
            for tgt_key in ("priority_score", "suggested_scan_mode",
                            "qualified_by", "reasons", "critical_issue_titles",
                            "scan_guidance", "llm_urgency", "llm_narrative",
                            "llm_focus_areas", "llm_priority_files",
                            "llm_scan_instructions", "llm_existing_debt_notes",
                            "llm_risk_if_ignored"):
                if week_merged.get(tgt_key):
                    existing[tgt_key] = week_merged[tgt_key]

            # Append PR records, top issues, etc. (let downstream dedupe
            # if needed)
            existing["pr_records"] = (
                existing.get("pr_records", []) + week_merged.get("pr_records", [])
            )
            for list_key in ("top_new_issues", "top_existing_issues",
                             "top_issues", "existing_code_issues",
                             "recommendations", "breaking_changes"):
                if week_merged.get(list_key):
                    existing.setdefault(list_key, [])
                    existing[list_key].extend(week_merged[list_key])

    return merged_repos


def _filter_files_by_month(
    pitboss_files: List[Dict],
    month: Optional[str],
) -> List[Dict]:
    """
    Filter loaded files to only those matching the given month label.
    `month` is in 'YYYY-MM' format. Files whose label doesn't start with the
    month are dropped.

    If month is None or empty, returns all files unchanged.
    """
    if not month:
        return pitboss_files

    kept = []
    for pf in pitboss_files:
        label = _extract_week_label(pf)
        if label and label.startswith(month):
            kept.append(pf)
        else:
            print(f"  ⏭️  Skipping file outside month {month}: "
                  f"{pf.get('s3_key', pf.get('local_path'))} (label={label})")
    return kept


# ── Phase 1: Prepare ─────────────────────────────────────────────


def extract_tasks_from_merged(
    merged_repos: Dict[str, Dict[str, Any]],
    repos_dir: Path,
    auto_clone: bool = False,
    threshold: int = 5,
) -> List[Dict]:
    """
    Build scan tasks from merged repo data (snapshot + candidates).

    A repo qualifies if:
      - effective_risk = max(max_risk, max_existing_risk) >= threshold, OR
      - it has any priority_score > 0 (means pit-boss already flagged it)

    Tasks are returned sorted by priority_score descending. When two repos
    have the same priority_score, the higher max_risk wins.
    """
    tasks = []

    # Sort by priority_score desc, then max_risk desc
    sorted_repos = sorted(
        merged_repos.items(),
        key=lambda kv: (
            kv[1].get("priority_score", 0),
            kv[1].get("max_risk", 0),
        ),
        reverse=True,
    )

    for i, (repo, data) in enumerate(sorted_repos):
        if not repo:
            continue

        max_risk = data.get("max_risk", 0)
        max_existing = data.get("max_existing_risk", 0)
        priority = data.get("priority_score", 0)

        effective_risk = max(max_risk, max_existing)
        # Qualify on either threshold OR pit-boss-determined priority
        if effective_risk < threshold and priority <= 0:
            continue

        repo_url = data.get("repo_url", f"https://github.com/{repo}")

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
                print(f"  ⚠️  Repo not found at {repos_dir}/{repo_name} — "
                      f"skipping {repo}")
                continue

        instruction_content = generate_instruction_file(repo, data)

        task_id = f"{repo.replace('/', '__')}__{int(time.time())}_{i}"
        instruction_path = INSTRUCTIONS_DIR / f"{task_id}.md"
        instruction_path.parent.mkdir(parents=True, exist_ok=True)
        instruction_path.write_text(instruction_content)

        # Determine scan mode and reasoning effort from pit-boss suggestion.
        # PR2 will wire these through to Strix; for now, just persist them
        # so the task carries the data forward.
        scan_mode = data.get("suggested_scan_mode", "default")

        tasks.append({
            "id": task_id,
            "repo": repo,
            "repo_url": repo_url,
            "repo_path": str(repo_path.resolve()),
            "instruction_file": str(instruction_path.resolve()),
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            # Risk + counts
            "max_risk": max_risk,
            "max_existing_risk": max_existing,
            "critical_count": data.get("new_critical_count", 0),
            "override_count": data.get("override_count", 0),
            # New fields from merged data
            "priority_score": priority,
            "suggested_scan_mode": scan_mode,
            "reasons": data.get("reasons", []),
            "qualified_by": data.get("qualified_by", {}),
            "has_llm_enrichment": bool(data.get("llm_scan_instructions")),
            "has_specific_issues": _has_specific_issues(data),
            # Strix run results (filled during scan)
            "strix_run_dir": None,
            "strix_exit_code": None,
            "report_file": None,
        })

    return tasks

def _has_specific_issues(data: Dict) -> bool:
    """
    Returns True if pit-boss provided ANY specific issues to itemize for this
    repo (Bucket 1). Returns False when the repo was flagged on risk score
    alone with no specific signal (Bucket 2).

    Bucket 1 repos get itemized issues PLUS the general checklist.
    Bucket 2 repos get the general checklist alone.
    Both buckets always run the general checklist as a safety net.
    """
    return bool(
        data.get("critical_issue_titles")
        or data.get("llm_scan_instructions")
        or data.get("llm_priority_files")
        or data.get("existing_code_issues")
        or data.get("top_new_issues")
        or data.get("top_existing_issues")
        or (data.get("scan_guidance") or {}).get("priority_files")
        or (data.get("scan_guidance") or {}).get("existing_ai_issues")
    )


def _build_general_checklist_section() -> List[str]:
    """
    The general web-application vulnerability checklist. Appears in every
    instruction file — Bucket 1 (after itemized issues) and Bucket 2 (alone).

    Strix is required to produce an explicit verdict per category, not skip
    any. NOT_APPLICABLE is acceptable when a category clearly doesn't fit
    (e.g., 'SQL injection' for a repo with no database) — but only with
    evidence of inspection.
    """
    lines = []
    lines.append("## General Web-App Vulnerability Checklist")
    lines.append("")
    lines.append("Audit the repository against the following categories. For each "
                 "one, produce an explicit verdict — do not skip a category.")
    lines.append("")
    lines.append("**Required output format per category:**")
    lines.append("")
    lines.append("- **Category**: name")
    lines.append("- **Files inspected**: which files you actually read for this check")
    lines.append("- **Verdict**: `VULNERABLE` | `LIKELY_SAFE` | `NOT_APPLICABLE` | `UNCLEAR`")
    lines.append("- **Evidence**: specific code references supporting the verdict")
    lines.append("- **Findings**: any specific vulnerabilities discovered (if VULNERABLE)")
    lines.append("")
    lines.append("`NOT_APPLICABLE` is acceptable when the category clearly doesn't "
                 "fit the codebase. `UNCLEAR` is acceptable when exploitability "
                 "can't be determined without runtime testing. Both still require "
                 "evidence of inspection.")
    lines.append("")
    lines.append("### Categories")
    lines.append("")

    categories = [
        ("Injection",
         "SQL injection (raw queries, ORM escape hatches, dynamic queries), "
         "command injection (`subprocess` with shell, `os.system`, `eval`, `exec`), "
         "template injection (SSTI), and prompt injection where LLMs are involved."),
        ("Path Traversal & File Handling",
         "File operations with user-controlled paths. Missing `..` checks. "
         "Archive extraction without path validation. `send_file` with unsanitized input."),
        ("SSRF & Server-Side Network Calls",
         "HTTP clients accepting user-controlled URLs without allowlist validation. "
         "Especially dangerous when targeting internal IPs, cloud metadata "
         "endpoints (169.254.169.254), or localhost."),
        ("Deserialization & Unsafe Parsing",
         "`pickle.load`, `yaml.load` without SafeLoader, Java deserialization, "
         "`eval` on JSON-like input, XML parsers with external entity resolution."),
        ("Authentication",
         "Missing auth on protected endpoints. Hardcoded credentials. Token "
         "validation accepting unsigned/expired tokens. Default passwords. "
         "Session fixation. Weak password reset flows."),
        ("Authorization (IDOR & Privilege Escalation)",
         "Operations on resource IDs from requests without ownership/role checks. "
         "IDORs. "
         "Role fields user-modifiable. Admin endpoints checked only by URL prefix. "
         "Permission checks performed after sensitive operations."),
        ("Secrets & Credentials",
         "Hardcoded API keys, tokens, passwords, private keys committed to the "
         "repo. Secrets logged. Secrets in error responses or URLs. Inspect "
         "config files, defaults, test fixtures, and CI files."),
        ("Cryptography",
         "MD5/SHA1 for security purposes. ECB mode. Hardcoded IVs or salts. "
         "`random` instead of `secrets` for tokens. Weak password hashing "
         "(no bcrypt/argon2/scrypt). Custom crypto."),
        ("Information Disclosure",
         "Verbose error pages or stack traces in production responses. Debug "
         "endpoints exposed. Internal hostnames or IPs leaked. PII or secrets "
         "in logs."),
        ("Input Validation & Rate Limiting",
         "Untrusted input used without type/length/format validation, especially "
         "where it controls flow or feeds external systems. Authentication "
         "endpoints and expensive operations without rate limits. Missing "
         "CSRF protection on state-changing endpoints. Permissive CORS."),
    ]

    for name, description in categories:
        lines.append(f"#### {name}")
        lines.append("")
        lines.append(description)
        lines.append("")

    return lines

def generate_instruction_file(repo: str, data: Dict) -> str:
    """
    Build a Strix instruction file from merged pit-boss data.

    Two buckets based on whether pit-boss provided specific issues:

      Bucket 1 (has specific issues): itemize the flagged issues for Strix to
        verify, THEN run the general web-app vulnerability checklist as a
        safety net. Pit-boss had signal but didn't see everything.

      Bucket 2 (no specific issues, just risk-flagged): run the general
        checklist as a complete audit pass.

    The general checklist runs in BOTH buckets. The difference is whether it
    sits alongside specific itemized issues or stands alone.
    """
    lines: List[str] = []

    # ── Header + urgency ────────────────────────────────────
    urgency = data.get("llm_urgency") or _infer_urgency(data)
    scan_mode = data.get("suggested_scan_mode", "default")

    lines.append(f"# Penetration Test Scope: {repo}")
    lines.append("")
    lines.append(f"**Urgency:** {urgency}  ")
    lines.append(f"**Suggested scan mode:** {scan_mode}  ")
    if data.get("priority_score"):
        lines.append(f"**Priority score:** {data['priority_score']}  ")
    lines.append("")

    # ── Risk context (always present) ───────────────────────
    lines.append("## Risk Context")
    lines.append("")
    lines.append(f"- Max NEW risk score: {data.get('max_risk', 0)}/10")
    lines.append(f"- Max EXISTING risk score: "
                 f"{data.get('max_existing_risk', 0)}/10")
    lines.append(f"- NEW critical issues: {data.get('new_critical_count', 0)}")
    lines.append(f"- EXISTING critical issues: "
                 f"{data.get('existing_critical_count', 0)}")
    lines.append(f"- PRs reviewed: {data.get('total_prs', 0)} "
                 f"({data.get('total_scans', data.get('total_prs', 0))} scans)")
    if data.get("override_count", 0) > 0:
        lines.append(f"- Overrides applied: {data['override_count']}")
    if data.get("fix_count", 0) > 0:
        lines.append(f"- Issues fixed during PR cycle: {data['fix_count']}")
    if data.get("persist_count", 0) > 0:
        lines.append(f"- Issues that persisted unfixed: {data['persist_count']}")
    lines.append("")

    reasons = data.get("reasons") or []
    if reasons:
        lines.append("**Why this repo was flagged:**")
        for r in reasons:
            lines.append(f"- {r}")
        lines.append("")

    has_specific = _has_specific_issues(data)

    # ── Bucket 1 sections: itemized issues from pit-boss ────
    if has_specific:
        # LLM threat assessment
        if data.get("llm_narrative"):
            lines.append("## Threat Assessment")
            lines.append("")
            lines.append(data["llm_narrative"])
            lines.append("")

        # Specific scan instructions from the LLM
        if data.get("llm_scan_instructions"):
            lines.append("## Specific Scan Instructions")
            lines.append("")
            lines.append("> The following targeting was generated by analyzing "
                         "PR-Bouncer review data. Treat as authoritative scope "
                         "guidance for the itemized issues.")
            lines.append("")
            lines.append(data["llm_scan_instructions"])
            lines.append("")

        # Priority files (combine LLM + scan_guidance)
        priority_files = []
        seen_files = set()
        for f in (data.get("llm_priority_files") or []):
            if f and f not in seen_files:
                priority_files.append(f)
                seen_files.add(f)
        for f in (data.get("scan_guidance") or {}).get("priority_files", []):
            if f and f not in seen_files:
                priority_files.append(f)
                seen_files.add(f)

        if priority_files:
            lines.append("## Priority Files — Examine These First")
            lines.append("")
            for f in priority_files:
                lines.append(f"- `{f}`")
            lines.append("")

        # Focus areas / vulnerability classes
        focus_areas = data.get("llm_focus_areas") or []
        if focus_areas:
            lines.append("## Focus Areas")
            lines.append("")
            lines.append("Concentrate on the following vulnerability classes "
                         "in the priority files above:")
            lines.append("")
            for fa in focus_areas:
                lines.append(f"- {fa}")
            lines.append("")

        # Critical issues already flagged
        crit_titles = data.get("critical_issue_titles") or []
        if crit_titles:
            lines.append("## Critical Issues Already Flagged — Verify Exploitability")
            lines.append("")
            lines.append("These were flagged during PR review. Confirm whether "
                         "they are reachable and exploitable in production code paths:")
            lines.append("")
            for t in crit_titles:
                lines.append(f"- {t}")
            lines.append("")

        # AI-found existing code issues
        existing_ai = (data.get("scan_guidance") or {}).get("existing_ai_issues", [])
        if existing_ai:
            lines.append("## Pre-Existing Issues (AI-detected, automated tools missed)")
            lines.append("")
            lines.append("| File | Issue | Severity |")
            lines.append("|------|-------|----------|")
            for issue in existing_ai:
                f_path = issue.get("file", "?")
                title = (issue.get("title", "?")
                         .replace("|", "\\|")
                         .replace("\n", " "))
                severity = issue.get("severity", "?")
                lines.append(f"| `{f_path}` | {title} | {severity} |")
            lines.append("")

        # Existing code issues from snapshot
        snapshot_existing = data.get("existing_code_issues") or []
        seen_keys = {
            (i.get("file", ""), i.get("title", ""))
            for i in existing_ai
        }
        snapshot_existing_unique = [
            i for i in snapshot_existing
            if isinstance(i, dict)
            and (i.get("file", ""), i.get("title", "")) not in seen_keys
        ]
        if snapshot_existing_unique:
            lines.append("## Additional Pre-Existing Issues (from PR scans)")
            lines.append("")
            lines.append("| File | Issue | Severity |")
            lines.append("|------|-------|----------|")
            for issue in snapshot_existing_unique[:15]:
                f_path = issue.get("file", "?")
                title = (issue.get("title", "?")
                         .replace("|", "\\|").replace("\n", " "))
                severity = issue.get("severity", "?")
                lines.append(f"| `{f_path}` | {title} | {severity} |")
            lines.append("")

        # Top issue patterns from snapshot
        top_new = data.get("top_new_issues") or []
        if top_new:
            lines.append("## Recently Introduced Issue Patterns (from PR scans)")
            lines.append("")
            lines.append("Issue types appearing in NEW code across recent PRs:")
            lines.append("")
            for issue in top_new[:10]:
                if isinstance(issue, (list, tuple)) and len(issue) >= 2:
                    rule, count = issue[0], issue[1]
                    lines.append(f"- `{rule}` (appeared {count}x)")
                elif isinstance(issue, dict):
                    rule = issue.get("rule") or issue.get("title", "?")
                    lines.append(f"- {rule}")
                else:
                    lines.append(f"- {issue}")
            lines.append("")

        top_existing = data.get("top_existing_issues") or []
        if top_existing:
            lines.append("## Pre-Existing Issue Patterns (technical debt)")
            lines.append("")
            lines.append("Issue types in EXISTING code — verify exploitability "
                         "of the most common ones:")
            lines.append("")
            for issue in top_existing[:10]:
                if isinstance(issue, (list, tuple)) and len(issue) >= 2:
                    rule, count = issue[0], issue[1]
                    lines.append(f"- `{rule}` (appeared {count}x)")
                elif isinstance(issue, dict):
                    rule = issue.get("rule") or issue.get("title", "?")
                    lines.append(f"- {rule}")
                else:
                    lines.append(f"- {issue}")
            lines.append("")

        # Focus rules from scan_guidance
        focus_rules = (data.get("scan_guidance") or {}).get("focus_rules", [])
        if focus_rules:
            lines.append("## Specific Tool-Detected Patterns")
            lines.append("")
            lines.append("Static analysis already flagged these rule IDs — "
                         "verify which are real vulnerabilities:")
            lines.append("")
            for rule_entry in focus_rules[:10]:
                if isinstance(rule_entry, (list, tuple)) and len(rule_entry) >= 2:
                    rule, count = rule_entry[0], rule_entry[1]
                    lines.append(f"- `{rule}` (fired {count}x)")
                else:
                    lines.append(f"- `{rule_entry}`")
            lines.append("")

        # LLM existing debt notes
        if data.get("llm_existing_debt_notes"):
            lines.append("## Pre-Existing Security Debt — Context")
            lines.append("")
            lines.append(data["llm_existing_debt_notes"])
            lines.append("")

        # LLM risk-if-ignored
        if data.get("llm_risk_if_ignored"):
            lines.append("## Impact if Vulnerabilities Are Exploited")
            lines.append("")
            lines.append(data["llm_risk_if_ignored"])
            lines.append("")

        # PR activity
        pr_records = data.get("pr_records") or []
        if pr_records:
            lines.append("## Recent PR Activity")
            lines.append("")
            lines.append("PRs that contributed to the risk assessment:")
            lines.append("")
            lines.append("| PR | Risk (new/exist) | Crits (new/exist) "
                         "| Trend | Overridden |")
            lines.append("|----|------------------|-------------------"
                         "|-------|------------|")
            for p in pr_records[:10]:
                if not isinstance(p, dict):
                    continue
                trend = p.get("trend") or "—"
                overridden = "yes" if p.get("was_overridden") else "no"
                lines.append(
                    f"| #{p.get('pr_number', '?')} "
                    f"| {p.get('risk_score', 0)}/{p.get('existing_risk_score', 0)} "
                    f"| {p.get('new_critical_count', 0)}/"
                    f"{p.get('existing_critical_count', 0)} "
                    f"| {trend} "
                    f"| {overridden} |"
                )
            lines.append("")

            all_persisted = []
            for p in pr_records:
                if isinstance(p, dict):
                    all_persisted.extend(p.get("issues_persisted", []))
            all_persisted = list(dict.fromkeys(all_persisted))
            if all_persisted:
                lines.append("### Issues That Persisted Across Re-Scans")
                lines.append("")
                lines.append("Developers were unable or unwilling to fix these. "
                             "They are strong candidates for exploitation testing:")
                lines.append("")
                for issue in all_persisted[:10]:
                    lines.append(f"- `{issue}`")
                lines.append("")

        # Recommendations
        recommendations = data.get("recommendations") or []
        if recommendations:
            lines.append("## Recommendations from PR Reviews")
            lines.append("")
            lines.append("Items the reviewing AI suggested addressing:")
            lines.append("")
            for r in recommendations[:10]:
                if isinstance(r, dict):
                    rec_text = r.get("recommendation") or r.get("title") or str(r)
                    pr_num = r.get("pr", "")
                    pr_marker = f" (PR #{pr_num})" if pr_num else ""
                    lines.append(f"- {rec_text}{pr_marker}")
                else:
                    lines.append(f"- {r}")
            lines.append("")

    # ── Scan Approach — frames how to use what's above + the checklist ──
    lines.append("---")
    lines.append("")
    lines.append("## Scan Approach")
    lines.append("")
    if has_specific:
        lines.append("This scan has **two parts**:")
        lines.append("")
        lines.append("1. **Verify the specific issues itemized above.** For each "
                     "pre-flagged item, determine reachability and exploitability "
                     "with a concrete attack scenario or proof-of-concept.")
        lines.append("2. **Then run the general vulnerability checklist below** as "
                     "a safety net. Pit-boss had signal for some specific issues, "
                     "but it didn't see everything. The checklist ensures broad "
                     "coverage of vulnerability classes pit-boss couldn't predict.")
    else:
        lines.append("Pit-boss flagged this repo on risk score alone, with no "
                     "specific issues to itemize. **Run the general vulnerability "
                     "checklist below as a complete audit pass.**")
    lines.append("")

    # ── General web-app vulnerability checklist (always runs) ──
    lines.extend(_build_general_checklist_section())

    # ── Testing methodology (always identical, always last) ──
    lines.append("## Testing Methodology")
    lines.append("")
    lines.append("This is a source-code-level penetration test against a "
                 "locally cloned repository.")
    lines.append("")
    lines.append("### Approach")
    lines.append("")
    lines.append("1. **Read the priority files end-to-end** before running "
                 "any tests. Understand the data flow.")
    lines.append("2. **Identify entry points** for each focus area: where "
                 "untrusted input enters the application.")
    lines.append("3. **Trace input through to sinks** — where it affects "
                 "state, output, or external systems.")
    lines.append("4. **For each suspected vulnerability**, attempt to "
                 "construct a working proof-of-concept.")
    lines.append("5. **Verify pre-flagged criticals** — confirm whether each "
                 "is actually exploitable, not just theoretical.")
    lines.append("6. **Check for variants** — once you find one instance of "
                 "a vulnerability class, scan for similar patterns.")
    lines.append("")
    lines.append("### Each finding must include")
    lines.append("")
    lines.append("- The **vulnerability class** (e.g., SQL injection, prompt "
                 "injection, IDOR, path traversal)")
    lines.append("- **Affected file(s) and line number(s)**")
    lines.append("- A **concrete attack scenario** describing how an external "
                 "attacker would exploit this — who, how, and what they gain")
    lines.append("- A **proof-of-concept** payload, request, or code snippet "
                 "where possible")
    lines.append("- **Severity rating**: CRITICAL, HIGH, MEDIUM, or LOW")
    lines.append("- Whether **existing security controls** (authentication, "
                 "input validation, output encoding, rate limiting) mitigate it")
    lines.append("")
    lines.append("### Out of scope")
    lines.append("")
    lines.append("- Cosmetic code quality issues without security impact")
    lines.append("- Theoretical vulnerabilities without a realistic "
                 "exploitation path")
    lines.append("- Best-practice recommendations unrelated to the focus "
                 "areas above")
    lines.append("- Dependencies and third-party libraries (focus on "
                 "first-party code)")
    lines.append("")
    lines.append("### Note 'good catches'")
    lines.append("")
    lines.append("If you encounter security controls that are correctly "
                 "implemented, mention them. This gives the team positive "
                 "feedback alongside findings and helps them understand which "
                 "patterns to replicate.")

    return "\n".join(lines)

def _infer_urgency(data: Dict) -> str:
    """Fallback urgency calculation when LLM didn't run."""
    max_risk = data.get("max_risk", 0)
    max_existing = data.get("max_existing_risk", 0)
    crits = (data.get("new_critical_count", 0)
             + data.get("existing_critical_count", 0))
    if max_risk >= 9 or crits >= 5:
        return "CRITICAL"
    if max_risk >= 7 or max_existing >= 9 or crits >= 2:
        return "HIGH"
    return "MEDIUM"


def _load_pitboss_files_dual(args) -> List[Dict]:
    """
    Load pit-boss files from the appropriate source based on args.

    Priority order:
      1. If --pitboss-json given: local files (legacy).
      2. If --snapshots-prefix and/or --candidates-prefix given: dual-source.
      3. If --s3-prefix given (legacy): single-source S3, treated as snapshots.

    Returns a list of pitboss file dicts, possibly filtered by --month.
    """
    pitboss_files: List[Dict] = []

    # Mode 1: local files (legacy, unchanged)
    if getattr(args, "pitboss_json", None):
        paths = (args.pitboss_json
                 if isinstance(args.pitboss_json, list)
                 else [args.pitboss_json])
        print(f"\n📥 Loading from local files ...")
        pitboss_files = load_pitboss_files_local(paths)

    # Mode 2: dual S3 prefixes (new — PR1 default for GHA)
    elif (getattr(args, "snapshots_prefix", None)
          or getattr(args, "candidates_prefix", None)):
        snap_prefix = getattr(args, "snapshots_prefix", "") or ""
        cand_prefix = getattr(args, "candidates_prefix", "") or ""

        if snap_prefix:
            print(f"\n📥 Loading snapshots from "
                  f"s3://{S3_BUCKET}/{snap_prefix}")
            pitboss_files.extend(load_pitboss_files_from_s3(snap_prefix))

        if cand_prefix:
            print(f"\n📥 Loading candidates from "
                  f"s3://{S3_BUCKET}/{cand_prefix}")
            pitboss_files.extend(load_pitboss_files_from_s3(cand_prefix))

    # Mode 3: legacy single prefix
    elif getattr(args, "s3_prefix", None):
        print(f"\n📥 Loading from S3: s3://{S3_BUCKET}/{args.s3_prefix}")
        pitboss_files = load_pitboss_files_from_s3(args.s3_prefix)

    else:
        return []

    # Optional month filter (caller-provided convention: "YYYY-MM")
    month = getattr(args, "month", None)
    if month:
        before = len(pitboss_files)
        pitboss_files = _filter_files_by_month(pitboss_files, month)
        if len(pitboss_files) < before:
            print(f"   Month filter ({month}): "
                  f"{before} files → {len(pitboss_files)} files")

    return pitboss_files

def cmd_precheck(args):
    """Read-only check: does any repo qualify for scanning?

    Applies threshold + monthly dedup. Does NOT write tasks.json or mark
    sources as processed. Sets GitHub Actions output 'has_work'.
    """
    print("=" * 60)
    print("  repo-shakedown — Precheck (read-only)")
    print("=" * 60)

    pitboss_files = _load_pitboss_files_dual(args)
    if not pitboss_files:
        print("\n⚠️  No pit-boss files to check.")
        output_file = os.environ.get("GITHUB_OUTPUT")
        if output_file:
            with open(output_file, "a") as f:
                f.write("has_work=false\n")
                f.write("candidate_count=0\n")
        return 0

    merged = build_merged_repo_index(pitboss_files)
    print(f"\n   Merged index: {len(merged)} unique repos across all sources")


    has_work = False
    eligible = []         # passes threshold, not yet scanned this month
    already_scanned = []  # passes threshold, but in scanned_repos.json
    below_threshold = []  # in merged index but doesn't pass threshold

    for repo, data in merged.items():
        max_risk = data.get("max_risk", 0)
        max_existing = data.get("max_existing_risk", 0)
        priority = data.get("priority_score", 0)
        effective_risk = max(max_risk, max_existing)
        llm_marker = " [LLM-enriched]" if data.get("llm_scan_instructions") else ""
        priority_marker = f" priority={priority}" if priority > 0 else ""

        # Bucket the repo
        passes_threshold = (effective_risk >= args.threshold) or (priority > 0)

        if not passes_threshold:
            below_threshold.append((repo, max_risk, max_existing, priority,
                                    priority_marker, llm_marker))
            continue

        if _is_repo_scanned_this_month(repo):
            already_scanned.append((repo, max_risk, max_existing, priority,
                                    priority_marker, llm_marker))
            continue

        eligible.append((repo, max_risk, max_existing, priority,
                         priority_marker, llm_marker))
        has_work = True

    # Sort each bucket by effective risk descending so highest-signal repos
    # appear first within their group
    def _sort_key(entry):
        _, max_r, max_e, _, _, _ = entry
        return max(max_r, max_e)

    eligible.sort(key=_sort_key, reverse=True)
    already_scanned.sort(key=_sort_key, reverse=True)
    below_threshold.sort(key=_sort_key, reverse=True)

    # ── Print the three buckets ──────────────────────────────
    if eligible:
        print(f"\n✅ Eligible to scan now ({len(eligible)} repo(s)):")
        for repo, max_r, max_e, _, prio_mk, llm_mk in eligible:
            print(f"   ✅ {repo} (new={max_r}, existing={max_e}{prio_mk}{llm_mk})")
    else:
        print(f"\n✅ Eligible to scan now: none")

    if already_scanned:
        print(f"\n☑️  Already scanned this month "
              f"({len(already_scanned)} repo(s), in scanned_repos.json):")
        for repo, max_r, max_e, _, prio_mk, llm_mk in already_scanned:
            print(f"   ☑️  {repo} (new={max_r}, existing={max_e}{prio_mk}{llm_mk})")

    if below_threshold:
        print(f"\n⏬ Below threshold {args.threshold} "
              f"({len(below_threshold)} repo(s) — pit-boss saw activity but "
              f"effective_risk < threshold):")
        for repo, max_r, max_e, _, prio_mk, llm_mk in below_threshold:
            print(f"   ⏬ {repo} (new={max_r}, existing={max_e}{prio_mk}{llm_mk})")

    # ── Summary ─────────────────────────────────────────────
    print(f"\n📋 Precheck summary:")
    print(f"   Total repos in merged index:  {len(merged)}")
    print(f"   Eligible to scan now:         {len(eligible)}")
    print(f"   Already scanned this month:   {len(already_scanned)}")
    print(f"   Below threshold {args.threshold}:           "
          f"{len(below_threshold)}")
    print(f"   Has work:                     {has_work}")

    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            f.write(f"has_work={'true' if has_work else 'false'}\n")
            f.write(f"candidate_count={len(eligible)}\n")

    return 0

def cmd_prepare(args):
    """Phase 1: Read pit-boss data, generate task queue."""
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

    if args.reprocess:
        if PROCESSED_FILE.exists():
            PROCESSED_FILE.unlink()
        print("  🔄 Reprocess mode — ignoring previous tracking")

    # ── Load all pit-boss files (snapshots + candidates) ────
    pitboss_files = _load_pitboss_files_dual(args)
    if not pitboss_files:
        print("\n⚠️  No new pit-boss files to process.")
        return 0

    # Classify what we got for the operator
    snap_count = sum(
        1 for pf in pitboss_files
        if _classify_pitboss_file(pf.get("data", {})) == "snapshot"
    )
    cand_count = sum(
        1 for pf in pitboss_files
        if _classify_pitboss_file(pf.get("data", {})) == "candidates"
    )
    print(f"\n   Loaded: {snap_count} snapshot(s), {cand_count} candidates file(s)")

    # ── Merge per-repo across all sources ───────────────────
    merged = build_merged_repo_index(pitboss_files)
    print(f"   Merged index: {len(merged)} unique repos")

    threshold = getattr(args, "threshold", 5)
    above = sum(
        1 for v in merged.values()
        if max(v.get("max_risk", 0), v.get("max_existing_risk", 0)) >= threshold
        or v.get("priority_score", 0) > 0
    )
    enriched = sum(1 for v in merged.values() if v.get("llm_scan_instructions"))
    print(f"   Above threshold ({threshold}/10) or pit-boss-flagged: {above}")
    print(f"   With LLM-enriched targeting: {enriched}")

    # ── Build tasks ─────────────────────────────────────────
    existing_tasks = load_tasks()
    existing_repos = {t["repo"] for t in existing_tasks
                      if t["status"] == "pending"}

    new_tasks = extract_tasks_from_merged(
        merged, repos_dir, auto_clone=auto_clone, threshold=threshold,
    )

    added = 0
    skipped_existing = 0
    skipped_monthly = 0

    for task in new_tasks:
        if task["repo"] in existing_repos:
            print(f"  ⏭️  {task['repo']} — already has a pending task")
            skipped_existing += 1
            continue
        if _is_repo_scanned_this_month(task["repo"]):
            print(f"  ⏭️  {task['repo']} — already scanned this month "
                  f"({_get_month_key()})")
            skipped_monthly += 1
            continue

        # Tag with sources for traceability
        task["source_keys"] = [
            pf["source_key"]
            for pf in pitboss_files
        ]
        task["source_names"] = [
            str(pf.get("s3_key", pf.get("local_path")))
            for pf in pitboss_files
        ]
        existing_tasks.append(task)
        existing_repos.add(task["repo"])
        added += 1

        priority_marker = f" priority={task['priority_score']}" \
            if task.get("priority_score", 0) > 0 else ""
        llm_marker = " [LLM-enriched]" if task.get("has_llm_enrichment") else ""
        print(f"  ✅ {task['repo']} "
              f"(risk={task['max_risk']}{priority_marker}{llm_marker})")

    # Mark all sources as processed
    for pf in pitboss_files:
        mark_source_processed(pf["source_key"])

    save_tasks(existing_tasks)

    pending = sum(1 for t in existing_tasks if t["status"] == "pending")
    processed = load_processed_sources()
    print(f"\n📋 Summary:")
    print(f"   New tasks added:    {added}")
    print(f"   Skipped (pending):  {skipped_existing}")
    print(f"   Skipped (monthly):  {skipped_monthly}")
    print(f"   Total pending:      {pending}")
    print(f"   Sources processed:  {len(processed)} (lifetime)")
    print(f"   Queue file:         {TASKS_FILE}")

    print(f"\n{'=' * 60}")
    print(f"  Run `python repo_shakedown.py scan` to scan one task.")
    print(f"  Run `python repo_shakedown.py run ...` to scan all tasks.")
    print(f"{'=' * 60}")
    return 0

# ── Phase 2: Scan ────────────────────────────────────────────────

# Mapping from pit-boss scan mode to Strix invocation parameters
SCAN_MODE_CONFIG = {
    "quick": {
        "strix_mode": "quick",
        "reasoning_effort": "medium",
        "timeout_seconds": 3600,    # 1 hour
        "description": "fast scan, medium reasoning",
    },
    "default": {
        "strix_mode": "standard",
        "reasoning_effort": "high",
        "timeout_seconds": 14400,   # 4 hours
        "description": "standard scan, high reasoning",
    },
    "deep": {
        "strix_mode": "deep",
        "reasoning_effort": "high",
        "timeout_seconds": 21600,   # 6 hours
        "description": "deep scan, high reasoning",
    },
}


def run_strix(task: Dict, llm_model: str) -> int:
    """
    Invoke Strix CLI in headless mode.

    Reads task["suggested_scan_mode"] (set by pit-boss via PR1) to vary
    scan depth, reasoning effort, and timeout. Tasks without that field
    default to "default" mode for backward compatibility.

    Returns exit code: 0 = clean, 2 = vulns found, other = failure.
    """
    repo_path = task["repo_path"]
    instruction_file = task["instruction_file"]
    scan_mode = task.get("suggested_scan_mode") or "default"

    # Look up config; fall back to default if pit-boss returned an unknown mode
    config = SCAN_MODE_CONFIG.get(scan_mode, SCAN_MODE_CONFIG["default"])

    env = os.environ.copy()
    env["STRIX_LLM"] = llm_model
    env["STRIX_REASONING_EFFORT"] = config["reasoning_effort"]

    # Set the right API key env var for the provider
    api_key = resolve_api_key(llm_model)
    if api_key:
        env["LLM_API_KEY"] = api_key

    cmd = [
        "strix",
        "-n",
        "--target", repo_path,
        "--instruction-file", instruction_file,
        "--scan-mode", config["strix_mode"],
    ]

    print(f"\n🔍 Running Strix:")
    print(f"   Repo:       {task['repo']}")
    print(f"   Mode:       {scan_mode} ({config['description']})")
    print(f"   Command:    {' '.join(cmd)}")
    print(f"   Target:     {repo_path}")
    print(f"   Effort:     {config['reasoning_effort']}")
    print(f"   Timeout:    {config['timeout_seconds'] // 60} min")
    print(f"   LLM:        {llm_model}")
    print(f"   Tier:       "
          f"{'A (LLM-enriched)' if task.get('has_llm_enrichment') else 'B/C (deterministic)'}")
    print("")

    try:
        result = subprocess.run(
            cmd, env=env,
            capture_output=False,
            timeout=config["timeout_seconds"],
        )
        return result.returncode
    except subprocess.TimeoutExpired:
        timeout_hours = config["timeout_seconds"] / 3600
        print(f"  ⚠️  Strix scan timed out ({timeout_hours:.1f} hour limit "
              f"for mode '{scan_mode}')")
        return -1
    except FileNotFoundError:
        print("  ❌ Strix CLI not found. Install: "
              "curl -sSL https://strix.ai/install | bash")
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
    print(f"   Effort: high")

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

    # Refresh in-memory task so the report sees exit_code, duration, etc.
    task = next((t for t in load_tasks() if t["id"] == task["id"]), task)

    # Copy results (excluding events.jsonl — large agent trace, not used)
    if run_dir and run_dir.exists():
        dest = RESULTS_DIR / task["id"]
        dest.mkdir(parents=True, exist_ok=True)
        for f in run_dir.rglob("*"):
            if f.is_file() and f.name != "events.jsonl":
                rel = f.relative_to(run_dir)
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(f.read_bytes())
        print(f"   Results: {dest}")

    # Report
    if status == "done":
        _report_single_task(task, run_dir, args.llm)

    tasks = load_tasks()
    remaining = sum(1 for t in tasks if t["status"] == "pending")
    print(f"\n📋 Remaining pending: {remaining}")
    return 0 if status == "done" else 1


# ── Phase 3: Report ──────────────────────────────────────────────


def _parse_strix_findings(run_dir: Optional[Path]) -> List[Dict[str, str]]:
    """Read vulnerabilities.csv from the Strix run dir.
 
    Returns a list of {id, title, severity, timestamp, file} dicts.
    Empty list if the CSV is missing, empty, or unreadable.
    Never reads events.jsonl.
    """
    if not run_dir or not run_dir.exists():
        return []
 
    csv_path = run_dir / "vulnerabilities.csv"
    if not csv_path.exists():
        return []
 
    try:
        with csv_path.open() as f:
            reader = csv.DictReader(f)
            return [row for row in reader if row.get("id")]
    except Exception as e:
        print(f"  ⚠️  Could not parse vulnerabilities.csv: {e}")
        return []


def _load_finding_detail(run_dir: Optional[Path], finding_id: str) -> Optional[str]:
    """Load the per-finding markdown file produced by Strix.

    Strix writes one markdown per finding at:
        <run_dir>/vulnerabilities/vuln-<id>.md

    Returns the file contents as a string, or None if missing/unreadable.
    Used as the body of per-finding Jira tickets.
    """
    if not run_dir or not run_dir.exists() or not finding_id:
        return None

    # finding_id from vulnerabilities.csv is typically the bare number ('0001')
    # or already prefixed ('vuln-0001'). Handle both.
    if finding_id.startswith("vuln-"):
        filename = f"{finding_id}.md"
    else:
        filename = f"vuln-{finding_id}.md"

    md_path = run_dir / "vulnerabilities" / filename
    if not md_path.exists():
        # Some Strix versions might use a different name convention
        # — try variations
        for alt in (run_dir / "vulnerabilities" / f"{finding_id}.md",
                    run_dir / f"vuln-{finding_id}.md"):
            if alt.exists():
                md_path = alt
                break
        else:
            return None

    try:
        return md_path.read_text()
    except Exception as e:
        print(f"  ⚠️  Could not read {md_path}: {e}")
        return None

def _read_strix_pentest_report(run_dir: Optional[Path]) -> Optional[str]:
    """Return the contents of Strix's penetration_test_report.md, or None."""
    if not run_dir or not run_dir.exists():
        return None
    report = run_dir / "penetration_test_report.md"
    if not report.exists():
        return None
    try:
        return report.read_text()
    except Exception as e:
        print(f"  ⚠️  Could not read penetration_test_report.md: {e}")
        return None
 
 
# ── Report assembly ──────────────────────────────────────────────
 

def _build_pitboss_mapping_section(task: Dict, findings: List[Dict]) -> str:
    """Prepend block that maps Strix findings to pit-boss risk context.

    Surfaces the scope tier, pit-boss-flagged issues, and the actual Strix
    findings so reviewers can immediately see correlation/divergence between
    what pit-boss expected and what Strix found.
    """
    lines = []
    lines.append(f"# Shakedown Report: {task['repo']}")
    lines.append("")

    # ── Scope context ───────────────────────────────────────
    lines.append("## Scan Configuration")
    lines.append("")
    lines.append(f"- **Repo:** `{task['repo']}`")
    lines.append(f"- **Source snapshot(s):** "
                 f"{', '.join(task.get('source_names', [task.get('source_name', 'N/A')]))}")
    lines.append(f"- **Scan mode used:** "
                 f"{task.get('suggested_scan_mode', 'default')}")
    lines.append(f"- **Scan type:** "
                f"{'Itemized + general checklist' if task.get('has_specific_issues') else 'General checklist (risk-flagged only)'}")
    if task.get("priority_score"):
        lines.append(f"- **Pit-boss priority score:** {task['priority_score']}")
    lines.append("")

    # ── Pit-boss risk picture ───────────────────────────────
    lines.append("## Pit-Boss Risk Picture")
    lines.append("")
    lines.append(f"- Max NEW risk: {task['max_risk']}/10")
    lines.append(f"- Max EXISTING risk: {task['max_existing_risk']}/10")
    lines.append(f"- Critical issues flagged by pit-boss: "
                 f"{task['critical_count']}")
    lines.append(f"- Override count: {task['override_count']}")
    lines.append("")

    # ── Why pit-boss flagged this repo ──────────────────────
    reasons = task.get("reasons", [])
    if reasons:
        lines.append("**Pit-boss flagged this repo because:**")
        for r in reasons:
            lines.append(f"- {r}")
        lines.append("")

    # ── Scan execution ──────────────────────────────────────
    lines.append("## Scan Execution")
    lines.append("")
    lines.append(f"- Duration: "
                 f"{task.get('duration_seconds', 0) // 60} min")
    lines.append(f"- Strix exit code: {task.get('strix_exit_code')}")
    lines.append(f"- LLM used: {task.get('llm_used', 'N/A')}")
    lines.append("")

    # ── Strix findings summary ──────────────────────────────
    lines.append("## Strix Findings Summary")
    lines.append("")
    if findings:
        lines.append(f"Strix reported **{len(findings)} finding(s)**:")
        lines.append("")
        lines.append("| ID | Severity | Title |")
        lines.append("|----|----------|-------|")
        for f in findings:
            title = f.get("title", "").replace("|", "\\|")
            lines.append(f"| {f.get('id', '?')} "
                         f"| {f.get('severity', '?')} "
                         f"| {title} |")
        lines.append("")
    else:
        lines.append("Strix produced no structured findings "
                     "(no `vulnerabilities.csv`).")
        lines.append("")

    lines.append("---")
    lines.append("")
    return "\n".join(lines)
 
def _assemble_report(task: Dict, run_dir: Optional[Path]) -> tuple[str, List[Dict]]:
    """Build the full report markdown.
 
    Returns (report_text, findings_list).
    """
    findings = _parse_strix_findings(run_dir)
    mapping = _build_pitboss_mapping_section(task, findings)
 
    if not findings:
        # No structured findings → state that explicitly. Don't synthesize.
        body = (
            "## Result\n\n"
            "**No findings to report.** Strix did not produce a structured "
            "`vulnerabilities.csv`. This usually means either the scan "
            "completed cleanly, or the scan terminated before producing "
            "findings. Inspect the uploaded `shakedown-results.zip` for the "
            "raw run output if needed.\n"
        )
        return mapping + body, findings
 
    pentest_report = _read_strix_pentest_report(run_dir)
    if pentest_report:
        body = "## Strix Penetration Test Report\n\n" + pentest_report
    else:
        body = (
            "## Strix Penetration Test Report\n\n"
            "_Strix reported findings in `vulnerabilities.csv` but the "
            "consolidated `penetration_test_report.md` is not available. "
            "See per-finding markdown files in the uploaded results zip._\n"
        )
    return mapping + body, findings
 
 
# ── S3 paths and uploads ─────────────────────────────────────────
 
def _get_aws_region() -> Optional[str]:
    """Resolve AWS region from env. No fallback."""
    return os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or None
 
 
def _s3_scan_prefix(task_id: str) -> str:
    """The S3 'folder' for this scan, e.g. shakedown-reports/<task_id>/."""
    base = S3_REPORTS_PREFIX.rstrip("/") if S3_REPORTS_PREFIX else "shakedown-reports"
    return f"{base}/{task_id}"
 
 
def _s3_console_url(bucket: str, key: str, region: str) -> str:
    """Build an AWS console URL for an S3 object.
 
    Uses the 'object' view so a single click opens the object detail page.
    """
    return (
        f"https://{region}.console.aws.amazon.com/s3/object/"
        f"{bucket}?region={region}&prefix={quote(key, safe='/')}"
    )
 
 
def _zip_run_dir(run_dir: Path, dest: Path) -> Optional[Path]:
    """Zip the entire Strix run dir into dest. Returns dest on success."""
    try:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in run_dir.rglob("*"):
                if f.is_file():
                    zf.write(f, arcname=f.relative_to(run_dir))
        return dest
    except Exception as e:
        print(f"  ⚠️  Could not build results zip: {e}")
        return None
 
 
def _upload_scan_to_s3(
    task: Dict,
    report_text: str,
    run_dir: Optional[Path],
) -> Optional[Dict[str, str]]:
    """Upload report + run-dir contents + zip to S3 under <prefix>/<task_id>/.
 
    Returns a dict of console URLs on success:
        {"report_url": ..., "zip_url": ..., "report_key": ..., "zip_key": ...}
    Returns None on any failure (caller should fall back to local).
    """
    if not S3_BUCKET:
        return None
 
    region = _get_aws_region()
    if not region:
        print("  ⚠️  No AWS_DEFAULT_REGION/AWS_REGION set — cannot build console URLs")
        # We still try the upload; we just can't make clickable URLs
    try:
        import boto3
    except ImportError:
        print("  ⚠️  boto3 not installed — cannot upload to S3")
        return None
 
    try:
        s3 = boto3.client("s3")
        scan_prefix = _s3_scan_prefix(task["id"])
        report_filename = f"{task['id']}_report.md"
        zip_filename = "shakedown-results.zip"
 
        report_key = f"{scan_prefix}/{report_filename}"
        zip_key = f"{scan_prefix}/{zip_filename}"
 
        # 1. Upload the assembled report
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=report_key,
            Body=report_text.encode(),
            ContentType="text/markdown",
        )
        print(f"  ☁️  Report uploaded: s3://{S3_BUCKET}/{report_key}")
 
        # 2. Upload Strix run-dir contents (vulnerabilities.csv,
        #    vulnerabilities/*.md, penetration_test_report.md, etc.)
        #    Skip events.jsonl — irrelevant per requirements.
        if run_dir and run_dir.exists():
            for f in run_dir.rglob("*"):
                if not f.is_file():
                    continue
                if f.name == "events.jsonl":
                    continue
                rel = f.relative_to(run_dir).as_posix()
                key = f"{scan_prefix}/{rel}"
                try:
                    s3.upload_file(str(f), S3_BUCKET, key)
                except Exception as e:
                    print(f"  ⚠️  Failed to upload {rel}: {e}")
 
            # 3. Build and upload the zip of the entire run dir (events.jsonl excluded)
            zip_local = WORK_DIR / f"{task['id']}_results.zip"
            zip_local.parent.mkdir(parents=True, exist_ok=True)
            # Exclude events.jsonl from the zip too
            try:
                with zipfile.ZipFile(zip_local, "w", zipfile.ZIP_DEFLATED) as zf:
                    for f in run_dir.rglob("*"):
                        if f.is_file() and f.name != "events.jsonl":
                            zf.write(f, arcname=f.relative_to(run_dir).as_posix())
                s3.upload_file(str(zip_local), S3_BUCKET, zip_key)
                print(f"  ☁️  Results zip uploaded: s3://{S3_BUCKET}/{zip_key}")
            except Exception as e:
                print(f"  ⚠️  Failed to upload results zip: {e}")
                zip_key = None
 
        result = {"report_key": report_key, "zip_key": zip_key}
        if region:
            result["report_url"] = _s3_console_url(S3_BUCKET, report_key, region)
            if zip_key:
                result["zip_url"] = _s3_console_url(S3_BUCKET, zip_key, region)
        return result
 
    except Exception as e:
        print(f"  ⚠️  S3 upload failed: {e}")
        return None
 
 
def _save_report_locally(task: Dict, report_text: str) -> Path:
    """Fallback: write report to REPORTS_DIR. Always succeeds (or raises)."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{task['id']}_report.md"
    report_path.write_text(report_text)
    print(f"  💾 Report saved locally: {report_path}")
    return report_path
 
 
# ── Slack ────────────────────────────────────────────────────────
 
def _format_findings_for_slack(findings: List[Dict]) -> str:
    """One line per finding: '• [SEVERITY] title'. Truncate long lists."""
    if not findings:
        return "_No findings reported._"
    lines = []
    for f in findings[:15]:
        sev = f.get("severity", "?")
        title = f.get("title", "(untitled)")
        lines.append(f"• *[{sev}]* {title}")
    if len(findings) > 15:
        lines.append(f"_…and {len(findings) - 15} more_")
    return "\n".join(lines)


def _send_slack_notification(
    task: Dict,
    findings: List[Dict],
    s3_urls: Optional[Dict[str, str]],
):
    """Post to Slack. Lists findings from the CSV plus S3 console links.
 
    Falls back to console output when SLACK_WEBHOOK_URL is unset.
    """
    findings_block = _format_findings_for_slack(findings)
    vuln_count = len(findings)
    severity_summary = (
        ", ".join(sorted({f.get("severity", "?") for f in findings}))
        if findings else "none"
    )
    headline_emoji = "🚨" if vuln_count > 0 else "✅"
 
    # Build links section — always present, but says "unavailable" when missing
    link_lines = []
    if s3_urls and s3_urls.get("report_url"):
        link_lines.append(f"📄 *<{s3_urls['report_url']}|Full report>*")
    if s3_urls and s3_urls.get("zip_url"):
        link_lines.append(f"📦 *<{s3_urls['zip_url']}|Results zip>*")
    if not link_lines:
        link_lines.append("_Report links unavailable (S3 upload skipped or failed; "
                          "check GitHub Actions artifact)._")
    links_block = "\n".join(link_lines)
 
    if not SLACK_WEBHOOK_URL:
        print("\n  📨 Slack webhook not configured — printing summary to console:")
        print(f"  {headline_emoji} Shakedown: {task['repo']}")
        print(f"     Risk: new={task['max_risk']}/10, existing={task['max_existing_risk']}/10")
        print(f"     Findings: {vuln_count} ({severity_summary})")
        print(f"     {findings_block}")
        print(f"     {links_block}")
        return
 
    try:
        import urllib.request
        payload = {
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"{headline_emoji} Shakedown: {task['repo']}",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn",
                         "text": f"*New risk:* {task['max_risk']}/10"},
                        {"type": "mrkdwn",
                         "text": f"*Existing risk:* {task['max_existing_risk']}/10"},
                        {"type": "mrkdwn",
                         "text": f"*Findings:* {vuln_count} ({severity_summary})"},
                        {"type": "mrkdwn",
                         "text": f"*Duration:* {task.get('duration_seconds', 0) // 60}m"},
                    ],
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": findings_block[:2900]},
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": links_block},
                },
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
        print(f"     Findings: {findings_block}")
        print(f"     {links_block}")


# Severity levels that warrant a Jira ticket. Anything else (LOW,
# INFORMATIONAL, unknown) is silently skipped.
_JIRA_TICKET_SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM"}


def _truncate_summary(text: str, max_len: int = 240) -> str:
    """Trim text so the final Jira summary fits within Jira's 255-char limit.
    Leaves headroom for prefix wrapping."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def _create_jira_tickets_per_finding(
    task: Dict,
    findings: List[Dict],
    run_dir: Optional[Path],
    s3_urls: Optional[Dict[str, str]],
):
    """Create one Jira ticket per CRITICAL/HIGH/MEDIUM finding.

    Title format: [SEVERITY] - repo_name - finding_title
    Body: the per-finding markdown that Strix wrote to
          <run_dir>/vulnerabilities/vuln-<id>.md
    Plus: an S3 backlink to the full Strix output zip when available.

    Silently does nothing when Jira env vars aren't set (the existing control
    flag, governed by the GHA's enable_jira input which empties the secrets
    when false).
    """
    # Gate: same control flag as the old per-scan implementation.
    # The GHA's enable_jira=false sets these to empty strings, so unconfigured
    # = no-op, exactly like before.
    if not all([JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN]):
        if findings:
            actionable = [f for f in findings
                          if (f.get("severity") or "").upper()
                          in _JIRA_TICKET_SEVERITIES]
            if actionable:
                print(f"\n  🎫 Jira not configured — would have filed "
                      f"{len(actionable)} ticket(s)")
        return

    if not findings:
        print("\n  🎫 No findings — no Jira tickets to create")
        return

    try:
        from jira import JIRA, JIRAError
    except ImportError:
        print("  ⚠️  jira library not installed (pip install jira) — "
              "skipping Jira integration")
        return

    # Connect once, reuse for all tickets in this scan
    try:
        client = JIRA(
            server=JIRA_BASE_URL.rstrip("/"),
            basic_auth=(JIRA_EMAIL, JIRA_API_TOKEN),
            max_retries=2,
        )
    except Exception as e:
        print(f"  ⚠️  Jira connection failed: {e}")
        return

    # Repo name without org prefix, for the title
    repo_short = task["repo"].split("/")[-1] if "/" in task["repo"] else task["repo"]

    # Filter to severities we ticket on
    actionable_findings = [
        f for f in findings
        if (f.get("severity") or "").upper() in _JIRA_TICKET_SEVERITIES
    ]
    skipped_low = len(findings) - len(actionable_findings)

    if not actionable_findings:
        print(f"\n  🎫 No actionable findings "
              f"(skipped {skipped_low} LOW/info finding(s))")
        return

    print(f"\n  🎫 Creating Jira tickets for "
          f"{len(actionable_findings)} finding(s) "
          f"(skipped {skipped_low} LOW/info)")

    created = []
    failed = 0

    for finding in actionable_findings:
        finding_id = finding.get("id", "")
        severity = (finding.get("severity") or "UNKNOWN").upper()
        finding_title = finding.get("title", "(untitled finding)")

        # Title: [SEVERITY] - repo_short - finding_title
        summary_body = f"[{severity}] - {repo_short} - {finding_title}"
        summary = _truncate_summary(summary_body)

        # Body: per-finding markdown if available, else CSV-derived fallback
        detail_md = _load_finding_detail(run_dir, finding_id)
        if detail_md:
            description_parts = [detail_md]
        else:
            description_parts = [
                f"# {finding_title}\n",
                f"**ID:** {finding_id}",
                f"**Severity:** {severity}",
                f"**Repo:** {task['repo']}",
                "",
                "_Strix did not produce a per-finding markdown for this "
                "vulnerability. See `vulnerabilities.csv` in the Strix run "
                "output for the raw record._",
            ]

        # S3 backlink for full Strix output
        if s3_urls and s3_urls.get("zip_url"):
            description_parts.append("\n---\n")
            description_parts.append(
                f"**Full Strix output:** {s3_urls['zip_url']}"
            )
        if s3_urls and s3_urls.get("report_url"):
            description_parts.append(
                f"**Assembled report:** {s3_urls['report_url']}"
            )

        # Truncate body to stay under Jira's description limit (~32k)
        description = "\n".join(description_parts)
        if len(description) > 30000:
            description = description[:30000] + "\n\n_[truncated]_"

        priority_name = {
            "CRITICAL": "Highest",
            "HIGH": "High",
            "MEDIUM": "Medium",
        }.get(severity, "Medium")

        fields = {
            "project": {"key": JIRA_PROJECT_KEY},
            "summary": summary,
            "description": description,
            "issuetype": {"name": "Task"},
            "priority": {"name": priority_name},
            "labels": [
                "repo-shakedown",
                "security-vuln-found",
                "automated",
                f"severity-{severity.lower()}",
                # Sanitize the repo label — Jira labels can't contain '/'
                f"repo-{task['repo'].replace('/', '-')}",
            ],
        }

        if JIRA_EPIC_KEY:
            fields["parent"] = {"key": JIRA_EPIC_KEY}

        # Try with everything; fall back without priority if that's rejected;
        # finally without parent if that's rejected too.
        attempts = [
            ("full", fields),
        ]
        f_no_priority = dict(fields)
        f_no_priority.pop("priority", None)
        attempts.append(("without priority", f_no_priority))
        if "parent" in f_no_priority:
            f_no_parent = dict(f_no_priority)
            f_no_parent.pop("parent", None)
            attempts.append(("without priority and without parent", f_no_parent))

        ticket_created = False
        for label, attempt_fields in attempts:
            try:
                issue = client.create_issue(fields=attempt_fields)
                key = issue.key
                created.append({
                    "key": key,
                    "severity": severity,
                    "title": finding_title,
                    "fallback": label,
                })
                fallback_note = f" ({label})" if label != "full" else ""
                print(f"     ✓ {key} [{severity}] {finding_title}{fallback_note}")
                ticket_created = True
                break
            except JIRAError as e:
                # Only retry on field-shape errors. Auth/permission errors
                # won't get better by removing fields, so bail out.
                err_text = str(e.text).lower() if hasattr(e, "text") else str(e).lower()
                if e.status_code in (400, 422) and (
                    "field" in err_text
                    or "parent" in err_text
                    or "priority" in err_text
                ):
                    continue
                else:
                    print(f"     ❌ [{severity}] {finding_title}: "
                          f"{e.status_code} {err_text}")
                    failed += 1
                    ticket_created = True  # don't try more attempts
                    break
            except Exception as e:
                print(f"     ❌ [{severity}] {finding_title}: {e}")
                failed += 1
                ticket_created = True
                break

        if not ticket_created:
            print(f"     ❌ [{severity}] {finding_title}: all attempts exhausted")
            failed += 1

    # Summary
    if created and failed:
        print(f"  🎫 Jira: created {len(created)}, failed {failed}")
    elif created:
        print(f"  🎫 Jira: created {len(created)} ticket(s)")
    elif failed:
        print(f"  🎫 Jira: all {failed} ticket(s) failed")

def _report_single_task(
    task: Dict,
    run_dir: Optional[Path],
    cli_llm: Optional[str] = None,  # kept for signature compatibility; unused
):
    """Generate the report from Strix's structured output and notify.
 
    Pipeline:
      1. Parse vulnerabilities.csv → findings list (or empty)
      2. Read penetration_test_report.md verbatim → body
         (or "no findings" message if no CSV)
      3. Prepend pit-boss mapping section
      4. Upload report + run-dir + zip to S3 (primary)
      5. If S3 fails or is unconfigured → save report to REPORTS_DIR
      6. Slack: lists findings + S3 console URLs
      7. Jira: ticket with the report as the body
    """
    print(f"\n📝 Generating report for {task['repo']} ...")
 
    report_text, findings = _assemble_report(task, run_dir)
 
    # Try S3 first
    s3_urls = _upload_scan_to_s3(task, report_text, run_dir)
 
    # Track where the report ended up so the task record points to it
    if s3_urls and s3_urls.get("report_key"):
        report_location = f"s3://{S3_BUCKET}/{s3_urls['report_key']}"
    else:
        # Fallback: write to local REPORTS_DIR (picked up by GHA artifact)
        local_path = _save_report_locally(task, report_text)
        report_location = str(local_path)
 
    # Update the task record
    tasks = load_tasks()
    update_task_status(
        tasks, task["id"], task.get("status", "done"),
        report_file=report_location,
    )
 
    # Notify
    _send_slack_notification(task, findings, s3_urls)
    _create_jira_tickets_per_finding(task, findings, run_dir, s3_urls)
 
    # Monthly dedup
    _mark_repo_scanned_this_month(task["repo"])


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
        print(f"   {icon} {t['repo']} — risk {t['max_risk']}/10"
            f"{vuln}{llm_note}")

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


def _validate_source_args(args) -> Optional[str]:
    """
    Ensure at least one source flag was provided. Returns an error message
    (str) on failure, None on success.
    """
    if args.command in ("run-one", "run", "prepare", "precheck"):
        has_source = (
            getattr(args, "pitboss_json", None)
            or getattr(args, "s3_prefix", None)
            or getattr(args, "snapshots_prefix", None)
            or getattr(args, "candidates_prefix", None)
        )
        if not has_source:
            return (
                "No pit-boss source provided. Pass at least one of: "
                "--pitboss-json, --s3-prefix, --snapshots-prefix, "
                "--candidates-prefix"
            )
    return None

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
                   help="LLM model for Strix "
                        "(e.g. gemini/gemini-2.5-pro, openai/gpt-5)")

    sub = p.add_subparsers(dest="command", required=True)

    # ── run-one: CI / GitHub Action mode ───────────────────────
    run_one_p = sub.add_parser(
        "run-one",
        help="CI mode: prepare + scan one repo + report (monthly dedup applied)",
    )
    run_one_p.add_argument("--repos-dir", required=True,
                           help="Directory to store cloned repositories")
    run_one_p.add_argument("--auto-clone", action="store_true",
                           help="Clone missing repos automatically using git clone")
    run_one_p.add_argument("--reprocess", action="store_true",
                           help="Ignore tracking — reprocess all files")
    run_one_p.add_argument("--threshold", type=int, default=5,
                           help="Min max_risk score to include a repo (default: 5)")
    run_one_p.add_argument(
        "--pitboss-json", nargs="+",
        help="Path(s) to local pit-boss JSON file(s) — "
            "either snapshots or candidates.json. Auto-detected by structure.",
    )
    run_one_p.add_argument(
        "--s3-prefix", type=str,
        help="[Legacy] Single S3 prefix for pit-boss files. "
            "Prefer --snapshots-prefix and/or --candidates-prefix.",
    )
    run_one_p.add_argument(
        "--snapshots-prefix", type=str,
        help="S3 prefix for pit-boss snapshot files "
            "(e.g. pitboss-snapshots/2026-04/). Combined with --candidates-prefix "
            "for richer scan instructions.",
    )
    run_one_p.add_argument(
        "--candidates-prefix", type=str,
        help="S3 prefix for pit-boss candidates.json files "
            "(e.g. shakedown/). Combined with --snapshots-prefix.",
    )
    run_one_p.add_argument(
        "--month", type=str,
        help="Filter ingested files to this month label "
            "(YYYY-MM). Useful when --candidates-prefix points at the parent "
            "shakedown/ folder containing many weeks.",
    )

    # ── run: all-in-one for local use ──────────────────────────
    run_p = sub.add_parser("run",
                           help="All-in-one: prepare + clone + scan all + report")
    run_p.add_argument("--repos-dir", required=True,
                       help="Directory to store cloned repositories")
    run_p.add_argument("--auto-clone", action="store_true",
                       help="Clone missing repos automatically using git clone")
    run_p.add_argument("--reprocess", action="store_true",
                       help="Ignore tracking — reprocess all files")
    run_p.add_argument("--threshold", type=int, default=5,
                       help="Min max_risk score to include a repo (default: 5)")
    run_p.add_argument(
        "--pitboss-json", nargs="+",
        help="Path(s) to local pit-boss JSON file(s) — "
            "either snapshots or candidates.json. Auto-detected by structure.",
    )
    run_p.add_argument(
        "--s3-prefix", type=str,
        help="[Legacy] Single S3 prefix for pit-boss files. "
            "Prefer --snapshots-prefix and/or --candidates-prefix.",
    )
    run_p.add_argument(
        "--snapshots-prefix", type=str,
        help="S3 prefix for pit-boss snapshot files "
            "(e.g. pitboss-snapshots/2026-04/). Combined with --candidates-prefix "
            "for richer scan instructions.",
    )
    run_p.add_argument(
        "--candidates-prefix", type=str,
        help="S3 prefix for pit-boss candidates.json files "
            "(e.g. shakedown/). Combined with --snapshots-prefix.",
    )
    run_p.add_argument(
        "--month", type=str,
        help="Filter ingested files to this month label "
            "(YYYY-MM). Useful when --candidates-prefix points at the parent "
            "shakedown/ folder containing many weeks.",
    )


    # ── prepare: build task queue only ─────────────────────────
    prep = sub.add_parser("prepare", help="Build scan tasks from candidates.json")
    prep.add_argument("--repos-dir", required=True,
                      help="Directory containing cloned repositories")
    prep.add_argument("--auto-clone", action="store_true",
                      help="Clone missing repos automatically using git clone")
    prep.add_argument("--reprocess", action="store_true",
                      help="Ignore tracking — reprocess all files")
    prep.add_argument("--threshold", type=int, default=5,
                      help="Min max_risk score to include a repo (default: 5)")
    prep.add_argument(
        "--pitboss-json", nargs="+",
        help="Path(s) to local pit-boss JSON file(s) — "
            "either snapshots or candidates.json. Auto-detected by structure.",
    )
    prep.add_argument(
        "--s3-prefix", type=str,
        help="[Legacy] Single S3 prefix for pit-boss files. "
            "Prefer --snapshots-prefix and/or --candidates-prefix.",
    )
    prep.add_argument(
        "--snapshots-prefix", type=str,
        help="S3 prefix for pit-boss snapshot files "
            "(e.g. pitboss-snapshots/2026-04/). Combined with --candidates-prefix "
            "for richer scan instructions.",
    )
    prep.add_argument(
        "--candidates-prefix", type=str,
        help="S3 prefix for pit-boss candidates.json files "
            "(e.g. shakedown/). Combined with --snapshots-prefix.",
    )
    prep.add_argument(
        "--month", type=str,
        help="Filter ingested files to this month label "
            "(YYYY-MM). Useful when --candidates-prefix points at the parent "
            "shakedown/ folder containing many weeks.",
    )


    # ── scan, report, status ────────────────────────────────────
    scan = sub.add_parser("scan", help="Run next pending scan")
    scan.add_argument("--force-reset", action="store_true",
                      help="Reset stuck 'running' tasks to 'pending'")

    precheck_p = sub.add_parser("precheck",
        help="Read-only: check whether any repo qualifies for scanning")
    precheck_p.add_argument("--threshold", type=int, default=5,
        help="Min max(new, existing) risk score (default: 5)")
    precheck_p.add_argument(
        "--pitboss-json", nargs="+",
        help="Path(s) to local pit-boss JSON file(s) — "
            "either snapshots or candidates.json. Auto-detected by structure.",
    )
    precheck_p.add_argument(
        "--s3-prefix", type=str,
        help="[Legacy] Single S3 prefix for pit-boss files. "
            "Prefer --snapshots-prefix and/or --candidates-prefix.",
    )
    precheck_p.add_argument(
        "--snapshots-prefix", type=str,
        help="S3 prefix for pit-boss snapshot files "
            "(e.g. pitboss-snapshots/2026-04/). Combined with --candidates-prefix "
            "for richer scan instructions.",
    )
    precheck_p.add_argument(
        "--candidates-prefix", type=str,
        help="S3 prefix for pit-boss candidates.json files "
            "(e.g. shakedown/). Combined with --snapshots-prefix.",
    )
    precheck_p.add_argument(
        "--month", type=str,
        help="Filter ingested files to this month label "
            "(YYYY-MM). Useful when --candidates-prefix points at the parent "
            "shakedown/ folder containing many weeks.",
    )


    sub.add_parser("report", help="Generate reports for completed scans")
    sub.add_parser("status", help="Show queue status")

    args = p.parse_args()
    err = _validate_source_args(args)
    if err:
        print(f"❌ {err}")
        return 1

    if args.command == "run-one":
        return cmd_run_one(args)
    elif args.command == "run":
        return cmd_run(args)
    elif args.command == "prepare":
        return cmd_prepare(args)
    elif args.command == "scan":
        return cmd_scan(args)
    elif args.command == "precheck":
        return cmd_precheck(args)
    elif args.command == "report":
        return cmd_report(args)
    elif args.command == "status":
        return cmd_status(args)


if __name__ == "__main__":
    sys.exit(main())