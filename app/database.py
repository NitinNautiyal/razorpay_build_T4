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
        """Initializes database tables and indexes."""
        if schema_file is None:
            schema_file = os.path.join(os.path.dirname(__file__), "schema.sql")
        
        with open(schema_file, "r") as f:
            schema_sql = f.read()

        with self.connect() as conn:
            cursor = conn.cursor()
            if self.is_sqlite:
                cursor.executescript(schema_sql)
            else:
                cursor.execute(schema_sql)

    def clear_tables(self):
        """Clears all tables in foreign key dependency order."""
        tables = [
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
                cursor.execute(f"DELETE FROM {t}")

# Global DB instance
db = Database()
