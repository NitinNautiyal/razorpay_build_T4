# Finance Agent (Controller - Reconciliation - Auditor)
### Automated CDMS ↔ Razorpay Reconciliation Engine & Review Surface (Spec v1)

A single-service, high-reliability reconciliation agent designed for weekly finance reconciliation cycles (50–200+ records) between CDMS invoices/credit notes and Razorpay payments/settlements.

---

## Architecture Overview

```
Razorpay ──(webhook)──▶  POST /webhooks/razorpay  ──▶  razorpay_events_raw (raw audit log)
                                                                 │
                                                       settlements (normalized)
                                                                 │
CDMS export (orders,       ┌─────────────────────────────────────┘
credit notes) ──(API/pull)──▶ orders / credit_notes tables
                                      │
                         Scheduled Weekly Job / Trigger
                         (POST /internal/run-reconciliation)
                                      │
                         reconciliation_runs + exceptions tables
                                      │
                         LLM Remark & Insight Pass (Batched)
                         (cross-references memory_context)
                                      │
                         memory_insights
                                      │
                         Review UI Dashboard (HTML5 / Tailwind)
```

---

## Key Features

1. **Deterministic SQL Match Engine**:
   - Computes Net Delta: `total_amount - sum(credit_notes) - sum(settlements)`.
   - Tolerance configured as constant (`0.09` rounding buffer).
   - Classifies:
     - `Underpayment / Pending Collection`
     - `Overpayment / Excess Settlement`
     - `Duplicate Credit Note`
     - `Tax Mismatch` (12% vs 18% GST)
     - `Unmatched Settlement / Orphan Payment`
2. **Razorpay Webhook Ingestion (`POST /webhooks/razorpay`)**:
   - HMAC-SHA256 signature verification via `X-Razorpay-Signature`.
   - Deduplication / Idempotency on `event_id` (`ON CONFLICT DO NOTHING`).
   - Normalization of payments into rupees from paise and notes tagging (`notes.invoice_no`).
3. **Memory Context & LLM Reasoning Pass**:
   - Integrates user-fed context (`memory_context`): Tax rate changes, policy updates, prompt payment discounts, disputed invoices.
   - Generates contextual financial remarks for each exception row (`exceptions.remark`).
   - Generates high-level pattern observations (`memory_insights`).
4. **Interactive Review Surface**:
   - Server-rendered interactive dashboard with KPI summary cards (Match Rate, Net Delta, Open Exceptions).
   - Filterable discrepancy exception table with instant resolution workflows and audit trail notes.
   - Memory Context manager and raw webhook stream explorer.
   - "Seed Demo Cycle" button for immediate end-to-end evaluation.

---

## Quickstart & Local Setup

### 1. Setup Environment
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 2. Run Tests
```bash
pytest -v tests/
```

### 3. Start the Server
```bash
uvicorn app.main:app --port 8000 --reload
```
Open [http://localhost:8000](http://localhost:8000) in your browser to access the Review UI.

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/webhooks/razorpay` | Ingests Razorpay webhook events (`X-Razorpay-Signature` verified) |
| `POST` | `/internal/run-reconciliation` | Cron endpoint (`0 6 * * MON`) to execute reconciliation cycle |
| `GET` | `/api/runs` | Lists historical reconciliation runs and match rates |
| `GET` | `/api/runs/{run_id}` | Retrieves run details, metrics, and exceptions |
| `GET` | `/api/exceptions` | Lists exceptions with optional filters (`run_id`, `resolved`, `error_type`) |
| `PATCH`| `/api/exceptions/{id}/resolve` | Marks exception as resolved with resolution note |
| `GET` | `/api/memory-context` | Retrieves active memory context rules |
| `POST`| `/api/memory-context` | Creates a new memory context rule (tax/policy/discount/dispute) |
| `POST`| `/api/seed-demo-data` | Populates sample realistic reconciliation batch |
