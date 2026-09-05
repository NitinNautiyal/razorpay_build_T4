"""Memory context and insight endpoints."""
from fastapi import APIRouter, HTTPException, Header
from typing import List, Dict, Any, Optional

from app.database import db
from app.models import MemoryContextCreate
from app.memory import add_memory_context, get_all_memory_context, get_memory_insights

router = APIRouter()

@router.get("/memory-context")
@router.get("/api/memory-context")
def list_memory_contexts():
    """Retrieves all user-fed memory contexts (tax rules, policies, discounts, disputes)."""
    return get_all_memory_context()

@router.post("/memory-context")
@router.post("/api/memory-context")
def create_memory_context(
    req: MemoryContextCreate,
    x_user_role: Optional[str] = Header(None, alias="X-User-Role")
):
    """
    Creates a new memory context rule to inform the reconciliation agent.
    Role-gated to Admin and Finance Controller (§1 D1 & §8).
    """
    actor_role = x_user_role or req.role or "admin"
    if actor_role not in ("admin", "finance_controller"):
        raise HTTPException(
            status_code=403,
            detail="Forbidden: Memory context rule creation is role-gated to Admin or Finance Controller."
        )

    new_id = add_memory_context(
        context_type=req.context_type,
        description=req.description,
        effective_date=req.effective_date,
        role=actor_role
    )
    created = db.fetchone("SELECT * FROM memory_context WHERE id = %s", (new_id,))

    # Log to audit trail
    db.log_audit(
        actor=f"{actor_role}_user",
        action="CREATE_MEMORY_RULE",
        entity_type="memory_context",
        entity_id=str(new_id),
        after_state=created
    )

    return {"status": "success", "context": created}

@router.delete("/memory-context/{context_id}")
@router.delete("/api/memory-context/{context_id}")
def delete_memory_context(
    context_id: int,
    x_user_role: Optional[str] = Header(None, alias="X-User-Role")
):
    """Deletes a memory context entry with role check and audit trail."""
    actor_role = x_user_role or "admin"
    if actor_role not in ("admin", "finance_controller"):
        raise HTTPException(status_code=403, detail="Forbidden: Only Admin or Controller can delete memory rules.")

    existing = db.fetchone("SELECT * FROM memory_context WHERE id = %s", (context_id,))
    if not existing:
        raise HTTPException(status_code=404, detail=f"Context {context_id} not found")
    
    db.execute("DELETE FROM memory_context WHERE id = %s", (context_id,))

    db.log_audit(
        actor=f"{actor_role}_user",
        action="DELETE_MEMORY_RULE",
        entity_type="memory_context",
        entity_id=str(context_id),
        before_state=existing
    )

    return {"status": "success", "message": f"Memory context {context_id} deleted"}

@router.get("/memory-insights")
@router.get("/api/memory-insights")
def list_memory_insights(limit: int = 50):
    """Retrieves pattern insights generated across reconciliation cycles."""
    return get_memory_insights(limit=limit)

