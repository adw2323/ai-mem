# Decisions - ai-mem Platform

## 2026-03-06 - Two-Phase Delivery Gate
- Context: Request requires no cloud provisioning until design is approved.
- Decision: Execute discovery/design/costing first, then block all provisioning until explicit approval.
- Rationale: Prevent accidental spend or architecture drift.
- Impact: Phase 2 will run only after explicit go-ahead.

## 2026-03-06 - Primary Source SQLite DB For Migration
- Context: Two SQLite databases exist; current MCP config points to the active local SQLite DB.
- Decision: Treat the active local SQLite DB as primary migration source, with optional merge from repo-local DB.
- Rationale: Matches active runtime and contains `notes` and `embeddings` tables required by migration scope.
- Impact: Migration scripts will accept multiple inputs but prioritize active MCP source.

## 2026-03-06 - Azure Region Baseline
- Context: Deployers should select a region appropriate for their data residency and latency requirements.
- Decision: Default region is a deployer-supplied parameter; examples use `<your-azure-region>`.
- Rationale: Keeps data residency and latency predictable per deployment.
- Impact: If preferred embedding model is unavailable in the chosen region, fallback model/region path is documented before rollout.

## 2026-03-06 - No Codex Wrapper Pattern
- Context: Existing repo may contain legacy wrapper logic and historical profile override scripts.
- Decision: New platform uses MCP + Azure Functions only; no `codex` interception.
- Rationale: Preserves normal interactive `codex` behavior and avoids hidden runtime side effects.
- Impact: Wrapper scripts/config references will be removed in Phase 2 cleanup.

## 2026-03-06 - Function Key For MCP Bridge Initial Cutover
- Context: Need immediate low-friction MCP connectivity while avoiding Cosmos DB keys on client machines.
- Decision: Use Azure Function key auth between local MCP bridge and Function API for initial build.
- Rationale: Meets "no Cosmos keys on client" requirement and keeps deployment simple/automatable.
- Impact: Endpoint key is stored in local Codex config; optional future hardening is Entra-authenticated Function access.

## 2026-03-06 - Embedding Provider Fallback
- Context: Region/model pricing and availability can shift; deterministic low-cost behavior is required at cutover.
- Decision: Function API supports Azure OpenAI embeddings when configured, with deterministic hash-vector fallback when not configured.
- Rationale: Guarantees vector search availability with near-zero operational complexity.
- Impact: Semantic quality is acceptable now and can be improved later by enabling Azure OpenAI deployment settings.

## 2026-03-14 - Retrieval Compression Before MCP Compression
- Context: Memory-related token use can be reduced either by external MCP schema compression or by returning less text from the memory system itself.
- Decision: Prioritize progressive-disclosure retrieval, budget-aware context assembly, and compact provenance-rich summaries inside `ai-mem` before introducing a generic MCP compression layer.
- Rationale: The current memory platform is a thin bridge with a small tool surface. The larger immediate win is to reduce recalled memory volume and avoid returning full records unless explicitly requested.
- Impact: Near-term implementation work will focus on new retrieval/context APIs and compact response shapes rather than adding another proxy tier.

## 2026-03-14 - External Memory Projects Are Design Input Only
- Context: External projects such as `thedotmack/claude-mem` provide useful retrieval, privacy, and recovery patterns, but they use different local architectures and licensing.
- Decision: Reuse the ideas, not the code, from external memory systems when evolving `ai-mem`.
- Rationale: The platform boundary is Azure Functions + Cosmos DB + MCP, and direct code reuse from AGPL- or PolyForm-licensed projects would create avoidable licensing and architecture friction.
- Impact: Backlog items derived from external projects will be implemented natively in this repo and documented as original Azure-platform features.

## 2026-03-14 - First Compression Slice Is Additive
- Context: Existing callers already rely on `memory_search_vectors`, `memory_get_shared`, and `memory_get_personal`.
- Decision: Introduce `memory_search_summaries`, `memory_get_items`, and `memory_build_context` as additive APIs before changing any existing memory workflow defaults.
- Rationale: This reduces rollout risk, keeps current tooling stable, and lets prompt assembly migrate to compact retrieval incrementally.
- Impact: The Function API and MCP bridge will temporarily expose both raw and compact retrieval paths until usage patterns are updated.

## 2026-03-14 - Default Request-Start Recall Uses Compact Context
- Context: The compact retrieval APIs are now implemented and validated, but the repo workflow still defaulted to raw vector-hit recall.
- Decision: Change the repo-level AGENTS deterministic memory workflow to call `memory_build_context` with a `small` budget at request start.
- Rationale: This is the lowest-risk way to realize token savings immediately while preserving the underlying vector search and leaving `memory_search_vectors` available as a low-level primitive.
- Impact: New turns should consume less memory-related prompt budget by default, with `memory_search_summaries` and `memory_get_items` available for inspection and drill-down.

## 2026-03-14 - Privacy Controls Use Explicit Non-Persistence
- Context: Some memory writes are useful for the current turn but should not be persisted or embedded.
- Decision: Add `store=false` support on write operations so the API can return normalized transient results and audit the skip without writing memory records or embeddings.
- Rationale: This creates a clear do-not-store path without introducing a hidden retention model or a new storage tier yet.
- Impact: Clients can start marking sensitive/transient material as non-persistent immediately; future work can add a dedicated scratch container if needed.

## 2026-03-14 - Recovery Starts With Export Import And Embedding Rebuild
- Context: The platform needed recovery mechanics for partial writes, migrations, and stale vector state.
- Decision: Start with additive `memory_export`, `memory_import`, and `memory_rebuild_embeddings` operations rather than a broader queue-based repair system.
- Rationale: These operations cover the highest-value recovery workflows while fitting the current Azure Functions + Cosmos architecture cleanly.
- Impact: Operators now have native API paths for snapshotting memory, replaying data, and re-embedding stored records; more advanced repair automation can build on these later.

## 2026-03-14 - Azure OpenAI Is The Production Embedding Path
- Context: The memory platform is intended to be a semantic recall system, not a keyword-only note store.
- Decision: Treat Azure OpenAI embeddings as the production path and label deterministic hash vectors as `degraded_hash` fallback rather than an equivalent mode.
- Rationale: Silent fallback hides retrieval-quality degradation and makes debugging semantic recall failures difficult.
- Impact: Retrieval and write responses now surface embedding mode, degraded state, and warnings so operators can see when recall quality is reduced.

## 2026-03-14 - Retrieval Telemetry Uses A Dedicated Container
- Context: Retrieval quality and latency needed observability, but append-only audit events should remain focused on governance and operator actions.
- Decision: Store retrieval telemetry in a dedicated `retrieval_log` Cosmos container instead of `audit_log`.
- Rationale: Retrieval telemetry is higher-volume, query-oriented operational data with a different retention and analysis profile than audit records.
- Impact: Search and context-build responses now emit telemetry IDs, and operators can inspect retrieval quality without polluting the audit stream.

## 2026-03-14 - Embedding Generation Moves Behind A Queue
- Context: Synchronous embedding generation couples primary writes to Azure OpenAI latency and failures.
- Decision: Persist primary memory records first, then enqueue embedding jobs to a storage queue processed by a queue-triggered Function worker.
- Rationale: This reduces write latency, isolates embedding failures, and gives the platform retry semantics without blocking primary writes.
- Impact: Memory writes now return embedding status and queue job IDs, and source records track pending, ready, or failed embedding state.

## 2026-03-14 - Function Layer Validates Signed Caller Context
- Context: The MCP bridge injects `userId`, `workspaceId`, and `repoId`, but the Function layer should not permanently trust client-supplied identity claims without validation.
- Decision: Require HMAC-signed caller context headers at the Function layer as the immediate hardening step while Entra-authenticated caller validation remains the target state.
- Rationale: Signed context is a pragmatic stopgap that materially raises the bar against accidental or malicious claim spoofing without blocking current deployment.
- Impact: MCP clients must now supply a shared secret and signed timestamped context headers; future Entra work can replace the shared-secret approach cleanly.

## 2026-03-15 - Rearchitecture Starts As Metadata Expansion
- Context: The platform already works as a centralized Azure memory service, but the next stage needs explicit concepts for project-level context, memory class, trust explainability, and artifact lineage without destabilizing the live system.
- Decision: Introduce the first rearchitecture slice as additive metadata on existing records and MCP write tools: `projectId`, `memoryScope`, `memoryClass`, `trustScore`, `trustDimensions`, `promotionStatus`, `artifactRef`, and `derivedFromIds`.
- Rationale: This preserves the current Cosmos container model and tool contracts while making the data model capable of supporting project-aware retrieval, episodic vs semantic memory handling, trust-aware ranking, and artifact-to-memory promotion later.
- Impact: Existing callers remain compatible, newer callers can start writing richer memory records immediately, and future retrieval work can use this metadata instead of requiring a disruptive schema rewrite.

## 2026-03-15 - Trust Must Be Explainable, Not Just Scalar
- Context: A single trust scalar is useful for ranking but hides too much of the reasoning needed to understand why a memory should or should not be relied on.
- Decision: Model trust as both `trustScore` and `trustDimensions`, with dimensions such as provenance strength, confirmation level, freshness, contradiction state, and reuse success.
- Rationale: This supports better future ranking behavior and makes low-trust or superseded memory explainable to operators and agents.
- Impact: Stored memory can now carry interpretable trust evidence instead of only salience-style metadata; retrieval and consolidation logic can later weight dimensions differently by intent and memory class.

## 2026-03-15 - Portable Access Layer Is Independent From Any Single Repo
- Context: The Azure backend is durable, but the practical memory write path still depended on repo-hosted MCP bridge code and setup scripts.
- Decision: Introduce a portable client layer as an installable Python package (`ai-mem`) with a shared API client core, MCP module entrypoint, and operator-facing CLI. Keep the repo-local MCP script only as a compatibility shim.
- Rationale: This separates platform durability from repo survivability. After installation, a machine can keep reading and writing memory without depending on this repository checkout to remain present.
- Impact: Azure client setup and build scripts now install the package into the user environment and configure Codex/Abacus to launch `python -m ai_mem.mcp_server` directly.

## 2026-03-15 - New Machine Bootstrap Uses Stored Client Config
- Context: Portable packaging alone is not sufficient if every command still requires copying endpoint and auth arguments from repo-local scripts or docs.
- Decision: Add user-level client config persistence plus `configure`, `login`, and `doctor` commands to the portable CLI, and allow setup scripts to install from a published package source via `AI_MEM_CLIENT_PACKAGE`.
- Rationale: A new machine should be able to reconnect with minimal bootstrap state and validate connectivity without depending on repo-local implementation details.
- Impact: The portable client now supports a repo-independent reconnection path, and the setup/build scripts can prefer published package distribution when available while retaining local-source fallback for development.

## 2026-03-15 - Retrieval Becomes Intent Aware Before Storage Changes Again
- Context: The platform now stores `projectId`, `memoryClass`, and `trustDimensions`, but leaving them unused in retrieval would make the rearchitecture mostly theoretical.
- Decision: Evolve retrieval additively by widening vector candidate generation and reranking summaries/context results using inferred intent, preferred memory class, repo/project scope fit, trust dimensions, recency, and promotion status.
- Rationale: This delivers the first real behavioral value from the richer metadata without changing the Cosmos container model or forcing a first-class project entity before the ranking policy is validated.
- Impact: Debug and incident queries can increasingly favor recent episodic local context, while design queries can increasingly favor semantic durable knowledge and higher-trust promoted records.
