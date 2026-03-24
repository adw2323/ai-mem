from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import subprocess
import shutil
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


@dataclass
class ClientConfig:
    endpoint: str
    function_key: str = ""
    shared_secret: str = ""
    user_id: str = ""
    workspace_id: str = ""
    repo_id: str = ""
    auth_mode: str = "dual"
    entra_scope: str = ""


CONFIG_ENV_VAR = "AI_MEM_CONFIG"
PACKAGE_SOURCE_ENV_VAR = "AI_MEM_CLIENT_PACKAGE"
CODEX_CONFIG_ENV_VAR = "CODEX_CONFIG_PATH"
CODEX_HOME_ENV_VAR = "CODEX_HOME"

GLOBAL_POLICY_BEGIN = "<!-- AI_MEM_MEMORY_TRIAD_POLICY_BEGIN -->"
GLOBAL_POLICY_END = "<!-- AI_MEM_MEMORY_TRIAD_POLICY_END -->"
CLAUDE_DEFAULT_PATH = str((Path.home() / "AppData" / "Roaming" / "npm" / "claude.cmd").resolve())
GEMINI_DEFAULT_PATH = str((Path.home() / "AppData" / "Roaming" / "npm" / "gemini.cmd").resolve())

GLOBAL_POLICY_BLOCK = textwrap.dedent(
    f"""
    {GLOBAL_POLICY_BEGIN}
    ## Memory Triad Default Workflow
    Default operating order:
    1. memory-first context pull
    2. targeted file reads for exact code truth
    3. optional Claude/Gemini review based on risk
    4. execute and validate

    Memory-first rules:
    - Query ai-mem first before broad repo search.
    - Use memory to route likely files/subsystems.
    - If memory conflicts with files, trust files for current truth.
    - If memory retrieval is weak/empty/ambiguous, DO NOT stop; continue to targeted file search (for example `rg`) and direct file reads.
    - Memory-first must never become memory-only.

    Standing resources:
    - Codex: orchestrator/executor.
    - Claude: architecture/deep-reasoning reviewer.
    - Gemini: independent skeptical checker.
    - ai-mem: prior decisions/failures/constraints source.

    Local invocation baselines (Windows):
    - Claude: `claude -p "<prompt>"` (or cached absolute path if configured)
    - Gemini: `gemini -p "<prompt>" -o text` (or cached absolute path if configured)
    - Known local paths:
      - Claude: `{CLAUDE_DEFAULT_PATH}`
      - Gemini: `{GEMINI_DEFAULT_PATH}`
    - ai-mem CLI fallback: `python -m ai_mem.cli <command>`
    - Do not repeatedly re-discover Claude/Gemini executables if known paths are already configured.

    Delegation policy:
    - Use Claude for architecture/tradeoff ambiguity.
    - Use Gemini for independent validation/challenge.
    - Use both for high-risk infra/security/compliance/API/data-model changes.
    - Avoid both for routine low-risk edits.

    Fallback policy:
    - If ai-mem MCP unavailable, use ai-mem CLI.
    - If memory route is stale/mismatched, run targeted `rg` fallback.
    - Every non-trivial run must persist a baseline run memory record with at least cwd, inferred repo_root (if available), inferred project identity, request, summary, and status.
    - Baseline writes should be journaled locally first when available, then promoted to memory.
    {GLOBAL_POLICY_END}
    """
).strip()

SKILL_CONTENT = textwrap.dedent(
    f"""
    ---
    name: memory-triad-defaults
    description: Enforce memory-first startup and risk-based Claude/Gemini review defaults.
    metadata:
      short-description: Memory-first startup and tri-model review defaults
    ---

    # Memory Triad Defaults

    ## Startup Checklist
    1. Query memory first:
       - `memory_get_personal`
       - `memory_get_shared`
       - `memory_list_open_tasks`
       - `memory_build_context` (`budget=small`)
    2. Use memory to select targeted files.
    3. Read files for exact current truth.
    4. If memory is insufficient, immediately run targeted `rg` + file reads (memory-first, not memory-only).
    5. Use Claude/Gemini based on risk.
    6. Execute + validate.
    7. Persist outcomes to memory (baseline run record required for non-trivial work).
    8. Prefer local journal-first baseline logging, then link promoted memory IDs when available.

    ## Invocation Recipes (Windows)
    - Claude: `claude -p "<prompt>"` (or cached absolute path)
    - Gemini: `gemini -p "<prompt>" -o text` (or cached absolute path)
    - Known local paths:
      - Claude: `{CLAUDE_DEFAULT_PATH}`
      - Gemini: `{GEMINI_DEFAULT_PATH}`
    - ai-mem fallback: `python -m ai_mem.cli search --query "<query>"`

    ## Conflict Rules
    - Memory vs files: trust files.
    - Reviewer disagreement: report explicitly.
    - If memory route fails, use targeted `rg` fallback.
    """
).strip() + "\n"


def config_path() -> Path:
    configured = os.environ.get(CONFIG_ENV_VAR, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".ai-mem" / "config.json"


def codex_config_path() -> Path:
    configured = os.environ.get(CODEX_CONFIG_ENV_VAR, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex" / "config.toml"


def codex_home_path() -> Path:
    configured = os.environ.get(CODEX_HOME_ENV_VAR, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"


def codex_agents_path() -> Path:
    return codex_home_path() / "AGENTS.md"


def _upsert_managed_block(existing: str, block: str, begin: str, end: str) -> str:
    pattern = rf"(?s){re.escape(begin)}.*?{re.escape(end)}"
    if re.search(pattern, existing):
        replaced = re.sub(pattern, lambda _m: block, existing).rstrip()
        return f"{replaced}\n"
    cleaned = existing.rstrip()
    if not cleaned:
        return f"# Global Codex Operating Policy\n\n{block}\n"
    return f"{cleaned}\n\n{block}\n"


def install_memory_triad_defaults(*, force_skill: bool = True) -> dict[str, str]:
    codex_home = codex_home_path()
    codex_home.mkdir(parents=True, exist_ok=True)

    agents_path = codex_agents_path()
    existing_agents = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
    updated_agents = _upsert_managed_block(
        existing_agents,
        GLOBAL_POLICY_BLOCK,
        GLOBAL_POLICY_BEGIN,
        GLOBAL_POLICY_END,
    )
    agents_path.write_text(updated_agents, encoding="utf-8")

    skill_dir = codex_home / "skills" / "memory-triad-defaults"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    if force_skill or (not skill_path.exists()):
        skill_path.write_text(SKILL_CONTENT, encoding="utf-8")

    return {
        "codexHome": str(codex_home),
        "agentsPath": str(agents_path),
        "skillPath": str(skill_path),
    }


def load_saved_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_config(data: dict[str, Any]) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def install_codex_mcp_server(
    cfg: ClientConfig,
    *,
    startup_timeout_sec: int = 60,
    server_name: str = "ai-mem-mcp",
    command: str = "python",
) -> Path:
    path = codex_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    pattern = rf"(?ms)^\[mcp_servers\.{re.escape(server_name)}\]\s*.*?(?=^\[|\Z)"
    cleaned = re.sub(pattern, "", existing).rstrip()
    block = (
        f"[mcp_servers.{server_name}]\n"
        f'command = "{command}"\n'
        f"args = ['-m', 'ai_mem.mcp_server', '--endpoint', '{cfg.endpoint}', '--function-key', '{cfg.function_key}', '--shared-secret', '{cfg.shared_secret}', '--auth-mode', '{cfg.auth_mode}', '--entra-scope', '{cfg.entra_scope}', '--user-id', '{cfg.user_id}', '--workspace-id', '{cfg.workspace_id}', '--repo-id', '{cfg.repo_id}']\n"
        f"startup_timeout_sec = {int(startup_timeout_sec)}\n"
    )
    updated = f"{cleaned}\n\n{block}\n" if cleaned else f"{block}\n"
    path.write_text(updated, encoding="utf-8")
    return path


def effective_package_source(local_fallback: str) -> str:
    return os.environ.get(PACKAGE_SOURCE_ENV_VAR, "").strip() or local_fallback


def resolve_cli_value(args: argparse.Namespace, name: str, saved: dict[str, Any], default: str = "") -> str:
    value = getattr(args, name, None)
    if value not in (None, ""):
        return str(value)
    env_name = f"AI_MEM_{name.upper()}"
    env_value = os.environ.get(env_name, "").strip()
    if env_value:
        return env_value
    saved_value = saved.get(name)
    if saved_value not in (None, ""):
        return str(saved_value)
    return default


def load_config_from_args(args: argparse.Namespace, *, require_auth: bool = True) -> ClientConfig:
    saved = load_saved_config()
    repo_default = saved.get("repo_id") or ""
    auth_mode = resolve_cli_value(args, "auth_mode", saved, "dual").strip().lower() or "dual"
    if auth_mode not in {"shared_secret", "entra", "dual", "off"}:
        raise ValueError("auth_mode must be one of: shared_secret, entra, dual, off")
    entra_scope = resolve_cli_value(args, "entra_scope", saved)
    endpoint = resolve_cli_value(args, "endpoint", saved)
    function_key = resolve_cli_value(args, "function_key", saved)
    shared_secret = resolve_cli_value(args, "shared_secret", saved)
    user_id = resolve_cli_value(args, "user_id", saved)
    workspace_id = resolve_cli_value(args, "workspace_id", saved)
    repo_id = resolve_cli_value(args, "repo_id", saved, str(repo_default))
    missing = []
    for field_name, value in {
        "endpoint": endpoint,
        "user_id": user_id,
        "workspace_id": workspace_id,
    }.items():
        if not value:
            missing.append(field_name)
    if require_auth:
        if auth_mode == "shared_secret":
            if not shared_secret:
                missing.append("shared_secret")
        elif auth_mode == "entra":
            if not entra_scope:
                missing.append("entra_scope")
        elif auth_mode == "dual":
            if (not shared_secret) and (not entra_scope):
                missing.append("shared_secret_or_entra_scope")
    if missing:
        raise ValueError(f"missing required config values: {', '.join(sorted(missing))}")
    return ClientConfig(
        endpoint=endpoint,
        function_key=function_key,
        shared_secret=shared_secret,
        user_id=user_id,
        workspace_id=workspace_id,
        repo_id=repo_id,
        auth_mode=auth_mode,
        entra_scope=entra_scope,
    )


def build_signed_headers(cfg: ClientConfig, payload: dict[str, Any]) -> dict[str, str]:
    timestamp = datetime.now(timezone.utc).isoformat()
    nonce = hashlib.sha256(f"{timestamp}|{time.time_ns()}|{os.getpid()}".encode("utf-8")).hexdigest()[:24]
    signed = "|".join(
        [
            str(payload.get("userId") or ""),
            str(payload.get("workspaceId") or ""),
            str(payload.get("repoId") or ""),
            timestamp,
            nonce,
        ]
    )
    signature = hmac.new(cfg.shared_secret.encode("utf-8"), signed.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {"x-codex-context-timestamp": timestamp, "x-codex-context-signature": signature, "x-codex-context-nonce": nonce}
    if cfg.function_key:
        headers["x-functions-key"] = cfg.function_key
    return headers


_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_REPO_CONTEXT_CACHE: dict[str, dict[str, str]] = {}
_CLI_PATH_CACHE: dict[str, str] = {}


def get_entra_token(cfg: ClientConfig) -> str:
    scope = str(cfg.entra_scope or "").strip()
    if not scope:
        return ""
    now = time.time()
    cached = _TOKEN_CACHE.get(scope)
    if cached and cached[1] > (now + 120):
        return cached[0]
    cmd = ["az", "account", "get-access-token", "--scope", scope, "--query", "accessToken", "-o", "tsv"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    if proc.returncode != 0:
        raise RuntimeError(f"failed to acquire Entra token for scope '{scope}': {(proc.stderr or '').strip()}")
    token = (proc.stdout or "").strip()
    if not token:
        raise RuntimeError(f"failed to acquire Entra token for scope '{scope}': empty token")
    _TOKEN_CACHE[scope] = (token, now + 3000)
    return token


def infer_repo_context(cwd: str) -> dict[str, str]:
    cwd_abs = str(Path(cwd or os.getcwd()).expanduser().resolve())
    cached = _REPO_CONTEXT_CACHE.get(cwd_abs)
    if cached:
        return dict(cached)

    repo_root = ""
    branch = ""
    try:
        proc_root = subprocess.run(
            ["git", "-C", cwd_abs, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if proc_root.returncode == 0:
            repo_root = (proc_root.stdout or "").strip()
    except Exception:
        repo_root = ""

    target = repo_root or cwd_abs
    try:
        proc_branch = subprocess.run(
            ["git", "-C", target, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if proc_branch.returncode == 0:
            branch = (proc_branch.stdout or "").strip()
    except Exception:
        branch = ""

    repo_name = Path(repo_root).name if repo_root else Path(cwd_abs).name
    context = {
        "cwd": cwd_abs,
        "repo_root": repo_root,
        "branch": branch,
        "repo_name": repo_name,
    }
    _REPO_CONTEXT_CACHE[cwd_abs] = dict(context)
    return context


def infer_project_id(cfg: ClientConfig, cwd: str, repo_root: str = "") -> str:
    workspace = (cfg.workspace_id or "workspace").strip().lower()
    basis = Path(repo_root or cwd or os.getcwd()).name.strip().lower()
    if not basis:
        basis = (cfg.repo_id or "project").strip().lower() or "project"
    slug = re.sub(r"[^a-z0-9._-]+", "-", basis).strip("-")
    slug = slug or "project"
    return f"proj:{workspace}:{slug}"


def resolve_cached_cli_binary(name: str, saved: dict[str, Any] | None = None) -> str:
    cache_key = str(name).strip().lower()
    if cache_key in _CLI_PATH_CACHE:
        return _CLI_PATH_CACHE[cache_key]

    env_name = f"AI_MEM_{cache_key.upper()}_PATH"
    env_path = os.environ.get(env_name, "").strip()
    if env_path and Path(env_path).exists():
        _CLI_PATH_CACHE[cache_key] = env_path
        return env_path

    saved_cfg = saved if isinstance(saved, dict) else load_saved_config()
    saved_key = f"{cache_key}_path"
    saved_path = str(saved_cfg.get(saved_key) or "").strip()
    if saved_path and Path(saved_path).exists():
        _CLI_PATH_CACHE[cache_key] = saved_path
        return saved_path

    candidates = [cache_key]
    if os.name == "nt":
        candidates = [f"{cache_key}.cmd", f"{cache_key}.exe", cache_key]
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            _CLI_PATH_CACHE[cache_key] = found
            if isinstance(saved_cfg, dict):
                saved_cfg[saved_key] = found
                try:
                    save_config(saved_cfg)
                except Exception:
                    pass
            return found

    return ""


def build_auth_headers(cfg: ClientConfig, payload: dict[str, Any]) -> dict[str, str]:
    mode = str(cfg.auth_mode or "dual").strip().lower()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if cfg.function_key:
        headers["x-functions-key"] = cfg.function_key
    if mode in {"shared_secret", "dual"} and cfg.shared_secret:
        headers.update(build_signed_headers(cfg, payload))
    if mode in {"entra", "dual"} and cfg.entra_scope:
        headers["Authorization"] = f"Bearer {get_entra_token(cfg)}"
    return headers


import random
import uuid

def robust_post(cfg: ClientConfig, op: str, payload: dict[str, Any], *, max_retries: int = 3, timeout: int = 10) -> dict[str, Any]:
    base = cfg.endpoint.rstrip("/")
    url = f"{base}/api/memory/{op}"
    full = {
        "userId": cfg.user_id,
        "workspaceId": cfg.workspace_id,
        "requestId": payload.get("requestId") or str(uuid.uuid4()),
        **payload,
    }
    if ("repoId" not in full) and cfg.repo_id:
        full["repoId"] = cfg.repo_id

    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            headers = build_auth_headers(cfg, full)
            rsp = requests.post(url, headers=headers, json=full, timeout=timeout)

            if rsp.status_code == 400 and "project not found" in (rsp.text or "").lower() and "projectId" in full and op.startswith("memory_add_"):
                fallback = dict(full)
                missing_project = str(fallback.pop("projectId", "") or "").strip()
                if missing_project:
                    source_meta = dict(fallback.get("sourceMeta") or {})
                    source_meta.setdefault("inferredProjectId", missing_project)
                    source_meta.setdefault("projectFallbackUsed", True)
                    fallback["sourceMeta"] = source_meta
                headers = build_auth_headers(cfg, fallback)
                rsp = requests.post(url, headers=headers, json=fallback, timeout=timeout)

            rsp.raise_for_status()
            return rsp.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.HTTPError) as exc:
            last_exc = exc
            is_transient = isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)) or (hasattr(exc, 'response') and exc.response is not None and exc.response.status_code >= 500)
            if not is_transient or attempt >= max_retries:
                break

            sleep_time = (2 ** attempt) + (random.uniform(0, 1))
            time.sleep(sleep_time)

    if isinstance(last_exc, requests.exceptions.Timeout) and op.startswith("memory_add_"):
        try:
            # Verify-on-timeout: check if it was actually written
            search_payload = {
                "userId": cfg.user_id,
                "workspaceId": cfg.workspace_id,
                "requestId": full["requestId"],
                "limit": 1
            }
            # We don't have a direct "get by requestId" but we can check audit logs or recent items
            # For simplicity and per requirement, we try to see if it exists in personal/shared
            for verify_op in ["memory_get_personal", "memory_get_shared"]:
                try:
                    v_rsp = requests.post(f"{base}/api/memory/{verify_op}", headers=build_auth_headers(cfg, search_payload), json=search_payload, timeout=5)
                    if v_rsp.status_code == 200:
                        items = v_rsp.json().get("items") or []
                        for item in items:
                            if item.get("requestId") == full["requestId"]:
                                return {**item, "ok": True, "verifiedAfterTimeout": True}
                except Exception:
                    continue
        except Exception:
            pass

    if last_exc:
        raise last_exc
    raise RuntimeError("robust_post failed without exception")

def post_operation(cfg: ClientConfig, op: str, payload: dict[str, Any]) -> dict[str, Any]:
    if op.startswith("memory_add_") or op == "project_upsert":
        return robust_post(cfg, op, payload)

    base = cfg.endpoint.rstrip("/")
    url = f"{base}/api/memory/{op}"
    full = {
        "userId": cfg.user_id,
        "workspaceId": cfg.workspace_id,
        **payload,
    }
    if ("repoId" not in full) and cfg.repo_id:
        full["repoId"] = cfg.repo_id
    headers = build_auth_headers(cfg, full)
    rsp = requests.post(url, headers=headers, json=full, timeout=45)
    rsp.raise_for_status()
    return rsp.json()


def parse_json_arg(value: str) -> Any:
    raw = str(value or "").strip()
    if not raw:
        return None
    return json.loads(raw)


def config_dict(cfg: ClientConfig) -> dict[str, str]:
    return {
        "endpoint": cfg.endpoint,
        "function_key": cfg.function_key,
        "shared_secret": cfg.shared_secret,
        "user_id": cfg.user_id,
        "workspace_id": cfg.workspace_id,
        "repo_id": cfg.repo_id,
        "auth_mode": cfg.auth_mode,
        "entra_scope": cfg.entra_scope,
    }


def add_common_write_metadata(
    payload: dict[str, Any],
    *,
    project_id: str = "",
    memory_scope: str = "",
    memory_class: str = "",
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
    payload["visibility"] = visibility
    payload["importance"] = float(importance)
    payload["confidence"] = float(confidence)
    payload["promotionStatus"] = promotion_status
    payload["store"] = bool(store)
    if project_id:
        payload["projectId"] = project_id
    if memory_scope:
        payload["memoryScope"] = memory_scope
    if memory_class:
        payload["memoryClass"] = memory_class
    if retention_days > 0:
        payload["retentionDays"] = int(retention_days)
    if trust_score >= 0:
        payload["trustScore"] = float(trust_score)
    trust_dimensions = parse_json_arg(trust_dimensions_json)
    if trust_dimensions is not None:
        payload["trustDimensions"] = trust_dimensions
    derived_from_ids = parse_json_arg(derived_from_ids_json)
    if derived_from_ids is not None:
        payload["derivedFromIds"] = derived_from_ids
    if artifact_ref:
        payload["artifactRef"] = artifact_ref
    if supersedes_id:
        payload["supersedesId"] = supersedes_id
    return payload
