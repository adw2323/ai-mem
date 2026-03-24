# ai-mem

Portable client package for the Azure-backed ai-mem platform.

This package provides:
- `ai-mem-mcp` for agent-facing MCP access
- `ai-mem` for direct CLI access to the same Function API

The package is designed to be installed independently of any single repository checkout.

Useful bootstrap commands:
- `ai-mem configure --endpoint ... --function-key ... --shared-secret ... --user-id ... --workspace-id ...`
- `ai-mem login`
- `ai-mem doctor`
- `ai-mem install-mcp`
- `ai-mem install-defaults`
  - Installs/updates global `~/.codex/AGENTS.md` managed policy block for memory-first + tri-model routing.
  - Installs/updates `~/.codex/skills/memory-triad-defaults/SKILL.md`.

Useful orchestration commands:
- `ai-mem route-review --task-type architecture --risk-level high --has-external-dependency`
- `ai-mem add-disagreement --claim "X is safe" --task-type architecture --risk-level high --codex-position ... --claude-position ... --resolution ...`
- `ai-mem orchestrate-review --task-title "Refactor auth flow" --task-type infra --risk-level high --query "auth refactor risk" --run-reviewers`
- `ai-mem project-upsert --name "ai-mem" --slug ai-mem`
- `ai-mem retrieval-logs --since-hours 24 --limit 500`
- `ai-mem audit-logs --since-hours 24 --operation embedding_job_failed --limit 200`
- `ai-mem rebuild-embeddings --scope all --limit 250`
