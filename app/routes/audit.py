"""Audit log query endpoint for compliance review."""
import json
from fastapi import APIRouter, Query
from typing import Optional, List, Dict, Any
from app.database import db

router = APIRouter()

@router.get("/audit-log")
@router.get("/api/audit-log")
def get_audit_log_endpoint(
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    actor: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500)
):
    """
    Retrieves read-only compliance audit trail (§5).
    Every accept/escalate/reopen/config-edit/batch-resolve is logged immutably.
    """
    query = "SELECT * FROM audit_log WHERE 1=1"
    params = []

    if entity_type:
        query += " AND entity_type = %s"
        params.append(entity_type)
    if entity_id:
        query += " AND entity_id = %s"
        params.append(str(entity_id))
    if actor:
        query += " AND actor = %s"
        params.append(actor)
    if action:
        query += " AND action = %s"
        params.append(action)

    query += " ORDER BY id DESC LIMIT %s"
    params.append(limit)

    rows = db.fetchall(query, tuple(params))
    for r in rows:
        if r.get("before_state") and isinstance(r["before_state"], str):
            try:
                r["before_state"] = json.loads(r["before_state"])
            except Exception:
                pass
        if r.get("after_state") and isinstance(r["after_state"], str):
            try:
                r["after_state"] = json.loads(r["after_state"])
            except Exception:
                pass

    return {
        "status": "success",
        "total": len(rows),
        "audit_logs": rows
    }
