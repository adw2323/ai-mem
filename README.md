# ai-mem

**Azure-backed memory platform for AI agents**

[![Build](https://github.com/your-org/ai-mem/actions/workflows/ci.yml/badge.svg)](https://github.com/your-org/ai-mem/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ai-mem)](https://pypi.org/project/ai-mem/)
[![License](https://img.shields.io/github/license/your-org/ai-mem)](LICENSE)

---

## What it is

`ai-mem` gives AI agents a durable, multi-user, centralized memory store backed by Azure. It replaces local SQLite-only memory with an Azure-hosted system that supports semantic search, trust scoring, episodic vs semantic memory classification, audit logging, and portable client access from any machine.

**The problem it solves:** Local memory is single-machine and fragile. When an agent switches machines, spawns a parallel session, or collaborates with other agents on a shared workspace, local memory is unavailable or inconsistent. `ai-mem` provides a shared memory layer that is reachable from anywhere, survives machine changes, and gives operators visibility into what agents have stored and retrieved.

---

## Architecture

```text
AI agent
  -> MCP tool call
     -> ai-mem MCP bridge server (stdio, local)
        -> Azure Function HTTP API
           -> Azure Cosmos DB
           -> Azure Storage Queue (embedding jobs)
           -> Azure OpenAI (embeddings)
```

- **Azure Functions** (Consumption plan): the API gateway. Handles all memory operations, auth validation, and audit logging.
- **Azure Cosmos DB**: five containers — `personal_memory`, `shared_memory`, `audit_log`, `embeddings`, `retrieval_log`.
- **Azure OpenAI**: optional semantic embeddings. Falls back to deterministic hash vectors when not configured (labeled as degraded).
- **`ai-mem` Python package**: portable CLI and MCP server. Installs once, works from any machine without a repo clone.

Agents never connect directly to Cosmos DB. The Function App uses managed identity for Cosmos access; clients authenticate to Functions via function key and HMAC-signed context headers.

---

## Quickstart

### Prerequisites

- Python 3.10+
- Azure subscription
- Azure CLI (`az login`)

### 1. Deploy the backend

```powershell
# Provision Azure resources, deploy Function App, configure MCP
.\scripts\azure\Invoke-CodexMemoryAzureBuild.ps1 `
  -Location "<your-azure-region>" `
  -UserId "you@example.com" `
  -WorkspaceId "myworkspace"
```

This script provisions Cosmos DB, deploys the Function App, assigns managed identity permissions, installs the client package, and writes your local MCP config in one step.

### 2. Install the client (existing deployment)

```bash
pip install ai-mem
```

### 3. Configure the client

```bash
ai-mem configure \
  --endpoint https://<your-function-app>.azurewebsites.net \
  --function-key <your-function-key> \
  --shared-secret <your-hmac-secret> \
  --user-id you@example.com \
  --workspace-id myworkspace
```

### 4. Verify connectivity

```bash
ai-mem doctor
```

### 5. Register the MCP server with your agent

```bash
ai-mem install-mcp
```

This writes the `[mcp_servers.ai-mem-mcp]` entry to `~/.codex/config.toml` so your agent can call memory tools directly.

---

## Key capabilities

| Capability | Description |
|---|---|
| **Semantic search** | Vector similarity search with lexical reranking via `memory_search_summaries` and `memory_build_context` |
| **Trust scoring** | Per-record `trustScore` and explainable `trustDimensions` (provenance, confirmation, freshness, contradiction) |
| **Episodic vs semantic memory** | `memoryClass` field distinguishes run summaries and incidents (episodic) from rules, decisions, and lessons (semantic) |
| **MCP integration** | Full MCP tool surface — agents call memory tools directly without CLI wrappers |
| **Audit trail** | Append-only `audit_log` container with per-operation event records |
| **Compact context assembly** | `memory_build_context` returns prompt-ready bullet blocks at `small`, `medium`, or `full` token budgets |
| **Project-aware retrieval** | `projectId` and `projectScopeMode` (off/prefer/strict) scope retrieval to the relevant project |
| **Queue-backed embeddings** | Writes return instantly; embedding generation is async via Azure Storage Queue |
| **Export / import / rebuild** | Snapshot memory, replay into a new workspace, or re-embed stored records |
| **Privacy controls** | `store=false` writes audit the skip without persisting the record or embedding |

---

## Agent workflow

At the start of each agent session:

```
memory_get_personal       # recent personal context
memory_get_shared         # shared workspace context
memory_list_open_tasks    # open tasks
memory_build_context      # compact semantic context (budget=small)
```

At the end of each meaningful agent run:

```
memory_add_run            # persist the run summary
memory_add_fact           # persist durable facts or decisions
```

---

## CLI reference (selected commands)

```bash
# Bootstrap
ai-mem configure --endpoint ... --function-key ... --shared-secret ... --user-id ... --workspace-id ...
ai-mem login
ai-mem doctor
ai-mem install-mcp
ai-mem install-defaults   # writes memory-first AGENTS.md policy block

# Memory reads
ai-mem search --query "auth refactor risk" --mode summaries
ai-mem get-shared --limit 20
ai-mem get-personal --limit 20

# Memory writes
ai-mem add-run --request "..." --summary "..." --status completed
ai-mem add-fact --key "deploy-policy" --value "Always use blue-green"

# Operations
ai-mem retrieval-logs --since-hours 24
ai-mem audit-logs --since-hours 24
ai-mem rebuild-embeddings --scope all
ai-mem promote --dry-run
```

---

## Docs

- [Architecture and design](docs/PROJECT.md)
- [Technical report](docs/TECHNICAL-REPORT.md)
- [Decisions log](docs/DECISIONS.md)
- [Status and incident notes](docs/STATUS.md)
- [TODO and backlog](docs/TODO.md)
- [Contributing](CONTRIBUTING.md)

---

## License

See [LICENSE](LICENSE).
