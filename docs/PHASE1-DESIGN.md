# Phase 1 Design - Azure Multi-User Codex Memory

## Scope Gate
This document is Phase 1 only (discovery, architecture, migration/cost/security plan).
No Azure resources have been provisioned.

## 1) Discovery Summary

### Current Codex Runtime Configuration (must remain unchanged)
From `~\.codex\config.toml`:

- `sandbox_mode = "danger-full-access"`
- `approval_policy = "never"`
- `[windows] sandbox = "elevated"`
- `[features] experimental_windows_sandbox = false`

These settings will be preserved exactly.

### Current `codex` behavior
- `Get-Command codex` resolves to npm-installed CLI binaries (`codex.ps1` / `codex.cmd`), not a profile function.
- PowerShell profile file exists and is empty (`Length = 0`), so no active alias interception currently.

### Existing memory assets discovered
- Active MCP SQLite DB (current config target): `~\.codex\codexmem.db` (path may vary)
- Repo SQLite DB (older/local workflow): `codex-memory\data\codexmem.db` (if present)
- Existing wrapper project in repo (if migrating from a previous setup): legacy CLI and install/profile interception scripts

## 2) SQLite Schema Inventory (Live Inspection)

## 2.1 Primary DB (`~\.codex\codexmem.db`)

Tables and fields:

1. `facts`
- `key TEXT PRIMARY KEY`
- `value TEXT`
- `tags TEXT`
- `updated_at TEXT`

2. `runs`
- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `started_at TEXT`
- `finished_at TEXT`
- `cwd TEXT`
- `repo_root TEXT`
- `branch TEXT`
- `request TEXT`
- `summary TEXT`
- `status TEXT`

3. `tasks`
- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `title TEXT`
- `state TEXT`
- `priority INT`
- `repo_root TEXT`
- `created_at TEXT`
- `updated_at TEXT`

4. `notes`
- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `kind TEXT`
- `repo_root TEXT`
- `ref TEXT`
- `text TEXT`
- `tags TEXT`
- `created_at TEXT`

5. `embeddings`
- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `kind TEXT`
- `ref_key TEXT`
- `repo_root TEXT`
- `text TEXT`
- `dims INT`
- `embedding BLOB`
- `tags TEXT`
- `created_at TEXT`

Current row counts (2026-03-06):
- `facts`: 44
- `runs`: 16
- `tasks`: 0
- `notes`: 0
- `embeddings`: 4

## 2.2 Secondary DB (`codex-memory\data\codexmem.db`)

Tables and fields:

1. `facts(key, value, tags, updated_at)`
2. `runs(id, started_at, finished_at, cwd, repo_root, branch, request, augmented_prompt, codex_output, status)`
3. `run_files(run_id, path, change_type)`
4. `run_diffs(run_id, diff_text, diff_hash)`
5. `tasks(id, title, state, priority, created_at, updated_at)`

Current row counts (2026-03-06):
- `facts`: 41
- `runs`: 14
- `tasks`: 18
- `run_files`: 52
- `run_diffs`: 8

## 3) Target Azure Architecture

Region baseline: `<your-azure-region>`

```text
Codex (interactive, unchanged)
  -> MCP bridge tools (local stdio server)
     -> HTTPS Azure Functions API (Consumption)
        -> Cosmos DB account (NoSQL, database: ai_mem)
           |- personal_memory   (pk /userId)
           |- shared_memory     (pk /workspaceId)
           |- audit_log         (pk /workspaceId, append-only)
           |- embeddings        (pk /workspaceId, vector + metadata)
```

## 3.1 Azure resources required
- Resource Group: `<your-resource-group>`
- Cosmos DB Account (NoSQL API, Free Tier if available)
- Azure Functions App (Consumption plan)
- Optional Azure OpenAI resource for embeddings (if enabled)

## 3.2 Cosmos DB logical model

Database: `ai_mem`
Containers:

1. `personal_memory` (partition key `/userId`)
- Personal facts, runs, tasks, user preferences

2. `shared_memory` (partition key `/workspaceId`)
- Shared facts, architecture decisions, runbooks, notes, shared tasks

3. `audit_log` (partition key `/workspaceId`)
- Append-only operation log

4. `embeddings` (partition key `/workspaceId`)
- Vector + source text + metadata for semantic retrieval

## 3.3 Canonical document shape
All memory documents include:
- `id`
- `userId`
- `workspaceId`
- `repoId` (optional)
- `kind` (`fact|decision|note|task|run_summary|document_chunk`)
- `tags` (array)
- `createdAt`
- `updatedAt`
- `source`

Additional type-specific fields:
- `content` (fact value / note text / summary text)
- `taskState`, `priority`, `closedAt` for tasks
- `status`, `request`, `branch`, `cwd` for runs
- `vector` and `vectorMeta` for embeddings
- `migration` object for migrated rows

## 4) API Layer (Azure Functions)

HTTP endpoints (one function per operation):
- `memory_get_personal`
- `memory_get_shared`
- `memory_add_fact`
- `memory_add_run`
- `memory_list_open_tasks`
- `memory_add_task`
- `memory_close_task`
- `memory_search_vectors`
- `memory_add_audit_event`

Behavior requirements:
- Strict JSON schema validation per endpoint
- Secret redaction before storage/logging
- Server-side audit writes to `audit_log`
- Deterministic idempotency keys for mutation endpoints (`requestId`)

## 5) Authentication and Security Model

Primary model:
- Function App uses Managed Identity to access Cosmos DB data plane.
- Assign least-privilege Cosmos DB RBAC role(s) to Function identity.
- Clients never receive Cosmos keys.

Fallback (documented only if MI setup is blocked):
- Function App uses Cosmos connection string in app settings temporarily.
- Rotate secrets and revert to MI as first post-cutover hardening step.

MCP client auth to Functions:
- Minimal build: function key in local MCP config (Cosmos key still not exposed).
- Hardened option: Microsoft Entra auth on Function endpoints (no function keys).

## 6) MCP Integration Design

Local MCP bridge server exposes these tools:
- `memory_get_personal`
- `memory_get_shared`
- `memory_add_fact`
- `memory_add_run`
- `memory_list_open_tasks`
- `memory_add_task`
- `memory_close_task`
- `memory_search_vectors`
- `memory_add_audit_event`

Flow:
- Tool call -> MCP bridge validates payload -> calls Function endpoint -> returns normalized JSON.
- Bridge appends deterministic `actor`, `workspaceId`, `repoId`.

No wrapper or `codex` command interception is used.

## 7) AGENTS.md Structure (Phase 2 target content)

At request start:
1. `memory_get_personal`
2. `memory_get_shared`
3. `memory_list_open_tasks`
4. `memory_search_vectors` with the incoming user request

After meaningful work:
1. `memory_add_run`
2. `memory_add_fact` for durable decisions/facts
3. `memory_add_task` / `memory_close_task`
4. `memory_add_audit_event`

Rules will be concise and deterministic, and will not redefine Codex runtime settings.

## 8) Migration Plan (SQLite -> Cosmos DB)

## 8.1 Source selection
Primary migration source:
- Active local SQLite DB (e.g. `~\.codex\codexmem.db`)

Optional merge source:
- Repo-local SQLite DB if present

## 8.2 Table-to-container mapping

1. `facts`
- Target: `personal_memory` or `shared_memory`
- Mapping:
  - `id`: `mig:sqlite:facts:<key>`
  - `kind`: `fact` (or `decision` if tags indicate decision)
  - `content`: `value`
  - `tags`: CSV -> array
  - `updatedAt`: `updated_at`
  - `createdAt`: `updated_at` (if no separate source timestamp)

2. `runs`
- Target: `personal_memory` (`kind = run_summary`)
- Include `request`, `summary` (or derived summary), `status`, `branch`, `cwd`, timestamps.

3. `tasks`
- Target:
  - default `personal_memory` (`kind = task`)
  - optional `shared_memory` when tagged/prefixed as shared

4. `notes`
- Target: `shared_memory` (`kind = note`) unless explicitly personal-tagged.

5. `embeddings`
- Target: `embeddings`
- Preserve text, dims, and decoded vector bytes where present.

6. `run_files`, `run_diffs` (secondary DB only)
- Fold into run document metadata under `artifacts` sub-object.

## 8.3 Required migration metadata
Every migrated document gets:
- `source = "sqlite"`
- `sourceTable`
- `sourceId`
- `migratedAt`

## 8.4 Idempotency and safety
- Use deterministic IDs per source row (`mig:sqlite:<table>:<pk>`).
- Upsert by ID; skip unchanged rows using checksum field.
- Migration can be re-run safely.
- No source row deletion.

## 8.5 Backup procedure
Before migration run:
1. Copy source DB to timestamped backup:
   - `codexmem.db.bak.<UTC timestamp>`
2. Preserve `-wal`/`-shm` companions when present.

## 8.6 Post-migration outputs
- JSON migration report:
  - start/end time
  - source files
  - rows scanned/migrated/skipped/failed by table
  - checksum summary
- Audit events to `audit_log` for each migration phase/table
- Optional re-embedding pass for any low-quality/legacy vectors

## 9) Wrapper Cleanup Plan (Phase 2)

## 9.1 Detection targets
- Legacy wrapper CLI directory
- `scripts/install.ps1` blocks that inject `codex` / `codexreal`
- Local PowerShell profile blocks containing markers:
  - `# >>> codexmem override >>>`
  - `function codex`
  - `function codexreal`
- Any scripts/docs invoking `codexreal` or aliasing `codex`

## 9.2 Planned cleanup actions
- Remove/deprecate wrapper code paths and alias install scripts.
- Remove profile override block if present.
- Remove obsolete MCP sqlite setup path from repo runbooks.
- Add Azure MCP setup path in docs.

## 9.3 Validation checks after cleanup
- `Get-Command codex -All` resolves to normal CLI binaries.
- `Get-Command codexreal` returns not found.
- Profile files contain no wrapper blocks.

## 10) Cost Estimate (3 users)

Workload assumption:
- 3 users
- 50 Codex requests/user/day
- 5 memory ops/request
- Total memory ops/day = 750
- Total memory ops/month (30 days) = 22,500

## 10.1 Cosmos DB
- Free tier allowance target: 1000 RU/s + 25 GB.
- Estimated usage at this workload is far below free tier.
- Estimated monthly cost: `$0` (if free-tier account available and total data <= 25 GB).

Overage reference (from Azure retail pricing):
- Provisioned throughput: `$0.008` per `100 RU/s-hour`
- Storage: `$0.25` per `GB-month`

## 10.2 Azure Functions (Consumption)
- Executions/month: ~22,500 (well below 1,000,000 free requests).
- Execution-time estimate: far below 400,000 GB-s free grant at this scale.
- Estimated monthly cost: `$0`.

## 10.3 Embeddings (Azure OpenAI)
Two practical options:

1. `embedding-ada-glbl` embedding meter:
- Price: `$0.0001 / 1K tokens` (`$0.10 / 1M tokens`)
- If ~2.25M tokens/month => `~$0.225/month`

2. `text-embedding-3-small`:
- Price: `$0.00002 / 1K tokens` (`$0.02 / 1M tokens`)
- Same 2.25M tokens => `~$0.045/month`

Inference: even with conservative token volume, embeddings remain near-zero monthly spend.

## 10.4 Total projected monthly cost
- Expected total: `$0` to approximately `$0.25/month` under stated workload.

## 11) Phase 2 Execution Plan (after approval)

1. Provision Azure resources in your chosen region (Cosmos + Functions + optional AOAI).
2. Create Cosmos DB database/containers and indexing policies (including vector config).
3. Deploy Function App API endpoints with validation, redaction, and audit writes.
4. Configure Function identity and Cosmos RBAC.
5. Implement/deploy MCP bridge for the 9 memory tools.
6. Replace root `AGENTS.md` with deterministic Azure-memory workflow.
7. Run wrapper cleanup and profile cleanup checks.
8. Run SQLite backup + idempotent migration.
9. Generate migration report and write migration audit events.
10. Validate all required behaviors:
   - Normal `codex` behavior
   - personal/shared isolation
   - audit log append behavior
   - vector search
   - tool accessibility
   - cost sanity

## 12) Source References

Architecture and service behavior:
- Azure Cosmos DB Free Tier: https://learn.microsoft.com/en-us/azure/cosmos-db/free-tier
- Azure Cosmos DB billing details: https://learn.microsoft.com/en-us/azure/cosmos-db/understand-your-bill
- Azure Cosmos DB for NoSQL vector search (Python): https://learn.microsoft.com/en-us/azure/cosmos-db/nosql/how-to-python-vector-index-query
- Azure Functions scale/hosting docs: https://learn.microsoft.com/en-us/azure/azure-functions/functions-scale
- Azure Functions pricing page: https://azure.microsoft.com/en-us/pricing/details/functions/
- Azure Cosmos DB RBAC built-in roles: https://learn.microsoft.com/en-us/azure/cosmos-db/nosql/security/reference-data-plane-roles
- Azure Retail Prices API reference: https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices

Pricing data points used for estimates (queried 2026-03-06):
- Cosmos DB meters via Azure Retail Prices API (`serviceName eq 'Azure Cosmos DB'`)
- Functions meters via Azure Retail Prices API (`serviceName eq 'Functions'`)
- Azure OpenAI embedding meters via Azure Retail Prices API (`contains(meterName,'embedding')`)
