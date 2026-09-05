import csv
import json
import uuid
from decimal import Decimal
from datetime import datetime, timezone

from app.database import db
from app.webhook import process_razorpay_event
from app.memory import add_memory_context
from app.reconciliation import run_reconciliation

def seed_from_csvs():
    print("Clearing prior tables...")
    db.clear_tables()

    # 1. Seed Memory Contexts required for domain reasoning & disputes
    add_memory_context(
        context_type="Tax Rate Change",
        description="[Demo] GST on Category B items revised from 12% to 18% effective Aug 15. Check invoices raised around transition date.",
        effective_date="2026-08-15",
        role="admin"
    )
    add_memory_context(
        context_type="Discount Scheme",
        description="[Demo] Apollo Pharmacy eligible for approved 5% prompt settlement cash discount on W34 cycles.",
        effective_date="2026-08-01",
        role="admin"
    )
    add_memory_context(
        context_type="Disputed Transaction",
        description="[Demo] INV-2026-DEMO-DISPUTE under dispute regarding damaged transit carton #4491.",
        effective_date="2026-08-20",
        role="finance_controller"
    )

    # 2. Ingest CDMS CSV
    with open("demo_cdms_all_discrepancies.csv", mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("cn_no"):
                db.execute(
                    "INSERT INTO credit_notes (cn_no, invoice_no, amount) VALUES (%s, %s, %s)",
                    (row["cn_no"].strip(), row["invoice_no"].strip(), float(row["amount"]))
                )
            elif row.get("invoice_no"):
                tot = float(row["total_amount"])
                rate = float(row.get("tax_rate") or "0.18")
                base = float(Decimal(str(row.get("base_amount") or (tot / (1 + rate)))).quantize(Decimal("0.01")))
                tax = float(Decimal(str(row.get("tax_amount") or (tot - base))).quantize(Decimal("0.01")))
                db.execute(
                    """INSERT INTO orders (invoice_no, customer_code, customer_name, invoice_date,
                                          base_amount, tax_rate, tax_amount, total_amount, status, cycle_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)""",
                    (
                        row["invoice_no"].strip(),
                        row.get("customer_code") or "CUST-001",
                        row.get("customer_name") or "Test Customer",
                        row.get("invoice_date") or "2026-09-04",
                        base, rate, tax, tot,
                        row.get("status") or "PD"
                    )
                )

    # 3. Ingest Razorpay Settlements CSV
    now = datetime.now(timezone.utc)
    with open("demo_razorpay_settlements.csv", mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pay_id = row["payment_id"].strip()
            inv_no = row.get("invoice_no", "").strip() or None
            amt_inr = float(row["amount"])
            utr = row.get("utr", "").strip() or f"UTR{uuid.uuid4().hex[:8].upper()}"

            event_id = f"evt_{uuid.uuid4().hex[:12]}"
            event_payload = {
                "id": event_id,
                "entity": "event",
                "event": "payment.captured",
                "contains": ["payment"],
                "created_at": int(now.timestamp()),
                "payload": {
                    "payment": {
                        "entity": {
                            "id": pay_id,
                            "amount": int(Decimal(str(amt_inr)) * 100),
                            "currency": "INR",
                            "status": "captured",
                            "order_id": f"order_{uuid.uuid4().hex[:10]}",
                            "method": row.get("method", "netbanking"),
                            "created_at": int(now.timestamp()),
                            "notes": {"invoice_no": inv_no} if inv_no else {},
                            "acquirer_data": {"utr": utr}
                        }
                    }
                }
            }
            process_razorpay_event(event_payload, json.dumps(event_payload))

    print("Data ingested successfully. Running reconciliation cycle...")
    run_id = run_reconciliation(cycle_label="DEMO-ALL-ERRORS-CYCLE")
    
    # Query run summary and exceptions
    run = db.fetchone("SELECT * FROM reconciliation_runs WHERE id = %s", (run_id,))
    exceptions = db.fetchall("SELECT id, invoice_no, customer_name, delta, error_type, status, pattern_key FROM exceptions WHERE run_id = %s", (run_id,))
    
    print("\n--- RECONCILIATION RUN RESULT ---")
    print(f"Run ID: {run['id']}")
    print(f"Total Records: {run['total_records']}, Matched: {run['matched_count']}, Match Rate: {run['match_rate']}%")
    print(f"\n--- FLAGGED EXCEPTIONS ({len(exceptions)}) ---")
    for exc in exceptions:
        print(f"[{exc['error_type']}] {exc['customer_name']} (Inv: {exc['invoice_no']}) -> Delta: ₹{exc['delta']:+,.2f} | Status: {exc['status']}")

if __name__ == "__main__":
    seed_from_csvs()
