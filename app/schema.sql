-- Engineering Spec v1: PostgreSQL Migration / Schema

CREATE TABLE IF NOT EXISTS reconciliation_runs (
  id              TEXT PRIMARY KEY,
  cycle_label     TEXT NOT NULL,
  started_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at     TIMESTAMP,
  total_records   INTEGER DEFAULT 0,
  matched_count   INTEGER DEFAULT 0,
  match_rate      NUMERIC(5,2) DEFAULT 0.00
);

CREATE TABLE IF NOT EXISTS orders (
  invoice_no      TEXT PRIMARY KEY,
  customer_code   TEXT NOT NULL,
  customer_name   TEXT NOT NULL,
  invoice_date    TEXT NOT NULL,
  base_amount     NUMERIC(12,2) NOT NULL,
  tax_rate        NUMERIC(4,3) NOT NULL,
  tax_amount      NUMERIC(12,2) NOT NULL,
  total_amount    NUMERIC(12,2) NOT NULL,
  status          TEXT NOT NULL,
  cycle_id        TEXT REFERENCES reconciliation_runs(id)
);

CREATE TABLE IF NOT EXISTS credit_notes (
  cn_no           TEXT PRIMARY KEY,
  invoice_no      TEXT NOT NULL REFERENCES orders(invoice_no),
  amount          NUMERIC(12,2) NOT NULL,
  created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS razorpay_events_raw (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id        TEXT UNIQUE NOT NULL,
  event_type      TEXT NOT NULL,
  payload         TEXT NOT NULL,
  received_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  processed       BOOLEAN NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settlements (
  payment_id      TEXT PRIMARY KEY,
  utr             TEXT,
  invoice_no      TEXT,
  amount          NUMERIC(12,2) NOT NULL,
  settled_at      TIMESTAMP NOT NULL,
  raw_event_id    INTEGER REFERENCES razorpay_events_raw(id)
);

CREATE TABLE IF NOT EXISTS exceptions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id          TEXT NOT NULL REFERENCES reconciliation_runs(id),
  invoice_no      TEXT,
  customer_name   TEXT,
  delta           NUMERIC(12,2) NOT NULL,
  error_type      TEXT NOT NULL,
  remark          TEXT,
  resolved        BOOLEAN NOT NULL DEFAULT 0,
  resolved_note   TEXT
);

CREATE TABLE IF NOT EXISTS memory_context (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  context_type    TEXT NOT NULL,
  description     TEXT NOT NULL,
  effective_date  TEXT,
  created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS memory_insights (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id          TEXT REFERENCES reconciliation_runs(id),
  insight         TEXT NOT NULL,
  created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_settlements_invoice ON settlements(invoice_no);
CREATE INDEX IF NOT EXISTS idx_exceptions_run ON exceptions(run_id);
