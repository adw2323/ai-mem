# ai-mem Platform

## Problem Statement
Local SQLite-based Codex memory is single-machine and wrapper-dependent. We need centralized, multi-user, low-cost memory in Azure with auditability, vector recall, and migration of existing SQLite data.

## Goals
- Build centralized memory storage in Azure Cosmos DB for multiple users.
- Provide Azure Functions API endpoints for memory operations and audit logging.
- Integrate Codex via MCP tools (no `codex` CLI wrapper).
- Migrate existing SQLite memory data safely and idempotently.
- Keep monthly cost at free or near-free levels.
- Reduce memory-related prompt token usage through compact retrieval and progressive disclosure.
- Evolve the platform from generic shared/personal memory into workspace-centered engineering memory with explicit project, scope, memory-class, and trust metadata.

## Non-Goals
- Rebuilding Codex CLI behavior or introducing custom wrappers.
- Kubernetes, containers, or App Service-hosted API layers.
- Large-scale analytics or BI workloads.

## Architecture / Components
- Azure Cosmos DB account (NoSQL API), database `ai_mem`, five containers.
- Azure Functions (Consumption plan) as API gateway to Cosmos DB.
- Optional Azure OpenAI embeddings for semantic recall.
- Portable client package (`ai-mem`) with shared client core, MCP module entrypoint, and CLI calling Azure Functions endpoints.
- Repo-level `AGENTS.md` deterministic memory workflow rules.
- Additive metadata model for `projectId`, `memoryScope`, `memoryClass`, trust fields, and artifact lineage without breaking existing shared/personal storage routing.

## Current Backlog
1. Migrate from shared-secret caller signing to Entra-authenticated Function access with validated caller identity and workspace authorization.
2. Decide whether a local sentence-transformer fallback is justified, or whether `degraded_hash` should remain the only non-Azure-OpenAI fallback.
3. Expand heuristic run-summary extraction into a higher-confidence extraction path for durable facts, decisions, and open tasks.
4. Add richer import merge policies beyond `detect_conflicts`, `upsert`, and `skip_existing`.
5. Decide whether transient `store=false` writes should stay audit-only or graduate to a short-lived scratch store.
6. Use retrieval telemetry to determine whether ranking should later incorporate salience fields such as `importance`, `confidence`, `referenceCount`, and `lastReferenced`.
7. Expand retrieval beyond flat vector ranking so project-aware, class-aware, and trust-aware reranking can bias debugging questions toward episodic local context and design questions toward semantic durable knowledge.
8. Formalize artifact-to-memory promotion and consolidation lineage rather than treating all stored records as the same memory type.
9. Publish the portable client package to PyPI so machine bootstrap no longer depends on a local clone for installation.
10. Fix request-shape compatibility regression where `memory_build_context` and `memory_search_summaries` can return HTTP 400 for payloads that include `project_scope_mode` (`auto`/`global`) in Codex sessions.

## Potential Roadmap Additions (For Consideration)
Source for consideration: Google Cloud's open-source "Always On Memory Agent" (`GoogleCloudPlatform/generative-ai`, March 2026) and fit-gap analysis against this platform's current architecture.

1. Add timer-triggered background consolidation (for example every `30m`) to convert raw episodic writes into higher-confidence durable memory classes with deduplication and contradiction checks.
2. Add structured conflict-resolution policies during consolidation (detect contradictory facts/decisions, assign trust outcome, and optionally queue human review).
3. Add managed context hydration: start retrieval with compact context and auto-expand only on uncertainty/cache-miss signals rather than a single static budget.
4. Add optional multimodal memory artifacts (for example `blobUri`/artifact linkage for screenshots, PDFs, audio/video) while preserving text-first queryability.
5. Add a lightweight memory explorer UI for review/edit/delete/grooming workflows to complement MCP/CLI tooling.
6. Evaluate "write now, consolidate later" as a first-class operating mode to reduce agent cognitive load during active task execution.

These are explicitly candidate enhancements and should be validated against existing Azure constraints (cost, trust/audit model, multi-user authorization boundaries, and operational latency SLOs) before implementation.

## Inputs / Dependencies
- Existing SQLite DB(s) (if migrating from a local setup)
- Azure subscription with Cosmos DB Free Tier availability
- Azure CLI session and deployment permissions

## Risks
- Embedding model availability may vary by Azure region.
- Free-tier Cosmos account may already be consumed by another account in subscription.
- Existing wrapper artifacts can leave stale local behavior unless fully cleaned.

## Acceptance Criteria
- [x] Cosmos DB schema and partitioning match design.
- [x] Function endpoints and MCP tools are available and validated.
- [x] SQLite migration runs idempotently with report and audit events.
- [x] `codex` resolves to normal CLI (no intercept aliases/functions/wrappers).
- [x] Cost estimate and observed usage remain near $0/month.
