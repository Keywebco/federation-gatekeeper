# Federation Gatekeeper v2.0

**Vault Monitor + Automation Hub — NextXus Federation Swarm Unit #001**

The Gatekeeper is the first autonomous agent unit in the NextXus Federation swarm network. It monitors, syncs, searches, and reports on the health of the federation-private-vault, and runs scheduled automation workflows.

## Architecture
- **Runtime**: Python 3.12 / FastAPI + Uvicorn
- **Memory footprint**: ~60MB (vs n8n's 600MB+)
- **Hosting**: Render free tier web service (Docker)
- **Scheduling**: Built-in thread-based cron (no external dependencies)

## Automated Workflows

### 1. Vault Heartbeat Monitor (every 6h)
- Checks vault health: file integrity, git status, commit freshness
- Alerts on dirty files, stale commits (>7 days), missing markdown

### 2. GitHub Vault Sync Alert (every 12h)
- Pulls latest changes and analyzes recent commits
- Flags unknown authors, mass deletions, suspicious commit messages

### 3. Federation Status (every 4h)
- Pings all known Federation Render services
- Aggregates health: operational / degraded / critical
- Tracks individual service HTTP status codes

## Vault Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Dashboard with vault stats, workflows, and activity log |
| GET | `/status` | JSON vault health and service status |
| GET | `/health` | Simple health check |
| POST | `/sync` | Trigger vault pull from GitHub |
| GET | `/query?q=term` | Full-text search of vault markdown files |
| GET | `/ask?q=question` | LLM-powered Q&A using vault context |
| GET | `/files` | List all vault files |
| GET | `/file/{path}` | Read a specific vault file |

## Automation Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/workflows` | List all workflows with status |
| GET | `/api/workflows/{id}/run` | Manually trigger a workflow |
| GET | `/api/workflows/{id}/logs` | View workflow execution logs |
| GET | `/api/logs` | View all execution logs |
| GET | `/api/federation` | Full federation overview |

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `PORT` | Yes | Server port (Render sets this) |
| `GITHUB_TOKEN` | Yes | GitHub PAT for vault access |
| `VAULT_REPO` | No | Vault clone URL (default: federation-private-vault) |
| `API_KEY` | No | Emergent LLM key for /ask endpoint |
| `API_BASE` | No | LLM API base URL |
| `RENDER_API_KEY` | No | Render API key for federation status checks |

## Deployment

Auto-deployed on Render via Docker on push to `main` of `Keywebco/federation-gatekeeper`.

## Version History
- v2.0.0: Added workflow automation engine (heartbeat, sync-alert, federation-status)
- v1.0.0: Initial vault monitor with search, sync, and LLM Q&A
