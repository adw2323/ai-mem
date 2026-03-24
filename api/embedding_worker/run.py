import json
import os
import sys
from pathlib import Path
from typing import Any

import azure.functions as func
from azure.cosmos import CosmosClient, exceptions
from azure.identity import DefaultAzureCredential


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from memory_api.run import MemoryService, now_iso, redact_text  # noqa: E402


SERVICE: MemoryService | None = None


def get_service() -> MemoryService:
    global SERVICE
    if SERVICE is None:
        SERVICE = MemoryService()
    return SERVICE


def _load_doc(service: MemoryService, scope: str, ref_id: str, workspace_id: str, user_id: str | None) -> dict[str, Any]:
    if scope == "shared":
        doc = service.shared.read_item(item=ref_id, partition_key=workspace_id)
        return {"doc": doc, "scope": "shared"}
    doc = service.personal.read_item(item=ref_id, partition_key=user_id)
    return {"doc": doc, "scope": "personal"}


def main(msg: func.QueueMessage) -> None:
    service = get_service()
    payload = json.loads(msg.get_body().decode("utf-8"))
    workspace_id = payload["workspaceId"]
    user_id = payload.get("userId")
    scope = payload.get("scope", "shared")
    ref_id = payload["sourceRefId"]
    text = payload.get("text", "")
    try:
        loaded = _load_doc(service, scope, ref_id, workspace_id, user_id)
        doc = loaded["doc"]
        embedding = service._store_embedding(  # noqa: SLF001
            workspace_id=workspace_id,
            user_id=user_id or doc.get("userId", "unknown"),
            repo_id=payload.get("repoId") or doc.get("repoId"),
            ref_id=ref_id,
            text=text,
            tags=payload.get("tags", []),
            source="queue_worker",
        )
        doc["embeddingStatus"] = "ready"
        doc["embeddingMode"] = embedding["embeddingMode"]
        doc["embeddingWarnings"] = embedding["warnings"]
        doc["updatedAt"] = now_iso()
        container = service.shared if loaded["scope"] == "shared" else service.personal
        container.upsert_item(doc)
        service._audit(workspace_id, user_id or "unknown", "embedding_job_completed", "embeddings", f"ready {ref_id}")  # noqa: SLF001
    except exceptions.CosmosHttpResponseError as exc:
        service._audit(workspace_id, user_id or "unknown", "embedding_job_failed", "embeddings", f"{ref_id} cosmos:{exc.message}")  # noqa: SLF001
        raise
    except Exception as exc:
        try:
            loaded = _load_doc(service, scope, ref_id, workspace_id, user_id)
            doc = loaded["doc"]
            doc["embeddingStatus"] = "failed"
            doc["embeddingWarnings"] = [f"embedding_worker_failed:{type(exc).__name__}"]
            doc["updatedAt"] = now_iso()
            container = service.shared if loaded["scope"] == "shared" else service.personal
            container.upsert_item(doc)
        except Exception:
            pass
        service._audit(workspace_id, user_id or "unknown", "embedding_job_failed", "embeddings", f"{ref_id} failed:{type(exc).__name__}")  # noqa: SLF001
        raise
