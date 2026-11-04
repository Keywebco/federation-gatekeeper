"""
Federation Automation Workflows — Scheduled Tasks for the Gatekeeper
Adds cron-scheduled workflow execution to the Gatekeeper node.

Workflows:
1. Vault Heartbeat Monitor  — pings vault health every 6 hours
2. GitHub Vault Sync Alert  — checks recent commits for anomalies every 12 hours
3. Federation Status        — aggregates Render service health every 4 hours
"""

import os
import json
import time
import asyncio
import logging
import threading
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("gatekeeper.workflows")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
VAULT_DIR = Path("/tmp/vault")
RENDER_API_KEY = os.getenv("RENDER_API_KEY", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
VAULT_REPO_API = os.getenv("VAULT_REPO_API", "Keywebco/federation-private-vault")

# ---------------------------------------------------------------------------
# Execution Log (in-memory, capped at 200 entries)
# ---------------------------------------------------------------------------
MAX_LOG_ENTRIES = 200
_execution_log: list[dict] = []
_log_lock = threading.Lock()


def _log_execution(workflow_id: str, status: str, detail: str, duration_ms: int = 0):
    """Record a workflow execution in the in-memory log."""
    entry = {
        "id": f"{workflow_id}-{int(time.time())}",
        "workflow": workflow_id,
        "status": status,
        "detail": detail[:2000],
        "duration_ms": duration_ms,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with _log_lock:
        _execution_log.append(entry)
        if len(_execution_log) > MAX_LOG_ENTRIES:
            _execution_log.pop(0)
    log.info(f"[{workflow_id}] {status}: {detail[:200]}")


def get_logs(workflow_id: Optional[str] = None, limit: int = 50) -> list[dict]:
    """Retrieve execution logs, optionally filtered by workflow."""
    with _log_lock:
        if workflow_id:
            filtered = [e for e in _execution_log if e["workflow"] == workflow_id]
        else:
            filtered = list(_execution_log)
    return filtered[-limit:]


# ---------------------------------------------------------------------------
# Workflow 1: Vault Heartbeat Monitor
# ---------------------------------------------------------------------------
def run_heartbeat() -> dict:
    """Check vault health: git status, file count, disk usage."""
    start = time.monotonic()
    try:
        if not VAULT_DIR.exists() or not (VAULT_DIR / ".git").exists():
            _log_execution("heartbeat", "warning", "vault_not_cloned: Vault not cloned yet")
            return {"ok": False, "error_code": "vault_not_cloned", "detail": "Vault not cloned yet"}

        # Check git status
        result = subprocess.run(
            ["git", "-C", str(VAULT_DIR), "status", "--porcelain"],
            capture_output=True, text=True, timeout=15,
        )
        dirty_files = len(result.stdout.strip().split("\n")) if result.stdout.strip() else 0

        # Count files
        md_files = list(VAULT_DIR.rglob("*.md"))
        all_files = [f for f in VAULT_DIR.rglob("*") if f.is_file() and ".git" not in str(f)]
        total_kb = sum(f.stat().st_size for f in all_files) / 1024

        # Last commit age
        commit_result = subprocess.run(
            ["git", "-C", str(VAULT_DIR), "log", "-1", "--format=%ct"],
            capture_output=True, text=True, timeout=10,
        )
        if commit_result.returncode == 0 and commit_result.stdout.strip():
            commit_age_hours = (time.time() - int(commit_result.stdout.strip())) / 3600
            git_log_failed = False
        else:
            commit_age_hours = -1
            git_log_failed = True

        elapsed = int((time.monotonic() - start) * 1000)
        status_str = "healthy"
        alerts = []

        if dirty_files > 0:
            alerts.append(f"{dirty_files} dirty files detected")
        if commit_age_hours > 168:  # 7 days
            alerts.append(f"Last commit {commit_age_hours:.0f}h ago (>7 days)")
        if git_log_failed:
            alerts.append("git log failed: no last commit found")
        if len(md_files) == 0:
            alerts.append("No markdown files found")
            status_str = "degraded"

        if alerts:
            status_str = "warning"

        detail = {
            "vault_status": status_str,
            "error_code": "git_log_failed" if git_log_failed else None,
            "markdown_files": len(md_files),
            "total_files": len(all_files),
            "total_size_kb": round(total_kb, 1),
            "dirty_files": dirty_files,
            "last_commit_age_hours": round(commit_age_hours, 1),
            "alerts": alerts,
        }
        _log_execution("heartbeat", status_str, json.dumps(detail), elapsed)
        return {"ok": True, "detail": detail}

    except Exception as e:
        elapsed = int((time.monotonic() - start) * 1000)
        _log_execution("heartbeat", "error", f"unexpected_error: {e}", elapsed)
        return {"ok": False, "error_code": "unexpected_error", "detail": str(e)}


# ---------------------------------------------------------------------------
# Workflow 2: GitHub Vault Sync Alert
# ---------------------------------------------------------------------------
def run_sync_alert() -> dict:
    """Check recent vault commits for anomalies (mass deletions, unknown authors)."""
    start = time.monotonic()
    try:
        if not VAULT_DIR.exists() or not (VAULT_DIR / ".git").exists():
            _log_execution("sync-alert", "error", "vault_not_cloned: Vault not cloned yet")
            return {"ok": False, "error_code": "vault_not_cloned", "detail": "Vault not cloned yet"}
        if not GITHUB_TOKEN:
            _log_execution("sync-alert", "skipped", "token_not_configured: No GITHUB_TOKEN configured")
            return {"ok": False, "error_code": "token_not_configured", "detail": "GITHUB_TOKEN not set"}

        # Pull latest
        if VAULT_DIR.exists() and (VAULT_DIR / ".git").exists():
            subprocess.run(
                ["git", "-C", str(VAULT_DIR), "pull", "--ff-only"],
                capture_output=True, text=True, timeout=60,
            )

        # Get recent commits (last 20)
        result = subprocess.run(
            ["git", "-C", str(VAULT_DIR), "log", "-20", "--format=%H|%an|%ae|%ci|%s"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            _log_execution("sync-alert", "error", f"git_log_failed: {result.stderr[:200]}")
            return {"ok": False, "error_code": "git_log_failed", "detail": "git log failed"}

        commits = []
        alerts = []
        known_authors = {"Keywebco", "keywebco", "github-actions", "GitHub", "Federation Vault Bot", "federation-vault-bot"}

        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("|", 4)
            if len(parts) < 5:
                continue
            commit = {
                "hash": parts[0][:8],
                "author": parts[1],
                "email": parts[2],
                "date": parts[3],
                "message": parts[4],
            }
            commits.append(commit)

            # Anomaly detection
            if not any(k.lower() in parts[1].lower() for k in known_authors):
                alerts.append(f"Unknown author: {parts[1]} ({parts[0][:8]})")

            if any(word in parts[4].lower() for word in ["delete all", "remove all", "purge", "wipe"]):
                alerts.append(f"Suspicious commit message: {parts[4][:80]} ({parts[0][:8]})")

        # Check for mass file changes via diffstat on last commit
        if commits:
            diff_result = subprocess.run(
                ["git", "-C", str(VAULT_DIR), "diff", "--stat", "HEAD~1..HEAD"],
                capture_output=True, text=True, timeout=15,
            )
            if diff_result.returncode == 0:
                lines = diff_result.stdout.strip().split("\n")
                if lines:
                    summary_line = lines[-1]
                    # Parse "X files changed, Y insertions, Z deletions"
                    import re
                    del_match = re.search(r"(\d+) deletion", summary_line)
                    if del_match and int(del_match.group(1)) > 100:
                        alerts.append(f"Mass deletion detected: {del_match.group(1)} lines removed in last commit")

        elapsed = int((time.monotonic() - start) * 1000)
        status_str = "clean" if not alerts else "alert"
        detail = {
            "recent_commits": len(commits),
            "alerts": alerts,
            "last_commit": commits[0] if commits else None,
        }
        _log_execution("sync-alert", status_str, json.dumps(detail), elapsed)
        return {"ok": True, "detail": detail}

    except Exception as e:
        elapsed = int((time.monotonic() - start) * 1000)
        _log_execution("sync-alert", "error", f"unexpected_error: {e}", elapsed)
        return {"ok": False, "error_code": "unexpected_error", "detail": str(e)}


# ---------------------------------------------------------------------------
# Workflow 3: Federation Status
# ---------------------------------------------------------------------------
def run_federation_status() -> dict:
    """Check health of all known Federation Render services."""
    start = time.monotonic()

    # Known Federation endpoints
    federation_services = [
        {"name": "federation-gatekeeper", "url": "https://federation-gatekeeper.onrender.com/health"},
        {"name": "federation-app-vault", "url": "https://federation-app-vault.onrender.com"},
        {"name": "nxs-emmy-board", "url": "https://nxs-emmy-board.onrender.com"},
        {"name": "nxs-staging", "url": "https://nxs-staging.onrender.com"},
        {"name": "nxs-federation-hub", "url": "https://nxs-federation-hub.onrender.com"},
        {"name": "nxs-truth-gate-core", "url": "https://nxs-truth-gate-core.onrender.com"},
        {"name": "nxs-ai-course-hub", "url": "https://nxs-ai-course-hub.onrender.com"},
        {"name": "nxs-ai-minds-lab", "url": "https://nxs-ai-minds-lab.onrender.com"},
        {"name": "nxs-unified-storefront", "url": "https://nxs-unified-storefront.onrender.com"},
    ]

    results = []
    healthy = 0
    degraded = 0
    down = 0

    for svc in federation_services:
        try:
            with httpx.Client(timeout=10, follow_redirects=True) as client:
                resp = client.get(svc["url"])
                code = resp.status_code
                if 200 <= code < 400:
                    status = "healthy"
                    healthy += 1
                elif 400 <= code < 500:
                    status = "degraded"
                    degraded += 1
                else:
                    status = "down"
                    down += 1
        except Exception as e:
            code = 0
            status = "unreachable"
            down += 1

        results.append({
            "name": svc["name"],
            "status": status,
            "http_code": code,
        })

    elapsed = int((time.monotonic() - start) * 1000)
    overall = "operational"
    if down > 0:
        overall = "degraded" if healthy > down else "critical"
    elif degraded > 0:
        overall = "partial"

    detail = {
        "overall": overall,
        "healthy": healthy,
        "degraded": degraded,
        "down": down,
        "total": len(federation_services),
        "services": results,
    }
    _log_execution("federation-status", overall, json.dumps(detail), elapsed)
    return {"ok": True, "detail": detail}


# ---------------------------------------------------------------------------
# Workflow Registry
# ---------------------------------------------------------------------------
WORKFLOWS = {
    "heartbeat": {
        "id": "heartbeat",
        "name": "Vault Heartbeat Monitor",
        "description": "Checks vault health: file integrity, git status, commit freshness",
        "interval_hours": 6,
        "runner": run_heartbeat,
    },
    "sync-alert": {
        "id": "sync-alert",
        "name": "GitHub Vault Sync Alert",
        "description": "Monitors recent commits for anomalies: unknown authors, mass deletions",
        "interval_hours": 12,
        "runner": run_sync_alert,
    },
    "federation-status": {
        "id": "federation-status",
        "name": "Federation Status",
        "description": "Pings all known Federation Render services and aggregates health",
        "interval_hours": 4,
        "runner": run_federation_status,
    },
}


# ---------------------------------------------------------------------------
# Scheduler (simple background thread with sleep-loop)
# ---------------------------------------------------------------------------
_scheduler_running = False
_last_run: dict[str, float] = {}


def _scheduler_loop():
    """Background thread that runs workflows on their configured intervals."""
    global _scheduler_running
    _scheduler_running = True
    log.info("Workflow scheduler started")

    # Run all workflows once on startup (after a 30s delay for vault sync)
    time.sleep(30)
    for wf_id, wf in WORKFLOWS.items():
        try:
            log.info(f"Initial run: {wf['name']}")
            wf["runner"]()
            _last_run[wf_id] = time.time()
        except Exception as e:
            log.error(f"Initial run failed for {wf_id}: {e}")

    # Then check every 5 minutes if any workflow is due
    while _scheduler_running:
        time.sleep(300)  # 5 minute check interval
        now = time.time()
        for wf_id, wf in WORKFLOWS.items():
            last = _last_run.get(wf_id, 0)
            interval_sec = wf["interval_hours"] * 3600
            if now - last >= interval_sec:
                try:
                    log.info(f"Scheduled run: {wf['name']}")
                    wf["runner"]()
                    _last_run[wf_id] = time.time()
                except Exception as e:
                    log.error(f"Scheduled run failed for {wf_id}: {e}")


def start_scheduler():
    """Start the background scheduler thread."""
    thread = threading.Thread(target=_scheduler_loop, daemon=True, name="workflow-scheduler")
    thread.start()
    log.info("Workflow scheduler thread launched")


def stop_scheduler():
    """Stop the scheduler."""
    global _scheduler_running
    _scheduler_running = False


def get_workflow_list() -> list[dict]:
    """Return workflow definitions with last-run info."""
    result = []
    for wf_id, wf in WORKFLOWS.items():
        last = _last_run.get(wf_id)
        # Get last log entry for status
        logs = get_logs(wf_id, limit=1)
        last_status = logs[-1]["status"] if logs else "never_run"
        result.append({
            "id": wf["id"],
            "name": wf["name"],
            "description": wf["description"],
            "interval_hours": wf["interval_hours"],
            "last_run": datetime.fromtimestamp(last, tz=timezone.utc).isoformat() if last else None,
            "last_status": last_status,
        })
    return result
