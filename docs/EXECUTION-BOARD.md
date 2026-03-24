# Execution Board - ai-mem

## Program Goal
Evolve `ai-mem` into a durable, workspace-centered memory platform that remains reliable across sessions, repos, and machines, with trust-aware retrieval and portable client access.

## Operating Model
- `Codex`: primary implementer and orchestrator.
- `Claude`: deep architecture/reliability reviewer for high-risk design and deployment decisions.
- `Gemini`: independent checker for assumptions, failure-mode coverage, and alternative implementation paths.
- Parallelism rule: run independent streams concurrently; merge only after explicit smoke tests and regression checks.

## Done
- Azure Function production cutover completed to production Function App.
- Old unstable Function App decommissioned.
- Tri-model routing endpoints live: `memory_route_review`, `memory_add_disagreement`.
- Embedding enqueue warning fixed (`ResourceExistsError` path removed from normal writes).
- Stream A completed:
  - Deterministic deploy script finalized: `scripts/azure/Invoke-CodexMemoryFunctionDeploy.ps1`.
  - Task runner wiring complete: `memory:azure:deploy-api`.
  - Deploy smoke gates are live and enforced.
- Stream B completed:
  - Queue-trigger `embedding_worker` deployed and verified.
  - End-to-end probe confirms `pending -> ready`.
  - Stale replay and poison queue ops shipped: `memory:azure:embedding-health` (`-ReplayStale`, `-ClearPoisonQueue`).
  - Current health baseline: sampled `19`, `ready=19`, `pending=0`, `failed=0`, `stalePending=0`.
- Stream C completed:
  - Retrieval telemetry log read endpoint shipped: `memory_get_retrieval_logs`.
  - Baseline report generator shipped: `memory:azure:telemetry-baseline`.
  - Current baseline published: `docs/reports/retrieval-baseline-<timestamp>.md`.
  - Tuning backlog published: `docs/reports/RETRIEVAL-TUNING-BACKLOG.md`.
- Embedding production path restored:
  - Production Function App now points to Azure OpenAI (`text-embedding-3-small`).
  - Live deploy smoke confirms `embeddingMode=azure_openai` and `embeddingDegraded=false`.
- Deploy safety contract hardened:
  - Deterministic deploy now blocks cutover when critical `AZURE_OPENAI_*` settings are missing.
  - Deterministic deploy now blocks cutover when post-deploy retrieval mode is degraded.
  - Emergency override is explicit: `-AllowDegradedEmbedding`.
- Stream D completed:
  - Scope-gated retrieval envelope (`projectScopeMode=off|prefer|strict`) shipped for vector/summaries/context flows.
  - Repo attachment index and project membership rerank signal (`projectMembershipFit`) shipped.
  - Compact context control (`includeItems`) shipped with API compatibility and compact-by-default client behavior.
  - Replay battery and live deploy validation passed on production Function App.
- Stream E completed:
  - Added first-class artifact write path: `memory_add_artifact` (API + CLI + MCP).
  - Added supersession linking (`supersedesId`) with explicit `status=superseded` and ranking penalty (`supersessionFit`).
  - Added canonical consolidation in `memory_auto_promote` to supersede duplicate canonical records.
  - Added lifecycle visibility through `memory_get_stats` promotion counts and promotion/consolidation output.
- Stream F completed:
  - Added auth policy mode: `MEMORY_AUTH_MODE=shared_secret|entra|dual|off`.
  - Added guarded insecure mode (`MEMORY_ALLOW_INSECURE_AUTH_OFF=true` required for `off`).
  - Added signed-request nonce replay protection (`x-codex-context-nonce` + TTL replay cache).
  - Added Entra caller checks with allowlist controls:
    - `MEMORY_ALLOWED_CALLER_OBJECT_IDS`
    - `MEMORY_ALLOWED_CALLER_PRINCIPALS`
    - `MEMORY_ALLOW_ALL_ENTRA_PRINCIPALS`
    - `MEMORY_ENABLE_ENTRA_IN_DUAL`
  - Updated deploy/client paths for dual-mode and Entra scope wiring.
- Stream C2 completed:
  - Added threshold alert script: `scripts/azure/Invoke-CodexMemoryTelemetryAlerts.ps1`.
  - Added task runner command: `memory:azure:telemetry-alerts`.
  - Alert report artifacts now publish under `docs/reports/retrieval-alerts-*.{json,md}`.

## Now
- Consolidate remaining uncommitted updates, publish the latest replay/alert reports, and cut release notes for the completed D/E/F/C2 streams.

## Next
- Optional hardening follow-ups:
  - Add nonce replay cache persistence for multi-instance strictness (Redis-backed) if required.
  - Add Entra-only smoke path once EasyAuth audience binding is finalized.
  - Tune `avgFinalTopScore` alert floor per environment baselines.
  - Execute write-path timeout hardening backlog in `docs/TODO.md` (P0 latency/reliability items first).

## Blocked
- None currently.

## Parallel Task Queue
1. Release/ops: commit and tag completed D/E/F/C2 delivery set.
2. Security hardening: decide whether to enforce Entra in dual mode for production.
3. Quality tuning: calibrate telemetry alert thresholds from the newest 7-day baseline.
4. Reliability regression: resolve HTTP 400 compatibility failures on `memory_build_context`/`memory_search_summaries` when Codex session payloads include `project_scope_mode` (`auto`/`global`), then ship a guardrail test.
5. Reliability regression: eliminate `memory_add_run` timeout ambiguity (idempotent writes, low interactive timeout, verify-on-timeout, latency SLO alerts).

## Definition of Program Success
- Deployments are deterministic and recoverable.
- Memory writes and embeddings are stable under normal load.
- Retrieval quality is measurable and improving.
- Workspace/project memory boundaries are explicit and enforced.
- Client access is portable and repo-independent by default.
