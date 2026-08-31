"""Exceptions review and resolution management endpoints."""
from fastapi import APIRouter, HTTPException, Query
from typing import Optional, List, Dict, Any

from app.database import db
from app.models import ExceptionResolveRequest

router = APIRouter()

@router.get("/api/exceptions")
def list_exceptions_endpoint(
    run_id: Optional[str] = None,
    resolved: Optional[bool] = None,
    error_type: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500)
):
    """Lists reconciliation exceptions with optional filters."""
    query = "SELECT * FROM exceptions WHERE 1=1"
    params = []

    if run_id:
        query += " AND run_id = %s"
        params.append(run_id)
    if resolved is not None:
        query += " AND resolved = %s"
        params.append(1 if resolved else 0)
    if error_type:
        query += " AND error_type = %s"
        params.append(error_type)

    query += " ORDER BY id DESC LIMIT %s"
    params.append(limit)

    rows = db.fetchall(query, tuple(params))
    for r in rows:
        if r.get("delta") is not None:
            r["delta"] = float(r["delta"])
        r["resolved"] = bool(r.get("resolved"))
    return rows

@router.patch("/api/exceptions/{exception_id}/resolve")
def resolve_exception_endpoint(exception_id: int, req: ExceptionResolveRequest):
    """Marks an exception as resolved with an audit note."""
    existing = db.fetchone("SELECT * FROM exceptions WHERE id = %s", (exception_id,))
    if not existing:
        raise HTTPException(status_code=404, detail=f"Exception {exception_id} not found")

    resolved_int = 1 if req.resolved else 0
    db.execute(
        "UPDATE exceptions SET resolved = %s, resolved_note = %s WHERE id = %s",
        (resolved_int, req.resolved_note, exception_id)
    )

    updated = db.fetchone("SELECT * FROM exceptions WHERE id = %s", (exception_id,))
    if updated and updated.get("delta") is not None:
        updated["delta"] = float(updated["delta"])
        updated["resolved"] = bool(updated.get("resolved"))

    return {"status": "success", "exception": updated}
