-- Engineering Spec v2: Extended Schema with Allocations & Audit Trail

CREATE TABLE IF NOT EXISTS reconciliation_runs (
  id              TEXT PRIMARY KEY,
  cycle_label     TEXT NOT NULL,
  started_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at     TIMESTAMP,
  total_records   INTEGER DEFAULT 0,
  matched_count   INTEGER DEFAULT 0,
  match_rate      NUMERIC(5,2) DEFAULT 0.00,
  status          TEXT DEFAULT 'complete', -- 'running', 'complete', 'failed', 'partial', 'queued'
  lock_acquired   BOOLEAN DEFAULT 0,
  queued_reason   TEXT,
  error_message   TEXT
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
  cycle_id        TEXT REFERENCES reconciliation_runs(id),
  version         INTEGER DEFAULT 1,
  superseded_by   TEXT
);

CREATE TABLE IF NOT EXISTS credit_notes (
  cn_no           TEXT PRIMARY KEY,
  invoice_no      TEXT NOT NULL REFERENCES orders(invoice_no),
  amount          NUMERIC(12,2) NOT NULL,
  created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  version         INTEGER DEFAULT 1,
  superseded_by   TEXT
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

CREATE TABLE IF NOT EXISTS settlement_allocations (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  settlement_id     TEXT NOT NULL REFERENCES settlements(payment_id),
  order_id          TEXT NOT NULL REFERENCES orders(invoice_no),
  allocated_amount  NUMERIC(12,2) NOT NULL,
  allocation_type   TEXT DEFAULT 'auto', -- 'auto' | 'manual'
  created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
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
  resolved_note   TEXT,
  status          TEXT DEFAULT 'open', -- 'open', 'resolved', 'escalated', 'reopened'
  escalated_at    TIMESTAMP,
  resolved_at     TIMESTAMP,
  resolved_by     TEXT,
  plausible_causes TEXT, -- JSON string array of dual plausible explanations
  pattern_key     TEXT   -- e.g. customer_name:error_type for cross-cycle aggregation
);

CREATE TABLE IF NOT EXISTS memory_context (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  context_type    TEXT NOT NULL,
  description     TEXT NOT NULL,
  effective_date  TEXT,
  created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  role            TEXT DEFAULT 'admin'
);

CREATE TABLE IF NOT EXISTS memory_insights (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id          TEXT REFERENCES reconciliation_runs(id),
  insight         TEXT NOT NULL,
  created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  pattern_key     TEXT,
  frequency       INTEGER DEFAULT 1,
  severity        TEXT DEFAULT 'Medium',
  actionable_fix  TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  actor           TEXT NOT NULL,
  action          TEXT NOT NULL, -- 'ACCEPT_REMARK', 'ESCALATE', 'REOPEN', 'ADD_NOTE', 'UPDATE_CONFIG', 'CREATE_MEMORY_RULE', 'BATCH_RESOLVE_PATTERN'
  entity_type     TEXT NOT NULL, -- 'exception', 'config', 'memory_context', 'run'
  entity_id       TEXT NOT NULL,
  before_state    TEXT,
  after_state     TEXT,
  timestamp       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_settlements_invoice ON settlements(invoice_no);
CREATE INDEX IF NOT EXISTS idx_exceptions_run ON exceptions(run_id);
CREATE INDEX IF NOT EXISTS idx_exceptions_status ON exceptions(status);
CREATE INDEX IF NOT EXISTS idx_exceptions_pattern_key ON exceptions(pattern_key);
CREATE INDEX IF NOT EXISTS idx_allocations_settlement ON settlement_allocations(settlement_id);
CREATE INDEX IF NOT EXISTS idx_allocations_order ON settlement_allocations(order_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON audit_log(entity_type, entity_id);
