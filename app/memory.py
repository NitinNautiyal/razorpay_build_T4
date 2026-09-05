"""Memory context and insight storage management."""
from typing import List, Dict, Any, Optional
from datetime import datetime
from app.database import db

def add_memory_context(
    context_type: str,
    description: str,
    effective_date: Optional[str] = None,
    role: str = "admin"
) -> int:
    """Adds user-fed context (tax changes, policy changes, discounts, disputes) with role tracking."""
    return db.execute(
        """INSERT INTO memory_context (context_type, description, effective_date, role)
           VALUES (%s, %s, %s, %s)""",
        (context_type, description, effective_date, role)
    )

def get_all_memory_context() -> List[Dict[str, Any]]:
    """Retrieves all memory context items ordered by creation date descending."""
    return db.fetchall("SELECT * FROM memory_context ORDER BY created_at DESC")

def add_memory_insight(
    run_id: Optional[str],
    insight: str,
    pattern_key: Optional[str] = None,
    frequency: int = 1,
    severity: str = "Medium",
    actionable_fix: Optional[str] = None
) -> int:
    """Stores agent-generated pattern insights with cross-cycle pattern keys."""
    return db.execute(
        """INSERT INTO memory_insights (run_id, insight, pattern_key, frequency, severity, actionable_fix)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (run_id, insight, pattern_key, frequency, severity, actionable_fix)
    )

def get_memory_insights(limit: int = 50) -> List[Dict[str, Any]]:
    """Retrieves historical agent insights."""
    return db.fetchall(
        "SELECT * FROM memory_insights ORDER BY created_at DESC LIMIT %s",
        (limit,)
    )

