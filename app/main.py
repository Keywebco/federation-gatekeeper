"""
Federation Gatekeeper — First Autonomous Swarm Unit
Obsidian Vault Monitor & Query Service for the NextXus Federation

This is the Gatekeeper node: it watches, syncs, searches, and reports
on the health of the federation-private-vault.
"""

import os
import re
import time
import hashlib
import subprocess
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
VAULT_REPO = os.getenv("VAULT_REPO", "https://github.com/Keywebco/federation-private-vault")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
API_KEY = os.getenv("API_KEY", "")
API_BASE = os.getenv("API_BASE", "https://integrations.emergentagent.com/llm")
VAULT_DIR = Path("/tmp/vault")
SYNC_LOCK = False
BOOT_TIME = datetime.now(timezone.utc)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("gatekeeper")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Federation Gatekeeper",
    description="Obsidian Vault Monitor — NextXus Federation Swarm Unit #001",
    version="1.0.0",
)

templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# ---------------------------------------------------------------------------
# Vault helpers
# ---------------------------------------------------------------------------

def _auth_url(repo_url: str) -> str:
    """Inject GitHub token into clone URL."""
    if GITHUB_TOKEN and "github.com" in repo_url:
        return repo_url.replace("https://github.com", f"https://x-access-token:{GITHUB_TOKEN}@github.com")
    return repo_url


def clone_or_pull() -> dict:
    """Clone or pull the vault repo. Returns status dict."""
    global SYNC_LOCK
    if SYNC_LOCK:
        return {"ok": False, "detail": "Sync already in progress"}
    SYNC_LOCK = True
    try:
        auth_url = _auth_url(VAULT_REPO)
        if VAULT_DIR.exists() and (VAULT_DIR / ".git").exists():
            result = subprocess.run(
                ["git", "-C", str(VAULT_DIR), "pull", "--ff-only"],
                capture_output=True, text=True, timeout=60,
            )
            action = "pull"
        else:
            VAULT_DIR.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                ["git", "clone", "--depth", "1", auth_url, str(VAULT_DIR)],
                capture_output=True, text=True, timeout=120,
            )
            action = "clone"

        ok = result.returncode == 0
        # Sanitize output — strip token from any error messages
        stdout = result.stdout.replace(GITHUB_TOKEN, "***") if GITHUB_TOKEN else result.stdout
        stderr = result.stderr.replace(GITHUB_TOKEN, "***") if GITHUB_TOKEN else result.stderr
        return {
            "ok": ok,
            "action": action,
            "detail": stdout.strip() if ok else stderr.strip(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "Git operation timed out"}
    except Exception as e:
        return {"ok": False, "detail": str(e)}
    finally:
        SYNC_LOCK = False


def vault_stats() -> dict:
    """Gather vault file statistics."""
    if not VAULT_DIR.exists():
        return {"synced": False, "files": 0, "markdown_files": 0, "total_size_kb": 0}
    md_files = list(VAULT_DIR.rglob("*.md"))
    all_files = [f for f in VAULT_DIR.rglob("*") if f.is_file() and ".git" not in str(f)]
    total_kb = sum(f.stat().st_size for f in all_files) / 1024
    # Get last commit info
    try:
        result = subprocess.run(
            ["git", "-C", str(VAULT_DIR), "log", "-1", "--format=%H|%ci|%s"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split("|", 2)
            last_commit = {"hash": parts[0][:8], "date": parts[1], "message": parts[2]}
        else:
            last_commit = None
    except Exception:
        last_commit = None

    return {
        "synced": True,
        "files": len(all_files),
        "markdown_files": len(md_files),
        "total_size_kb": round(total_kb, 1),
        "last_commit": last_commit,
    }


def search_vault(query: str, max_results: int = 10) -> list[dict]:
    """Search vault markdown files for a query string. Returns matching excerpts."""
    if not VAULT_DIR.exists():
        return []

    query_lower = query.lower()
    query_terms = query_lower.split()
    results = []

    for md_file in VAULT_DIR.rglob("*.md"):
        if ".git" in str(md_file):
            continue
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        content_lower = content.lower()
        # Score: count occurrences of each term
        score = sum(content_lower.count(term) for term in query_terms)
        if score == 0:
            continue

        # Extract matching context (first match with surrounding lines)
        lines = content.split("\n")
        excerpts = []
        for i, line in enumerate(lines):
            if any(term in line.lower() for term in query_terms):
                start = max(0, i - 1)
                end = min(len(lines), i + 3)
                excerpt = "\n".join(lines[start:end]).strip()
                if excerpt and len(excerpt) > 10:
                    excerpts.append(excerpt[:500])
                if len(excerpts) >= 2:
                    break

        rel_path = str(md_file.relative_to(VAULT_DIR))
        results.append({
            "file": rel_path,
            "score": score,
            "title": _extract_title(content),
            "excerpts": excerpts,
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:max_results]


def _extract_title(content: str) -> str:
    """Extract first H1 heading or first line as title."""
    for line in content.split("\n")[:10]:
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    # Fallback: first non-empty line
    for line in content.split("\n")[:5]:
        line = line.strip()
        if line:
            return line[:80]
    return "(untitled)"


def vault_file_tree() -> list[str]:
    """Return a flat list of vault file paths (excluding .git)."""
    if not VAULT_DIR.exists():
        return []
    files = []
    for f in sorted(VAULT_DIR.rglob("*")):
        if f.is_file() and ".git" not in str(f):
            files.append(str(f.relative_to(VAULT_DIR)))
    return files


# ---------------------------------------------------------------------------
# LLM helper (optional, for /ask endpoint)
# ---------------------------------------------------------------------------

async def llm_query(prompt: str, context: str = "") -> str:
    """Query the Emergent LLM endpoint."""
    if not API_KEY:
        return "(LLM not configured)"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{API_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "claude-sonnet-4-20250514",
                    "messages": [
                        {"role": "system", "content": (
                            "You are the Federation Gatekeeper, an AI assistant for the NextXus Federation's "
                            "Obsidian vault. Answer questions about the vault contents concisely and accurately. "
                            "If vault context is provided, use it to inform your answer."
                        )},
                        {"role": "user", "content": f"Vault context:\n{context}\n\nQuestion: {prompt}" if context else prompt},
                    ],
                    "max_tokens": 1024,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"LLM query failed: {e}")
        return f"(LLM query failed: {str(e)[:100]})"

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_sync():
    """Auto-sync vault on startup."""
    log.info("Gatekeeper starting — syncing vault...")
    result = clone_or_pull()
    log.info(f"Vault sync: {result}")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Gatekeeper dashboard."""
    stats = vault_stats()
    uptime_seconds = (datetime.now(timezone.utc) - BOOT_TIME).total_seconds()
    uptime_str = f"{int(uptime_seconds // 3600)}h {int((uptime_seconds % 3600) // 60)}m"
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "stats": stats,
        "uptime": uptime_str,
        "boot_time": BOOT_TIME.isoformat(),
        "vault_repo": VAULT_REPO,
    })


@app.get("/status")
async def status():
    """Vault health and status."""
    stats = vault_stats()
    uptime_seconds = (datetime.now(timezone.utc) - BOOT_TIME).total_seconds()
    return {
        "service": "federation-gatekeeper",
        "version": "1.0.0",
        "role": "Obsidian Vault Monitor — Swarm Unit #001",
        "status": "operational" if stats["synced"] else "vault_not_synced",
        "uptime_seconds": round(uptime_seconds),
        "boot_time": BOOT_TIME.isoformat(),
        "vault": stats,
        "federation": "NextXus",
        "node_type": "gatekeeper",
    }


@app.get("/health")
async def health():
    """Simple health check for Render."""
    return {"status": "ok", "service": "federation-gatekeeper"}


@app.post("/sync")
async def sync():
    """Trigger vault sync from GitHub."""
    result = clone_or_pull()
    stats = vault_stats()
    return {
        "sync": result,
        "vault": stats,
    }


@app.get("/query")
async def query(
    q: str = Query(..., description="Search query for vault files"),
    limit: int = Query(10, ge=1, le=50),
):
    """Search vault markdown files. Returns plain text results."""
    if not VAULT_DIR.exists():
        raise HTTPException(status_code=503, detail="Vault not synced yet. Call /sync first.")
    results = search_vault(q, max_results=limit)
    return {
        "query": q,
        "results_count": len(results),
        "results": results,
    }


@app.get("/ask")
async def ask(
    q: str = Query(..., description="Question to ask about the vault"),
):
    """Ask a question about the vault using LLM + vault context."""
    # Search vault for context
    vault_results = search_vault(q, max_results=5)
    context_parts = []
    for r in vault_results:
        context_parts.append(f"## {r['file']}\n" + "\n".join(r["excerpts"]))
    context = "\n\n".join(context_parts)

    answer = await llm_query(q, context)
    return {
        "question": q,
        "answer": answer,
        "sources": [r["file"] for r in vault_results],
    }


@app.get("/files")
async def files():
    """List all vault files."""
    tree = vault_file_tree()
    return {
        "total": len(tree),
        "files": tree,
    }


@app.get("/file/{path:path}")
async def read_file(path: str):
    """Read a specific vault file."""
    file_path = VAULT_DIR / path
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    if ".git" in str(file_path):
        raise HTTPException(status_code=403, detail="Cannot access .git files")
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
        return {
            "path": path,
            "size_bytes": file_path.stat().st_size,
            "content": content,
            "hash": hashlib.sha256(content.encode()).hexdigest()[:16],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Workflow Automation Routes (added by Federation Hub enhancement)
# ---------------------------------------------------------------------------
from app.workflows import (
    WORKFLOWS, get_workflow_list, get_logs,
    start_scheduler, stop_scheduler,
)


@app.on_event("startup")
async def start_workflows():
    """Launch the workflow scheduler on startup."""
    log.info("Starting workflow automation scheduler...")
    start_scheduler()


@app.on_event("shutdown")
async def stop_workflows():
    """Stop the scheduler on shutdown."""
    stop_scheduler()


@app.get("/api/workflows")
async def list_workflows():
    """List all registered workflows with status."""
    return {
        "workflows": get_workflow_list(),
        "total": len(WORKFLOWS),
    }


@app.get("/api/workflows/{workflow_id}/run")
async def trigger_workflow(workflow_id: str):
    """Manually trigger a workflow execution."""
    if workflow_id not in WORKFLOWS:
        raise HTTPException(status_code=404, detail=f"Unknown workflow: {workflow_id}")

    wf = WORKFLOWS[workflow_id]
    result = wf["runner"]()
    return {
        "workflow": workflow_id,
        "name": wf["name"],
        "result": result,
        "triggered": "manual",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/workflows/{workflow_id}/logs")
async def workflow_logs(workflow_id: str, limit: int = 20):
    """Retrieve execution logs for a specific workflow."""
    if workflow_id not in WORKFLOWS:
        raise HTTPException(status_code=404, detail=f"Unknown workflow: {workflow_id}")
    logs = get_logs(workflow_id, limit=limit)
    return {
        "workflow": workflow_id,
        "entries": logs,
        "count": len(logs),
    }


@app.get("/api/logs")
async def all_logs(limit: int = 50):
    """Retrieve all workflow execution logs."""
    logs = get_logs(limit=limit)
    return {
        "entries": logs,
        "count": len(logs),
    }


@app.get("/api/federation")
async def federation_overview():
    """Full federation overview: vault + workflows + service health."""
    stats = vault_stats()
    workflows = get_workflow_list()
    uptime_seconds = (datetime.now(timezone.utc) - BOOT_TIME).total_seconds()
    return {
        "node": "federation-gatekeeper",
        "role": "Vault Monitor + Automation Hub",
        "version": "2.0.0",
        "uptime_seconds": round(uptime_seconds),
        "vault": stats,
        "workflows": workflows,
        "federation": "NextXus",
    }
