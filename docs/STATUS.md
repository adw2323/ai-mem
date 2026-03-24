# Status - ai-mem Platform

## Current State
Phase 2 execution is complete. Azure resources are provisioned, MCP bridge is configured, SQLite data is migrated, and validation checks passed.

Execution now follows: `docs/EXECUTION-BOARD.md`.

## Completed
- 2026-03-06 - Confirmed local repo state and existing Codex memory tooling.
- 2026-03-06 - Inspected active SQLite schema and row counts.
- 2026-03-06 - Inspected secondary SQLite schema.
- 2026-03-06 - Verified current Codex runtime config preserves required settings and `codex` is not currently profile-intercepted.
- 2026-03-06 - Drafted full Azure architecture, migration, security, and cost plan for approval.
- 2026-03-06 - Provisioned Azure resources: resource group, Cosmos DB account, function app, storage account.
- 2026-03-06 - Deployed Azure Function API for memory operations and audit logging.
- 2026-03-06 - Configured local MCP server `ai-mem-mcp` in `~/.codex/config.toml`.
- 2026-03-06 - Ran idempotent migration from local SQLite DB; generated migration report.
- 2026-03-06 - Removed legacy in-repo wrapper implementation and retired SQLite MCP setup script.
- 2026-03-06 - Validated personal isolation, shared reads, audit log writes, vector search, and normal `codex` CLI behavior.
- 2026-03-14 - Added the first retrieval-compression implementation slice: compact search summaries, hydration-by-ID, and budget-aware context assembly in the Function API and MCP bridge.
- 2026-03-14 - Added retrieval dedupe and ranking improvements, privacy-aware do-not-store write paths, and export/import plus embedding-rebuild operations in the Function API and MCP bridge.
- 2026-03-14 - Extended the Azure build validation path to exercise compact retrieval endpoints.
- 2026-03-14 - Switched the repo-level deterministic AGENTS memory workflow to use `memory_build_context` with a `small` budget instead of raw `memory_search_vectors`.
- 2026-03-14 - Published `TECHNICAL-REPORT.md` as a standalone implementation brief for external AI handoff and operator reference.
- 2026-03-14 - Added explicit embedding quality modes and warnings so degraded hash fallback is surfaced as degraded rather than silent.
- 2026-03-14 - Added retrieval telemetry via dedicated `retrieval_log` storage, including scores, budgets, result counts, latency, and query-mode metadata.
- 2026-03-14 - Added signed caller-context validation in the Function API and matching MCP bridge signing using a shared secret.
- 2026-03-14 - Added queue-backed embedding generation with a queue-triggered worker so primary writes no longer depend on synchronous embedding generation.
- 2026-03-14 - Added salience metadata fields (`importance`, `confidence`, `referenceCount`, `lastReferenced`) plus conflict-detection import mode and optional heuristic run-summary auto-extraction.
- 2026-03-15 - Added the first rearchitecture metadata slice for workspace-centered engineering memory: `projectId`, `memoryScope`, `memoryClass`, `trustScore`, `trustDimensions`, `promotionStatus`, `artifactRef`, and `derivedFromIds` now flow through the Function API summaries/import comparisons and the MCP write tools.
- 2026-03-15 - Added the first portable-access slice: `ai-mem` is now an installable Python package with shared client core, CLI, and MCP module entrypoint, and the Azure client setup/build scripts now install and configure the package instead of copying a repo-local MCP script.
- 2026-03-15 - Added portable bootstrap commands and config persistence to the client package: `configure`, `login`, `doctor`, and `install-mcp` now support reconnecting a new machine through user-level config rather than repo-local arguments alone, and setup scripts now support published-package install via `AI_MEM_CLIENT_PACKAGE`.
- 2026-03-15 - Added the next retrieval slice: `memory_search_summaries` now widens candidate generation and reranks results by inferred intent, preferred memory class, project/repo scope fit, trust dimensions, recency, and promotion status; `memory_build_context` now carries ranking signals through compact context assembly; CLI and MCP search surfaces now accept `projectId`, `intent`, and `preferredMemoryClass`.
- 2026-03-17 - Completed deterministic deploy hardening stream:
  - `scripts/azure/Invoke-CodexMemoryFunctionDeploy.ps1` is now the primary deploy path.
  - Deploys now validate route and shared-memory smoke checks after publish.
- 2026-03-17 - Completed embedding reliability stream:
  - Root cause fixed for queue poison behavior by base64 queue encoding in producer (`TextBase64EncodePolicy`).
  - Added embedding health operations script: `Invoke-CodexMemoryEmbeddingHealth.ps1`.
  - Added stale replay and poison queue cleanup options (`-ReplayStale`, `-ClearPoisonQueue`).
  - Verified live E2E probe transitions `pending -> ready`.
- 2026-03-17 - Completed retrieval telemetry baseline stream:
  - Added retrieval log API read endpoint (`memory_get_retrieval_logs`) and audit read endpoint (`memory_get_audit_events`).
  - Added CLI/MCP surfaces: `retrieval-logs`, `audit-logs`, `rebuild-embeddings`.
  - Added baseline report generator: `scripts/azure/New-CodexMemoryTelemetryBaseline.ps1`.
  - Published baseline report and tuning backlog under `docs/reports/`.
- 2026-03-17 - Restored production semantic embedding path to Azure OpenAI (`text-embedding-3-small`); live retrieval mode now reports `embeddingMode=azure_openai` with no degraded warnings.
- 2026-03-17 - Added hard deploy guardrails in `Invoke-CodexMemoryFunctionDeploy.ps1` so cutover fails by default when critical embedding settings are missing or post-deploy embedding mode is degraded; emergency override requires explicit `-AllowDegradedEmbedding`.
- 2026-03-17 - Completed Stream D workspace/project envelope hardening:
  - Added `projectScopeMode` (`off|prefer|strict`) and fallback telemetry on retrieval paths.
  - Added repo attachment index and `projectMembershipFit` rerank signal.
  - Added compact context `includeItems` control with compact default on CLI/MCP.
- 2026-03-17 - Completed Stream E lifecycle automation slice:
  - Added `memory_add_artifact` operation (API + CLI + MCP).
  - Added supersession linking (`supersedesId`) with explicit superseded state.
  - Added canonical consolidation in `memory_auto_promote` and surfaced consolidation output.
- 2026-03-17 - Completed Stream F auth hardening slice:
  - Added `MEMORY_AUTH_MODE` policy (`shared_secret|entra|dual|off`) with guarded insecure-off switch.
  - Added HMAC nonce replay protection (`x-codex-context-nonce`) and timestamp window enforcement.
  - Added Entra caller allowlist controls and dual-mode Entra gating switch.
  - Updated client/setup/deploy scripts for auth-mode and Entra scope wiring.
- 2026-03-17 - Completed Stream C follow-up for telemetry alerts:
  - Added `Invoke-CodexMemoryTelemetryAlerts.ps1` threshold evaluation script.
  - Published retrieval alert reports under `docs/reports/`.

## In Progress
- 2026-03-14 - Defined the next improvement track for memory retrieval ergonomics, prompt-token reduction, privacy controls, and recovery/export workflows.
- 2026-03-14 - Implementation remains additive; callers still use the old retrieval tools until prompt assembly is switched to the new compact APIs.
- 2026-03-15 - Reframing the platform model around episodic vs semantic memory, explicit trust dimensions, and class-aware retrieval without changing the existing storage backend.
- 2026-03-15 - Reframing client survivability so the write path is portable after install and no longer depends on retaining this repository checkout on the local machine.
- 2026-03-17 - Release/ops closeout for completed D/E/F/C2 changes (docs sync, report publication, commit/tag prep).
- 2026-03-23 - Investigating ai-mem API validation regression: `memory_build_context` and `memory_search_summaries` return HTTP 400 for some payloads that include `project_scope_mode` (`auto`/`global`). Evidence indicates endpoint-level request validation mismatch (not MCP startup timing).
- 2026-03-23 - Investigating write-path latency and timeout behavior on `memory_add_run`: some calls time out at the client-side limit but the journal entry is still persisted, indicating response-path latency/timeout rather than guaranteed write loss.

## Incident Notes (2026-03-23)
- Symptom: `memory_add_run` occasionally returns timeout (`timed out awaiting tools/call after 120s`) during active Codex sessions.
- Verified behavior: at least one timed-out write was later observed in `memory_get_journal_recent`, confirming write success despite timeout response to caller.
- Risk: caller sees failure while backend may have committed, which causes duplicate writes, uncertainty, and poor UX.
- Performance expectation: write path should be near-instant (targeting sub-second typical latency, low single-digit seconds worst-case), not 120s waits.

## Next Actions
1. Security policy decision: keep dual-mode default or enforce Entra-in-dual in production.
2. Tune `avgFinalTopScore` threshold from current live baseline (`retrieval-alerts-*` reports).
3. Optional hardening: persistent nonce replay cache for strict multi-instance replay guarantees.
4. Patch and deploy request-shape normalization/validation compatibility for `project_scope_mode` on retrieval endpoints and add a regression test covering Codex session payloads.
5. Write-path reliability hardening:
   - reduce client write timeout from 120s to a strict low bound suitable for interactive use,
   - add bounded retry with jitter for timeout/5xx,
   - make write operations idempotent (`request_id`) and verify-on-timeout before surfacing failure,
   - evaluate fast-ack (`202`) + async persistence for non-blocking writes.
6. Endpoint stability hardening:
   - confirm client routing is pinned to the intended production app/version,
   - remove ambiguity from parallel legacy/prod Function App names where applicable.

## Blockers
- None.
