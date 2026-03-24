from __future__ import annotations

import argparse
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import (
    ClientConfig,
    add_common_write_metadata,
    infer_project_id,
    infer_repo_context,
    load_config_from_args,
    parse_json_arg,
    post_operation,
)
from .journal import (
    append_journal_entry,
    append_journal_link,
    extract_promoted_memory_id,
    is_non_trivial_run,
    list_recent_journal_entries,
)


mcp = FastMCP("ai-mem-mcp")
CFG: ClientConfig | None = None


def _cfg() -> ClientConfig:
    if CFG is None:
        raise RuntimeError("client config not initialized")
    return CFG


def _post(op: str, payload: dict[str, Any]) -> dict[str, Any]:
    return post_operation(_cfg(), op, payload)


@mcp.tool()
def memory_get_personal(limit: int = 50) -> dict[str, Any]:
    return _post("memory_get_personal", {"limit": int(limit)})


@mcp.tool()
def memory_get_shared(limit: int = 50) -> dict[str, Any]:
    return _post("memory_get_shared", {"limit": int(limit)})


@mcp.tool()
def memory_project_upsert(
    name: str,
    slug: str = "",
    description: str = "",
    repos_csv: str = "",
    tags_csv: str = "",
    status: str = "active",
) -> dict[str, Any]:
    repos = [item.strip() for item in (repos_csv or "").split(",") if item.strip()]
    tags = [item.strip() for item in (tags_csv or "").split(",") if item.strip()]
    return _post(
        "project_upsert",
        {
            "name": name,
            "slug": slug,
            "description": description,
            "repos": repos,
            "tags": tags,
            "status": status,
            "actor": _cfg().user_id,
        },
    )


@mcp.tool()
def memory_project_get(project_id: str = "", slug: str = "") -> dict[str, Any]:
    return _post("project_get", {"projectId": project_id, "slug": slug})


@mcp.tool()
def memory_project_list(status: str = "", limit: int = 50) -> dict[str, Any]:
    return _post("project_list", {"status": status, "limit": int(limit)})


@mcp.tool()
def memory_project_archive(project_id: str) -> dict[str, Any]:
    return _post("project_archive", {"projectId": project_id, "actor": _cfg().user_id})


@mcp.tool()
def memory_route_review(
    task_type: str = "general",
    risk_level: str = "low",
    has_canonical_context: bool = False,
    has_external_dependency: bool = False,
    has_unresolved_disagreement: bool = False,
    force_three_model_review: bool = False,
) -> dict[str, Any]:
    return _post(
        "memory_route_review",
        {
            "taskType": task_type,
            "riskLevel": risk_level,
            "hasCanonicalContext": bool(has_canonical_context),
            "hasExternalDependency": bool(has_external_dependency),
            "hasUnresolvedDisagreement": bool(has_unresolved_disagreement),
            "forceThreeModelReview": bool(force_three_model_review),
        },
    )


@mcp.tool()
def memory_add_disagreement(
    claim: str,
    task_type: str = "general",
    risk_level: str = "medium",
    codex_position: str = "",
    claude_position: str = "",
    gemini_position: str = "",
    resolution: str = "pending",
    resolution_status: str = "pending",
    correct_model: str = "",
    outcome: str = "pending",
    evidence: str = "",
    tags_csv: str = "",
    scope: str = "shared",
    project_id: str = "",
    memory_scope: str = "workspace",
    memory_class: str = "episodic",
    promotion_status: str = "candidate",
    importance: float = 0.7,
    confidence: float = 0.6,
    store: bool = True,
) -> dict[str, Any]:
    tags = [value.strip() for value in (tags_csv or "").split(",") if value.strip()]
    payload = {
        "scope": scope,
        "kind": "disagreement",
        "claim": claim,
        "taskType": task_type,
        "riskLevel": risk_level,
        "codexPosition": codex_position,
        "claudePosition": claude_position,
        "geminiPosition": gemini_position,
        "resolution": resolution,
        "resolutionStatus": resolution_status,
        "correctModel": correct_model,
        "outcome": outcome,
        "evidence": evidence,
        "tags": tags,
        "source": "mcp",
        "actor": _cfg().user_id,
    }
    return _post(
        "memory_add_disagreement",
        add_common_write_metadata(
            payload,
            project_id=project_id,
            memory_scope=memory_scope,
            memory_class=memory_class,
            visibility="normal",
            retention_days=0,
            importance=importance,
            confidence=confidence,
            trust_score=-1.0,
            trust_dimensions_json="",
            artifact_ref="",
            derived_from_ids_json="",
            supersedes_id="",
            promotion_status=promotion_status,
            store=store,
        ),
    )


@mcp.tool()
def memory_add_fact(
    key: str,
    value: str,
    tags_csv: str = "",
    scope: str = "shared",
    project_id: str = "",
    memory_scope: str = "",
    memory_class: str = "semantic",
    visibility: str = "normal",
    retention_days: int = 0,
    importance: float = 0.5,
    confidence: float = 0.8,
    trust_score: float = -1.0,
    trust_dimensions_json: str = "",
    artifact_ref: str = "",
    derived_from_ids_json: str = "",
    supersedes_id: str = "",
    promotion_status: str = "durable",
    store: bool = True,
) -> dict[str, Any]:
    tags = [t.strip() for t in (tags_csv or "").split(",") if t.strip()]
    payload = {
        "id": f"fact:{_cfg().workspace_id}:{key}",
        "scope": scope,
        "kind": "fact",
        "content": value,
        "tags": tags,
        "source": "mcp",
        "sourceMeta": {"key": key},
        "actor": _cfg().user_id,
    }
    return _post(
        "memory_add_fact",
        add_common_write_metadata(
            payload,
            project_id=project_id,
            memory_scope=memory_scope,
            memory_class=memory_class,
            visibility=visibility,
            retention_days=retention_days,
            importance=importance,
            confidence=confidence,
            trust_score=trust_score,
            trust_dimensions_json=trust_dimensions_json,
            artifact_ref=artifact_ref,
            derived_from_ids_json=derived_from_ids_json,
            supersedes_id=supersedes_id,
            promotion_status=promotion_status,
            store=store,
        ),
    )


@mcp.tool()
def memory_add_artifact(
    title: str,
    content: str = "",
    artifact_type: str = "",
    artifact_ref: str = "",
    tags_csv: str = "",
    scope: str = "shared",
    project_id: str = "",
    memory_scope: str = "",
    memory_class: str = "episodic",
    promotion_status: str = "candidate",
    importance: float = 0.6,
    confidence: float = 0.7,
    derived_from_ids_json: str = "",
    supersedes_id: str = "",
    store: bool = True,
) -> dict[str, Any]:
    tags = [t.strip() for t in (tags_csv or "").split(",") if t.strip()]
    payload = {
        "scope": scope,
        "kind": "artifact",
        "title": title,
        "content": content,
        "artifactType": artifact_type,
        "artifactRef": artifact_ref,
        "tags": tags,
        "source": "mcp",
        "actor": _cfg().user_id,
    }
    return _post(
        "memory_add_artifact",
        add_common_write_metadata(
            payload,
            project_id=project_id,
            memory_scope=memory_scope,
            memory_class=memory_class,
            visibility="normal",
            retention_days=0,
            importance=importance,
            confidence=confidence,
            trust_score=-1.0,
            trust_dimensions_json="",
            artifact_ref=artifact_ref,
            derived_from_ids_json=derived_from_ids_json,
            supersedes_id=supersedes_id,
            promotion_status=promotion_status,
            store=store,
        ),
    )


@mcp.tool()
def memory_add_run(
    branch: str = "",
    cwd: str = "",
    request: str = "",
    summary: str = "",
    status: str = "completed",
    project_id: str = "",
    memory_scope: str = "repo",
    memory_class: str = "episodic",
    visibility: str = "normal",
    retention_days: int = 0,
    importance: float = 0.5,
    confidence: float = 0.8,
    trust_score: float = -1.0,
    trust_dimensions_json: str = "",
    artifact_ref: str = "",
    derived_from_ids_json: str = "",
    supersedes_id: str = "",
    promotion_status: str = "durable",
    auto_extract: bool = False,
    extract_scope: str = "personal",
    store: bool = True,
) -> dict[str, Any]:
    request = (request or "").strip() or "(unspecified request)"
    summary = (summary or "").strip() or "(unspecified summary)"
    context = infer_repo_context(cwd or os.getcwd())
    effective_cwd = context["cwd"]
    effective_branch = branch or context["branch"] or "unknown"
    effective_project_id = project_id or infer_project_id(_cfg(), effective_cwd, context.get("repo_root", ""))
    effective_repo_id = context.get("repo_name", "") or _cfg().repo_id
    payload = {
        "kind": "run_summary",
        "branch": effective_branch,
        "cwd": effective_cwd,
        "repoRoot": context.get("repo_root", ""),
        "repoId": effective_repo_id,
        "request": request,
        "summary": summary,
        "status": status,
        "tags": ["run", status],
        "source": "mcp",
        "actor": _cfg().user_id,
        "autoExtract": bool(auto_extract),
        "extractScope": extract_scope,
        "sourceMeta": {
            "workspaceId": _cfg().workspace_id,
            "repoId": _cfg().repo_id,
            "inferredProjectId": effective_project_id,
            "repoRoot": context.get("repo_root", ""),
            "repoName": context.get("repo_name", ""),
        },
    }
    non_trivial = is_non_trivial_run(request, summary, status)
    journal_entry = None
    if non_trivial:
        journal_entry = append_journal_entry(
            workspace_id=_cfg().workspace_id,
            project_id=effective_project_id,
            repo_id=effective_repo_id,
            cwd=effective_cwd,
            repo_root=context.get("repo_root", ""),
            branch=effective_branch,
            request_summary=request,
            action_summary=summary,
            outcome=status,
            source="mcp",
            status=status,
            source_meta={
                "memoryScope": memory_scope,
                "memoryClass": memory_class,
                "visibility": visibility,
                "retentionDays": int(retention_days),
            },
        )
    write_payload = add_common_write_metadata(
        payload,
        project_id=effective_project_id,
        memory_scope=memory_scope,
        memory_class=memory_class,
        visibility=visibility,
        retention_days=retention_days,
        importance=importance,
        confidence=confidence,
        trust_score=trust_score,
        trust_dimensions_json=trust_dimensions_json,
        artifact_ref=artifact_ref,
        derived_from_ids_json=derived_from_ids_json,
        supersedes_id=supersedes_id,
        promotion_status=promotion_status,
        store=store,
    )
    try:
        result = _post("memory_add_run", write_payload)
        promoted_id = extract_promoted_memory_id(result)
        if journal_entry and promoted_id:
            append_journal_link(
                parent_journal_id=journal_entry["journalId"],
                promoted_memory_id=promoted_id,
                source="mcp",
            )
        if journal_entry:
            result["journal"] = {
                "recorded": True,
                "journalId": journal_entry.get("journalId", ""),
                "path": "local",
            }
        return result
    except Exception as exc:
        if journal_entry:
            return {
                "ok": False,
                "error": str(exc),
                "journal": {
                    "recorded": True,
                    "journalId": journal_entry.get("journalId", ""),
                    "path": "local",
                },
                "remoteWrite": {"attempted": True, "ok": False},
            }
        raise


@mcp.tool()
def memory_get_journal_recent(limit: int = 20) -> dict[str, Any]:
    items = list_recent_journal_entries(limit=int(limit))
    return {"ok": True, "count": len(items), "items": items}


@mcp.tool()
def memory_list_open_tasks(scope: str = "shared", limit: int = 20) -> dict[str, Any]:
    return _post("memory_list_open_tasks", {"scope": scope, "limit": int(limit)})


@mcp.tool()
def memory_add_task(
    title: str,
    priority: int = 2,
    scope: str = "shared",
    tags_csv: str = "",
    project_id: str = "",
    memory_scope: str = "",
    memory_class: str = "semantic",
    visibility: str = "normal",
    retention_days: int = 0,
    importance: float = 0.5,
    confidence: float = 0.8,
    trust_score: float = -1.0,
    trust_dimensions_json: str = "",
    artifact_ref: str = "",
    derived_from_ids_json: str = "",
    supersedes_id: str = "",
    promotion_status: str = "durable",
    store: bool = True,
) -> dict[str, Any]:
    tags = [t.strip() for t in (tags_csv or "").split(",") if t.strip()]
    payload = {
        "scope": scope,
        "title": title,
        "priority": int(priority),
        "taskState": "open",
        "tags": tags,
        "source": "mcp",
        "actor": _cfg().user_id,
    }
    return _post(
        "memory_add_task",
        add_common_write_metadata(
            payload,
            project_id=project_id,
            memory_scope=memory_scope,
            memory_class=memory_class,
            visibility=visibility,
            retention_days=retention_days,
            importance=importance,
            confidence=confidence,
            trust_score=trust_score,
            trust_dimensions_json=trust_dimensions_json,
            artifact_ref=artifact_ref,
            derived_from_ids_json=derived_from_ids_json,
            supersedes_id=supersedes_id,
            promotion_status=promotion_status,
            store=store,
        ),
    )


@mcp.tool()
def memory_close_task(task_id: str, scope: str = "shared") -> dict[str, Any]:
    return _post("memory_close_task", {"scope": scope, "id": task_id, "actor": _cfg().user_id})


@mcp.tool()
def memory_search_vectors(
    query: str,
    k: int = 8,
    project_id: str = "",
    intent: str = "",
    preferred_memory_class: str = "",
    project_scope_mode: str = "",
) -> dict[str, Any]:
    return _post(
        "memory_search_vectors",
        {
            "query": query,
            "k": int(k),
            "projectId": project_id,
            "intent": intent,
            "preferredMemoryClass": preferred_memory_class,
            "projectScopeMode": project_scope_mode,
        },
    )


@mcp.tool()
def memory_search_summaries(
    query: str,
    k: int = 8,
    project_id: str = "",
    intent: str = "",
    preferred_memory_class: str = "",
    project_scope_mode: str = "",
) -> dict[str, Any]:
    return _post(
        "memory_search_summaries",
        {
            "query": query,
            "k": int(k),
            "projectId": project_id,
            "intent": intent,
            "preferredMemoryClass": preferred_memory_class,
            "projectScopeMode": project_scope_mode,
        },
    )


@mcp.tool()
def memory_get_items(ids_json: str, include_content: bool = True) -> dict[str, Any]:
    ids = parse_json_arg(ids_json or "[]")
    return _post("memory_get_items", {"ids": ids, "includeContent": bool(include_content)})


@mcp.tool()
def memory_get_stats(scope: str = "all", limit: int = 500) -> dict[str, Any]:
    return _post("memory_get_stats", {"scope": scope, "limit": int(limit)})


@mcp.tool()
def memory_get_retrieval_logs(limit: int = 200, since_hours: int = 24, operation: str = "") -> dict[str, Any]:
    return _post(
        "memory_get_retrieval_logs",
        {"limit": int(limit), "sinceHours": int(since_hours), "operation": operation},
    )


@mcp.tool()
def memory_get_audit_events(limit: int = 200, since_hours: int = 24, operation: str = "") -> dict[str, Any]:
    return _post(
        "memory_get_audit_events",
        {"limit": int(limit), "sinceHours": int(since_hours), "operation": operation},
    )


@mcp.tool()
def memory_build_context(
    query: str,
    budget: str = "small",
    k: int = 8,
    project_id: str = "",
    intent: str = "",
    preferred_memory_class: str = "",
    project_scope_mode: str = "",
    include_items: bool = False,
) -> dict[str, Any]:
    return _post(
        "memory_build_context",
        {
            "query": query,
            "budget": budget,
            "k": int(k),
            "projectId": project_id,
            "intent": intent,
            "preferredMemoryClass": preferred_memory_class,
            "projectScopeMode": project_scope_mode,
            "includeItems": bool(include_items),
        },
    )


@mcp.tool()
def memory_export(scope: str = "all", limit: int = 200, include_embeddings: bool = False) -> dict[str, Any]:
    return _post("memory_export", {"scope": scope, "limit": int(limit), "includeEmbeddings": bool(include_embeddings)})


@mcp.tool()
def memory_import(items_json: str, mode: str = "upsert") -> dict[str, Any]:
    items = parse_json_arg(items_json or "[]")
    return _post("memory_import", {"items": items, "mode": mode})


@mcp.tool()
def memory_rebuild_embeddings(scope: str = "all", limit: int = 100) -> dict[str, Any]:
    return _post("memory_rebuild_embeddings", {"scope": scope, "limit": int(limit)})


@mcp.tool()
def memory_auto_promote(scope: str = "all", limit: int = 500, dry_run: bool = True) -> dict[str, Any]:
    return _post("memory_auto_promote", {"scope": scope, "limit": int(limit), "dryRun": bool(dry_run)})


@mcp.tool()
def memory_add_audit_event(operation: str, target_container: str, summary: str, source: str = "mcp") -> dict[str, Any]:
    return _post(
        "memory_add_audit_event",
        {
            "operation": operation,
            "targetContainer": target_container,
            "summary": summary,
            "source": source,
            "actor": _cfg().user_id,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint")
    parser.add_argument("--function-key")
    parser.add_argument("--shared-secret")
    parser.add_argument("--auth-mode", choices=["shared_secret", "entra", "dual", "off"], default="")
    parser.add_argument("--entra-scope", default="")
    parser.add_argument("--user-id")
    parser.add_argument("--workspace-id")
    parser.add_argument("--repo-id", default="")
    args = parser.parse_args()
    global CFG
    CFG = load_config_from_args(args)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
