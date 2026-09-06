# Federation Gatekeeper

**Obsidian Vault Monitor — NextXus Federation Swarm Unit #001**

The Gatekeeper is the first autonomous agent unit in the NextXus Federation swarm network. It monitors, syncs, searches, and reports on the health of the federation-private-vault (Obsidian knowledge base).

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Dashboard with vault stats and animated UI |
| GET | `/status` | JSON vault health and service status |
| GET | `/health` | Simple health check |
| POST | `/sync` | Trigger vault pull from GitHub |
| GET | `/query?q=term` | Full-text search of vault markdown files |
| GET | `/ask?q=question` | LLM-powered Q&A using vault context |
| GET | `/files` | List all vault files |
| GET | `/file/{path}` | Read a specific vault file |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `API_KEY` | Emergent LLM key for /ask endpoint |
| `API_BASE` | LLM API base URL (default: https://integrations.emergentagent.com/llm) |
| `GITHUB_TOKEN` | GitHub token for private vault access |
| `VAULT_REPO` | Vault repository URL |

## Deployment

Deployed on Render via Docker. Auto-syncs vault on startup.

## Federation

Part of the NextXus Federation autonomous swarm network. The Gatekeeper is the first node, responsible for vault integrity and knowledge access.
