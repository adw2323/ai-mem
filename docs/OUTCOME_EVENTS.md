## ai-mem Outcome Layer

### New event APIs

Each event writes a dedicated document and updates its target summary fields without over-penalizing disagreements.

| Operation | Purpose | Key payload fields |
| --- | --- | --- |
| `memory_add_failure_event` | Record actionable failures (stale memory, incorrect routing, file conflict). | `targetId`, `failureKind`, `severity`, `reason`, `runId`, `taskId`, `recordLowSeverity`, `evidence` |
| `memory_add_correction_event` | Capture intent to correct or supersede a memory. | `targetId`, `correctionKind`, `correctedById`, `newVersionId`, `relatedDisagreementId`, `changes`, `evidence` |
| `memory_add_resolution_event` | Close disagreements; adjusts trust only after `confirmed`/`refuted`. | `targetId`, `disagreementId`, `verdict`, `trustDelta`, `dimensionDeltas`, `chosenOutcome`, `evidence` |
| `memory_add_trust_event` | Emit a controlled trust nudge for reinforcement or decay outside contested contexts. | `targetId`, `eventKind`, `trustDelta`, `dimensionDeltas`, `reason` |

### Target metadata enhancements

Every memory now includes:

- `failureCount`, `lastFailureAt` (recent failure visibility)
- `correctionCount`, `lastCorrectionAt`
- `hasPendingResolution`, `resolutionState`, `isContested` (default `resolved`)
- `trustScore` updates still derive from `trustDimensions`, which now mix in event-driven deltas

### Retrieval flags & warnings

Vector/summary search respect the following optional payload fields:

- `includeContested` (default `false`): allow items with `hasPendingResolution=true`.
- `includeFailed` (default `false`): allow items with recent `failureCount`.
- `minTrustScore` (default `0.3`): drop entries below the floor.
- `failureRecencyDays` (default `7`): how fresh a failure must be to block a hit.

When hits are pruned, responses append `retrievalWarnings` like `contested_pending_resolution`, `recent_failure`, or `low_trust`.

### CLI event commands

Four new commands mirror the HTTP APIs (`add-failure-event`, `add-correction-event`, `add-resolution-event`, `add-trust-event`) and accept workspace/user routing, evidence, run/task metadata, and the same semantics described above. Use `ai-mem` CLI `--help` to see full argument lists.

### Trust safety

Trust deltas are capped (`±0.12`) and windows ensure repeated penalties do not wipe an item. Resolutions only adjust trust for `confirmed`/`refuted` verdicts; ambiguous disagreements simply clear the contested flag. Corrections do not cascade to descendants automatically—they flag the record but leave downstream mixes for future review.
