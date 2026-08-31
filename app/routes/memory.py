"""Memory context and insight endpoints."""
from fastapi import APIRouter, HTTPException
from typing import List, Dict, Any, Optional

from app.database import db
from app.models import MemoryContextCreate
from app.memory import add_memory_context, get_all_memory_context, get_memory_insights

router = APIRouter()

@router.get("/api/memory-context")
def list_memory_contexts():
    """Retrieves all user-fed memory contexts (tax rules, policies, discounts, disputes)."""
    return get_all_memory_context()

@router.post("/api/memory-context")
def create_memory_context(req: MemoryContextCreate):
    """Creates a new memory context entry to inform the reconciliation agent."""
    new_id = add_memory_context(
        context_type=req.context_type,
        description=req.description,
        effective_date=req.effective_date
    )
    created = db.fetchone("SELECT * FROM memory_context WHERE id = %s", (new_id,))
    return {"status": "success", "context": created}

@router.delete("/api/memory-context/{context_id}")
def delete_memory_context(context_id: int):
    """Deletes a memory context entry."""
    existing = db.fetchone("SELECT id FROM memory_context WHERE id = %s", (context_id,))
    if not existing:
        raise HTTPException(status_code=404, detail=f"Context {context_id} not found")
    
    db.execute("DELETE FROM memory_context WHERE id = %s", (context_id,))
    return {"status": "success", "message": f"Memory context {context_id} deleted"}

@router.get("/api/memory-insights")
def list_memory_insights(limit: int = 20):
    """Retrieves pattern insights generated across reconciliation cycles."""
    return get_memory_insights(limit=limit)
