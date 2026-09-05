"""Database access layer supporting SQLite and PostgreSQL with uniform execution."""
import os
import sqlite3
import contextlib
from typing import Any, Dict, List, Optional, Tuple, Union
from decimal import Decimal
from datetime import datetime
from app.config import DATABASE_URL

# Register adapter for Decimal and datetime for sqlite3
sqlite3.register_adapter(Decimal, str)
sqlite3.register_adapter(datetime, lambda dt: dt.isoformat())

def _dict_factory(cursor: sqlite3.Cursor, row: Tuple) -> Dict[str, Any]:
    fields = [column[0] for column in cursor.description]
    return {key: value for key, value in zip(fields, row)}

class Database:
    def __init__(self, db_url: str = DATABASE_URL):
        self.db_url = db_url
        self.is_sqlite = not (db_url.startswith("postgres://") or db_url.startswith("postgresql://"))
        self._sqlite_path = db_url.replace("sqlite:///", "").replace("sqlite://", "") if self.is_sqlite else None

    def get_connection(self):
        if self.is_sqlite:
            conn = sqlite3.connect(self._sqlite_path, check_same_thread=False)
            conn.row_factory = _dict_factory
            conn.execute("PRAGMA foreign_keys = ON")
            return conn
        else:
            import psycopg2
            import psycopg2.extras
            conn = psycopg2.connect(self.db_url)
            return conn

    @contextlib.contextmanager
    def connect(self):
        conn = self.get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute(self, query: str, params: Union[Tuple, List, Dict] = ()) -> int:
        """Executes a query and returns lastrowid or affected rows."""
        norm_query, norm_params = self._normalize_query(query, params)
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(norm_query, norm_params)
            return cursor.lastrowid or cursor.rowcount

    def fetchall(self, query: str, params: Union[Tuple, List, Dict] = ()) -> List[Dict[str, Any]]:
        norm_query, norm_params = self._normalize_query(query, params)
        with self.connect() as conn:
            if not self.is_sqlite:
                import psycopg2.extras
                cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            else:
                cursor = conn.cursor()
            cursor.execute(norm_query, norm_params)
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def fetchone(self, query: str, params: Union[Tuple, List, Dict] = ()) -> Optional[Dict[str, Any]]:
        norm_query, norm_params = self._normalize_query(query, params)
        with self.connect() as conn:
            if not self.is_sqlite:
                import psycopg2.extras
                cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            else:
                cursor = conn.cursor()
            cursor.execute(norm_query, norm_params)
            row = cursor.fetchone()
            return dict(row) if row else None

    def _normalize_query(self, query: str, params: Any) -> Tuple[str, Any]:
        """Ensures query placeholders match the database driver (%s vs ?)."""
        if self.is_sqlite:
            # Replace %s with ? if given %s format
            # Be careful not to replace %% or within strings
            if isinstance(params, (list, tuple)):
                q = query.replace("%s", "?")
                return q, params
            elif isinstance(params, dict):
                # SQLite supports :param_name
                return query, params
        return query, params

    def init_db(self, schema_file: Optional[str] = None):
        """Initializes database tables and indexes, and runs migrations for new columns."""
        if schema_file is None:
            schema_file = os.path.join(os.path.dirname(__file__), "schema.sql")
        
        with open(schema_file, "r") as f:
            schema_sql = f.read()

        with self.connect() as conn:
            cursor = conn.cursor()
            if self.is_sqlite:
                # First run migrations if tables already exist
                self._run_sqlite_migrations(conn)
                cursor.executescript(schema_sql)
            else:
                cursor.execute(schema_sql)

    def _run_sqlite_migrations(self, conn: sqlite3.Connection):
        """Ensures newly added columns exist in existing SQLite tables."""
        migrations = [
            ("reconciliation_runs", "status", "TEXT DEFAULT 'complete'"),
            ("reconciliation_runs", "lock_acquired", "BOOLEAN DEFAULT 0"),
            ("reconciliation_runs", "queued_reason", "TEXT"),
            ("reconciliation_runs", "error_message", "TEXT"),
            ("orders", "version", "INTEGER DEFAULT 1"),
            ("orders", "superseded_by", "TEXT"),
            ("credit_notes", "version", "INTEGER DEFAULT 1"),
            ("credit_notes", "superseded_by", "TEXT"),
            ("exceptions", "status", "TEXT DEFAULT 'open'"),
            ("exceptions", "escalated_at", "TIMESTAMP"),
            ("exceptions", "resolved_at", "TIMESTAMP"),
            ("exceptions", "resolved_by", "TEXT"),
            ("exceptions", "plausible_causes", "TEXT"),
            ("exceptions", "pattern_key", "TEXT"),
            ("memory_context", "role", "TEXT DEFAULT 'admin'"),
            ("memory_insights", "pattern_key", "TEXT"),
            ("memory_insights", "frequency", "INTEGER DEFAULT 1"),
            ("memory_insights", "severity", "TEXT DEFAULT 'Medium'"),
            ("memory_insights", "actionable_fix", "TEXT"),
        ]
        cursor = conn.cursor()
        for table, col, col_def in migrations:
            try:
                cursor.execute(f"PRAGMA table_info({table})")
                rows = cursor.fetchall()
                if rows:
                    cols = [row["name"] if isinstance(row, dict) else row[1] for row in rows]
                    if col not in cols:
                        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
            except Exception:
                pass
                pass

    def log_audit(
        self,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str,
        before_state: Optional[Any] = None,
        after_state: Optional[Any] = None
    ) -> int:
        """Appends an immutable audit log entry."""
        import json
        b_str = json.dumps(before_state, default=str) if before_state is not None else None
        a_str = json.dumps(after_state, default=str) if after_state is not None else None
        return self.execute(
            """INSERT INTO audit_log (actor, action, entity_type, entity_id, before_state, after_state)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (actor, action, entity_type, str(entity_id), b_str, a_str)
        )

    def clear_tables(self):
        """Clears all tables in foreign key dependency order."""
        tables = [
            "audit_log",
            "settlement_allocations",
            "exceptions",
            "memory_insights",
            "memory_context",
            "settlements",
            "credit_notes",
            "orders",
            "reconciliation_runs",
            "razorpay_events_raw"
        ]
        with self.connect() as conn:
            cursor = conn.cursor()
            for t in tables:
                try:
                    cursor.execute(f"DELETE FROM {t}")
                except Exception:
                    pass

# Global DB instance
db = Database()
