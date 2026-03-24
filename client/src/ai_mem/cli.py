from __future__ import annotations

import argparse
import json
import os
import subprocess

from .client import (
    ClientConfig,
    add_common_write_metadata,
    infer_project_id,
    infer_repo_context,
    config_dict,
    config_path,
    install_memory_triad_defaults,
    install_codex_mcp_server,
    load_config_from_args,
    load_saved_config,
    parse_json_arg,
    post_operation,
    resolve_cached_cli_binary,
    save_config,
)
from .journal import (
    append_journal_entry,
    append_journal_link,
    extract_promoted_memory_id,
    is_non_trivial_run,
    list_recent_journal_entries,
)


def add_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--endpoint")
    parser.add_argument("--function-key")
    parser.add_argument("--shared-secret")
    parser.add_argument("--auth-mode", choices=["shared_secret", "entra", "dual", "off"], default="")
    parser.add_argument("--entra-scope", default="")
    parser.add_argument("--user-id")
    parser.add_argument("--workspace-id")
    parser.add_argument("--repo-id", default="")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-mem")
    sub = parser.add_subparsers(dest="command", required=True)

    configure = sub.add_parser("configure")
    add_connection_args(configure)

    login = sub.add_parser("login")
    add_connection_args(login)

    doctor = sub.add_parser("doctor")
    add_connection_args(doctor)

    install_mcp = sub.add_parser("install-mcp")
    add_connection_args(install_mcp)
    install_mcp.add_argument("--server-name", default="ai-mem-mcp")
    install_mcp.add_argument("--launcher-command", default="python")
    install_mcp.add_argument("--startup-timeout-sec", type=int, default=60)

    install_defaults = sub.add_parser("install-defaults")
    install_defaults.add_argument("--no-force-skill", action="store_true")

    get_shared = sub.add_parser("get-shared")
    add_connection_args(get_shared)
    get_shared.add_argument("--limit", type=int, default=50)

    get_personal = sub.add_parser("get-personal")
    add_connection_args(get_personal)
    get_personal.add_argument("--limit", type=int, default=50)

    journal_list = sub.add_parser("journal-list")
    journal_list.add_argument("--limit", type=int, default=20)

    add_run = sub.add_parser("add-run")
    add_connection_args(add_run)
    add_run.add_argument("--branch", default="")
    add_run.add_argument("--cwd", default="")
    add_run.add_argument("--request", required=True)
    add_run.add_argument("--summary", required=True)
    add_run.add_argument("--status", default="completed")
    add_run.add_argument("--project-id", default="")
    add_run.add_argument("--memory-scope", default="repo")
    add_run.add_argument("--memory-class", default="episodic")
    add_run.add_argument("--visibility", default="normal")
    add_run.add_argument("--retention-days", type=int, default=0)
    add_run.add_argument("--importance", type=float, default=0.5)
    add_run.add_argument("--confidence", type=float, default=0.8)
    add_run.add_argument("--trust-score", type=float, default=-1.0)
    add_run.add_argument("--trust-dimensions-json", default="")
    add_run.add_argument("--artifact-ref", default="")
    add_run.add_argument("--derived-from-ids-json", default="")
    add_run.add_argument("--supersedes-id", default="")
    add_run.add_argument("--promotion-status", default="durable")
    add_run.add_argument("--auto-extract", action="store_true")
    add_run.add_argument("--extract-scope", default="personal")
    add_run.add_argument("--store", dest="store", action="store_true")
    add_run.add_argument("--no-store", dest="store", action="store_false")
    add_run.set_defaults(store=True)

    add_fact = sub.add_parser("add-fact")
    add_connection_args(add_fact)
    add_fact.add_argument("--key", required=True)
    add_fact.add_argument("--value", required=True)
    add_fact.add_argument("--tags-csv", default="")
    add_fact.add_argument("--scope", default="shared")
    add_fact.add_argument("--project-id", default="")
    add_fact.add_argument("--memory-scope", default="")
    add_fact.add_argument("--memory-class", default="semantic")
    add_fact.add_argument("--visibility", default="normal")
    add_fact.add_argument("--retention-days", type=int, default=0)
    add_fact.add_argument("--importance", type=float, default=0.5)
    add_fact.add_argument("--confidence", type=float, default=0.8)
    add_fact.add_argument("--trust-score", type=float, default=-1.0)
    add_fact.add_argument("--trust-dimensions-json", default="")
    add_fact.add_argument("--artifact-ref", default="")
    add_fact.add_argument("--derived-from-ids-json", default="")
    add_fact.add_argument("--supersedes-id", default="")
    add_fact.add_argument("--promotion-status", default="durable")
    add_fact.add_argument("--store", dest="store", action="store_true")
    add_fact.add_argument("--no-store", dest="store", action="store_false")
    add_fact.set_defaults(store=True)

    add_artifact = sub.add_parser("add-artifact")
    add_connection_args(add_artifact)
    add_artifact.add_argument("--title", required=True)
    add_artifact.add_argument("--content", default="")
    add_artifact.add_argument("--artifact-type", default="")
    add_artifact.add_argument("--artifact-ref", default="")
    add_artifact.add_argument("--tags-csv", default="")
    add_artifact.add_argument("--scope", default="shared")
    add_artifact.add_argument("--project-id", default="")
    add_artifact.add_argument("--memory-scope", default="")
    add_artifact.add_argument("--memory-class", default="episodic")
    add_artifact.add_argument("--promotion-status", default="candidate")
    add_artifact.add_argument("--importance", type=float, default=0.6)
    add_artifact.add_argument("--confidence", type=float, default=0.7)
    add_artifact.add_argument("--derived-from-ids-json", default="")
    add_artifact.add_argument("--supersedes-id", default="")
    add_artifact.add_argument("--store", dest="store", action="store_true")
    add_artifact.add_argument("--no-store", dest="store", action="store_false")
    add_artifact.set_defaults(store=True)

    search = sub.add_parser("search")
    add_connection_args(search)
    search.add_argument("--query", required=True)
    search.add_argument("--k", type=int, default=8)
    search.add_argument("--mode", choices=["vectors", "summaries", "context"], default="summaries")
    search.add_argument("--budget", default="small")
    search.add_argument("--include-items", action="store_true")
    search.add_argument("--project-id", default="")
    search.add_argument("--project-scope-mode", choices=["off", "prefer", "strict"], default="")
    search.add_argument("--intent", default="")
    search.add_argument("--preferred-memory-class", default="")

    export = sub.add_parser("export")
    add_connection_args(export)
    export.add_argument("--scope", default="all")
    export.add_argument("--limit", type=int, default=200)
    export.add_argument("--include-embeddings", action="store_true")

    project_upsert = sub.add_parser("project-upsert")
    add_connection_args(project_upsert)
    project_upsert.add_argument("--name", required=True)
    project_upsert.add_argument("--slug", default="")
    project_upsert.add_argument("--description", default="")
    project_upsert.add_argument("--repos-csv", default="")
    project_upsert.add_argument("--tags-csv", default="")
    project_upsert.add_argument("--status", default="active")

    project_get = sub.add_parser("project-get")
    add_connection_args(project_get)
    project_get.add_argument("--project-id", default="")
    project_get.add_argument("--slug", default="")

    project_list = sub.add_parser("project-list")
    add_connection_args(project_list)
    project_list.add_argument("--status", default="")
    project_list.add_argument("--limit", type=int, default=50)

    project_archive = sub.add_parser("project-archive")
    add_connection_args(project_archive)
    project_archive.add_argument("--project-id", required=True)

    stats = sub.add_parser("stats")
    add_connection_args(stats)
    stats.add_argument("--scope", default="all")
    stats.add_argument("--limit", type=int, default=500)

    retrieval_logs = sub.add_parser("retrieval-logs")
    add_connection_args(retrieval_logs)
    retrieval_logs.add_argument("--limit", type=int, default=200)
    retrieval_logs.add_argument("--since-hours", type=int, default=24)
    retrieval_logs.add_argument("--operation", default="")

    audit_logs = sub.add_parser("audit-logs")
    add_connection_args(audit_logs)
    audit_logs.add_argument("--limit", type=int, default=200)
    audit_logs.add_argument("--since-hours", type=int, default=24)
    audit_logs.add_argument("--operation", default="")

    rebuild = sub.add_parser("rebuild-embeddings")
    add_connection_args(rebuild)
    rebuild.add_argument("--scope", default="all")
    rebuild.add_argument("--limit", type=int, default=100)

    promote = sub.add_parser("promote")
    add_connection_args(promote)
    promote.add_argument("--scope", default="all")
    promote.add_argument("--limit", type=int, default=500)
    promote.add_argument("--dry-run", action="store_true")

    route_review = sub.add_parser("route-review")
    add_connection_args(route_review)
    route_review.add_argument("--task-type", default="general")
    route_review.add_argument("--risk-level", default="low")
    route_review.add_argument("--has-canonical-context", action="store_true")
    route_review.add_argument("--has-external-dependency", action="store_true")
    route_review.add_argument("--has-unresolved-disagreement", action="store_true")
    route_review.add_argument("--force-three-model-review", action="store_true")

    add_disagreement = sub.add_parser("add-disagreement")
    add_connection_args(add_disagreement)
    add_disagreement.add_argument("--claim", required=True)
    add_disagreement.add_argument("--scope", default="shared")
    add_disagreement.add_argument("--task-type", default="general")
    add_disagreement.add_argument("--risk-level", default="medium")
    add_disagreement.add_argument("--codex-position", default="")
    add_disagreement.add_argument("--claude-position", default="")
    add_disagreement.add_argument("--gemini-position", default="")
    add_disagreement.add_argument("--resolution", default="pending")
    add_disagreement.add_argument("--resolution-status", default="pending")
    add_disagreement.add_argument("--correct-model", default="")
    add_disagreement.add_argument("--outcome", default="pending")
    add_disagreement.add_argument("--evidence", default="")
    add_disagreement.add_argument("--tags-csv", default="")
    add_disagreement.add_argument("--project-id", default="")
    add_disagreement.add_argument("--memory-scope", default="workspace")
    add_disagreement.add_argument("--memory-class", default="episodic")
    add_disagreement.add_argument("--promotion-status", default="candidate")
    add_disagreement.add_argument("--importance", type=float, default=0.7)
    add_disagreement.add_argument("--confidence", type=float, default=0.6)
    add_disagreement.add_argument("--store", dest="store", action="store_true")
    add_disagreement.add_argument("--no-store", dest="store", action="store_false")
    add_disagreement.set_defaults(store=True)

    orchestrate_review = sub.add_parser("orchestrate-review")
    add_connection_args(orchestrate_review)
    orchestrate_review.add_argument("--task-title", required=True)
    orchestrate_review.add_argument("--task-type", default="general")
    orchestrate_review.add_argument("--risk-level", default="low")
    orchestrate_review.add_argument("--query", default="")
    orchestrate_review.add_argument("--project-id", default="")
    orchestrate_review.add_argument("--has-canonical-context", action="store_true")
    orchestrate_review.add_argument("--has-external-dependency", action="store_true")
    orchestrate_review.add_argument("--has-unresolved-disagreement", action="store_true")
    orchestrate_review.add_argument("--force-three-model-review", action="store_true")
    orchestrate_review.add_argument("--run-reviewers", action="store_true")
    orchestrate_review.add_argument("--claude-model", default="claude-sonnet-4-6")
    orchestrate_review.add_argument("--gemini-model", default="")

    raw = sub.add_parser("raw")
    add_connection_args(raw)
    raw.add_argument("--operation", required=True)
    raw.add_argument("--payload-json", required=True)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "configure":
        cfg = load_config_from_args(args)
        saved = config_dict(cfg)
        saved["claude_path"] = resolve_cached_cli_binary("claude", saved)
        saved["gemini_path"] = resolve_cached_cli_binary("gemini", saved)
        path = save_config(saved)
        print(
            json.dumps(
                {
                    "ok": True,
                    "configPath": str(path),
                    "workspaceId": cfg.workspace_id,
                    "repoId": cfg.repo_id,
                    "claudePath": saved.get("claude_path", ""),
                    "geminiPath": saved.get("gemini_path", ""),
                },
                indent=2,
            )
        )
        return
    if args.command == "login":
        cfg = load_config_from_args(args)
        result = post_operation(cfg, "memory_get_shared", {"limit": 1})
        saved = config_dict(cfg)
        saved["claude_path"] = resolve_cached_cli_binary("claude", saved)
        saved["gemini_path"] = resolve_cached_cli_binary("gemini", saved)
        path = save_config(saved)
        print(
            json.dumps(
                {
                    "ok": True,
                    "configPath": str(path),
                    "workspaceId": cfg.workspace_id,
                    "repoId": cfg.repo_id,
                    "claudePath": saved.get("claude_path", ""),
                    "geminiPath": saved.get("gemini_path", ""),
                    "validation": {"sharedCount": len(result.get("items", []))},
                },
                indent=2,
            )
        )
        return
    if args.command == "doctor":
        saved = load_saved_config()
        cfg = load_config_from_args(args)
        result = post_operation(cfg, "memory_get_shared", {"limit": 1})
        warnings = []
        if cfg.endpoint.strip().lower().endswith("example.invalid"):
            warnings.append("endpoint_looks_placeholder")
        print(
            json.dumps(
                {
                    "ok": True,
                    "configPath": str(config_path()),
                    "savedConfigKeys": sorted(saved.keys()),
                    "endpoint": cfg.endpoint,
                    "userId": cfg.user_id,
                    "workspaceId": cfg.workspace_id,
                    "repoId": cfg.repo_id,
                    "authMode": cfg.auth_mode,
                    "entraScope": cfg.entra_scope,
                    "claudePath": resolve_cached_cli_binary("claude", saved),
                    "geminiPath": resolve_cached_cli_binary("gemini", saved),
                    "validation": {"sharedCount": len(result.get("items", []))},
                    "warnings": warnings,
                },
                indent=2,
            )
        )
        return
    if args.command == "install-defaults":
        installed = install_memory_triad_defaults(force_skill=(not bool(args.no_force_skill)))
        print(
            json.dumps(
                {
                    "ok": True,
                    "installed": installed,
                    "notes": [
                        "Global AGENTS policy block installed/updated.",
                        "memory-triad-defaults skill installed/updated.",
                    ],
                },
                indent=2,
            )
        )
        return
    if args.command == "install-mcp":
        cfg = load_config_from_args(args)
        save_config(config_dict(cfg))
        path = install_codex_mcp_server(
            cfg,
            startup_timeout_sec=args.startup_timeout_sec,
            server_name=args.server_name,
            command=args.launcher_command,
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "configPath": str(config_path()),
                    "codexConfigPath": str(path),
                    "serverName": args.server_name,
                    "module": "ai_mem.mcp_server",
                },
                indent=2,
            )
        )
        return

    if args.command == "journal-list":
        items = list_recent_journal_entries(limit=int(args.limit))
        print(json.dumps({"ok": True, "count": len(items), "items": items}, indent=2))
        return

    cfg = load_config_from_args(args)

    if args.command == "get-shared":
        result = post_operation(cfg, "memory_get_shared", {"limit": int(args.limit)})
    elif args.command == "get-personal":
        result = post_operation(cfg, "memory_get_personal", {"limit": int(args.limit)})
    elif args.command == "add-run":
        request_text = (args.request or "").strip() or "(unspecified request)"
        summary_text = (args.summary or "").strip() or "(unspecified summary)"
        context = infer_repo_context(args.cwd or os.getcwd())
        effective_cwd = context["cwd"]
        effective_branch = args.branch or context["branch"] or "unknown"
        effective_project_id = args.project_id or infer_project_id(cfg, effective_cwd, context.get("repo_root", ""))
        effective_repo_id = context.get("repo_name", "") or cfg.repo_id
        payload = {
            "kind": "run_summary",
            "branch": effective_branch,
            "cwd": effective_cwd,
            "repoRoot": context.get("repo_root", ""),
            "repoId": effective_repo_id,
            "request": request_text,
            "summary": summary_text,
            "status": args.status,
            "tags": ["run", args.status],
            "source": "cli",
            "actor": cfg.user_id,
            "autoExtract": bool(args.auto_extract),
            "extractScope": args.extract_scope,
            "sourceMeta": {
                "workspaceId": cfg.workspace_id,
                "repoId": cfg.repo_id,
                "inferredProjectId": effective_project_id,
                "repoRoot": context.get("repo_root", ""),
                "repoName": context.get("repo_name", ""),
            },
        }
        non_trivial = is_non_trivial_run(request_text, summary_text, args.status)
        journal_entry = None
        if non_trivial:
            journal_entry = append_journal_entry(
                workspace_id=cfg.workspace_id,
                project_id=effective_project_id,
                repo_id=effective_repo_id,
                cwd=effective_cwd,
                repo_root=context.get("repo_root", ""),
                branch=effective_branch,
                request_summary=request_text,
                action_summary=summary_text,
                outcome=args.status,
                source="cli",
                status=args.status,
                source_meta={
                    "memoryScope": args.memory_scope,
                    "memoryClass": args.memory_class,
                    "visibility": args.visibility,
                    "retentionDays": int(args.retention_days),
                },
            )
        write_payload = add_common_write_metadata(
            payload,
            project_id=effective_project_id,
            memory_scope=args.memory_scope,
            memory_class=args.memory_class,
            visibility=args.visibility,
            retention_days=args.retention_days,
            importance=args.importance,
            confidence=args.confidence,
            trust_score=args.trust_score,
            trust_dimensions_json=args.trust_dimensions_json,
            artifact_ref=args.artifact_ref,
            derived_from_ids_json=args.derived_from_ids_json,
            supersedes_id=args.supersedes_id,
            promotion_status=args.promotion_status,
            store=args.store,
        )
        try:
            result = post_operation(cfg, "memory_add_run", write_payload)
            promoted_id = extract_promoted_memory_id(result)
            if journal_entry and promoted_id:
                append_journal_link(
                    parent_journal_id=journal_entry["journalId"],
                    promoted_memory_id=promoted_id,
                    source="cli",
                )
            if journal_entry:
                result["journal"] = {
                    "recorded": True,
                    "journalId": journal_entry.get("journalId", ""),
                    "path": "local",
                }
        except Exception as exc:
            if journal_entry:
                result = {
                    "ok": False,
                    "error": str(exc),
                    "journal": {
                        "recorded": True,
                        "journalId": journal_entry.get("journalId", ""),
                        "path": "local",
                    },
                    "remoteWrite": {"attempted": True, "ok": False},
                }
            else:
                raise
    elif args.command == "add-fact":
        tags = [t.strip() for t in (args.tags_csv or "").split(",") if t.strip()]
        payload = {
            "id": f"fact:{cfg.workspace_id}:{args.key}",
            "scope": args.scope,
            "kind": "fact",
            "content": args.value,
            "tags": tags,
            "source": "cli",
            "sourceMeta": {"key": args.key},
            "actor": cfg.user_id,
        }
        result = post_operation(
            cfg,
            "memory_add_fact",
            add_common_write_metadata(
                payload,
                project_id=args.project_id,
                memory_scope=args.memory_scope,
                memory_class=args.memory_class,
                visibility=args.visibility,
                retention_days=args.retention_days,
                importance=args.importance,
                confidence=args.confidence,
                trust_score=args.trust_score,
                trust_dimensions_json=args.trust_dimensions_json,
                artifact_ref=args.artifact_ref,
                derived_from_ids_json=args.derived_from_ids_json,
                supersedes_id=args.supersedes_id,
                promotion_status=args.promotion_status,
                store=args.store,
            ),
        )
    elif args.command == "add-artifact":
        tags = [t.strip() for t in (args.tags_csv or "").split(",") if t.strip()]
        payload = {
            "scope": args.scope,
            "kind": "artifact",
            "title": args.title,
            "content": args.content,
            "artifactType": args.artifact_type,
            "artifactRef": args.artifact_ref,
            "tags": tags,
            "source": "cli",
            "actor": cfg.user_id,
        }
        result = post_operation(
            cfg,
            "memory_add_artifact",
            add_common_write_metadata(
                payload,
                project_id=args.project_id,
                memory_scope=args.memory_scope,
                memory_class=args.memory_class,
                visibility="normal",
                retention_days=0,
                importance=args.importance,
                confidence=args.confidence,
                trust_score=-1.0,
                trust_dimensions_json="",
                artifact_ref=args.artifact_ref,
                derived_from_ids_json=args.derived_from_ids_json,
                supersedes_id=args.supersedes_id,
                promotion_status=args.promotion_status,
                store=args.store,
            ),
        )
    elif args.command == "search":
        search_payload = {
            "query": args.query,
            "k": int(args.k),
            "projectId": args.project_id,
            "projectScopeMode": args.project_scope_mode,
            "intent": args.intent,
            "preferredMemoryClass": args.preferred_memory_class,
        }
        if args.mode == "vectors":
            result = post_operation(cfg, "memory_search_vectors", search_payload)
        elif args.mode == "context":
            result = post_operation(
                cfg,
                "memory_build_context",
                {**search_payload, "budget": args.budget, "includeItems": bool(args.include_items)},
            )
        else:
            result = post_operation(cfg, "memory_search_summaries", search_payload)
    elif args.command == "export":
        result = post_operation(
            cfg,
            "memory_export",
            {"scope": args.scope, "limit": int(args.limit), "includeEmbeddings": bool(args.include_embeddings)},
        )
    elif args.command == "project-upsert":
        repos = [value.strip() for value in (args.repos_csv or "").split(",") if value.strip()]
        tags = [value.strip() for value in (args.tags_csv or "").split(",") if value.strip()]
        result = post_operation(
            cfg,
            "project_upsert",
            {
                "name": args.name,
                "slug": args.slug,
                "description": args.description,
                "repos": repos,
                "tags": tags,
                "status": args.status,
            },
        )
    elif args.command == "project-get":
        result = post_operation(cfg, "project_get", {"projectId": args.project_id, "slug": args.slug})
    elif args.command == "project-list":
        result = post_operation(cfg, "project_list", {"status": args.status, "limit": int(args.limit)})
    elif args.command == "project-archive":
        result = post_operation(cfg, "project_archive", {"projectId": args.project_id})
    elif args.command == "stats":
        result = post_operation(cfg, "memory_get_stats", {"scope": args.scope, "limit": int(args.limit)})
    elif args.command == "retrieval-logs":
        result = post_operation(
            cfg,
            "memory_get_retrieval_logs",
            {"limit": int(args.limit), "sinceHours": int(args.since_hours), "operation": args.operation},
        )
    elif args.command == "audit-logs":
        result = post_operation(
            cfg,
            "memory_get_audit_events",
            {"limit": int(args.limit), "sinceHours": int(args.since_hours), "operation": args.operation},
        )
    elif args.command == "rebuild-embeddings":
        result = post_operation(
            cfg,
            "memory_rebuild_embeddings",
            {"scope": args.scope, "limit": int(args.limit)},
        )
    elif args.command == "promote":
        result = post_operation(
            cfg,
            "memory_auto_promote",
            {"scope": args.scope, "limit": int(args.limit), "dryRun": bool(args.dry_run)},
        )
    elif args.command == "route-review":
        result = post_operation(
            cfg,
            "memory_route_review",
            {
                "taskType": args.task_type,
                "riskLevel": args.risk_level,
                "hasCanonicalContext": bool(args.has_canonical_context),
                "hasExternalDependency": bool(args.has_external_dependency),
                "hasUnresolvedDisagreement": bool(args.has_unresolved_disagreement),
                "forceThreeModelReview": bool(args.force_three_model_review),
            },
        )
    elif args.command == "add-disagreement":
        tags = [t.strip() for t in (args.tags_csv or "").split(",") if t.strip()]
        payload = {
            "scope": args.scope,
            "kind": "disagreement",
            "claim": args.claim,
            "taskType": args.task_type,
            "riskLevel": args.risk_level,
            "codexPosition": args.codex_position,
            "claudePosition": args.claude_position,
            "geminiPosition": args.gemini_position,
            "resolution": args.resolution,
            "resolutionStatus": args.resolution_status,
            "correctModel": args.correct_model,
            "outcome": args.outcome,
            "evidence": args.evidence,
            "tags": tags,
            "source": "cli",
            "actor": cfg.user_id,
        }
        result = post_operation(
            cfg,
            "memory_add_disagreement",
            add_common_write_metadata(
                payload,
                project_id=args.project_id,
                memory_scope=args.memory_scope,
                memory_class=args.memory_class,
                visibility="normal",
                retention_days=0,
                importance=args.importance,
                confidence=args.confidence,
                trust_score=-1.0,
                trust_dimensions_json="",
                artifact_ref="",
                derived_from_ids_json="",
                supersedes_id="",
                promotion_status=args.promotion_status,
                store=args.store,
            ),
        )
    elif args.command == "orchestrate-review":
        route = post_operation(
            cfg,
            "memory_route_review",
            {
                "taskType": args.task_type,
                "riskLevel": args.risk_level,
                "hasCanonicalContext": bool(args.has_canonical_context),
                "hasExternalDependency": bool(args.has_external_dependency),
                "hasUnresolvedDisagreement": bool(args.has_unresolved_disagreement),
                "forceThreeModelReview": bool(args.force_three_model_review),
            },
        )
        memory_context = {}
        if args.query:
            memory_context = post_operation(
                cfg,
                "memory_build_context",
                {
                    "query": args.query,
                    "k": 8,
                    "budget": "small",
                    "projectId": args.project_id,
                },
            )
        result = {
            "ok": True,
            "taskTitle": args.task_title,
            "taskType": args.task_type,
            "riskLevel": args.risk_level,
            "route": route,
            "memoryContext": {
                "itemCount": int(memory_context.get("itemCount", 0) or 0) if isinstance(memory_context, dict) else 0,
                "context": memory_context.get("context", "") if isinstance(memory_context, dict) else "",
            },
            "reviewersRan": [],
            "reviewerOutputs": [],
        }
        if args.run_reviewers:
            context_text = result["memoryContext"]["context"]
            reviewer_prompt = (
                f"Task: {args.task_title}\n"
                f"TaskType: {args.task_type}\n"
                f"RiskLevel: {args.risk_level}\n"
                "Return JSON with keys: claim, verdict (PASS|REVISE|BLOCK), evidence, risk.\n"
                "Memory context:\n"
                f"{context_text}\n"
            )
            for reviewer in route.get("reviewers", []):
                if reviewer == "claude":
                    claude_bin = resolve_cached_cli_binary("claude")
                    if not claude_bin:
                        result["reviewerOutputs"].append({"reviewer": "claude", "ok": False, "error": "claude_not_found"})
                        continue
                    cmd = [claude_bin, "--model", args.claude_model, "--dangerously-skip-permissions", "-p", reviewer_prompt]
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
                    result["reviewersRan"].append("claude")
                    result["reviewerOutputs"].append(
                        {
                            "reviewer": "claude",
                            "ok": proc.returncode == 0,
                            "stdout": (proc.stdout or "").strip(),
                            "stderr": (proc.stderr or "").strip(),
                            "returnCode": proc.returncode,
                        }
                    )
                elif reviewer == "gemini":
                    gemini_bin = resolve_cached_cli_binary("gemini")
                    if not gemini_bin:
                        result["reviewerOutputs"].append({"reviewer": "gemini", "ok": False, "error": "gemini_not_found"})
                        continue
                    cmd = [gemini_bin]
                    if args.gemini_model:
                        cmd.extend(["-m", args.gemini_model])
                    cmd.extend(["-p", reviewer_prompt, "-o", "text"])
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
                    result["reviewersRan"].append("gemini")
                    result["reviewerOutputs"].append(
                        {
                            "reviewer": "gemini",
                            "ok": proc.returncode == 0,
                            "stdout": (proc.stdout or "").strip(),
                            "stderr": (proc.stderr or "").strip(),
                            "returnCode": proc.returncode,
                        }
                    )
    else:
        result = post_operation(cfg, args.operation, parse_json_arg(args.payload_json))

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
