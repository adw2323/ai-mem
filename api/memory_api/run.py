import datetime as dt
import base64
import hashlib
import hmac
import json
import math
import os
import re
import time
import uuid
from collections import Counter
from typing import Any

import azure.functions as func
import requests
from azure.cosmos import CosmosClient, exceptions
from azure.identity import DefaultAzureCredential
from azure.storage.queue import QueueClient, TextBase64EncodePolicy
from azure.core.exceptions import ResourceExistsError


SECRET_RE = re.compile(r"(?i)\b([a-z0-9_]*(?:key|token|secret|password))\s*[:=]\s*([^\s]+)")
TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]+")
ISO_DATE_RE = re.compile(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b")
DEFAULT_IMPORTANCE = 0.5
DEFAULT_CONFIDENCE = 0.8
NOISE_TOKENS = {
    "a",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "by",
    "can",
    "for",
    "from",
    "how",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "today",
    "was",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
}
RECENCY_QUERY_TERMS = {
    "today",
    "current",
    "currently",
    "now",
    "latest",
    "newest",
    "most recent",
    "as of",
}
CHANNEL_SPECIFIC_TERMS = {
    "push",
    "email",
    "sms",
    "opsgenie",
    "pagerduty",
    "slack",
    "teams",
    "webhook",
}
FAILURE_RECENCY_DAYS_DEFAULT = 7.0
MIN_TRUST_SCORE_DEFAULT = 0.3
AUTH_MODE_VALUES = {"shared_secret", "entra", "dual", "off"}
SIGNED_NONCE_TTL_SECONDS = 600
SIGNED_NONCE_CACHE_LIMIT = 5000
SIGNED_NONCE_CACHE: dict[str, float] = {}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def redact_text(value: str) -> str:
    if not value:
        return value
    return SECRET_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", value)


def split_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def stable_hash_embedding(text: str, dims: int = 256) -> list[float]:
    vec = [0.0] * dims
    toks = TOKEN_RE.findall(text.lower())
    if not toks:
        return vec
    for tok in toks:
        digest = hashlib.sha256(tok.encode("utf-8", errors="replace")).digest()
        idx = int.from_bytes(digest[:4], "little") % dims
        sign = -1.0 if (digest[4] & 1) else 1.0
        mag = 1.0 + (digest[5] / 255.0)
        vec[idx] += sign * mag
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    aa = a[:n]
    bb = b[:n]
    ab = sum(x * y for x, y in zip(aa, bb))
    na = math.sqrt(sum(x * x for x in aa))
    nb = math.sqrt(sum(y * y for y in bb))
    if na == 0 or nb == 0:
        return 0.0
    return ab / (na * nb)


def compact_text(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)].rstrip() + "..."


def keyword_tokens(value: Any) -> set[str]:
    tokens: set[str] = set()
    for token in TOKEN_RE.findall(str(value or "").lower()):
        cleaned = token.strip().lower()
        if not cleaned:
            continue
        if cleaned in NOISE_TOKENS:
            continue
        if cleaned.isdigit():
            continue
        if len(cleaned) <= 2:
            continue
        tokens.add(cleaned)
    return tokens


def keyword_overlap(query_text: str, candidate_text: str) -> float:
    query_tokens = keyword_tokens(query_text)
    candidate_tokens = keyword_tokens(candidate_text)
    if not query_tokens or not candidate_tokens:
        return 0.0
    return len(query_tokens & candidate_tokens) / len(query_tokens)


def env_float(name: str, default: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(str(raw).strip())
    except ValueError:
        return default
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


SUMMARY_GATE_MIN_SCORE = env_float("AI_MEM_GATE_MIN_SCORE", 0.32, 0.0, 1.5)
SUMMARY_GATE_MIN_VECTOR = env_float("AI_MEM_GATE_MIN_VECTOR_SCORE", 0.34, 0.0, 1.0)
SUMMARY_GATE_MIN_LEXICAL = env_float("AI_MEM_GATE_MIN_LEXICAL_SCORE", 0.12, 0.0, 1.0)
SUMMARY_GATE_CHANNEL_MISMATCH_MAX_SCORE = env_float("AI_MEM_GATE_CHANNEL_MISMATCH_MAX_SCORE", 0.6, 0.0, 1.5)


def _extract_latest_date(value: str) -> dt.datetime | None:
    best: dt.datetime | None = None
    for match in ISO_DATE_RE.finditer(str(value or "")):
        try:
            parsed = dt.datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                tzinfo=dt.timezone.utc,
            )
        except ValueError:
            continue
        if best is None or parsed > best:
            best = parsed
    return best


def temporal_query_multiplier(query_text: str, item: dict[str, Any]) -> float:
    lowered_query = str(query_text or "").lower()
    prefers_recency = any(token in lowered_query for token in RECENCY_QUERY_TERMS)
    tags = {str(tag).strip().lower() for tag in (item.get("tags") or []) if str(tag).strip()}
    if not prefers_recency:
        return 1.0

    latest_date = _extract_latest_date(
        " ".join(
            [
                str(item.get("title") or ""),
                str(item.get("excerpt") or ""),
                str(item.get("whyMatched") or ""),
            ]
        )
    )
    score = 1.0
    if latest_date is None:
        score *= 0.95
    else:
        age_days = max((dt.datetime.now(dt.timezone.utc) - latest_date).total_seconds() / 86400.0, 0.0)
        if age_days <= 90:
            score *= 1.18
        elif age_days <= 365:
            score *= 1.12
        elif age_days <= 730:
            score *= 1.05
        elif age_days <= 1460:
            score *= 0.95
        else:
            score *= 0.88
    if "new" in tags or "latest" in tags or "current" in tags:
        score *= 1.08
    if "old" in tags or "deprecated" in tags or "legacy" in tags:
        score *= 0.85
    return max(0.75, min(1.3, score))


def channel_specificity_multiplier(query_text: str, item: dict[str, Any]) -> float:
    query_tokens = keyword_tokens(query_text)
    required_channel_tokens = query_tokens & CHANNEL_SPECIFIC_TERMS
    if not required_channel_tokens:
        return 1.0
    candidate_tokens = keyword_tokens(
        " ".join(
            [
                str(item.get("title") or ""),
                str(item.get("excerpt") or ""),
                str(item.get("whyMatched") or ""),
            ]
        )
    )
    if required_channel_tokens & candidate_tokens:
        return 1.1
    return 0.72


def evaluate_summary_gate(items: list[dict[str, Any]], query_text: str) -> dict[str, Any]:
    if not items:
        return {
            "allow": False,
            "reason": "empty_results",
            "topScore": 0.0,
            "topVectorScore": 0.0,
            "topLexicalScore": 0.0,
            "thresholds": {
                "minScore": SUMMARY_GATE_MIN_SCORE,
                "minVectorScore": SUMMARY_GATE_MIN_VECTOR,
                "minLexicalScore": SUMMARY_GATE_MIN_LEXICAL,
                "channelMismatchMaxScore": SUMMARY_GATE_CHANNEL_MISMATCH_MAX_SCORE,
            },
        }
    top = items[0]
    top_score = float(top.get("score") or 0.0)
    top_vector = float(top.get("vectorScore") or 0.0)
    top_lexical = float(top.get("lexicalScore") or 0.0)
    query_tokens = keyword_tokens(query_text)
    required_channel_tokens = query_tokens & CHANNEL_SPECIFIC_TERMS
    top_tokens = keyword_tokens(
        " ".join(
            [
                str(top.get("title") or ""),
                str(top.get("excerpt") or ""),
                str(top.get("whyMatched") or ""),
            ]
        )
    )
    thresholds = {
        "minScore": SUMMARY_GATE_MIN_SCORE,
        "minVectorScore": SUMMARY_GATE_MIN_VECTOR,
        "minLexicalScore": SUMMARY_GATE_MIN_LEXICAL,
        "channelMismatchMaxScore": SUMMARY_GATE_CHANNEL_MISMATCH_MAX_SCORE,
    }
    if required_channel_tokens and not (required_channel_tokens & top_tokens) and top_score < SUMMARY_GATE_CHANNEL_MISMATCH_MAX_SCORE:
        return {
            "allow": False,
            "reason": "channel_mismatch_low_score",
            "topScore": top_score,
            "topVectorScore": top_vector,
            "topLexicalScore": top_lexical,
            "requiredChannelTokens": sorted(required_channel_tokens),
            "thresholds": thresholds,
        }
    if top_score < SUMMARY_GATE_MIN_SCORE and top_vector < SUMMARY_GATE_MIN_VECTOR and top_lexical < SUMMARY_GATE_MIN_LEXICAL:
        return {
            "allow": False,
            "reason": "below_quality_thresholds",
            "topScore": top_score,
            "topVectorScore": top_vector,
            "topLexicalScore": top_lexical,
            "thresholds": thresholds,
        }
    return {
        "allow": True,
        "reason": "accepted",
        "topScore": top_score,
        "topVectorScore": top_vector,
        "topLexicalScore": top_lexical,
        "thresholds": thresholds,
    }


def should_return_summary_matches(items: list[dict[str, Any]], query_text: str) -> bool:
    return bool(evaluate_summary_gate(items, query_text).get("allow"))


def canonical_hash(kind: str, text: str) -> str:
    normalized = " ".join(str(text or "").lower().split())
    return hashlib.sha256(f"{kind}|{normalized}".encode("utf-8", errors="replace")).hexdigest()


def normalize_auth_mode(value: Any) -> str:
    candidate = str(value or "dual").strip().lower()
    if candidate in AUTH_MODE_VALUES:
        return candidate
    return "dual"


def clamp_score(value: Any, default: float) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return default
    if score < 0:
        return 0.0
    if score > 1:
        return 1.0
    return score


def parse_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def clamp_dimension_map(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, float] = {}
    for key, raw in value.items():
        name = str(key or "").strip()
        if not name:
            continue
        normalized[name] = clamp_score(raw, 0.0)
    return normalized


def default_memory_class(kind: str) -> str:
    lowered = str(kind or "").strip().lower()
    if lowered in {"run_summary", "incident", "experiment", "debug_session"}:
        return "episodic"
    return "semantic"


def default_memory_scope(kind: str, storage_scope: str) -> str:
    lowered_kind = str(kind or "").strip().lower()
    lowered_scope = str(storage_scope or "").strip().lower()
    if lowered_scope == "personal":
        return "personal"
    if lowered_kind == "run_summary":
        return "repo"
    return "workspace"


def build_trust_dimensions(
    payload: dict[str, Any],
    *,
    confidence: float,
    importance: float,
    reference_count: int,
    default_freshness: float = 1.0,
) -> dict[str, float]:
    provided = clamp_dimension_map(payload.get("trustDimensions"))
    if provided:
        return provided
    return {
        "provenance_strength": confidence,
        "confirmation_level": confidence,
        "freshness": clamp_score(payload.get("freshness"), default_freshness),
        "contradiction_state": clamp_score(payload.get("contradictionState"), 1.0),
        "reuse_success": clamp_score(payload.get("reuseSuccess"), min(reference_count / 5.0, 1.0)),
        "importance_alignment": importance,
    }


def aggregate_trust_score(payload: dict[str, Any], dimensions: dict[str, float], confidence: float) -> float:
    if payload.get("trustScore") is not None:
        return clamp_score(payload.get("trustScore"), confidence)
    if not dimensions:
        return confidence
    weights = {
        "provenance_strength": 0.3,
        "confirmation_level": 0.2,
        "freshness": 0.15,
        "contradiction_state": 0.15,
        "reuse_success": 0.1,
        "importance_alignment": 0.1,
    }
    weighted = 0.0
    total = 0.0
    for key, score in dimensions.items():
        weight = weights.get(key, 0.05)
        weighted += score * weight
        total += weight
    if total <= 0:
        return confidence
    return clamp_score(weighted / total, confidence)


def aggregate_trust_dimensions(dimensions: dict[str, float], confidence: float) -> float:
    if not dimensions:
        return confidence
    weights = {
        "provenance_strength": 0.3,
        "confirmation_level": 0.2,
        "freshness": 0.15,
        "contradiction_state": 0.15,
        "reuse_success": 0.1,
        "importance_alignment": 0.05,
        "source_diversity": 0.03,
        "human_validation": 0.02,
    }
    weighted = 0.0
    total = 0.0
    for key, score in dimensions.items():
        weight = weights.get(key, 0.03)
        weighted += clamp_score(score, 0.0) * weight
        total += weight
    if total <= 0:
        return confidence
    return clamp_score(weighted / total, confidence)


RESOLUTION_PENDING = "pending"
RESOLUTION_RESOLVED = "resolved"
RESOLUTION_CONTESTED = "contested"
RESOLUTION_SUPERSEDED = "superseded"
EVENT_TRUST_DELTA_CAP = 0.12


def _default_outcome_fields() -> dict[str, Any]:
    return {
        "failureCount": 0,
        "lastFailureAt": None,
        "correctionCount": 0,
        "lastCorrectionAt": None,
        "hasPendingResolution": False,
        "resolutionState": RESOLUTION_RESOLVED,
        "isContested": False,
    }


def _clamp_trust_delta(value: float) -> float:
    return max(-EVENT_TRUST_DELTA_CAP, min(EVENT_TRUST_DELTA_CAP, float(value or 0.0)))


def _event_scope_defaults(scope: str, payload: dict[str, Any]) -> dict[str, Any]:
    defaults = {
        "scope": scope,
        "memoryScope": scope,
        "status": "active",
        "promotionStatus": "candidate",
        "source": str(payload.get("source") or "codex"),
        "createdAt": now_iso(),
        "updatedAt": now_iso(),
    }
    if scope == "personal":
        defaults["userId"] = payload.get("userId")
    else:
        defaults["workspaceId"] = payload.get("workspaceId", "default")
    defaults["projectId"] = payload.get("projectId")
    defaults["repoId"] = payload.get("repoId")
    return defaults


def parse_iso_datetime(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def infer_retrieval_intent(query_text: str, explicit_intent: Any = "") -> str:
    explicit = str(explicit_intent or "").strip().lower()
    if explicit:
        return explicit
    lowered = str(query_text or "").lower()
    if any(token in lowered for token in ["error", "failed", "failure", "debug", "incident", "traceback", "exception", "bug"]):
        return "debug"
    if any(token in lowered for token in ["design", "architecture", "decision", "pattern", "approach", "strategy"]):
        return "design"
    if any(token in lowered for token in ["task", "plan", "todo", "next step", "follow up", "follow-up"]):
        return "planning"
    if any(token in lowered for token in ["runbook", "operate", "operational", "deploy", "deployment"]):
        return "operational"
    return "general"


def preferred_memory_class(intent: str, explicit_class: Any = "") -> str:
    explicit = str(explicit_class or "").strip().lower()
    if explicit:
        return explicit
    if intent in {"debug", "operational"}:
        return "episodic"
    if intent in {"design"}:
        return "semantic"
    return ""


def recency_multiplier(updated_at: Any, last_referenced: Any) -> float:
    timestamps = [value for value in [parse_iso_datetime(last_referenced), parse_iso_datetime(updated_at)] if value is not None]
    if not timestamps:
        return 1.0
    latest = max(timestamps)
    age_days = max((dt.datetime.now(dt.timezone.utc) - latest).total_seconds() / 86400.0, 0.0)
    if age_days <= 1:
        return 1.08
    if age_days <= 7:
        return 1.05
    if age_days <= 30:
        return 1.02
    if age_days <= 90:
        return 0.98
    return 0.93


def normalize_project_scope_mode(payload: dict[str, Any]) -> str:
    mode = str(payload.get("projectScopeMode") or "").strip().lower()
    if not mode or mode == "auto":
        return "prefer" if str(payload.get("projectId") or "").strip() else "off"
    if mode in {"strict", "required", "hard"}:
        return "strict"
    if mode in {"prefer", "soft", "fallback"}:
        return "prefer"
    if mode in {"off", "none", "disabled", "global"}:
        return "off"
    raise ValueError("projectScopeMode must be one of: off, prefer, strict")


def apply_project_scope_envelope(
    items: list[dict[str, Any]],
    docs_by_source: dict[str, tuple[dict[str, Any], str]],
    top_k: int,
    project_filter: str,
    project_scope_mode: str,
) -> tuple[list[dict[str, Any]], bool]:
    if not project_filter or project_scope_mode == "off":
        return items[:top_k], False
    matched: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for item in items:
        source_ref_id = str(item.get("sourceRefId") or "")
        doc_pair = docs_by_source.get(source_ref_id)
        doc_project = str(doc_pair[0].get("projectId") or "").strip() if doc_pair else ""
        if doc_project == project_filter:
            matched.append(item)
        else:
            unmatched.append(item)
    if project_scope_mode == "strict":
        return matched[:top_k], False
    if len(matched) >= top_k:
        return matched[:top_k], False
    fill_count = max(top_k - len(matched), 0)
    fallback_used = fill_count > 0 and len(unmatched) > 0
    return matched + unmatched[:fill_count], fallback_used


def scope_multiplier(doc: dict[str, Any], payload: dict[str, Any], storage_scope: str) -> float:
    score = 1.0
    repo_id = str(payload.get("repoId") or "").strip()
    project_id = str(payload.get("projectId") or "").strip()
    doc_repo = str(doc.get("repoId") or "").strip()
    doc_project = str(doc.get("projectId") or "").strip()
    memory_scope = str(doc.get("memoryScope") or "").strip().lower()

    if repo_id and doc_repo:
        score *= 1.2 if repo_id == doc_repo else 0.9
    if project_id and doc_project:
        score *= 1.25 if project_id == doc_project else 0.92

    if memory_scope == "repo":
        score *= 1.08 if repo_id and repo_id == doc_repo else 0.95
    elif memory_scope == "project":
        score *= 1.1 if project_id and project_id == doc_project else 0.98
    elif memory_scope == "workspace":
        score *= 1.0
    elif memory_scope == "personal":
        score *= 0.96
    elif memory_scope == "reference":
        score *= 0.97

    if storage_scope == "personal":
        score *= 0.96
    return score


def project_membership_multiplier(doc: dict[str, Any], payload: dict[str, Any], repo_project_index: dict[str, set[str]]) -> float:
    if not repo_project_index:
        return 1.0
    score = 1.0
    project_id = str(payload.get("projectId") or "").strip()
    repo_id = str(payload.get("repoId") or "").strip()
    doc_project = str(doc.get("projectId") or "").strip()
    doc_repo = str(doc.get("repoId") or "").strip()

    if project_id and repo_id:
        requested_repo_projects = repo_project_index.get(repo_id, set())
        score *= 1.05 if project_id in requested_repo_projects else 0.9

    if doc_repo:
        attached_projects = repo_project_index.get(doc_repo, set())
        if doc_project:
            score *= 1.04 if doc_project in attached_projects else 0.94
        elif project_id and project_id in attached_projects:
            score *= 1.02

    return max(0.85, min(1.2, score))


def class_multiplier(memory_class: Any, preferred_class: str, intent: str) -> float:
    actual = str(memory_class or "").strip().lower()
    preferred = str(preferred_class or "").strip().lower()
    if not preferred or not actual:
        return 1.0
    if actual == preferred:
        return 1.15
    if intent == "planning":
        return 0.97
    return 0.88


def trust_multiplier(summary: dict[str, Any]) -> float:
    trust_score = clamp_score(summary.get("trustScore"), clamp_score(summary.get("confidence"), DEFAULT_CONFIDENCE))
    dimensions = clamp_dimension_map(summary.get("trustDimensions"))
    contradiction = dimensions.get("contradiction_state", 1.0)
    freshness = dimensions.get("freshness", 1.0)
    reuse_success = dimensions.get("reuse_success", min(int(summary.get("referenceCount", 0) or 0) / 5.0, 1.0))
    confirmation = dimensions.get("confirmation_level", trust_score)
    penalty = 0.7 if contradiction < 0.5 else 1.0
    return max(0.6, min(1.25, (0.75 + (trust_score * 0.25) + (freshness * 0.06) + (reuse_success * 0.05) + (confirmation * 0.04)) * penalty))


def promotion_multiplier(promotion_status: Any) -> float:
    status = str(promotion_status or "").strip().lower()
    if status == "canonical":
        return 1.12
    if status == "durable":
        return 1.05
    if status == "candidate":
        return 0.9
    if status == "superseded":
        return 0.62
    if status == "archived":
        return 0.8
    return 1.0


def supersession_multiplier(summary: dict[str, Any]) -> float:
    status = str(summary.get("status") or "").strip().lower()
    if status == "superseded":
        return 0.55
    if summary.get("supersededById"):
        return 0.7
    return 1.0


def age_in_days(*values: Any) -> float:
    timestamps = [value for value in [parse_iso_datetime(item) for item in values] if value is not None]
    if not timestamps:
        return 0.0
    earliest = min(timestamps)
    return max((dt.datetime.now(dt.timezone.utc) - earliest).total_seconds() / 86400.0, 0.0)


def _normalize_task_type(value: Any) -> str:
    lowered = str(value or "").strip().lower()
    aliases = {
        "script": "routine_scripting",
        "scripting": "routine_scripting",
        "routine": "routine_scripting",
        "architecture": "architecture",
        "design": "architecture",
        "infra": "infra_security_compliance",
        "security": "infra_security_compliance",
        "compliance": "infra_security_compliance",
        "debug": "debugging",
        "debugging": "debugging",
        "refactor": "refactoring",
        "refactoring": "refactoring",
        "plan": "planning",
        "planning": "planning",
    }
    return aliases.get(lowered, lowered or "general")


def _normalize_risk_level(value: Any) -> str:
    lowered = str(value or "").strip().lower()
    if lowered in {"high", "critical"}:
        return "high"
    if lowered in {"medium", "med", "moderate"}:
        return "medium"
    return "low"


def route_review_policy(
    *,
    task_type: str,
    risk_level: str,
    has_canonical_context: bool,
    has_external_dependency: bool,
    unresolved_disagreement: bool,
    force_three_model_review: bool,
) -> dict[str, Any]:
    normalized_task = _normalize_task_type(task_type)
    normalized_risk = _normalize_risk_level(risk_level)
    route = "codex_only"
    reviewers: list[str] = []
    reasons: list[str] = []
    if force_three_model_review:
        route = "codex_claude_gemini"
        reviewers = ["claude", "gemini"]
        reasons.append("force_three_model_review")
    elif normalized_risk == "high" or unresolved_disagreement:
        route = "codex_claude_gemini"
        reviewers = ["claude", "gemini"]
        reasons.append("high_risk_or_unresolved_disagreement")
    elif normalized_task in {"architecture", "infra_security_compliance"}:
        route = "codex_claude"
        reviewers = ["claude"]
        reasons.append("logic_security_review_required")
    elif normalized_task in {"debugging", "planning"}:
        route = "codex_claude"
        reviewers = ["claude"]
        reasons.append("reasoning_review_helpful")
    elif normalized_task in {"refactoring"} and has_external_dependency:
        route = "codex_gemini"
        reviewers = ["gemini"]
        reasons.append("external_dependency_validation")
    elif has_external_dependency:
        route = "codex_gemini"
        reviewers = ["gemini"]
        reasons.append("external_dependency_validation")
    elif normalized_risk == "medium":
        route = "codex_claude"
        reviewers = ["claude"]
        reasons.append("medium_risk_review")
    if has_canonical_context and route == "codex_only":
        reasons.append("canonical_context_available")
    if not has_canonical_context and route == "codex_only":
        reasons.append("no_canonical_context_but_low_risk")
    return {
        "route": route,
        "reviewers": reviewers,
        "taskType": normalized_task,
        "riskLevel": normalized_risk,
        "reasons": reasons,
        "policyVersion": "tri-model-routing-v1",
        "reviewSchema": {
            "requiredFields": ["claim", "verdict", "evidence", "risk"],
            "allowedVerdicts": ["PASS", "REVISE", "BLOCK"],
        },
    }


class MemoryService:
    def __init__(self) -> None:
        endpoint = os.environ["COSMOS_ENDPOINT"]
        self.db_name = os.environ.get("COSMOS_DB_NAME", "ai_mem")
        cred = DefaultAzureCredential(exclude_shared_token_cache_credential=True)
        client = CosmosClient(endpoint, credential=cred)
        self.db = client.get_database_client(self.db_name)
        self.personal = self.db.get_container_client("personal_memory")
        self.shared = self.db.get_container_client("shared_memory")
        self.audit = self.db.get_container_client("audit_log")
        self.emb = self.db.get_container_client("embeddings")
        self.retrieval = self.db.get_container_client("retrieval_log")
        self.embed_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
        self.embed_key = os.environ.get("AZURE_OPENAI_API_KEY", "")
        self.embed_deployment = os.environ.get("AZURE_OPENAI_EMBED_DEPLOYMENT", "")
        self.embed_api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
        self.local_embed_model = os.environ.get("LOCAL_EMBED_MODEL_NAME", "").strip()
        self.embedding_write_mode = os.environ.get("EMBEDDING_WRITE_MODE", "queue").strip().lower()
        self.embed_queue_name = os.environ.get("MEMORY_EMBED_QUEUE_NAME", "embedding-jobs").strip() or "embedding-jobs"
        self.auto_extract_default = parse_bool(os.environ.get("AUTO_EXTRACT_RUN_SUMMARIES"), default=False)
        self._queue_client: QueueClient | None = None

    def _get_queue_client(self) -> QueueClient:
        if self._queue_client is None:
            self._queue_client = QueueClient.from_connection_string(
                os.environ["AzureWebJobsStorage"],
                queue_name=self.embed_queue_name,
                message_encode_policy=TextBase64EncodePolicy(),
            )
            try:
                self._queue_client.create_queue()
            except ResourceExistsError:
                pass
        return self._queue_client

    def _query_by_id(self, container: Any, doc_id: str, partition_key_name: str, partition_key_value: str | None) -> dict[str, Any] | None:
        if partition_key_value:
            try:
                return container.read_item(item=doc_id, partition_key=partition_key_value)
            except exceptions.CosmosResourceNotFoundError:
                pass
        query = f"SELECT TOP 1 * FROM c WHERE c.id=@id AND c.{partition_key_name}=@pk"
        params = [{"name": "@id", "value": doc_id}, {"name": "@pk", "value": partition_key_value}]
        items = list(container.query_items(query=query, parameters=params, enable_cross_partition_query=True))
        return items[0] if items else None

    def _query_container_docs(self, container: Any, query: str, parameters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return list(container.query_items(query=query, parameters=parameters, enable_cross_partition_query=True))

    def _load_source_doc(self, source_ref_id: str, workspace_id: str, user_id: str | None) -> tuple[dict[str, Any] | None, str | None]:
        shared_doc = self._query_by_id(self.shared, source_ref_id, "workspaceId", workspace_id)
        if shared_doc is not None:
            return shared_doc, "shared"
        personal_doc = self._query_by_id(self.personal, source_ref_id, "userId", user_id)
        if personal_doc is not None:
            return personal_doc, "personal"
        return None, None

    def _get_container_for_scope(self, scope: str) -> Any:
        return self.shared if scope == "shared" else self.personal

    def _get_partition_key_name(self, scope: str) -> str:
        return "workspaceId" if scope == "shared" else "userId"

    def _doc_primary_text(self, doc: dict[str, Any]) -> str:
        for key in ("content", "summary", "title", "request", "text"):
            value = doc.get(key)
            if value:
                return str(value)
        if str(doc.get("kind") or "").strip().lower() == "disagreement":
            joined = "\n".join(
                part
                for part in [
                    str(doc.get("claim") or "").strip(),
                    str(doc.get("codexPosition") or "").strip(),
                    str(doc.get("claudePosition") or "").strip(),
                    str(doc.get("geminiPosition") or "").strip(),
                    str(doc.get("resolution") or "").strip(),
                ]
                if part
            )
            if joined:
                return joined
        return ""

    def _doc_canonical_hash(self, doc: dict[str, Any]) -> str:
        existing = str(doc.get("canonicalHash") or "").strip()
        if existing:
            return existing
        return canonical_hash(str(doc.get("kind") or "note"), self._doc_primary_text(doc))

    def _compute_dynamic_trust_dimensions(self, doc: dict[str, Any]) -> dict[str, float]:
        dimensions = clamp_dimension_map(doc.get("trustDimensions"))
        confidence = clamp_score(doc.get("confidence"), DEFAULT_CONFIDENCE)
        importance = clamp_score(doc.get("importance"), DEFAULT_IMPORTANCE)
        reference_count = int(doc.get("referenceCount", 0) or 0)
        freshness = 1.0
        active_age_days = age_in_days(doc.get("lastReferenced"), doc.get("updatedAt"), doc.get("createdAt"))
        if active_age_days > 90:
            freshness = 0.5
        elif active_age_days > 30:
            freshness = 0.72
        elif active_age_days > 14:
            freshness = 0.86
        contradiction_state = clamp_score(dimensions.get("contradiction_state", 1.0), 1.0)
        source_count = max(1, len(doc.get("derivedFromIds") or []) + (1 if doc.get("artifactRef") else 0))
        refreshed = {
            **dimensions,
            "provenance_strength": clamp_score(dimensions.get("provenance_strength", confidence), confidence),
            "confirmation_level": clamp_score(dimensions.get("confirmation_level", confidence), confidence),
            "freshness": clamp_score(dimensions.get("freshness", freshness), freshness),
            "contradiction_state": contradiction_state,
            "reuse_success": clamp_score(reference_count / 20.0, 0.0),
            "importance_alignment": clamp_score(dimensions.get("importance_alignment", importance), importance),
            "source_diversity": clamp_score(dimensions.get("source_diversity", min(source_count / 3.0, 1.0)), min(source_count / 3.0, 1.0)),
            "human_validation": clamp_score(dimensions.get("human_validation", 0.0), 0.0),
        }
        return refreshed

    def _refresh_doc_trust(self, doc: dict[str, Any]) -> dict[str, Any]:
        dimensions = self._compute_dynamic_trust_dimensions(doc)
        confidence = clamp_score(doc.get("confidence"), DEFAULT_CONFIDENCE)
        doc["trustDimensions"] = dimensions
        doc["trustScore"] = aggregate_trust_dimensions(dimensions, confidence)
        return doc

    def _evaluate_promotion(self, doc: dict[str, Any]) -> dict[str, Any]:
        current = str(doc.get("promotionStatus") or "").strip().lower() or "durable"
        memory_class = str(doc.get("memoryClass") or "").strip().lower()
        trust_score = clamp_score(doc.get("trustScore"), clamp_score(doc.get("confidence"), DEFAULT_CONFIDENCE))
        reference_count = int(doc.get("referenceCount", 0) or 0)
        contradiction = clamp_score((doc.get("trustDimensions") or {}).get("contradiction_state", 1.0), 1.0)
        age_days = age_in_days(doc.get("createdAt"), doc.get("updatedAt"))
        proposed = current
        reasons: list[str] = []
        if current == "candidate" and reference_count >= 5 and trust_score >= 0.65 and age_days >= 3 and contradiction >= 0.6:
            proposed = "durable"
            reasons = ["reference_count", "trust_score", "age"]
        elif (
            current == "durable"
            and memory_class == "semantic"
            and reference_count >= 15
            and trust_score >= 0.80
            and age_days >= 14
            and contradiction >= 0.7
        ):
            proposed = "canonical"
            reasons = ["reference_count", "trust_score", "age", "memory_class"]
        return {"current": current, "proposed": proposed, "changed": proposed != current, "reasons": reasons}

    def _doc_summary(self, doc: dict[str, Any], score: float | None = None, match_text: str = "") -> dict[str, Any]:
        self._refresh_doc_trust(doc)
        primary = self._doc_primary_text(doc)
        item = {
            "id": doc.get("id"),
            "kind": doc.get("kind"),
            "projectId": doc.get("projectId"),
            "memoryScope": doc.get("memoryScope"),
            "memoryClass": doc.get("memoryClass"),
            "status": doc.get("status"),
            "promotionStatus": doc.get("promotionStatus"),
            "tags": doc.get("tags", []),
            "updatedAt": doc.get("updatedAt"),
            "createdAt": doc.get("createdAt"),
            "repoId": doc.get("repoId"),
            "workspaceId": doc.get("workspaceId"),
            "userId": doc.get("userId"),
            "title": compact_text(doc.get("title") or primary, 120),
            "excerpt": compact_text(primary or match_text, 220),
            "canonicalHash": self._doc_canonical_hash(doc),
            "visibility": doc.get("visibility", "normal"),
            "retentionDays": doc.get("retentionDays"),
            "importance": clamp_score(doc.get("importance"), DEFAULT_IMPORTANCE),
            "confidence": clamp_score(doc.get("confidence"), DEFAULT_CONFIDENCE),
            "trustScore": clamp_score(doc.get("trustScore"), clamp_score(doc.get("confidence"), DEFAULT_CONFIDENCE)),
            "trustDimensions": clamp_dimension_map(doc.get("trustDimensions")),
            "referenceCount": int(doc.get("referenceCount", 0) or 0),
            "lastReferenced": doc.get("lastReferenced"),
            "embeddingStatus": doc.get("embeddingStatus", "unknown"),
            "embeddingMode": doc.get("embeddingMode"),
            "supersedesId": doc.get("supersedesId"),
            "supersededById": doc.get("supersededById"),
        }
        if score is not None:
            item["score"] = score
        return item

    def _dedupe_summary_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        best_by_hash: dict[str, dict[str, Any]] = {}
        for item in items:
            fingerprint = str(item.get("canonicalHash") or item.get("sourceRefId") or item.get("id"))
            existing = best_by_hash.get(fingerprint)
            if existing is None or float(item.get("score") or 0.0) > float(existing.get("score") or 0.0):
                best_by_hash[fingerprint] = item
        deduped = list(best_by_hash.values())
        deduped.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
        return deduped

    def _rerank_summary_item(
        self,
        item: dict[str, Any],
        doc: dict[str, Any],
        storage_scope: str,
        payload: dict[str, Any],
        repo_project_index: dict[str, set[str]] | None = None,
    ) -> dict[str, Any]:
        query_text = str(payload.get("query") or payload.get("queryText") or "")
        intent = infer_retrieval_intent(query_text, payload.get("intent"))
        preferred_class = preferred_memory_class(intent, payload.get("preferredMemoryClass"))
        scope_fit = scope_multiplier(doc, payload, storage_scope)
        class_fit = class_multiplier(item.get("memoryClass"), preferred_class, intent)
        trust_fit = trust_multiplier(item)
        recency_fit = recency_multiplier(item.get("updatedAt"), item.get("lastReferenced"))
        promotion_fit = promotion_multiplier(item.get("promotionStatus"))
        channel_fit = channel_specificity_multiplier(query_text, item)
        temporal_fit = temporal_query_multiplier(query_text, item)
        project_membership_fit = project_membership_multiplier(doc, payload, repo_project_index or {})
        supersession_fit = supersession_multiplier(item)
        final_score = (
            float(item.get("score") or 0.0)
            * scope_fit
            * class_fit
            * trust_fit
            * recency_fit
            * promotion_fit
            * channel_fit
            * temporal_fit
            * project_membership_fit
            * supersession_fit
        )
        item["score"] = final_score
        item["rankingSignals"] = {
            "intent": intent,
            "preferredMemoryClass": preferred_class,
            "scopeFit": round(scope_fit, 4),
            "classFit": round(class_fit, 4),
            "trustFit": round(trust_fit, 4),
            "recencyFit": round(recency_fit, 4),
            "promotionFit": round(promotion_fit, 4),
            "channelFit": round(channel_fit, 4),
            "temporalFit": round(temporal_fit, 4),
            "projectMembershipFit": round(project_membership_fit, 4),
            "supersessionFit": round(supersession_fit, 4),
        }
        return item

    def _list_docs(self, scope: str, workspace_id: str, user_id: str | None, limit: int) -> list[dict[str, Any]]:
        scope = scope.lower()
        if scope == "shared":
            items = self._query_container_docs(
                self.shared,
                "SELECT TOP @lim * FROM c WHERE c.workspaceId=@ws AND (NOT IS_DEFINED(c.kind) OR c.kind != 'project') ORDER BY c.updatedAt DESC",
                [{"name": "@lim", "value": limit}, {"name": "@ws", "value": workspace_id}],
            )
            for item in items:
                item["_memoryScope"] = "shared"
            return items
        if scope == "personal":
            if not user_id:
                raise ValueError("userId is required for personal scope")
            items = self._query_container_docs(
                self.personal,
                "SELECT TOP @lim * FROM c WHERE c.userId=@uid AND (NOT IS_DEFINED(c.kind) OR c.kind != 'project') ORDER BY c.updatedAt DESC",
                [{"name": "@lim", "value": limit}, {"name": "@uid", "value": user_id}],
            )
            for item in items:
                item["_memoryScope"] = "personal"
            return items
        if scope == "all":
            items = self._list_docs("shared", workspace_id, user_id, limit)
            items.extend(self._list_docs("personal", workspace_id, user_id, limit))
            items.sort(key=lambda item: str(item.get("updatedAt") or ""), reverse=True)
            return items[:limit]
        raise ValueError("scope must be one of: shared, personal, all")

    def _project_id(self, workspace_id: str, slug: str) -> str:
        return f"proj:{workspace_id}:{slug}"

    def _normalize_slug(self, value: str) -> str:
        lowered = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
        return lowered[:80]

    def _ensure_project_exists(self, workspace_id: str, project_id: str) -> None:
        normalized = str(project_id or "").strip()
        if not normalized:
            return
        project = self._query_by_id(self.shared, normalized, "workspaceId", workspace_id)
        if project is None or str(project.get("kind") or "").strip().lower() != "project":
            raise ValueError(f"project not found: {normalized}")

    def _load_repo_project_index(self, workspace_id: str) -> dict[str, set[str]]:
        if not workspace_id:
            return {}
        items = self._query_container_docs(
            self.shared,
            "SELECT c.id, c.repos, c.status FROM c WHERE c.workspaceId=@ws AND c.kind='project'",
            [{"name": "@ws", "value": workspace_id}],
        )
        index: dict[str, set[str]] = {}
        for item in items:
            status = str(item.get("status") or "").strip().lower()
            if status in {"archived", "inactive"}:
                continue
            project_id = str(item.get("id") or "").strip()
            if not project_id:
                continue
            for repo_id in (item.get("repos") or []):
                normalized_repo = str(repo_id or "").strip()
                if not normalized_repo:
                    continue
                index.setdefault(normalized_repo, set()).add(project_id)
        return index

    def _embed_local_model(self, clean: str) -> dict[str, Any] | None:
        if not self.local_embed_model:
            return None
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore

            model = SentenceTransformer(self.local_embed_model)
            vector = model.encode(clean, normalize_embeddings=True)
            if hasattr(vector, "tolist"):
                vector = vector.tolist()
            vector_list = [float(item) for item in vector]
            return {
                "vector": vector_list,
                "embeddingMode": "local_model",
                "degraded": False,
                "warnings": [],
            }
        except Exception as exc:
            return {
                "vector": stable_hash_embedding(clean),
                "embeddingMode": "degraded_hash",
                "degraded": True,
                "warnings": [f"local_model_failed:{type(exc).__name__}"],
            }

    def embed(self, text: str) -> dict[str, Any]:
        clean = redact_text(text or "")
        if self.embed_endpoint and self.embed_key and self.embed_deployment:
            try:
                url = f"{self.embed_endpoint}/openai/deployments/{self.embed_deployment}/embeddings"
                rsp = requests.post(
                    url,
                    params={"api-version": self.embed_api_version},
                    headers={"api-key": self.embed_key, "Content-Type": "application/json"},
                    json={"input": clean},
                    timeout=15,
                )
                rsp.raise_for_status()
                data = rsp.json()
                vector = [float(item) for item in data["data"][0]["embedding"]]
                return {
                    "vector": vector,
                    "embeddingMode": "azure_openai",
                    "degraded": False,
                    "warnings": [],
                }
            except Exception as exc:
                local = self._embed_local_model(clean)
                if local is not None:
                    local["warnings"] = [f"azure_openai_failed:{type(exc).__name__}", *local.get("warnings", [])]
                    return local
                return {
                    "vector": stable_hash_embedding(clean),
                    "embeddingMode": "degraded_hash",
                    "degraded": True,
                    "warnings": [f"azure_openai_failed:{type(exc).__name__}", "semantic_recall_may_be_weaker"],
                }

        local = self._embed_local_model(clean)
        if local is not None:
            if local.get("embeddingMode") == "local_model":
                local["warnings"] = ["azure_openai_not_configured"]
            else:
                local["warnings"] = ["azure_openai_not_configured", *local.get("warnings", [])]
            return local
        return {
            "vector": stable_hash_embedding(clean),
            "embeddingMode": "degraded_hash",
            "degraded": True,
            "warnings": ["azure_openai_not_configured", "semantic_recall_may_be_weaker"],
        }

    def _base_doc(self, payload: dict[str, Any]) -> dict[str, Any]:
        ts = now_iso()
        kind = payload.get("kind", "note")
        confidence = clamp_score(payload.get("confidence"), DEFAULT_CONFIDENCE)
        importance = clamp_score(payload.get("importance"), DEFAULT_IMPORTANCE)
        reference_count = int(payload.get("referenceCount", 0) or 0)
        storage_scope = str(payload.get("scope") or "shared").lower()
        trust_dimensions = build_trust_dimensions(
            payload,
            confidence=confidence,
            importance=importance,
            reference_count=reference_count,
        )
        return {
            "id": payload.get("id") or str(uuid.uuid4()),
            "requestId": payload.get("requestId"),
            "userId": payload.get("userId", "unknown"),
            "workspaceId": payload.get("workspaceId", "default"),
            "projectId": payload.get("projectId"),
            "repoId": payload.get("repoId"),
            "kind": kind,
            "storageScope": storage_scope,
            "memoryScope": payload.get("memoryScope") or default_memory_scope(str(kind), storage_scope),
            "memoryClass": payload.get("memoryClass") or default_memory_class(str(kind)),
            "status": payload.get("status", "active"),
            "promotionStatus": payload.get("promotionStatus", "durable"),
            "tags": split_tags(payload.get("tags")),
            "createdAt": payload.get("createdAt", ts),
            "updatedAt": payload.get("updatedAt", ts),
            "source": payload.get("source", "mcp"),
            "visibility": payload.get("visibility", "normal"),
            "store": bool(payload.get("store", True)),
            "importance": importance,
            "confidence": confidence,
            "trustScore": aggregate_trust_score(payload, trust_dimensions, confidence),
            "trustDimensions": trust_dimensions,
            "referenceCount": reference_count,
            "lastReferenced": payload.get("lastReferenced"),
            "embeddingStatus": payload.get("embeddingStatus", "pending"),
            "embeddingMode": payload.get("embeddingMode"),
            "embeddingVersion": payload.get("embeddingVersion", "v1"),
            "artifactRef": payload.get("artifactRef"),
            "derivedFromIds": payload.get("derivedFromIds", []),
            "supersedesId": payload.get("supersedesId"),
        }
        for key, default_value in _default_outcome_fields().items():
            doc.setdefault(key, default_value)

    def _audit(self, workspace_id: str, actor: str, operation: str, target: str, summary: str, source: str = "function") -> None:
        item = {
            "id": str(uuid.uuid4()),
            "workspaceId": workspace_id or "default",
            "actor": actor or "unknown",
            "operation": operation,
            "targetContainer": target,
            "timestamp": now_iso(),
            "summary": redact_text(summary or ""),
            "source": source,
        }
        self.audit.create_item(item)

    def _log_retrieval(self, payload: dict[str, Any]) -> str | None:
        try:
            timestamp = now_iso()
            item = {
                "id": f"retrieval:{uuid.uuid4()}",
                "timestamp": timestamp,
                "createdAt": timestamp,
                **payload,
            }
            self.retrieval.create_item(item)
            return item["id"]
        except Exception:
            return None

    def _store_embedding(self, workspace_id: str, user_id: str, repo_id: str | None, ref_id: str, text: str, tags: list[str], source: str) -> dict[str, Any]:
        embedded = self.embed(text or "")
        emb_id = f"emb:{ref_id}"
        vector = embedded["vector"]
        doc = {
            "id": emb_id,
            "workspaceId": workspace_id,
            "userId": user_id,
            "repoId": repo_id,
            "kind": "document_chunk",
            "text": redact_text(text or ""),
            "vector": vector,
            "vectorMeta": {
                "dims": len(vector),
                "model": embedded["embeddingMode"],
                "embeddingMode": embedded["embeddingMode"],
                "embeddingVersion": "v1",
                "generatedAt": now_iso(),
                "degraded": embedded["degraded"],
                "warnings": embedded["warnings"],
            },
            "tags": tags,
            "createdAt": now_iso(),
            "updatedAt": now_iso(),
            "source": source,
            "sourceRefId": ref_id,
        }
        self.emb.upsert_item(doc)
        return {
            "embeddingId": emb_id,
            "dims": len(vector),
            "embeddingMode": embedded["embeddingMode"],
            "degraded": embedded["degraded"],
            "warnings": embedded["warnings"],
        }

    def _update_embedding_state(self, doc: dict[str, Any], scope: str, status: str, mode: str | None = None, warnings: list[str] | None = None) -> None:
        doc["embeddingStatus"] = status
        doc["updatedAt"] = now_iso()
        if mode:
            doc["embeddingMode"] = mode
        if warnings:
            doc["embeddingWarnings"] = warnings
        container = self._get_container_for_scope(scope)
        container.upsert_item(doc)

    def _queue_embedding_job(self, doc: dict[str, Any], scope: str, text: str) -> tuple[str | None, list[str]]:
        try:
            queue = self._get_queue_client()
            job = {
                "jobId": str(uuid.uuid4()),
                "workspaceId": doc["workspaceId"],
                "repoId": doc.get("repoId"),
                "userId": doc["userId"],
                "scope": scope,
                "sourceRefId": doc["id"],
                "kind": doc["kind"],
                "text": redact_text(text),
                "tags": doc.get("tags", []),
                "requestedAt": now_iso(),
                "embeddingVersion": doc.get("embeddingVersion", "v1"),
            }
            queue.send_message(json.dumps(job))
            self._update_embedding_state(doc, scope, "pending")
            return job["jobId"], []
        except Exception as exc:
            self._update_embedding_state(doc, scope, "enqueue_failed", warnings=[f"embedding_enqueue_failed:{type(exc).__name__}"])
            return None, [f"embedding_enqueue_failed:{type(exc).__name__}"]

    def _should_persist(self, payload: dict[str, Any]) -> bool:
        return bool(payload.get("store", True))

    def _transient_result(self, doc: dict[str, Any], scope: str, operation: str, actor: str) -> dict[str, Any]:
        self._audit(doc["workspaceId"], actor, operation, "transient_memory", f"skip store {doc['id']}")
        return {"ok": True, "id": doc["id"], "scope": scope, "stored": False, "item": self._doc_summary(doc)}

    def _resolve_partition_key(self, scope: str, doc: dict[str, Any], workspace_id: str, user_id: str | None) -> str:
        if scope == "shared":
            return str(doc.get("workspaceId") or workspace_id)
        return str(doc.get("userId") or user_id or "unknown")

    def _apply_supersession_link(self, container: Any, scope: str, doc: dict[str, Any], actor: str) -> bool:
        supersedes_id = str(doc.get("supersedesId") or "").strip()
        if not supersedes_id or supersedes_id == str(doc.get("id")):
            return False
        workspace_id = str(doc.get("workspaceId") or "")
        user_id = str(doc.get("userId") or "")
        partition_key = self._resolve_partition_key(scope, doc, workspace_id, user_id)
        target = self._query_by_id(container, supersedes_id, self._get_partition_key_name(scope), partition_key)
        if target is None:
            return False
        target["status"] = "superseded"
        target["supersededById"] = str(doc.get("id"))
        target["promotionStatus"] = "superseded"
        target["updatedAt"] = now_iso()
        self._refresh_doc_trust(target)
        container.upsert_item(target)
        lineage = [str(item).strip() for item in (doc.get("derivedFromIds") or []) if str(item).strip()]
        if supersedes_id not in lineage:
            lineage.append(supersedes_id)
            doc["derivedFromIds"] = lineage
            doc["updatedAt"] = now_iso()
            self._refresh_doc_trust(doc)
            container.upsert_item(doc)
        self._audit(workspace_id, actor, "memory_supersession_link", f"{scope}_memory", f"new={doc.get('id')} old={supersedes_id}")
        return True

    def _consolidate_canonical_duplicates(self, container: Any, scope: str, doc: dict[str, Any], actor: str) -> list[str]:
        if str(doc.get("promotionStatus") or "").strip().lower() != "canonical":
            return []
        canonical = str(doc.get("canonicalHash") or "").strip()
        if not canonical:
            return []
        scope_query = "c.workspaceId=@pk" if scope == "shared" else "c.userId=@pk"
        partition_value = str(doc.get("workspaceId") if scope == "shared" else doc.get("userId") or "")
        kind = str(doc.get("kind") or "")
        if not partition_value or not kind:
            return []
        matches = self._query_container_docs(
            container,
            "SELECT * FROM c WHERE "
            + scope_query
            + " AND c.id!=@id AND c.kind=@kind AND IS_DEFINED(c.canonicalHash) AND c.canonicalHash=@hash AND (NOT IS_DEFINED(c.status) OR c.status!='superseded')",
            [
                {"name": "@pk", "value": partition_value},
                {"name": "@id", "value": str(doc.get("id"))},
                {"name": "@kind", "value": kind},
                {"name": "@hash", "value": canonical},
            ],
        )
        consolidated: list[str] = []
        for target in matches:
            target_id = str(target.get("id") or "").strip()
            if not target_id:
                continue
            target["status"] = "superseded"
            target["supersededById"] = str(doc.get("id"))
            target["promotionStatus"] = "superseded"
            target["updatedAt"] = now_iso()
            self._refresh_doc_trust(target)
            container.upsert_item(target)
            consolidated.append(target_id)
        if consolidated:
            lineage = [str(item).strip() for item in (doc.get("derivedFromIds") or []) if str(item).strip()]
            for item in consolidated:
                if item not in lineage:
                    lineage.append(item)
            doc["derivedFromIds"] = lineage
            doc["updatedAt"] = now_iso()
            self._refresh_doc_trust(doc)
            container.upsert_item(doc)
            self._audit(
                str(doc.get("workspaceId") or ""),
                actor,
                "memory_consolidate_canonical",
                f"{scope}_memory",
                f"canonical={doc.get('id')} superseded={len(consolidated)}",
            )
        return consolidated

    def _write_memory_record(self, container: Any, doc: dict[str, Any], scope: str, text: str, operation: str, actor: str) -> dict[str, Any]:
        request_id = str(doc.get("requestId") or "").strip()
        workspace_id = str(doc.get("workspaceId") or "")
        if request_id:
            query = "SELECT * FROM c WHERE c.workspaceId=@ws AND c.requestId=@req"
            existing = self._query_container_docs(container, query, [{"name": "@ws", "value": workspace_id}, {"name": "@req", "value": request_id}])
            if existing:
                found = existing[0]
                self._audit(workspace_id, actor, f"{operation}_dedup", f"{scope}_memory", f"skip duplicate {found['id']} for request {request_id}")
                return {**self._doc_summary(found), "ok": True, "scope": scope, "stored": True, "duplicate": True}

        container.upsert_item(doc)
        self._apply_supersession_link(container, scope, doc, actor)
        response = {"ok": True, "id": doc["id"], "scope": scope, "stored": True, "warnings": []}
        if self.embedding_write_mode == "queue":
            job_id, warnings = self._queue_embedding_job(doc, scope, text)
            response["embeddingStatus"] = doc.get("embeddingStatus")
            if job_id:
                response["queueJobId"] = job_id
            if warnings:
                response["warnings"] = warnings
        else:
            embedding = self._store_embedding(
                workspace_id=doc["workspaceId"],
                user_id=doc["userId"],
                repo_id=doc.get("repoId"),
                ref_id=doc["id"],
                text=text,
                tags=doc["tags"],
                source=doc["source"],
            )
            self._update_embedding_state(doc, scope, "ready", mode=embedding["embeddingMode"], warnings=embedding["warnings"])
            response["embedding"] = embedding
            response["embeddingStatus"] = "ready"
            response["warnings"] = embedding["warnings"]
        self._audit(doc["workspaceId"], actor, operation, f"{scope}_memory", f"upsert {doc['id']} req={request_id}")
        return response

    def _touch_reference(self, doc: dict[str, Any], scope: str) -> None:
        try:
            container = self._get_container_for_scope(scope)
            doc["referenceCount"] = int(doc.get("referenceCount", 0) or 0) + 1
            doc["lastReferenced"] = now_iso()
            self._refresh_doc_trust(doc)
            container.upsert_item(doc)
        except Exception:
            pass

    def _build_event_document(self, kind: str, scope: str, payload: dict[str, Any], extras: dict[str, Any]) -> dict[str, Any]:
        doc = _event_scope_defaults(scope, payload)
        doc["id"] = payload.get("eventId") or str(uuid.uuid4())
        doc["kind"] = kind
        doc["targetId"] = payload.get("targetId")
        doc["targetKind"] = payload.get("targetKind")
        doc.update(extras or {})
        return doc

    def _load_event_target_doc(self, scope: str, target_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not target_id:
            return None
        container = self._get_container_for_scope(scope)
        partition_name = self._get_partition_key_name(scope)
        partition_value = payload.get("workspaceId") if scope == "shared" else payload.get("userId")
        if not partition_value:
            return None
        return self._query_by_id(container, target_id, partition_name, partition_value)

    def _apply_event_summary(
        self,
        doc: dict[str, Any],
        *,
        failure: bool = False,
        correction: bool = False,
        resolution_state: str | None = None,
    ) -> dict[str, Any]:
        now = now_iso()
        if failure:
            doc["failureCount"] = int(doc.get("failureCount", 0) or 0) + 1
            doc["lastFailureAt"] = now
            doc["hasPendingResolution"] = True
            doc["isContested"] = True
            doc["resolutionState"] = RESOLUTION_PENDING
        if correction:
            doc["correctionCount"] = int(doc.get("correctionCount", 0) or 0) + 1
            doc["lastCorrectionAt"] = now
        if resolution_state:
            doc["resolutionState"] = resolution_state
            doc["hasPendingResolution"] = resolution_state != RESOLUTION_RESOLVED
            doc["isContested"] = resolution_state in {RESOLUTION_PENDING, RESOLUTION_CONTESTED}
        doc["updatedAt"] = now
        return doc

    def _apply_trust_delta(
        self,
        doc: dict[str, Any],
        *,
        delta: float = 0.0,
        dimension_deltas: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        safe_delta = _clamp_trust_delta(delta)
        if abs(safe_delta) < 1e-6 and not dimension_deltas:
            return doc
        dims = clamp_dimension_map(doc.get("trustDimensions"))
        for key, value in (dimension_deltas or {}).items():
            dims[key] = clamp_score(float(value or 0.0) + dims.get(key, 0.0), 1.0)
        if safe_delta:
            dims["confirmation_level"] = clamp_score(dims.get("confirmation_level", 0.0) + safe_delta, 1.0)
        doc["trustDimensions"] = dims
        return self._refresh_doc_trust(doc)

    def _record_event(self, scope: str, event: dict[str, Any]) -> None:
        container = self._get_container_for_scope(scope)
        container.upsert_item(event)

    def _normalize_event_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value if item]
        if isinstance(value, str):
            return [value]
        return []

    def _normalize_dimension_deltas(self, value: Any) -> dict[str, float]:
        if isinstance(value, dict):
            return {str(k): float(v or 0.0) for k, v in value.items()}
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return {str(k): float(v or 0.0) for k, v in parsed.items()}
            except json.JSONDecodeError:
                pass
        return {}

    def _evaluate_retrieval_outcome(self, doc: dict[str, Any], payload: dict[str, Any]) -> tuple[bool, list[str], float]:
        include_contested = bool(payload.get("includeContested"))
        include_failed = bool(payload.get("includeFailed"))
        try:
            failure_window = float(payload.get("failureRecencyDays") or FAILURE_RECENCY_DAYS_DEFAULT)
        except (TypeError, ValueError):
            failure_window = FAILURE_RECENCY_DAYS_DEFAULT
        try:
            min_trust_score = clamp_score(float(payload.get("minTrustScore") or MIN_TRUST_SCORE_DEFAULT), 1.0)
        except (TypeError, ValueError):
            min_trust_score = MIN_TRUST_SCORE_DEFAULT
        failure_count = int(doc.get("failureCount", 0) or 0)
        last_failure = doc.get("lastFailureAt")
        failure_recent = failure_count and age_in_days(last_failure) <= failure_window
        contested = bool(doc.get("hasPendingResolution"))
        trust_score = clamp_score(doc.get("trustScore"), clamp_score(doc.get("confidence"), DEFAULT_CONFIDENCE))
        warnings: list[str] = []
        skip = False
        if contested:
            warnings.append("contested_pending_resolution")
            if not include_contested:
                skip = True
        if failure_recent:
            warnings.append("recent_failure")
            if not include_failed:
                skip = True
        if trust_score < min_trust_score:
            warnings.append("low_trust")
            skip = True
        return skip, warnings, trust_score

    def memory_add_failure_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        target_id = str(payload.get("targetId") or "").strip()
        if not target_id:
            raise ValueError("targetId is required for failure events")
        scope = str(payload.get("scope") or "shared").lower()
        if scope not in {"shared", "personal"}:
            raise ValueError("scope must be shared or personal")
        severity = str(payload.get("severity") or "medium").strip().lower()
        if severity not in {"low", "medium", "high", "critical"}:
            raise ValueError("severity must be one of low|medium|high|critical")
        if severity == "low" and not bool(payload.get("recordLowSeverity")):
            return {"ok": True, "skipped": "low severity not recorded"}
        failure_kind = str(payload.get("failureKind") or "unknown")
        extras = {
            "severity": severity,
            "failureKind": failure_kind,
            "reason": str(payload.get("reason") or ""),
            "evidence": self._normalize_event_list(payload.get("evidence")),
            "runId": payload.get("runId"),
            "taskId": payload.get("taskId"),
            "sourceModel": str(payload.get("sourceModel") or ""),
            "recordedBy": str(payload.get("actor") or "system"),
        }
        event = self._build_event_document("failure_event", scope, payload, extras)
        container = self._get_container_for_scope(scope)
        target_doc = self._load_event_target_doc(scope, target_id, payload)
        summary = None
        if target_doc:
            self._apply_event_summary(target_doc, failure=True)
            container.upsert_item(target_doc)
            summary = self._doc_summary(target_doc)
        self._record_event(scope, event)
        return {"ok": True, "event": event, "targetSummary": summary}

    def memory_add_correction_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        target_id = str(payload.get("targetId") or "").strip()
        if not target_id:
            raise ValueError("targetId is required for correction events")
        scope = str(payload.get("scope") or "shared").lower()
        if scope not in {"shared", "personal"}:
            raise ValueError("scope must be shared or personal")
        correction_kind = str(payload.get("correctionKind") or "amended").strip().lower()
        extras = {
            "correctionKind": correction_kind,
            "reason": str(payload.get("reason") or ""),
            "correctedById": payload.get("correctedById"),
            "newVersionId": payload.get("newVersionId"),
            "relatedDisagreementId": payload.get("relatedDisagreementId"),
            "changes": payload.get("changes"),
            "runId": payload.get("runId"),
            "evidence": self._normalize_event_list(payload.get("evidence")),
        }
        event = self._build_event_document("correction_event", scope, payload, extras)
        container = self._get_container_for_scope(scope)
        target_doc = self._load_event_target_doc(scope, target_id, payload)
        summary = None
        if target_doc:
            self._apply_event_summary(target_doc, correction=True, resolution_state=RESOLUTION_RESOLVED)
            container.upsert_item(target_doc)
            summary = self._doc_summary(target_doc)
        self._record_event(scope, event)
        return {"ok": True, "event": event, "targetSummary": summary}

    def memory_add_resolution_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        target_id = str(payload.get("targetId") or "").strip()
        if not target_id:
            raise ValueError("targetId is required for resolution events")
        scope = str(payload.get("scope") or "shared").lower()
        if scope not in {"shared", "personal"}:
            raise ValueError("scope must be shared or personal")
        verdict = str(payload.get("verdict") or "ambiguous").strip().lower()
        if verdict not in {"confirmed", "refuted", "ambiguous"}:
            raise ValueError("verdict must be confirmed|refuted|ambiguous")
        delta = float(payload.get("trustDelta") or 0.0)
        dimension_deltas = self._normalize_dimension_deltas(payload.get("dimensionDeltas"))
        extras = {
            "verdict": verdict,
            "disagreementId": payload.get("disagreementId"),
            "chosenOutcome": payload.get("chosenOutcome"),
            "losingOutcomes": payload.get("losingOutcomes"),
            "evidence": self._normalize_event_list(payload.get("evidence")),
            "runId": payload.get("runId"),
            "trustDelta": delta,
            "dimensionDeltas": dimension_deltas,
            "reason": str(payload.get("reason") or ""),
        }
        event = self._build_event_document("resolution_event", scope, payload, extras)
        container = self._get_container_for_scope(scope)
        target_doc = self._load_event_target_doc(scope, target_id, payload)
        summary = None
        if target_doc:
            self._apply_event_summary(target_doc, resolution_state=RESOLUTION_RESOLVED)
            if verdict == "confirmed":
                self._apply_trust_delta(target_doc, delta=-abs(delta), dimension_deltas=dimension_deltas)
            elif verdict == "refuted":
                self._apply_trust_delta(target_doc, delta=abs(delta), dimension_deltas=dimension_deltas)
            container.upsert_item(target_doc)
            summary = self._doc_summary(target_doc)
        self._record_event(scope, event)
        return {"ok": True, "event": event, "targetSummary": summary}

    def memory_add_trust_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        target_id = str(payload.get("targetId") or "").strip()
        if not target_id:
            raise ValueError("targetId is required for trust events")
        scope = str(payload.get("scope") or "shared").lower()
        if scope not in {"shared", "personal"}:
            raise ValueError("scope must be shared or personal")
        event_kind = str(payload.get("eventKind") or "reinforced")
        delta = float(payload.get("trustDelta") or 0.0)
        dimension_deltas = self._normalize_dimension_deltas(payload.get("dimensionDeltas"))
        extras = {
            "eventKind": event_kind,
            "reason": str(payload.get("reason") or ""),
            "runId": payload.get("runId"),
            "dimensionDeltas": dimension_deltas,
            "trustDelta": delta,
        }
        event = self._build_event_document("trust_event", scope, payload, extras)
        container = self._get_container_for_scope(scope)
        target_doc = self._load_event_target_doc(scope, target_id, payload)
        summary = None
        if target_doc and not target_doc.get("hasPendingResolution"):
            self._apply_trust_delta(target_doc, delta=delta, dimension_deltas=dimension_deltas)
            container.upsert_item(target_doc)
            summary = self._doc_summary(target_doc)
        self._record_event(scope, event)
        return {"ok": True, "event": event, "targetSummary": summary}

    def _record_retrieval_feedback(self, selected: list[tuple[dict[str, Any], str]]) -> list[dict[str, Any]]:
        promotions = []
        for doc, scope in selected:
            self._touch_reference(doc, scope)
            promotion = self._evaluate_promotion(doc)
            if promotion["changed"]:
                doc["promotionStatus"] = promotion["proposed"]
                doc["updatedAt"] = now_iso()
                self._refresh_doc_trust(doc)
                try:
                    self._get_container_for_scope(scope).upsert_item(doc)
                except Exception:
                    continue
                promotions.append(
                    {
                        "id": doc.get("id"),
                        "scope": scope,
                        "from": promotion["current"],
                        "to": promotion["proposed"],
                        "reasons": promotion["reasons"],
                    }
                )
        return promotions

    def _compare_docs(self, existing: dict[str, Any], incoming: dict[str, Any]) -> list[dict[str, Any]]:
        interesting = [
            "kind",
            "projectId",
            "memoryScope",
            "memoryClass",
            "status",
            "promotionStatus",
            "content",
            "summary",
            "title",
            "taskState",
            "priority",
            "tags",
            "visibility",
            "retentionDays",
            "importance",
            "confidence",
            "trustScore",
            "trustDimensions",
            "artifactRef",
            "derivedFromIds",
            "supersedesId",
        ]
        diffs = []
        for field in interesting:
            if existing.get(field) != incoming.get(field):
                diffs.append({"field": field, "existing": existing.get(field), "incoming": incoming.get(field)})
        return diffs

    def _extract_run_candidates(self, doc: dict[str, Any], extract_scope: str) -> list[dict[str, Any]]:
        combined = "\n".join(
            line.strip()
            for line in [str(doc.get("request") or ""), str(doc.get("summary") or "")]
            if line.strip()
        )
        candidates: list[dict[str, Any]] = []
        if not combined:
            return candidates
        for raw_line in re.split(r"[\r\n]+", combined):
            line = raw_line.strip(" -\t")
            if not line:
                continue
            lowered = line.lower()
            if any(token in lowered for token in ["todo", "follow up", "follow-up", "next step", "need to", "remaining"]):
                candidates.append(
                    {
                        "id": f"derived:task:{doc['id']}:{len(candidates)}",
                        "scope": extract_scope,
                        "memoryScope": extract_scope,
                        "memoryClass": "semantic",
                        "promotionStatus": "candidate",
                        "derivedFromIds": [doc["id"]],
                        "title": compact_text(line, 180),
                        "kind": "task",
                        "taskState": "open",
                        "priority": 2,
                        "tags": ["derived", "run_summary"],
                    }
                )
            elif any(token in lowered for token in ["decided", "decision", "switched", "standardized", "moved to"]):
                candidates.append(
                    {
                        "id": f"derived:decision:{doc['id']}:{len(candidates)}",
                        "scope": extract_scope,
                        "memoryScope": extract_scope,
                        "memoryClass": "semantic",
                        "promotionStatus": "candidate",
                        "derivedFromIds": [doc["id"]],
                        "kind": "decision",
                        "content": compact_text(line, 240),
                        "tags": ["derived", "run_summary", "decision"],
                    }
                )
            elif any(token in lowered for token in ["confirmed", "verified", "validated", "identified", "found", "documented"]):
                candidates.append(
                    {
                        "id": f"derived:fact:{doc['id']}:{len(candidates)}",
                        "scope": extract_scope,
                        "memoryScope": extract_scope,
                        "memoryClass": "semantic",
                        "promotionStatus": "candidate",
                        "derivedFromIds": [doc["id"]],
                        "kind": "fact",
                        "content": compact_text(line, 240),
                        "tags": ["derived", "run_summary", "fact"],
                    }
                )
        return candidates[:8]

    def _persist_extracted_candidates(self, candidates: list[dict[str, Any]], root_doc: dict[str, Any], actor: str) -> list[dict[str, Any]]:
        persisted = []
        for candidate in candidates:
            scope = candidate.get("scope", "personal")
            container = self._get_container_for_scope(scope)
            doc = self._base_doc(
                {
                    **candidate,
                    "userId": root_doc["userId"],
                    "workspaceId": root_doc["workspaceId"],
                    "projectId": root_doc.get("projectId"),
                    "repoId": root_doc.get("repoId"),
                    "source": "auto_extract",
                    "importance": 0.7,
                    "confidence": 0.6,
                    "embeddingStatus": "pending",
                }
            )
            if doc["kind"] == "task":
                doc["title"] = redact_text(str(candidate.get("title", "")))
                doc["taskState"] = candidate.get("taskState", "open")
                doc["priority"] = int(candidate.get("priority", 2))
                doc["canonicalHash"] = canonical_hash(doc["kind"], doc["title"])
                text = doc["title"]
            else:
                doc["content"] = redact_text(str(candidate.get("content", "")))
                doc["canonicalHash"] = canonical_hash(doc["kind"], doc["content"])
                text = doc["content"]
            result = self._write_memory_record(container, doc, scope, text, f"memory_auto_extract_{doc['kind']}", actor)
            persisted.append({"id": doc["id"], "kind": doc["kind"], "scope": scope, "embeddingStatus": result.get("embeddingStatus")})
        return persisted

    def _search_embeddings(self, workspace_id: str, query_text: str, top_k: int) -> dict[str, Any]:
        embedded = self.embed(query_text)
        q_vec = embedded["vector"]
        candidates = self._query_container_docs(
            self.emb,
            "SELECT * FROM c WHERE c.workspaceId=@ws",
            [{"name": "@ws", "value": workspace_id}],
        )
        scored = []
        for candidate in candidates:
            vec = candidate.get("vector") or []
            if not isinstance(vec, list):
                continue
            vector_score = cosine([float(item) for item in q_vec], [float(item) for item in vec])
            lexical_score = keyword_overlap(query_text, candidate.get("text", ""))
            score = (vector_score * 0.85) + (lexical_score * 0.15)
            scored.append(
                {
                    "id": candidate.get("id"),
                    "score": score,
                    "vectorScore": vector_score,
                    "lexicalScore": lexical_score,
                    "text": candidate.get("text", ""),
                    "sourceRefId": candidate.get("sourceRefId"),
                    "tags": candidate.get("tags", []),
                    "kind": candidate.get("kind"),
                    "sourceEmbeddingMode": ((candidate.get("vectorMeta") or {}).get("embeddingMode")),
                }
            )
        scored.sort(key=lambda item: item["score"], reverse=True)
        return {
            "embedded": embedded,
            "items": scored[:top_k],
        }

    def memory_add_fact(self, payload: dict[str, Any]) -> dict[str, Any]:
        scope = (payload.get("scope") or "shared").lower()
        self._ensure_project_exists(str(payload.get("workspaceId") or ""), str(payload.get("projectId") or ""))
        container = self._get_container_for_scope(scope)
        doc = self._base_doc(payload)
        doc["kind"] = payload.get("kind", "fact")
        doc["content"] = redact_text(str(payload.get("content", "")))
        doc["sourceMeta"] = payload.get("sourceMeta")
        doc["canonicalHash"] = canonical_hash(doc["kind"], doc["content"])
        if not self._should_persist(payload):
            return self._transient_result(doc, scope, "memory_add_fact", payload.get("actor", "unknown"))
        return self._write_memory_record(container, doc, scope, doc["content"], "memory_add_fact", payload.get("actor", "unknown"))

    def memory_add_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_project_exists(str(payload.get("workspaceId") or ""), str(payload.get("projectId") or ""))
        doc = self._base_doc(payload)
        doc["kind"] = payload.get("kind", "run_summary")
        doc["status"] = payload.get("status", "completed")
        doc["memoryClass"] = payload.get("memoryClass", "episodic")
        doc["memoryScope"] = payload.get("memoryScope") or default_memory_scope(doc["kind"], "personal")
        doc["request"] = redact_text(str(payload.get("request", "")))
        doc["summary"] = redact_text(str(payload.get("summary", "")))
        doc["branch"] = payload.get("branch")
        doc["cwd"] = payload.get("cwd")
        doc["canonicalHash"] = canonical_hash(doc["kind"], f"{doc['request']}\n{doc['summary']}")
        if not self._should_persist(payload):
            return self._transient_result(doc, "personal", "memory_add_run", payload.get("actor", "unknown"))
        result = self._write_memory_record(self.personal, doc, "personal", f"{doc['request']}\n{doc['summary']}", "memory_add_run", payload.get("actor", "unknown"))
        auto_extract = parse_bool(payload.get("autoExtract"), self.auto_extract_default)
        if auto_extract:
            extract_scope = str(payload.get("extractScope", "personal")).lower()
            candidates = self._extract_run_candidates(doc, extract_scope)
            result["autoExtracted"] = self._persist_extracted_candidates(candidates, doc, payload.get("actor", "unknown"))
        return result

    def memory_add_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        scope = (payload.get("scope") or "shared").lower()
        self._ensure_project_exists(str(payload.get("workspaceId") or ""), str(payload.get("projectId") or ""))
        container = self._get_container_for_scope(scope)
        doc = self._base_doc(payload)
        doc["kind"] = "task"
        doc["title"] = redact_text(str(payload.get("title", "")))
        doc["taskState"] = payload.get("taskState", "open")
        doc["priority"] = int(payload.get("priority", 2))
        doc["canonicalHash"] = canonical_hash(doc["kind"], doc["title"])
        if not self._should_persist(payload):
            return self._transient_result(doc, scope, "memory_add_task", payload.get("actor", "unknown"))
        return self._write_memory_record(container, doc, scope, doc["title"], "memory_add_task", payload.get("actor", "unknown"))

    def memory_add_artifact(self, payload: dict[str, Any]) -> dict[str, Any]:
        scope = (payload.get("scope") or "shared").lower()
        self._ensure_project_exists(str(payload.get("workspaceId") or ""), str(payload.get("projectId") or ""))
        container = self._get_container_for_scope(scope)
        doc = self._base_doc(payload)
        doc["kind"] = "artifact"
        doc["memoryClass"] = payload.get("memoryClass") or "episodic"
        doc["memoryScope"] = payload.get("memoryScope") or default_memory_scope(doc["kind"], scope)
        doc["promotionStatus"] = payload.get("promotionStatus") or "candidate"
        doc["title"] = redact_text(str(payload.get("title") or ""))
        doc["artifactType"] = str(payload.get("artifactType") or "")
        doc["artifactRef"] = str(payload.get("artifactRef") or doc.get("artifactRef") or "")
        doc["content"] = redact_text(str(payload.get("content") or payload.get("summary") or ""))
        canonical_text = f"{doc.get('title')}\n{doc.get('artifactRef')}\n{doc.get('content')}"
        doc["canonicalHash"] = canonical_hash(doc["kind"], canonical_text)
        if not self._should_persist(payload):
            return self._transient_result(doc, scope, "memory_add_artifact", payload.get("actor", "unknown"))
        return self._write_memory_record(container, doc, scope, canonical_text, "memory_add_artifact", payload.get("actor", "unknown"))

    def memory_close_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        scope = (payload.get("scope") or "shared").lower()
        container = self._get_container_for_scope(scope)
        task_id = payload.get("id")
        if not task_id:
            raise ValueError("id is required")
        partition_key = payload.get("workspaceId") if scope == "shared" else payload.get("userId")
        doc = container.read_item(item=task_id, partition_key=partition_key)
        doc["taskState"] = "closed"
        doc["updatedAt"] = now_iso()
        doc["closedAt"] = now_iso()
        container.upsert_item(doc)
        self._audit(doc["workspaceId"], payload.get("actor", "unknown"), "memory_close_task", f"{scope}_memory", f"close {task_id}")
        return {"ok": True, "id": task_id, "scope": scope}

    def memory_get_personal(self, payload: dict[str, Any]) -> dict[str, Any]:
        user_id = payload.get("userId")
        if not user_id:
            raise ValueError("userId is required")
        limit = int(payload.get("limit", 50))
        items = self._query_container_docs(
            self.personal,
            "SELECT TOP @lim * FROM c WHERE c.userId=@uid AND (NOT IS_DEFINED(c.kind) OR c.kind != 'project') ORDER BY c.updatedAt DESC",
            [{"name": "@lim", "value": limit}, {"name": "@uid", "value": user_id}],
        )
        return {"ok": True, "items": items}

    def memory_get_shared(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = payload.get("workspaceId")
        if not workspace_id:
            raise ValueError("workspaceId is required")
        limit = int(payload.get("limit", 50))
        items = self._query_container_docs(
            self.shared,
            "SELECT TOP @lim * FROM c WHERE c.workspaceId=@ws AND (NOT IS_DEFINED(c.kind) OR c.kind != 'project') ORDER BY c.updatedAt DESC",
            [{"name": "@lim", "value": limit}, {"name": "@ws", "value": workspace_id}],
        )
        return {"ok": True, "items": items}

    def project_upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = payload.get("workspaceId")
        if not workspace_id:
            raise ValueError("workspaceId is required")
        name = str(payload.get("name") or "").strip()
        slug = self._normalize_slug(str(payload.get("slug") or name))
        if not slug:
            raise ValueError("name or slug is required")
        project_id = self._project_id(workspace_id, slug)
        existing = self._query_by_id(self.shared, project_id, "workspaceId", workspace_id)
        now = now_iso()
        repos = [str(item).strip() for item in (payload.get("repos") or []) if str(item).strip()]
        tags = [str(item).strip() for item in (payload.get("tags") or []) if str(item).strip()]
        doc = {
            "id": project_id,
            "kind": "project",
            "workspaceId": workspace_id,
            "slug": slug,
            "name": name or (existing or {}).get("name") or slug,
            "description": str(payload.get("description") or (existing or {}).get("description") or ""),
            "repos": repos or (existing or {}).get("repos") or [],
            "tags": tags or (existing or {}).get("tags") or [],
            "status": str(payload.get("status") or (existing or {}).get("status") or "active"),
            "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else (existing or {}).get("metadata") or {},
            "createdAt": (existing or {}).get("createdAt") or now,
            "createdBy": (existing or {}).get("createdBy") or payload.get("actor") or payload.get("userId") or "unknown",
            "updatedAt": now,
            "updatedBy": payload.get("actor") or payload.get("userId") or "unknown",
        }
        self.shared.upsert_item(doc)
        self._audit(workspace_id, payload.get("actor", "unknown"), "project_upsert", "shared_memory", f"project={project_id}")
        return {"ok": True, "id": project_id, "slug": slug, "created": existing is None, "item": doc}

    def project_get(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = payload.get("workspaceId")
        if not workspace_id:
            raise ValueError("workspaceId is required")
        project_id = str(payload.get("projectId") or "").strip()
        slug = self._normalize_slug(str(payload.get("slug") or ""))
        if not project_id and not slug:
            raise ValueError("projectId or slug is required")
        lookup_id = project_id or self._project_id(workspace_id, slug)
        item = self._query_by_id(self.shared, lookup_id, "workspaceId", workspace_id)
        if item is None or str(item.get("kind") or "") != "project":
            raise ValueError("project not found")
        return {"ok": True, "item": item}

    def project_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = payload.get("workspaceId")
        if not workspace_id:
            raise ValueError("workspaceId is required")
        status = str(payload.get("status") or "").strip().lower()
        limit = int(payload.get("limit", 50))
        if status:
            items = self._query_container_docs(
                self.shared,
                "SELECT TOP @lim * FROM c WHERE c.workspaceId=@ws AND c.kind='project' AND LOWER(c.status)=@status ORDER BY c.updatedAt DESC",
                [{"name": "@lim", "value": limit}, {"name": "@ws", "value": workspace_id}, {"name": "@status", "value": status}],
            )
        else:
            items = self._query_container_docs(
                self.shared,
                "SELECT TOP @lim * FROM c WHERE c.workspaceId=@ws AND c.kind='project' ORDER BY c.updatedAt DESC",
                [{"name": "@lim", "value": limit}, {"name": "@ws", "value": workspace_id}],
            )
        return {"ok": True, "count": len(items), "items": items}

    def project_archive(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = payload.get("workspaceId")
        if not workspace_id:
            raise ValueError("workspaceId is required")
        project_id = str(payload.get("projectId") or "").strip()
        if not project_id:
            raise ValueError("projectId is required")
        item = self._query_by_id(self.shared, project_id, "workspaceId", workspace_id)
        if item is None or str(item.get("kind") or "") != "project":
            raise ValueError("project not found")
        item["status"] = "archived"
        item["updatedAt"] = now_iso()
        item["updatedBy"] = payload.get("actor") or payload.get("userId") or "unknown"
        self.shared.upsert_item(item)
        self._audit(workspace_id, payload.get("actor", "unknown"), "project_archive", "shared_memory", f"project={project_id}")
        return {"ok": True, "id": project_id}

    def memory_list_open_tasks(self, payload: dict[str, Any]) -> dict[str, Any]:
        scope = (payload.get("scope") or "shared").lower()
        limit = int(payload.get("limit", 50))
        if scope == "shared":
            workspace_id = payload.get("workspaceId")
            if not workspace_id:
                raise ValueError("workspaceId is required for shared scope")
            items = self._query_container_docs(
                self.shared,
                "SELECT TOP @lim * FROM c WHERE c.kind='task' AND c.workspaceId=@ws AND (NOT IS_DEFINED(c.taskState) OR c.taskState='open') ORDER BY c.updatedAt DESC",
                [{"name": "@lim", "value": limit}, {"name": "@ws", "value": workspace_id}],
            )
        else:
            user_id = payload.get("userId")
            if not user_id:
                raise ValueError("userId is required for personal scope")
            items = self._query_container_docs(
                self.personal,
                "SELECT TOP @lim * FROM c WHERE c.kind='task' AND c.userId=@uid AND (NOT IS_DEFINED(c.taskState) OR c.taskState='open') ORDER BY c.updatedAt DESC",
                [{"name": "@lim", "value": limit}, {"name": "@uid", "value": user_id}],
            )
        return {"ok": True, "items": items, "scope": scope}

    def memory_add_audit_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = payload.get("workspaceId", "default")
        self._audit(
            workspace_id=workspace_id,
            actor=payload.get("actor", "unknown"),
            operation=payload.get("operation", "custom"),
            target=payload.get("targetContainer", payload.get("target", "n/a")),
            summary=payload.get("summary", ""),
            source=payload.get("source", "mcp"),
        )
        return {"ok": True}

    def memory_route_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = payload.get("workspaceId")
        if not workspace_id:
            raise ValueError("workspaceId is required")
        task_type = payload.get("taskType", "general")
        risk_level = payload.get("riskLevel", "low")
        has_canonical_context = bool(payload.get("hasCanonicalContext", False))
        has_external_dependency = bool(payload.get("hasExternalDependency", False))
        unresolved_disagreement = bool(payload.get("hasUnresolvedDisagreement", False))
        force_three_model_review = bool(payload.get("forceThreeModelReview", False))
        policy = route_review_policy(
            task_type=task_type,
            risk_level=risk_level,
            has_canonical_context=has_canonical_context,
            has_external_dependency=has_external_dependency,
            unresolved_disagreement=unresolved_disagreement,
            force_three_model_review=force_three_model_review,
        )
        return {"ok": True, **policy}

    def memory_add_disagreement(self, payload: dict[str, Any]) -> dict[str, Any]:
        scope = str(payload.get("scope") or "shared").lower()
        if scope not in {"shared", "personal"}:
            raise ValueError("scope must be shared or personal")
        workspace_id = str(payload.get("workspaceId") or "").strip()
        if not workspace_id:
            raise ValueError("workspaceId is required")
        if scope == "personal" and not str(payload.get("userId") or "").strip():
            raise ValueError("userId is required for personal scope")
        claim = redact_text(str(payload.get("claim") or "").strip())
        if not claim:
            raise ValueError("claim is required")
        doc = self._base_doc(
            {
                **payload,
                "kind": "disagreement",
                "memoryClass": payload.get("memoryClass") or "episodic",
                "promotionStatus": payload.get("promotionStatus") or "candidate",
                "importance": payload.get("importance", 0.7),
                "confidence": payload.get("confidence", 0.6),
            }
        )
        doc["claim"] = claim
        doc["taskType"] = _normalize_task_type(payload.get("taskType"))
        doc["riskLevel"] = _normalize_risk_level(payload.get("riskLevel"))
        doc["codexPosition"] = redact_text(str(payload.get("codexPosition") or ""))
        doc["claudePosition"] = redact_text(str(payload.get("claudePosition") or ""))
        doc["geminiPosition"] = redact_text(str(payload.get("geminiPosition") or ""))
        doc["resolution"] = redact_text(str(payload.get("resolution") or "pending"))
        doc["resolutionStatus"] = str(payload.get("resolutionStatus") or "pending")
        doc["correctModel"] = str(payload.get("correctModel") or "")
        doc["outcome"] = str(payload.get("outcome") or "pending")
        doc["evidence"] = redact_text(str(payload.get("evidence") or ""))
        doc["content"] = "\n".join(
            part
            for part in [doc["claim"], doc["codexPosition"], doc["claudePosition"], doc["geminiPosition"], doc["resolution"]]
            if part
        )
        doc["canonicalHash"] = canonical_hash(
            doc["kind"],
            f"{doc['claim']}|{doc['taskType']}|{doc['codexPosition']}|{doc['claudePosition']}|{doc['geminiPosition']}",
        )
        if not self._should_persist(payload):
            return self._transient_result(doc, scope, "memory_add_disagreement", payload.get("actor", "unknown"))
        container = self._get_container_for_scope(scope)
        stored = self._write_memory_record(
            container,
            doc,
            scope,
            "\n".join([doc["claim"], doc["codexPosition"], doc["claudePosition"], doc["geminiPosition"], doc["resolution"]]),
            "memory_add_disagreement",
            payload.get("actor", "unknown"),
        )
        return {**stored, "item": self._doc_summary(doc)}

    def memory_search_vectors(self, payload: dict[str, Any], emit_telemetry: bool = True) -> dict[str, Any]:
        workspace_id = payload.get("workspaceId")
        if not workspace_id:
            raise ValueError("workspaceId is required")
        user_id = payload.get("userId")
        query_text = payload.get("query") or payload.get("queryText") or ""
        top_k = int(payload.get("k", 8))
        project_filter = str(payload.get("projectId") or "").strip()
        project_scope_mode = normalize_project_scope_mode(payload)
        started = time.perf_counter()
        vector_limit = max(top_k * 10, top_k) if project_filter and project_scope_mode != "off" else top_k
        searched = self._search_embeddings(workspace_id, query_text, vector_limit)
        embedded = searched["embedded"]
        items = searched["items"]
        docs_by_source: dict[str, tuple[dict[str, Any], str]] = {}
        for item in items:
            source_ref = str(item.get("sourceRefId") or "")
            if not source_ref or source_ref in docs_by_source:
                continue
            doc, scope = self._load_source_doc(source_ref, workspace_id, user_id)
            if doc is None:
                continue
            docs_by_source[source_ref] = (doc, scope)
        filtered_items, project_fallback_used = apply_project_scope_envelope(
            items,
            docs_by_source,
            top_k,
            project_filter,
            project_scope_mode,
        )
        pruned_items: list[dict[str, Any]] = []
        for item in filtered_items:
            source_ref = str(item.get("sourceRefId") or "")
            if source_ref and source_ref in docs_by_source:
                doc, _ = docs_by_source[source_ref]
                skip, warnings, _ = self._evaluate_retrieval_outcome(doc, payload)
                if skip:
                    continue
                if warnings:
                    item_warnings = item.get("warnings") or []
                    item["warnings"] = item_warnings + warnings
            pruned_items.append(item)
        filtered_items = pruned_items
        top_vector_score = float(filtered_items[0]["vectorScore"]) if filtered_items else 0.0
        top_lexical_score = float(filtered_items[0]["lexicalScore"]) if filtered_items else 0.0
        final_top_score = float(filtered_items[0]["score"]) if filtered_items else 0.0
        latency_ms = int((time.perf_counter() - started) * 1000)
        telemetry_id = None
        if emit_telemetry:
            telemetry_id = self._log_retrieval(
                {
                    "workspaceId": workspace_id,
                    "repoId": payload.get("repoId"),
                    "userId": payload.get("userId"),
                    "operation": "memory_search_vectors",
                    "query": redact_text(query_text),
                    "embeddingMode": embedded["embeddingMode"],
                    "degraded": embedded["degraded"],
                    "warningCodes": embedded["warnings"],
                    "resultCount": len(filtered_items),
                    "itemsReturned": len(filtered_items),
                    "topVectorScore": top_vector_score,
                    "topLexicalScore": top_lexical_score,
                    "finalTopScore": final_top_score,
                    "budget": payload.get("budget"),
                    "latencyMs": latency_ms,
                    "projectFilter": project_filter,
                    "projectScopeMode": project_scope_mode,
                    "projectFallbackUsed": project_fallback_used,
                }
            )
        return {
            "ok": True,
            "embeddingMode": embedded["embeddingMode"],
            "degraded": embedded["degraded"],
            "warnings": embedded["warnings"],
            "telemetryId": telemetry_id,
            "latencyMs": latency_ms,
            "projectFilter": project_filter,
            "projectScopeMode": project_scope_mode,
            "projectFallbackUsed": project_fallback_used,
            "items": filtered_items,
        }

    def memory_search_summaries(self, payload: dict[str, Any], emit_telemetry: bool = True) -> dict[str, Any]:
        workspace_id = payload.get("workspaceId")
        if not workspace_id:
            raise ValueError("workspaceId is required")
        user_id = payload.get("userId")
        top_k = int(payload.get("k", 8))
        query_text = payload.get("query") or payload.get("queryText") or ""
        project_filter = str(payload.get("projectId") or "").strip()
        project_scope_mode = normalize_project_scope_mode(payload)
        intent = infer_retrieval_intent(str(query_text), payload.get("intent"))
        preferred_class = preferred_memory_class(intent, payload.get("preferredMemoryClass"))
        started = time.perf_counter()
        repo_project_index = self._load_repo_project_index(workspace_id)
        raw = self.memory_search_vectors({**payload, "k": max(top_k * 4, top_k), "projectScopeMode": "off"}, emit_telemetry=False)
        items = []
        docs_by_source: dict[str, tuple[dict[str, Any], str]] = {}
        for hit in raw.get("items", []):
            source_ref_id = hit.get("sourceRefId")
            if not source_ref_id:
                continue
            doc, scope = self._load_source_doc(source_ref_id, workspace_id, user_id)
            if doc is None:
                continue
            docs_by_source[str(source_ref_id)] = (doc, scope)
            item = self._doc_summary(doc, score=hit.get("score"), match_text=hit.get("text", ""))
            item["scope"] = scope
            item["sourceRefId"] = source_ref_id
            item["whyMatched"] = compact_text(hit.get("text", ""), 160)
            item["vectorScore"] = hit.get("vectorScore")
            item["lexicalScore"] = hit.get("lexicalScore")
            skip, warnings, _ = self._evaluate_retrieval_outcome(doc, payload)
            if skip:
                continue
            if warnings:
                item["retrievalWarnings"] = warnings
            items.append(self._rerank_summary_item(item, doc, scope, payload, repo_project_index))
        deduped = self._dedupe_summary_items(items)
        returned, project_fallback_used = apply_project_scope_envelope(
            deduped,
            docs_by_source,
            top_k,
            project_filter,
            project_scope_mode,
        )
        gate_eval = evaluate_summary_gate(returned, str(query_text))
        gate_allowed = bool(gate_eval.get("allow"))
        gate_suppressed = not gate_allowed
        if gate_suppressed:
            returned = []
        referenced_docs = [docs_by_source[str(item.get("sourceRefId"))] for item in returned if str(item.get("sourceRefId")) in docs_by_source]
        promotions = self._record_retrieval_feedback(referenced_docs)
        for item in returned:
            source_ref_id = str(item.get("sourceRefId") or "")
            if source_ref_id in docs_by_source:
                doc, _ = docs_by_source[source_ref_id]
                item["referenceCount"] = int(doc.get("referenceCount", 0) or 0)
                item["lastReferenced"] = doc.get("lastReferenced")
                item["trustScore"] = clamp_score(doc.get("trustScore"), clamp_score(doc.get("confidence"), DEFAULT_CONFIDENCE))
                item["trustDimensions"] = clamp_dimension_map(doc.get("trustDimensions"))
                item["promotionStatus"] = doc.get("promotionStatus")
                item["failureCount"] = int(doc.get("failureCount", 0) or 0)
                item["hasPendingResolution"] = bool(doc.get("hasPendingResolution"))
                item["resolutionState"] = doc.get("resolutionState")
                item.setdefault("retrievalWarnings", [])
        latency_ms = int((time.perf_counter() - started) * 1000)
        telemetry_id = None
        if emit_telemetry:
            if gate_suppressed:
                self._log_retrieval(
                    {
                        "workspaceId": workspace_id,
                        "repoId": payload.get("repoId"),
                        "userId": payload.get("userId"),
                        "operation": "memory_search_summaries_gate",
                        "query": redact_text(payload.get("query") or payload.get("queryText") or ""),
                        "embeddingMode": raw.get("embeddingMode"),
                        "degraded": raw.get("degraded"),
                        "warningCodes": raw.get("warnings", []),
                        "resultCount": len(deduped),
                        "itemsReturned": 0,
                        "topVectorScore": float(gate_eval.get("topVectorScore") or 0.0),
                        "topLexicalScore": float(gate_eval.get("topLexicalScore") or 0.0),
                        "finalTopScore": float(gate_eval.get("topScore") or 0.0),
                        "intent": intent,
                        "preferredMemoryClass": preferred_class,
                        "budget": payload.get("budget"),
                        "latencyMs": latency_ms,
                        "projectFilter": project_filter,
                        "projectScopeMode": project_scope_mode,
                        "projectFallbackUsed": project_fallback_used,
                        "gateSuppressed": True,
                        "gateReason": gate_eval.get("reason"),
                        "gateThresholds": gate_eval.get("thresholds"),
                    }
                )
            telemetry_id = self._log_retrieval(
                {
                    "workspaceId": workspace_id,
                    "repoId": payload.get("repoId"),
                    "userId": payload.get("userId"),
                    "operation": "memory_search_summaries",
                    "query": redact_text(payload.get("query") or payload.get("queryText") or ""),
                    "embeddingMode": raw.get("embeddingMode"),
                    "degraded": raw.get("degraded"),
                    "warningCodes": raw.get("warnings", []),
                    "resultCount": len(deduped),
                    "itemsReturned": len(returned),
                    "topVectorScore": float(returned[0]["vectorScore"]) if returned else 0.0,
                    "topLexicalScore": float(returned[0]["lexicalScore"]) if returned else 0.0,
                    "finalTopScore": float(returned[0]["score"]) if returned else 0.0,
                    "intent": intent,
                    "preferredMemoryClass": preferred_class,
                    "budget": payload.get("budget"),
                    "latencyMs": latency_ms,
                    "promotionCount": len(promotions),
                    "projectFilter": project_filter,
                    "projectScopeMode": project_scope_mode,
                    "projectFallbackUsed": project_fallback_used,
                    "gateSuppressed": gate_suppressed,
                    "gateReason": gate_eval.get("reason"),
                    "gateThresholds": gate_eval.get("thresholds"),
                }
            )
        return {
            "ok": True,
            "embeddingMode": raw.get("embeddingMode"),
            "degraded": raw.get("degraded"),
            "warnings": raw.get("warnings", []),
            "telemetryId": telemetry_id,
            "latencyMs": latency_ms,
            "intent": intent,
            "preferredMemoryClass": preferred_class,
            "items": returned,
            "rawHitCount": len(items),
            "dedupedHitCount": len(deduped),
            "promotions": promotions,
            "projectFilter": project_filter,
            "projectScopeMode": project_scope_mode,
            "projectFallbackUsed": project_fallback_used,
        }

    def memory_get_items(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = payload.get("workspaceId")
        if not workspace_id:
            raise ValueError("workspaceId is required")
        user_id = payload.get("userId")
        ids = payload.get("ids") or []
        if not isinstance(ids, list) or not ids:
            raise ValueError("ids must be a non-empty list")
        include_content = bool(payload.get("includeContent", True))
        items = []
        for doc_id in ids:
            doc, scope = self._load_source_doc(str(doc_id), workspace_id, user_id)
            if doc is None:
                continue
            self._touch_reference(doc, scope)
            hydrated = dict(doc) if include_content else self._doc_summary(doc)
            hydrated["scope"] = scope
            items.append(hydrated)
        return {"ok": True, "items": items}

    def memory_build_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = payload.get("workspaceId")
        if not workspace_id:
            raise ValueError("workspaceId is required")
        budget = str(payload.get("budget", "small")).lower()
        if budget not in {"small", "medium", "full"}:
            raise ValueError("budget must be one of: small, medium, full")
        include_items = parse_bool(payload.get("includeItems"), default=True)
        limit_map = {"small": 4, "medium": 8, "full": 12}
        text_limit_map = {"small": 700, "medium": 1600, "full": 3200}
        started = time.perf_counter()
        summaries = self.memory_search_summaries({**payload, "k": limit_map[budget]}, emit_telemetry=False)
        selected = summaries.get("items", [])
        lines = []
        tags = Counter()
        used = 0
        max_chars = text_limit_map[budget]
        for item in selected:
            for tag in item.get("tags", []):
                tags[str(tag)] += 1
            line = f"- [{item.get('kind')}] {item.get('title') or item.get('excerpt')}"
            excerpt = item.get("excerpt")
            if excerpt and excerpt not in line:
                line = f"{line}: {excerpt}"
            if item.get("whyMatched"):
                line = f"{line} | why: {item['whyMatched']}"
            if item.get("rankingSignals"):
                line = f"{line} | rank: {item['rankingSignals']}"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
        latency_ms = int((time.perf_counter() - started) * 1000)
        telemetry_id = self._log_retrieval(
            {
                "workspaceId": workspace_id,
                "repoId": payload.get("repoId"),
                "userId": payload.get("userId"),
                "operation": "memory_build_context",
                "query": redact_text(payload.get("query") or payload.get("queryText") or ""),
                "embeddingMode": summaries.get("embeddingMode"),
                "degraded": summaries.get("degraded"),
                "warningCodes": summaries.get("warnings", []),
                "resultCount": len(selected),
                "itemsReturned": len(lines),
                "topVectorScore": float(selected[0]["vectorScore"]) if selected else 0.0,
                "topLexicalScore": float(selected[0]["lexicalScore"]) if selected else 0.0,
                "finalTopScore": float(selected[0]["score"]) if selected else 0.0,
                "intent": summaries.get("intent"),
                "preferredMemoryClass": summaries.get("preferredMemoryClass"),
                "budget": budget,
                "latencyMs": latency_ms,
                "promotionCount": len(summaries.get("promotions", [])),
                "projectScopeMode": summaries.get("projectScopeMode", "off"),
                "projectFallbackUsed": bool(summaries.get("projectFallbackUsed")),
                "itemsIncluded": include_items,
            }
        )
        warnings = list(summaries.get("warnings", []))
        if summaries.get("degraded") and "embedding_quality_degraded" not in warnings:
            warnings.insert(0, "embedding_quality_degraded")
        return {
            "ok": True,
            "budget": budget,
            "embeddingMode": summaries.get("embeddingMode"),
            "degraded": summaries.get("degraded"),
            "warnings": warnings,
            "telemetryId": telemetry_id,
            "latencyMs": latency_ms,
            "intent": summaries.get("intent"),
            "preferredMemoryClass": summaries.get("preferredMemoryClass"),
            "items": selected if include_items else [],
            "itemsIncluded": include_items,
            "context": "\n".join(lines),
            "tagSummary": [tag for tag, _ in tags.most_common(8)],
            "itemCount": len(selected),
            "promotions": summaries.get("promotions", []),
            "projectScopeMode": summaries.get("projectScopeMode", "off"),
            "projectFallbackUsed": bool(summaries.get("projectFallbackUsed")),
        }

    def memory_get_retrieval_logs(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = payload.get("workspaceId")
        if not workspace_id:
            raise ValueError("workspaceId is required")
        limit = int(payload.get("limit", 200))
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if limit > 1000:
            limit = 1000
        since_hours = int(payload.get("sinceHours", 24))
        if since_hours < 1:
            since_hours = 1
        operation_filter = str(payload.get("operation") or "").strip()
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=since_hours)).isoformat()
        query = (
            "SELECT TOP @lim * FROM c WHERE c.workspaceId=@ws "
            "AND ((IS_DEFINED(c.createdAt) AND c.createdAt>=@since) OR "
            "(NOT IS_DEFINED(c.createdAt) AND IS_DEFINED(c.timestamp) AND c.timestamp>=@since))"
        )
        parameters = [
            {"name": "@lim", "value": limit},
            {"name": "@ws", "value": workspace_id},
            {"name": "@since", "value": since},
        ]
        if operation_filter:
            query += " AND c.operation=@op"
            parameters.append({"name": "@op", "value": operation_filter})
        query += " ORDER BY c.createdAt DESC"
        items = self._query_container_docs(self.retrieval, query, parameters)
        return {
            "ok": True,
            "limit": limit,
            "sinceHours": since_hours,
            "operation": operation_filter,
            "itemCount": len(items),
            "items": items,
        }

    def memory_get_audit_events(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = payload.get("workspaceId")
        if not workspace_id:
            raise ValueError("workspaceId is required")
        limit = int(payload.get("limit", 200))
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if limit > 1000:
            limit = 1000
        since_hours = int(payload.get("sinceHours", 24))
        if since_hours < 1:
            since_hours = 1
        operation_filter = str(payload.get("operation") or "").strip()
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=since_hours)).isoformat()
        query = (
            "SELECT TOP @lim * FROM c WHERE c.workspaceId=@ws "
            "AND ((IS_DEFINED(c.createdAt) AND c.createdAt>=@since) OR "
            "(NOT IS_DEFINED(c.createdAt) AND IS_DEFINED(c.timestamp) AND c.timestamp>=@since))"
        )
        parameters = [
            {"name": "@lim", "value": limit},
            {"name": "@ws", "value": workspace_id},
            {"name": "@since", "value": since},
        ]
        if operation_filter:
            query += " AND c.operation=@op"
            parameters.append({"name": "@op", "value": operation_filter})
        query += " ORDER BY c.createdAt DESC"
        items = self._query_container_docs(self.audit, query, parameters)
        return {
            "ok": True,
            "limit": limit,
            "sinceHours": since_hours,
            "operation": operation_filter,
            "itemCount": len(items),
            "items": items,
        }

    def memory_export(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = payload.get("workspaceId")
        if not workspace_id:
            raise ValueError("workspaceId is required")
        user_id = payload.get("userId")
        scope = str(payload.get("scope", "all")).lower()
        limit = int(payload.get("limit", 200))
        include_embeddings = bool(payload.get("includeEmbeddings", False))
        items = self._list_docs(scope, workspace_id, user_id, limit)
        response = {"ok": True, "scope": scope, "itemCount": len(items), "items": items}
        if include_embeddings:
            response["embeddings"] = self._query_container_docs(
                self.emb,
                "SELECT TOP @lim * FROM c WHERE c.workspaceId=@ws ORDER BY c.updatedAt DESC",
                [{"name": "@lim", "value": limit}, {"name": "@ws", "value": workspace_id}],
            )
        return response

    def memory_import(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = payload.get("workspaceId")
        if not workspace_id:
            raise ValueError("workspaceId is required")
        user_id = payload.get("userId")
        items = payload.get("items") or []
        if not isinstance(items, list) or not items:
            raise ValueError("items must be a non-empty list")
        mode = str(payload.get("mode", "upsert")).lower()
        if mode not in {"upsert", "skip_existing", "detect_conflicts"}:
            raise ValueError("mode must be one of: upsert, skip_existing, detect_conflicts")
        imported = 0
        skipped = 0
        conflicts = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            doc = dict(raw)
            doc.setdefault("workspaceId", workspace_id)
            doc.setdefault("userId", user_id or doc.get("userId") or "unknown")
            doc.setdefault("updatedAt", now_iso())
            doc.setdefault("createdAt", doc["updatedAt"])
            doc.setdefault("importance", DEFAULT_IMPORTANCE)
            doc.setdefault("confidence", DEFAULT_CONFIDENCE)
            scope = str(doc.get("scope", "shared")).lower()
            container = self._get_container_for_scope(scope)
            partition_key = workspace_id if scope == "shared" else doc.get("userId")
            existing = self._query_by_id(container, str(doc.get("id")), self._get_partition_key_name(scope), partition_key)
            if mode == "skip_existing" and existing is not None:
                skipped += 1
                continue
            if mode == "detect_conflicts":
                if existing is None:
                    continue
                diffs = self._compare_docs(existing, doc)
                if diffs:
                    conflicts.append({"id": doc.get("id"), "scope": scope, "fields": diffs})
                continue
            doc["canonicalHash"] = self._doc_canonical_hash(doc)
            container.upsert_item(doc)
            imported += 1
        self._audit(workspace_id, payload.get("actor", "unknown"), "memory_import", "memory", f"imported={imported} skipped={skipped} conflicts={len(conflicts)} mode={mode}")
        return {"ok": True, "imported": imported, "skipped": skipped, "conflicts": conflicts, "mode": mode}

    def memory_rebuild_embeddings(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = payload.get("workspaceId")
        if not workspace_id:
            raise ValueError("workspaceId is required")
        user_id = payload.get("userId")
        scope = str(payload.get("scope", "all")).lower()
        limit = int(payload.get("limit", 100))
        docs = self._list_docs(scope, workspace_id, user_id, limit)
        rebuilt = 0
        warnings = []
        for doc in docs:
            if str(doc.get("kind")) == "task":
                continue
            text = self._doc_primary_text(doc)
            if not text:
                continue
            scope_name = str(doc.get("_memoryScope") or ("shared" if scope == "shared" else "personal"))
            if self.embedding_write_mode == "queue":
                _, enqueue_warnings = self._queue_embedding_job(doc, scope_name, text)
                if enqueue_warnings:
                    warnings.extend(enqueue_warnings)
            else:
                embedding = self._store_embedding(
                    workspace_id=doc.get("workspaceId", workspace_id),
                    user_id=doc.get("userId", user_id or "unknown"),
                    repo_id=doc.get("repoId"),
                    ref_id=str(doc.get("id")),
                    text=text,
                    tags=doc.get("tags", []),
                    source=doc.get("source", "function"),
                )
                self._update_embedding_state(doc, scope_name, "ready", mode=embedding["embeddingMode"], warnings=embedding["warnings"])
                warnings.extend(embedding["warnings"])
            rebuilt += 1
        self._audit(workspace_id, payload.get("actor", "unknown"), "memory_rebuild_embeddings", "embeddings", f"rebuilt={rebuilt} scope={scope}")
        return {"ok": True, "scope": scope, "rebuilt": rebuilt, "warnings": warnings}

    def memory_get_stats(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = payload.get("workspaceId")
        if not workspace_id:
            raise ValueError("workspaceId is required")
        user_id = payload.get("userId")
        scope = str(payload.get("scope", "all")).lower()
        limit = int(payload.get("limit", 500))
        docs = self._list_docs(scope, workspace_id, user_id, limit)
        by_class = Counter()
        by_scope = Counter()
        by_promotion = Counter()
        candidate_ids = []
        for doc in docs:
            self._refresh_doc_trust(doc)
            by_class[str(doc.get("memoryClass") or "unknown")] += 1
            by_scope[str(doc.get("memoryScope") or "unknown")] += 1
            by_promotion[str(doc.get("promotionStatus") or "unknown")] += 1
            evaluation = self._evaluate_promotion(doc)
            if evaluation["changed"]:
                candidate_ids.append(
                    {
                        "id": doc.get("id"),
                        "kind": doc.get("kind"),
                        "memoryClass": doc.get("memoryClass"),
                        "promotionFrom": evaluation["current"],
                        "promotionTo": evaluation["proposed"],
                        "referenceCount": int(doc.get("referenceCount", 0) or 0),
                        "trustScore": clamp_score(doc.get("trustScore"), DEFAULT_CONFIDENCE),
                    }
                )
        candidate_ids.sort(key=lambda item: (item["promotionTo"], item["trustScore"], item["referenceCount"]), reverse=True)
        return {
            "ok": True,
            "scope": scope,
            "totalItems": len(docs),
            "memoryClassCounts": dict(by_class),
            "memoryScopeCounts": dict(by_scope),
            "promotionCounts": dict(by_promotion),
            "promotionCandidates": candidate_ids[:25],
        }

    def memory_auto_promote(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = payload.get("workspaceId")
        if not workspace_id:
            raise ValueError("workspaceId is required")
        user_id = payload.get("userId")
        scope = str(payload.get("scope", "all")).lower()
        limit = int(payload.get("limit", 500))
        dry_run = bool(payload.get("dryRun", False))
        docs = self._list_docs(scope, workspace_id, user_id, limit)
        promoted = []
        consolidated = []
        for doc in docs:
            self._refresh_doc_trust(doc)
            evaluation = self._evaluate_promotion(doc)
            if not evaluation["changed"]:
                continue
            record = {
                "id": doc.get("id"),
                "scope": str(doc.get("_memoryScope") or "shared"),
                "kind": doc.get("kind"),
                "memoryClass": doc.get("memoryClass"),
                "from": evaluation["current"],
                "to": evaluation["proposed"],
                "referenceCount": int(doc.get("referenceCount", 0) or 0),
                "trustScore": clamp_score(doc.get("trustScore"), DEFAULT_CONFIDENCE),
                "reasons": evaluation["reasons"],
            }
            if not dry_run:
                doc["promotionStatus"] = evaluation["proposed"]
                doc["updatedAt"] = now_iso()
                self._refresh_doc_trust(doc)
                container = self._get_container_for_scope(record["scope"])
                container.upsert_item(doc)
                consolidated_ids = self._consolidate_canonical_duplicates(container, record["scope"], doc, payload.get("actor", "unknown"))
                if consolidated_ids:
                    consolidated.append({"id": str(doc.get("id")), "scope": record["scope"], "supersededIds": consolidated_ids})
            promoted.append(record)
        self._audit(
            workspace_id,
            payload.get("actor", "unknown"),
            "memory_auto_promote",
            "memory",
            f"scope={scope} promoted={len(promoted)} dryRun={dry_run}",
        )
        return {
            "ok": True,
            "scope": scope,
            "dryRun": dry_run,
            "promoted": promoted,
            "promotionCount": len(promoted),
            "consolidated": consolidated,
        }


SERVICE: MemoryService | None = None


def get_service() -> MemoryService:
    global SERVICE
    if SERVICE is None:
        SERVICE = MemoryService()
    return SERVICE


def _parse_payload(req: func.HttpRequest) -> dict[str, Any]:
    if req.method == "GET":
        return {k: v for k, v in req.params.items()}
    try:
        return req.get_json() or {}
    except ValueError:
        return {}


def _json_response(code: int, payload: dict[str, Any]) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(payload, default=str), status_code=code, mimetype="application/json")


def _canonical_context(payload: dict[str, Any], timestamp: str, nonce: str = "") -> str:
    return "|".join(
        [
            str(payload.get("userId") or ""),
            str(payload.get("workspaceId") or ""),
            str(payload.get("repoId") or ""),
            str(timestamp or ""),
            str(nonce or ""),
        ]
    )


def _validate_signed_context(req: func.HttpRequest, payload: dict[str, Any], *, required: bool) -> bool:
    secret = os.environ.get("MEMORY_HMAC_SECRET", "").strip()
    if not parse_bool(os.environ.get("MEMORY_REQUIRE_SIGNED_CONTEXT"), default=True):
        if required:
            raise ValueError("shared-secret auth required but MEMORY_REQUIRE_SIGNED_CONTEXT is disabled")
        return False
    if not secret:
        if required:
            raise ValueError("shared-secret auth required but MEMORY_HMAC_SECRET is missing")
        return False
    timestamp = req.headers.get("x-codex-context-timestamp", "")
    signature = req.headers.get("x-codex-context-signature", "")
    nonce = req.headers.get("x-codex-context-nonce", "")
    require_nonce = parse_bool(os.environ.get("MEMORY_REQUIRE_SIGNED_NONCE"), default=False)
    if not timestamp or not signature:
        if required:
            raise ValueError("signed caller context is required")
        return False
    if require_nonce and not nonce:
        raise ValueError("signed caller context nonce is required")
    try:
        ts = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid caller context timestamp") from exc
    age = abs((dt.datetime.now(dt.timezone.utc) - ts).total_seconds())
    if age > 300:
        raise ValueError("caller context timestamp is stale")
    expected = hmac.new(
        secret.encode("utf-8"),
        _canonical_context(payload, timestamp, nonce).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise ValueError("invalid caller context signature")
    if nonce:
        now_epoch = dt.datetime.now(dt.timezone.utc).timestamp()
        stale_keys = [key for key, expiry in SIGNED_NONCE_CACHE.items() if expiry <= now_epoch]
        for key in stale_keys:
            SIGNED_NONCE_CACHE.pop(key, None)
        if len(SIGNED_NONCE_CACHE) > SIGNED_NONCE_CACHE_LIMIT:
            for key in sorted(SIGNED_NONCE_CACHE, key=SIGNED_NONCE_CACHE.get)[: max(len(SIGNED_NONCE_CACHE) - SIGNED_NONCE_CACHE_LIMIT, 1)]:
                SIGNED_NONCE_CACHE.pop(key, None)
        replay_key = f"{payload.get('workspaceId')}|{payload.get('userId')}|{nonce}"
        if replay_key in SIGNED_NONCE_CACHE:
            raise ValueError("signed caller context nonce replay detected")
        SIGNED_NONCE_CACHE[replay_key] = now_epoch + SIGNED_NONCE_TTL_SECONDS
    return True


def _parse_allowed_values(env_name: str) -> set[str]:
    raw = str(os.environ.get(env_name, "") or "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _extract_entra_principal(req: func.HttpRequest) -> dict[str, str]:
    encoded = req.headers.get("x-ms-client-principal", "")
    claims: dict[str, str] = {}
    if encoded:
        try:
            payload = encoded
            padding = len(payload) % 4
            if padding:
                payload += "=" * (4 - padding)
            decoded = base64.b64decode(payload.encode("utf-8"), validate=False).decode("utf-8")
            principal = json.loads(decoded)
            for claim in principal.get("claims") or []:
                typ = str(claim.get("typ") or "").strip().lower()
                val = str(claim.get("val") or "").strip()
                if typ and val:
                    claims[typ] = val
        except Exception:
            claims = {}
    object_id = str(req.headers.get("x-ms-client-principal-id", "") or claims.get("http://schemas.microsoft.com/identity/claims/objectidentifier", "")).strip()
    principal_name = str(req.headers.get("x-ms-client-principal-name", "") or claims.get("preferred_username", "") or claims.get("upn", "")).strip()
    app_id = str(claims.get("appid", "")).strip()
    return {"objectId": object_id, "principalName": principal_name, "appId": app_id}


def _validate_entra_context(req: func.HttpRequest, payload: dict[str, Any], *, required: bool) -> bool:
    principal = _extract_entra_principal(req)
    has_identity = bool(principal["objectId"] or principal["principalName"] or principal["appId"])
    if not has_identity:
        if required:
            raise ValueError("entra caller context is required")
        return False
    allowed_object_ids = _parse_allowed_values("MEMORY_ALLOWED_CALLER_OBJECT_IDS")
    allowed_principals = _parse_allowed_values("MEMORY_ALLOWED_CALLER_PRINCIPALS")
    allow_all = parse_bool(os.environ.get("MEMORY_ALLOW_ALL_ENTRA_PRINCIPALS"), default=False)
    if not allow_all and not allowed_object_ids and not allowed_principals:
        raise ValueError("Entra auth requires caller allowlist or MEMORY_ALLOW_ALL_ENTRA_PRINCIPALS=true")
    candidates = {principal["objectId"].lower(), principal["principalName"].lower(), principal["appId"].lower()}
    if allowed_object_ids and not (allowed_object_ids & candidates):
        raise ValueError("entra caller object is not authorized")
    if allowed_principals and not (allowed_principals & candidates):
        raise ValueError("entra caller principal is not authorized")
    payload_user = str(payload.get("userId") or "").strip().lower()
    principal_user = principal["principalName"].strip().lower()
    principal_object = principal["objectId"].strip().lower()
    if payload_user and principal_user and payload_user != principal_user and payload_user != principal_object:
        raise ValueError("payload userId does not match authenticated Entra principal")
    return True


def _validate_caller_context(req: func.HttpRequest, payload: dict[str, Any]) -> str:
    auth_mode = normalize_auth_mode(os.environ.get("MEMORY_AUTH_MODE", "dual"))
    if auth_mode == "off":
        if not parse_bool(os.environ.get("MEMORY_ALLOW_INSECURE_AUTH_OFF"), default=False):
            raise ValueError("MEMORY_AUTH_MODE=off requires MEMORY_ALLOW_INSECURE_AUTH_OFF=true")
        return "off"
    if auth_mode == "shared_secret":
        _validate_signed_context(req, payload, required=True)
        return "shared_secret"
    if auth_mode == "entra":
        _validate_entra_context(req, payload, required=True)
        return "entra"
    signed_ok = _validate_signed_context(req, payload, required=False)
    enable_entra_in_dual = parse_bool(os.environ.get("MEMORY_ENABLE_ENTRA_IN_DUAL"), default=False)
    entra_ok = _validate_entra_context(req, payload, required=False) if enable_entra_in_dual else False
    if not signed_ok and not entra_ok:
        raise ValueError("caller context must include valid shared-secret signature or Entra identity")
    return "shared_secret" if signed_ok else "entra"


def main(req: func.HttpRequest) -> func.HttpResponse:
    op = (req.route_params.get("op") or "").strip()
    payload = _parse_payload(req)
    payload = payload if isinstance(payload, dict) else {}
    try:
        payload["_authMode"] = _validate_caller_context(req, payload)
        service = get_service()
        handlers = {
            "memory_get_personal": service.memory_get_personal,
            "memory_get_shared": service.memory_get_shared,
            "project_upsert": service.project_upsert,
            "project_get": service.project_get,
            "project_list": service.project_list,
            "project_archive": service.project_archive,
            "memory_get_items": service.memory_get_items,
            "memory_get_stats": service.memory_get_stats,
            "memory_route_review": service.memory_route_review,
            "memory_export": service.memory_export,
            "memory_import": service.memory_import,
            "memory_add_fact": service.memory_add_fact,
            "memory_add_artifact": service.memory_add_artifact,
            "memory_add_run": service.memory_add_run,
            "memory_add_disagreement": service.memory_add_disagreement,
            "memory_add_failure_event": service.memory_add_failure_event,
            "memory_add_correction_event": service.memory_add_correction_event,
            "memory_add_resolution_event": service.memory_add_resolution_event,
            "memory_add_trust_event": service.memory_add_trust_event,
            "memory_list_open_tasks": service.memory_list_open_tasks,
            "memory_add_task": service.memory_add_task,
            "memory_close_task": service.memory_close_task,
            "memory_rebuild_embeddings": service.memory_rebuild_embeddings,
            "memory_auto_promote": service.memory_auto_promote,
            "memory_search_summaries": service.memory_search_summaries,
            "memory_search_vectors": service.memory_search_vectors,
            "memory_build_context": service.memory_build_context,
            "memory_get_retrieval_logs": service.memory_get_retrieval_logs,
            "memory_get_audit_events": service.memory_get_audit_events,
            "memory_add_audit_event": service.memory_add_audit_event,
        }
        handler = handlers.get(op)
        if handler is None:
            return _json_response(404, {"ok": False, "error": f"unknown operation: {op}"})
        result = handler(payload)
        return _json_response(200, result if isinstance(result, dict) else {"ok": True, "result": result})
    except ValueError as exc:
        return _json_response(400, {"ok": False, "error": str(exc)})
    except exceptions.CosmosHttpResponseError as exc:
        return _json_response(500, {"ok": False, "error": f"cosmos error: {exc.message}"})
    except Exception as exc:
        return _json_response(500, {"ok": False, "error": str(exc)})
