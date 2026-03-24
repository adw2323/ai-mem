# TODO - ai-mem

## P0 - Write Path Latency and Timeout Reliability
- [x] Reduce interactive write timeout (`memory_add_run` and adjacent write ops) from `120s` to a strict interactive ceiling (target <= `10s`).
- [x] Add bounded retry with exponential backoff + jitter for timeout and transient `5xx` responses.
- [x] Add idempotency key support (`request_id`) for write endpoints so retries cannot duplicate records.
- [x] Add verify-on-timeout flow in client: after timeout, perform read check before returning failure.
- [x] Add end-to-end regression test that simulates slow server response with eventual commit and validates caller behavior.

## P0 - Reliability Regression: HTTP 400 compatibility failures
- [x] Resolve HTTP 400 compatibility failures on `memory_build_context`/`memory_search_summaries` when Codex session payloads include `project_scope_mode` (`auto`/`global`), then ship a guardrail test.

## P0 - SLO and Observability
- [ ] Define and publish write-path latency SLOs:
  - `p50 < 300ms`
  - `p95 < 1.5s`
  - hard failure budget at `5-10s` for interactive calls.
- [ ] Emit latency + timeout telemetry for each write op (request type, timeout, retry count, final outcome).
- [ ] Add alert on timeout-rate and tail-latency drift for write endpoints.

## P1 - API Execution Model
- [ ] Evaluate fast-ack pattern (`202 Accepted`) with queue-backed async persistence for long writes.
- [ ] If fast-ack adopted, add completion status lookup endpoint and client-side polling contract.

## P1 - Endpoint/Deployment Hygiene
- [ ] Audit active ai-mem function app endpoints and deprecate ambiguous legacy names.
- [ ] Ensure clients are pinned to a single intended production endpoint.
- [ ] Confirm app-plan suitability for low-latency writes; evaluate Premium/AlwaysOn if Consumption cold-start affects SLA.

## P2 - Roadmap Candidates (For Consideration)
- [ ] Prototype timer-triggered consolidation (`30m`) that promotes episodic writes into durable facts/decisions/tasks with dedup + contradiction detection.
- [ ] Define contradiction-resolution policy during consolidation (auto-resolve vs review queue) with trust metadata updates.
- [ ] Prototype managed context hydration (expand retrieval budget only when uncertainty/cache-miss is detected).
- [ ] Design multimodal artifact support (`blobUri`-linked memories for screenshots/PDFs/audio/video) without degrading text retrieval quality.
- [ ] Evaluate a lightweight memory explorer UI for curation/grooming workflows.

## References
- Incident note: `docs/STATUS.md` (`Incident Notes (2026-03-23)`).
