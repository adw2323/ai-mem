from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


@dataclass
class JournalConfig:
    path: Path


def default_journal_path() -> Path:
    base = Path.home() / ".codex" / "memories" / "journal"
    base.mkdir(parents=True, exist_ok=True)
    return base / "runs.jsonl"


def load_journal_config() -> JournalConfig:
    return JournalConfig(path=default_journal_path())


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_non_trivial_run(request: str, summary: str, status: str = "completed") -> bool:
    req = (request or "").strip().lower()
    summ = (summary or "").strip().lower()
    if not req and not summ:
        return False
    if status.lower() in {"failed", "error", "aborted"}:
        return True
    # Extremely short, read-like interactions are treated as trivial.
    if len(req) < 8 and len(summ) < 16:
        return False
    return True


def _models_from_env() -> list[str]:
    candidates = [
        os.environ.get("CODEX_MODEL", "").strip(),
        os.environ.get("OPENAI_MODEL", "").strip(),
    ]
    return [c for c in candidates if c]


def _tools_from_env() -> list[str]:
    # Best-effort only; callers may inject richer tool context explicitly later.
    val = os.environ.get("CODEX_TOOLS_USED", "").strip()
    if not val:
        return []
    return [item.strip() for item in val.split(",") if item.strip()]


def append_journal_entry(
    *,
    workspace_id: str,
    project_id: str,
    repo_id: str,
    cwd: str,
    repo_root: str,
    branch: str,
    request_summary: str,
    action_summary: str,
    outcome: str,
    source: str,
    status: str,
    tools_used: list[str] | None = None,
    models_used: list[str] | None = None,
    promoted_memory_ids: list[str] | None = None,
    source_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = load_journal_config()
    entry = {
        "journalId": str(uuid4()),
        "ts": utc_now(),
        "kind": "journal_entry",
        "workspaceId": workspace_id,
        "projectId": project_id,
        "repoId": repo_id,
        "cwd": cwd,
        "repoRoot": repo_root,
        "branch": branch,
        "requestSummary": request_summary,
        "actionSummary": action_summary,
        "outcome": outcome,
        "status": status,
        "source": source,
        "toolsUsed": tools_used or _tools_from_env(),
        "modelsUsed": models_used or _models_from_env(),
        "promotedMemoryIds": promoted_memory_ids or [],
        "sourceMeta": source_meta or {},
    }
    with cfg.path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=True) + "\n")
    return entry


def append_journal_link(*, parent_journal_id: str, promoted_memory_id: str, source: str = "system") -> dict[str, Any]:
    cfg = load_journal_config()
    event = {
        "journalEventId": str(uuid4()),
        "ts": utc_now(),
        "kind": "journal_link_event",
        "parentJournalId": parent_journal_id,
        "promotedMemoryId": promoted_memory_id,
        "source": source,
    }
    with cfg.path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=True) + "\n")
    return event


def extract_promoted_memory_id(result: dict[str, Any] | None) -> str:
    if not isinstance(result, dict):
        return ""
    for key in ("memoryId", "itemId", "id", "runId"):
        value = str(result.get(key, "") or "").strip()
        if value:
            return value
    item = result.get("item")
    if isinstance(item, dict):
        value = str(item.get("id", "") or "").strip()
        if value:
            return value
    data = result.get("data")
    if isinstance(data, dict):
        value = str(data.get("id", "") or "").strip()
        if value:
            return value
    return ""


def list_recent_journal_entries(limit: int = 20) -> list[dict[str, Any]]:
    cfg = load_journal_config()
    if not cfg.path.exists():
        return []
    lines = cfg.path.read_text(encoding="utf-8").splitlines()
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        if len(out) >= max(1, int(limit)):
            break
        try:
            item = json.loads(line)
        except Exception:
            continue
        if item.get("kind") == "journal_entry":
            out.append(item)
    return out
