"""Reconciliation run triggering and query endpoints."""
from fastapi import APIRouter, HTTPException, Query
from typing import Optional, List, Dict, Any

from app.database import db
from app.models import ReconciliationTriggerRequest
from app.reconciliation import run_reconciliation, get_exceptions

router = APIRouter()

@router.post("/internal/run-reconciliation")
@router.post("/api/reconciliation/run")
def trigger_reconciliation_endpoint(req: Optional[ReconciliationTriggerRequest] = None):
    """
    Triggers scheduled or manual reconciliation pass.
    Processes pending CDMS orders, credit notes, and Razorpay settlements.
    Generates run statistics, exception records, and batched LLM remarks.
    """
    cycle_label = req.cycle_label if req else None
    skip_llm = req.skip_llm if req else False
    
    run_id = run_reconciliation(cycle_label=cycle_label, skip_llm=skip_llm)
    run_details = db.fetchone("SELECT * FROM reconciliation_runs WHERE id = %s", (run_id,))
    exceptions = get_exceptions(run_id)

    return {
        "status": "completed",
        "run": run_details,
        "exceptions_count": len(exceptions),
        "exceptions": exceptions
    }

@router.get("/api/runs")
def list_runs_endpoint(limit: int = Query(20, ge=1, le=100)):
    """Lists reconciliation run history with match rates."""
    runs = db.fetchall(
        "SELECT * FROM reconciliation_runs ORDER BY started_at DESC LIMIT %s",
        (limit,)
    )
    return runs

@router.get("/api/runs/{run_id}")
def get_run_details_endpoint(run_id: str):
    """Retrieves full details and exceptions for a specific reconciliation cycle."""
    run = db.fetchone("SELECT * FROM reconciliation_runs WHERE id = %s", (run_id,))
    if not run:
        raise HTTPException(status_code=404, detail=f"Reconciliation run {run_id} not found")
    
    exceptions = get_exceptions(run_id)
    insights = db.fetchall("SELECT * FROM memory_insights WHERE run_id = %s ORDER BY id ASC", (run_id,))

    return {
        "run": run,
        "exceptions": exceptions,
        "insights": insights
    }
