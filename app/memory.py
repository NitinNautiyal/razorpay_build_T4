"""Memory context and insight storage management."""
from typing import List, Dict, Any, Optional
from datetime import datetime
from app.database import db

def add_memory_context(context_type: str, description: str, effective_date: Optional[str] = None) -> int:
    """Adds user-fed context (tax changes, policy changes, discounts, disputes)."""
    return db.execute(
        """INSERT INTO memory_context (context_type, description, effective_date)
           VALUES (%s, %s, %s)""",
        (context_type, description, effective_date)
    )

def get_all_memory_context() -> List[Dict[str, Any]]:
    """Retrieves all memory context items ordered by creation date descending."""
    return db.fetchall("SELECT * FROM memory_context ORDER BY created_at DESC")

def add_memory_insight(run_id: Optional[str], insight: str) -> int:
    """Stores agent-generated pattern insights."""
    return db.execute(
        """INSERT INTO memory_insights (run_id, insight)
           VALUES (%s, %s)""",
        (run_id, insight)
    )

def get_memory_insights(limit: int = 20) -> List[Dict[str, Any]]:
    """Retrieves historical agent insights."""
    return db.fetchall(
        "SELECT * FROM memory_insights ORDER BY created_at DESC LIMIT %s",
        (limit,)
    )
