"""Exceptions review, lifecycle actions, and resolution management endpoints."""
import json
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Query
from typing import Optional, List, Dict, Any

from app.database import db
from app.models import ExceptionResolveRequest, ExceptionActionRequest, BatchResolvePatternRequest

router = APIRouter()

def _enrich_exception(r: Dict[str, Any]) -> Dict[str, Any]:
    """Enriches exception row with delta float, parsed plausible causes, and SLA aging clock."""
    if r.get("delta") is not None:
        r["delta"] = float(r["delta"])
    r["resolved"] = bool(r.get("resolved"))
    if not r.get("status"):
        r["status"] = "resolved" if r["resolved"] else "open"

    # Plausible causes JSON parsing
    raw_causes = r.get("plausible_causes")
    if raw_causes and isinstance(raw_causes, str):
        try:
            r["plausible_causes_list"] = json.loads(raw_causes)
        except Exception:
            r["plausible_causes_list"] = []
    else:
        r["plausible_causes_list"] = []

    # Calculate SLA aging if escalated
    if r.get("escalated_at"):
        try:
            esc_dt = datetime.fromisoformat(r["escalated_at"].replace("Z", "+00:00"))
            now_dt = datetime.now(timezone.utc)
            aging_hours = round((now_dt - esc_dt).total_seconds() / 3600.0, 1)
            r["aging_hours"] = max(aging_hours, 0.1)
        except Exception:
            r["aging_hours"] = 1.0
    else:
        r["aging_hours"] = 0.0

    return r

@router.get("/api/exceptions")
@router.get("/reconciliation/exceptions")
def list_exceptions_endpoint(
    run_id: Optional[str] = None,
    resolved: Optional[bool] = None,
    status: Optional[str] = None, # 'open', 'resolved', 'escalated', 'reopened'
    error_type: Optional[str] = None,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    limit: Optional[int] = None
):
    """Lists reconciliation exceptions with optional filters and pagination."""
    query = "SELECT * FROM exceptions WHERE 1=1"
    params = []

    if run_id:
        query += " AND run_id = %s"
        params.append(run_id)
    if status:
        query += " AND status = %s"
        params.append(status)
    elif resolved is not None:
        query += " AND resolved = %s"
        params.append(1 if resolved else 0)
    if error_type and error_type.lower() != "all":
        query += " AND error_type LIKE %s"
        params.append(f"%{error_type}%")
    if search:
        query += " AND (invoice_no LIKE %s OR customer_name LIKE %s OR remark LIKE %s)"
        s_term = f"%{search}%"
        params.extend([s_term, s_term, s_term])

    effective_limit = limit if limit else page_size
    offset = (page - 1) * effective_limit

    # Get total count for pagination
    count_query = f"SELECT COUNT(*) as total FROM ({query})"
    count_row = db.fetchone(count_query, tuple(params))
    total_count = count_row.get("total", 0) if count_row else 0

    query += " ORDER BY id DESC LIMIT %s OFFSET %s"
    params.extend([effective_limit, offset])

    rows = db.fetchall(query, tuple(params))
    enriched = [_enrich_exception(r) for r in rows]

    return {
        "total": total_count,
        "page": page,
        "page_size": effective_limit,
        "exceptions": enriched
    } if not limit else enriched

@router.get("/reconciliation/runs/{run_id}/exceptions")
def list_run_exceptions_paginated(
    run_id: str,
    status: Optional[str] = None,
    error_type: Optional[str] = None,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200)
):
    """Returns paginated, filterable exceptions for a specific run (§5)."""
    return list_exceptions_endpoint(
        run_id=run_id,
        status=status,
        error_type=error_type,
        search=search,
        page=page,
        page_size=page_size
    )

@router.patch("/reconciliation/exceptions/{exception_id}")
def update_exception_lifecycle(exception_id: int, req: ExceptionActionRequest):
    """
    Performs lifecycle action on an exception (§1 C3-C5):
    - accept: marks resolved, records timestamp, logs to audit
    - escalate: adds to human review queue with aging clock, logs to audit
    - reopen: reversible resolution; reopens resolved/escalated item with reason
    - add_note: attaches controller audit note
    """
    existing = db.fetchone("SELECT * FROM exceptions WHERE id = %s", (exception_id,))
    if not existing:
        raise HTTPException(status_code=404, detail=f"Exception {exception_id} not found")

    action = req.action.lower()
    actor = req.actor or "finance_controller"
    now_iso = datetime.now(timezone.utc).isoformat()
    note = req.note or ""

    before_state = {
        "status": existing.get("status", "open"),
        "resolved": bool(existing.get("resolved")),
        "resolved_note": existing.get("resolved_note")
    }

    if action == "accept":
        final_note = note if note else existing.get("resolved_note")
        db.execute(
            """UPDATE exceptions
               SET status = 'resolved', resolved = 1, resolved_at = %s, resolved_by = %s,
                   resolved_note = %s
               WHERE id = %s""",
            (now_iso, actor, final_note, exception_id)
        )
        audit_action = "ACCEPT_REMARK"

    elif action == "escalate":
        final_note = note if note else f"Escalated to human review by {actor}"
        db.execute(
            """UPDATE exceptions
               SET status = 'escalated', resolved = 0, escalated_at = %s,
                   resolved_note = %s
               WHERE id = %s""",
            (now_iso, final_note, exception_id)
        )
        audit_action = "ESCALATE"

    elif action == "reopen":
        final_note = note if note else f"Reopened by {actor}"
        db.execute(
            """UPDATE exceptions
               SET status = 'reopened', resolved = 0,
                   resolved_note = %s
               WHERE id = %s""",
            (final_note, exception_id)
        )
        audit_action = "REOPEN"

    elif action in ("add_note", "note"):
        new_note = f"{existing.get('resolved_note')} | {note}" if existing.get('resolved_note') else note
        db.execute(
            """UPDATE exceptions
               SET resolved_note = %s
               WHERE id = %s""",
            (new_note, exception_id)
        )
        audit_action = "ADD_NOTE"
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported action: {req.action}")

    updated = db.fetchone("SELECT * FROM exceptions WHERE id = %s", (exception_id,))
    enriched = _enrich_exception(updated)

    # Immutable audit trail entry
    db.log_audit(
        actor=actor,
        action=audit_action,
        entity_type="exception",
        entity_id=str(exception_id),
        before_state=before_state,
        after_state={"status": enriched["status"], "resolved": enriched["resolved"], "note": note}
    )

    return {"status": "success", "action": action, "exception": enriched}

@router.patch("/api/exceptions/{exception_id}/resolve")
def resolve_exception_endpoint(exception_id: int, req: ExceptionResolveRequest):
    """Marks an exception as resolved with an audit note (Backward compatibility)."""
    action_req = ExceptionActionRequest(
        action="accept" if req.resolved else "reopen",
        note=req.resolved_note,
        actor=req.actor or "finance_controller"
    )
    res = update_exception_lifecycle(exception_id, action_req)
    return {"status": "success", "exception": res["exception"]}

@router.post("/reconciliation/exceptions/batch-resolve-pattern")
def batch_resolve_pattern_endpoint(req: BatchResolvePatternRequest):
    """
    Batch-resolves all open exceptions matching a given pattern_key (§1 D2).
    Applies the agent's recommended resolution across repeat occurrences.
    """
    pattern_key = req.pattern_key
    actor = req.actor or "finance_controller"
    note = req.note or f"Batch resolved via pattern rule: {pattern_key}"
    now_iso = datetime.now(timezone.utc).isoformat()

    matching = db.fetchall(
        "SELECT id FROM exceptions WHERE pattern_key = %s AND resolved = 0",
        (pattern_key,)
    )
    if not matching:
        # Try matching by customer_name or error_type if pattern_key format "Customer:Type"
        if ":" in pattern_key:
            cust, err = pattern_key.split(":", 1)
            matching = db.fetchall(
                "SELECT id FROM exceptions WHERE customer_name = %s AND error_type LIKE %s AND resolved = 0",
                (cust.strip(), f"%{err.strip()}%")
            )

    resolved_ids = [m["id"] for m in matching]
    if resolved_ids:
        for eid in resolved_ids:
            db.execute(
                """UPDATE exceptions
                   SET status = 'resolved', resolved = 1, resolved_at = %s, resolved_by = %s,
                       resolved_note = %s
                   WHERE id = %s""",
                (now_iso, actor, note, eid)
            )

        db.log_audit(
            actor=actor,
            action="BATCH_RESOLVE_PATTERN",
            entity_type="pattern",
            entity_id=pattern_key,
            after_state={"resolved_count": len(resolved_ids), "exception_ids": resolved_ids, "note": note}
        )

    return {
        "status": "success",
        "pattern_key": pattern_key,
        "resolved_count": len(resolved_ids),
        "resolved_ids": resolved_ids
    }

