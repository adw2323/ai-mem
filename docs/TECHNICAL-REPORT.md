# ai-mem Technical Report

Date: 2026-03-14
Project: `ai-mem`
Scope: Technical description of the Azure-hosted memory platform implemented in this repo.

## 1. Executive Summary

`ai-mem` is a shared memory platform for AI coding agents. It replaces the older local SQLite-only memory workflow with an Azure-hosted system built on Azure Functions, Azure Cosmos DB, and a local MCP bridge server.

Its purpose is to give AI agents a durable memory layer that supports:
- personal memory
- shared workspace memory
- project-aware memory metadata
- task tracking
- run summaries
- audit logging
- retrieval telemetry
- semantic and lexical retrieval
- compact context assembly for prompt efficiency
- export/import and embedding rebuild recovery flows

The platform is intentionally not a Codex CLI wrapper. Agents call MCP tools, the MCP bridge calls an Azure Function API, and the Function API reads and writes Cosmos DB.

## 2. What The App Actually Does

At a functional level, the app provides these capabilities:

1. Store durable facts
- Agents can write personal or shared facts.
- Facts are tagged, timestamped, and embedded for later retrieval.

2. Store run summaries
- Agents can log what they did, where they worked, the branch, request, and outcome summary.
- These become searchable memory records.

3. Track tasks
- Agents can create and close personal or shared tasks.
- Open tasks can be listed separately from general memory.

4. Keep an append-only audit trail
- Important memory operations can write audit events into a dedicated audit container.
- This gives operators an activity trail separate from the memory records themselves.

5. Keep retrieval telemetry
- Search and context-build operations write telemetry into a dedicated retrieval log.
- This captures retrieval quality and latency data without polluting governance-oriented audit events.

6. Retrieve memory by direct list queries
- Agents can fetch recent personal memory.
- Agents can fetch recent shared memory.
- Agents can list open tasks.

7. Retrieve memory semantically
- Agents can search embeddings by query text.
- The system ranks results using vector similarity plus lexical overlap.

8. Retrieve compact summaries instead of full records
- The platform can return lightweight, deduped search summaries first.
- Full records can then be hydrated only when needed.

9. Build prompt-ready memory context
- The platform can assemble a compressed context block using `small`, `medium`, or `full` budgets.
- This is used to reduce prompt token consumption at request start.

10. Support privacy-aware non-persistent writes
- Write operations support `store=false`.
- In that mode the API normalizes the item and audits the skip, but does not persist the memory record or embedding.

11. Support operational recovery
- Memory can be exported.
- Memory can be imported in additive modes.
- Embeddings can be rebuilt from stored records.

12. Decouple primary writes from embedding generation
- Memory records are persisted first.
- Embedding generation is queued and processed by a queue-triggered worker.
- Source records track embedding state such as `pending`, `ready`, or `failed`.

## 3. Current Architecture

The runtime architecture is:

```text
AI agent
  -> MCP tool call
     -> local MCP bridge server (stdio)
        -> Azure Function HTTP endpoint
           -> Cosmos DB containers
           -> retrieval telemetry container
           -> storage queue for embedding jobs
           -> optional Azure OpenAI embeddings
```

Key design point:
- clients do not connect directly to Cosmos DB
- clients use the Function API through the MCP bridge
- the Azure Function uses managed identity to access Cosmos DB

## 4. Deployed Azure Shape

The project docs record the current deployed baseline in the chosen Azure region.

Provisioned resources (example shape — your deployment will use different names):
- Resource Group: `<your-resource-group>`
- Cosmos DB account: `<your-cosmos-account>`
- Cosmos DB database: `ai_mem`
- Function App: `<your-function-app>`
- Storage Account: `<your-storage-account>`

Current deployment automation is handled by:
- `scripts/azure/Invoke-CodexMemoryAzureBuild.ps1`

That script provisions the Azure resources, configures Cosmos DB containers, deploys the Function code, assigns managed identity permissions, updates local MCP config, migrates SQLite data, and runs validation calls.

## 5. Data Model

The Cosmos DB database is `ai_mem` with five containers:

1. `personal_memory`
- Partition key: `/userId`
- Stores user-specific facts, run summaries, and personal tasks.

2. `shared_memory`
- Partition key: `/workspaceId`
- Stores shared facts, shared tasks, notes, and collaborative memory.

3. `audit_log`
- Partition key: `/workspaceId`
- Stores append-only audit events.

4. `embeddings`
- Partition key: `/workspaceId`
- Stores vectorized searchable chunks and retrieval metadata.

5. `retrieval_log`
- Partition key: `/workspaceId`
- Stores retrieval telemetry such as query mode, scores, latency, budgets, and result counts.

Canonical document fields used across memory records include:
- `id`
- `userId`
- `workspaceId`
- `projectId`
- `repoId`
- `kind`
- `memoryScope`
- `memoryClass`
- `status`
- `promotionStatus`
- `tags`
- `createdAt`
- `updatedAt`
- `source`
- `visibility`
- `retentionDays`
- `importance`
- `confidence`
- `trustScore`
- `trustDimensions`
- `referenceCount`
- `lastReferenced`
- `embeddingStatus`
- `embeddingMode`
- `artifactRef`
- `derivedFromIds`
- `supersedesId`

Type-specific fields include:
- `content` for facts/notes
- `request`, `summary`, `branch`, `cwd`, `status` for runs
- `title`, `taskState`, `priority`, `closedAt` for tasks

The current rearchitecture direction also distinguishes between:
- episodic memory such as run summaries, incidents, and experiments
- semantic memory such as rules, decisions, lessons, and durable findings

Today that distinction is represented as additive metadata via `memoryClass`; retrieval behavior remains backward-compatible and will evolve later.

Embedding records include:
- `text`
- `vector`
- `vectorMeta`
- `sourceRefId`

## 6. API Surface

The Azure Function implements one routed HTTP entry point that dispatches to specific memory operations.

Implemented operations are:
- `memory_get_personal`
- `memory_get_shared`
- `memory_get_items`
- `memory_export`
- `memory_import`
- `memory_add_fact`
- `memory_add_run`
- `memory_list_open_tasks`
- `memory_add_task`
- `memory_close_task`
- `memory_rebuild_embeddings`
- `memory_search_summaries`
- `memory_search_vectors`
- `memory_build_context`
- `memory_add_audit_event`

Function implementation path:
- `api/memory_api/run.py`

HTTP route shape:
- `/api/memory/<operation>`

The Function accepts JSON payloads, validates required fields in code, executes the operation, and returns normalized JSON responses.

## 7. MCP Tool Surface

The local MCP bridge exposes matching tools to AI agents.

Bridge implementation path:
- installed package module `ai_mem.mcp_server`
- repo compatibility shim: `scripts/mcp/ai_mem_server.py`

The bridge:
- runs as a stdio MCP server
- injects `userId`, `workspaceId`, and `repoId` into every request
- authenticates to Azure Functions using the Function key
- exposes tool names that directly mirror the Function operations

The portable client layer now exists independently of the repo as an installable Python package:
- package: `client/`
- shared client core: `ai_mem.client`
- MCP module: `ai_mem.mcp_server`
- CLI: `ai_mem.cli`
- user config file: `~/.ai-mem/config.json` by default

This is the first portability slice intended to break the hidden dependency on repo-local bridge code for ongoing access after installation.

The client package now also supports:
- `ai-mem configure` to persist endpoint/auth/workspace defaults
- `ai-mem login` to validate connectivity and save config in one step
- `ai-mem doctor` to verify that the local machine can still reach the memory platform using saved or overridden config
- `ai-mem install-mcp` to write the Codex MCP config entry from the installed client itself

Package installation source can be overridden through `AI_MEM_CLIENT_PACKAGE`, allowing setup scripts to prefer a published package or release artifact when available. The installed CLI now also owns Codex MCP bootstrap through `ai-mem install-mcp`, which moves one more piece of machine setup out of repo-local PowerShell and into the portable client itself.

Current MCP-exposed tools are:
- `memory_get_personal`
- `memory_get_shared`
- `memory_add_fact`
- `memory_add_run`
- `memory_list_open_tasks`
- `memory_add_task`
- `memory_close_task`
- `memory_search_vectors`
- `memory_search_summaries`
- `memory_get_items`
- `memory_build_context`
- `memory_export`
- `memory_import`
- `memory_rebuild_embeddings`
- `memory_add_audit_event`

## 8. Retrieval And Ranking Behavior

The retrieval model is intentionally pragmatic rather than academically complex.

The current implementation now stores richer metadata than it uses for ranking. In particular, records can now carry:
- `projectId`
- `memoryScope`
- `memoryClass`
- `trustScore`
- `trustDimensions`
- artifact lineage fields

This enables the retrieval phase to become project-aware, class-aware, and trust-aware without changing the storage backend first.

### 8.1 Embedding generation

The system supports two embedding modes:

1. Azure OpenAI embeddings
- Used when `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, and deployment settings are configured.

2. Local model fallback when explicitly configured
- Optional path if a local sentence-transformer model is configured and available in the runtime.

3. Deterministic hash fallback embeddings
- Used only as a degraded fallback when Azure OpenAI and any configured local model path are unavailable.
- Responses explicitly label this as degraded retrieval quality.

### 8.2 Search scoring

`memory_search_vectors` computes:
- `vectorScore`
- `lexicalScore`
- `score = vectorScore * 0.85 + lexicalScore * 0.15`

This means retrieval is primarily vector-based, but exact-token overlap still influences ranking.

### 8.3 Summary retrieval

`memory_search_summaries`:
- runs vector search first
- loads the source documents behind the vector hits
- produces compact summaries
- adds `whyMatched`
- includes `scope`
- dedupes near-identical results using a canonical hash

### 8.4 Context assembly

`memory_build_context`:
- accepts `budget` values of `small`, `medium`, or `full`
- requests compact summaries
- selects a bounded number of items
- assembles prompt-ready bullet lines
- returns:
  - `context`
  - `items`
  - `tagSummary`
  - `itemCount`

Current budget behavior:
- `small`: up to 4 items, about 700 chars
- `medium`: up to 8 items, about 1600 chars
- `full`: up to 12 items, about 3200 chars

This is the main prompt-token reduction feature now in active use.

Planned retrieval evolution:
- debugging and incident questions should bias toward recent episodic memory and artifact-backed local context
- design and architecture questions should bias toward semantic memory, confirmed decisions, and supersession-aware lessons
- trust dimensions should eventually influence ranking as well as explainability

That retrieval evolution has now started additively. `memory_search_summaries` widens raw vector candidate selection and reranks summaries using:
- inferred or caller-supplied intent
- preferred memory class
- repo/project scope fit
- trust score plus trust dimensions
- recency and reference activity
- promotion status

`memory_build_context` now carries the resulting ranking signals through compact context output so the caller can inspect why a memory ranked highly instead of seeing only a flattened score.

### 8.5 Retrieval telemetry

The platform now logs retrieval operations into `retrieval_log` with fields such as:
- query
- embedding mode
- degraded status
- result count
- items returned
- top vector score
- top lexical score
- final top score
- budget
- latency
- workspace and repo context

## 9. Write Behavior And Privacy Controls

All primary write paths normalize and redact content before persistence.

Implemented privacy-related controls:
- `visibility`
- `retentionDays`
- `store=false`

Important current behavior:
- `visibility` and `retentionDays` are stored as metadata
- there is no full retention enforcement engine yet
- `store=false` is the actual enforced privacy control today

`store=false` behavior:
- the API returns a normalized item summary
- the write is audited as a transient skip
- no document is persisted
- no embedding is written

This gives agents a usable do-not-store path without needing a separate scratch database yet.

## 10. Embedding Pipeline

Embedding generation is now queue-driven by default.

Write path:
- persist memory record
- enqueue embedding job to `embedding-jobs`
- queue-triggered worker generates embedding
- worker writes embedding record
- worker updates source record status

Benefits:
- lower write latency
- isolation from Azure OpenAI failure modes
- retryable background processing
- explicit pending/ready/failed embedding state on source documents

## 11. Audit And Redaction Behavior

The Function API includes basic secret redaction before storage and logging.

Current redaction behavior:
- regex matches common secret-like keys such as `key`, `token`, `secret`, and `password`
- matching inline values are rewritten to `[REDACTED]`

Audit behavior:
- write operations can emit entries to `audit_log`
- audit entries include:
  - operation
  - actor
  - workspace
  - target container
  - timestamp
  - summary

The audit log is meant to support operator review and migration/recovery traceability.
Retrieval telemetry is intentionally stored separately in `retrieval_log`.

## 12. Migration From SQLite

The Azure app was designed to replace older SQLite-backed memory stores.

Migration sources:
- the active local SQLite DB (path varies per user)
- optional older repo-local SQLite memory DB

Migration properties:
- idempotent
- deterministic IDs
- source metadata preserved
- migration report generated
- no failures documented in the completed migration report

Documented migrated counts from the execution report:
- `facts`: 85
- `runs`: 30
- `tasks`: 18
- `embeddings`: 4

Migration automation path:
- `scripts/migrate/sqlite_to_azure_memory.py`

Migration report path:
- `docs/reports/migration-report-<timestamp>.json`

## 13. Authentication And Security Model

Current model:

1. Function App to Cosmos DB
- Azure managed identity
- Cosmos DB built-in data contributor role assignment

2. MCP bridge to Function API
- Function key authentication
- timestamped HMAC-signed caller context for `userId`, `workspaceId`, and `repoId`

3. Client machines
- do not hold Cosmos DB keys

Security posture achieved:
- better than direct client-to-database access
- preserves a clean control point at the Function layer

Current hardening backlog:
- move from Function key plus shared-secret caller signing to Entra-authenticated Function access
- optional Function key rotation

## 14. What It Does Not Do

The system does not currently do these things:

1. It does not wrap or intercept the `codex` CLI.
2. It does not provide hard-delete lifecycle management.
3. It does not yet enforce retention or visibility policy beyond storing metadata and honoring `store=false`.
4. It does not yet support advanced import merge modes beyond `upsert`, `skip_existing`, and `detect_conflicts`.
5. It does not yet derive caller identity from Entra claims at the Function layer.
6. It is not a general analytics warehouse or BI system.
7. It is not intended for direct end-user querying outside the MCP and Function API pattern.

## 15. Current Operational State

According to the project status docs:
- Phase 2 execution is complete.
- Azure resources are provisioned.
- MCP bridge is configured.
- SQLite data is migrated.
- validation checks passed.

Implemented post-launch improvements include:
- compact search summaries
- hydration-by-ID
- budget-aware context assembly
- retrieval dedupe
- lexical-plus-vector ranking
- `store=false` transient writes
- export/import
- embedding rebuild
- queue-backed embedding generation
- signed caller context validation
- retrieval telemetry
- conflict-detection import preview
- salience metadata and reference tracking
- optional heuristic run-summary auto-extraction

The repo-level AGENTS workflow has also been updated to use:
- `memory_build_context(query, budget="small")`

This means compact recall is now the default request-start pattern in this repo.

## 16. Known Constraints And Tradeoffs

1. Embedding quality depends on whether Azure OpenAI embeddings are enabled.
- Without Azure OpenAI, the system can use an explicitly configured local model or fall back to `degraded_hash`.
- `degraded_hash` keeps the system alive but materially reduces semantic quality.

2. Retrieval remains additive during migration.
- Older raw retrieval paths still exist.
- Compact retrieval is preferred, but backward compatibility is intentionally preserved.

3. Privacy controls are only partially policy-driven today.
- Metadata exists for future policy enforcement.
- non-persistence is the only actively enforced privacy boundary right now.

4. Recovery is practical rather than fully automated.
- export/import/rebuild operations exist
- queue-backed embedding is present
- broader repair orchestration still does not exist

## 17. Source Files To Read First

If another AI system needs the most important implementation sources, start here:

1. Project scope and current state
- `docs/PROJECT.md`
- `docs/STATUS.md`
- `docs/DECISIONS.md`

2. Design and deployment record
- `docs/PHASE1-DESIGN.md`
- `docs/PHASE2-EXECUTION.md`

3. Function API implementation
- `api/memory_api/run.py`

4. MCP bridge implementation
- `scripts/mcp/ai_mem_server.py`

5. Deployment automation
- `scripts/azure/Invoke-CodexMemoryAzureBuild.ps1`
- `scripts/azure/Invoke-CodexMemoryClientSetup.ps1`

## 18. Machine-Readable Summary

Use this block when handing the project to another AI:

```text
Project: ai-mem
Purpose: Azure-hosted shared/personal memory platform for AI coding agents.
Architecture: MCP stdio bridge -> Azure Function HTTP API -> Cosmos DB containers + retrieval telemetry container + storage queue for embedding jobs -> optional Azure OpenAI embeddings.
Primary storage: Cosmos DB NoSQL database "ai_mem".
Containers: personal_memory (/userId), shared_memory (/workspaceId), audit_log (/workspaceId), embeddings (/workspaceId), retrieval_log (/workspaceId).
Current auth: MCP bridge uses Function key plus shared-secret caller signing; Function App uses managed identity to access Cosmos DB.
No wrapper: The system does not intercept or wrap the codex CLI.
Core operations: get personal/shared memory, add fact/run/task, close task, list open tasks, vector search, summary search, item hydration, compact context build, export, import, rebuild embeddings, add audit event.
Retrieval model: vector similarity plus lexical overlap, with compact deduped summary retrieval, prompt-budget-aware context assembly, explicit embedding mode reporting, and retrieval telemetry.
Embedding pipeline: primary writes enqueue background embedding jobs instead of depending on synchronous embedding generation.
Privacy model: redact obvious secret patterns; support visibility metadata, retention metadata, and enforced non-persistent writes via store=false.
Recovery model: export/import, conflict-detection import preview, embedding rebuild, and queue-backed embedding retries are implemented; advanced repair orchestration is not.
Migration status: SQLite sources were migrated idempotently into Azure and validated.
Operational status: deployed and active; compact request-start recall is the default repo workflow.
Main code: api/memory_api/run.py and scripts/mcp/ai_mem_server.py
Main docs: docs/PROJECT.md, docs/STATUS.md, docs/DECISIONS.md, docs/PHASE1-DESIGN.md, docs/PHASE2-EXECUTION.md
```
