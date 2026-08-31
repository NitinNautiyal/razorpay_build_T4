"""CDMS ingestion endpoints, test data seeding, and raw data exploration."""
import json
import uuid
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException

from app.database import db
from app.models import OrderCreate, CreditNoteCreate
from app.webhook import process_razorpay_event
from app.memory import add_memory_context

router = APIRouter()

@router.post("/api/cdms/orders")
def create_order_endpoint(order: OrderCreate):
    """Ingests a single CDMS order."""
    db.execute(
        """INSERT INTO orders (invoice_no, customer_code, customer_name, invoice_date,
                              base_amount, tax_rate, tax_amount, total_amount, status, cycle_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            order.invoice_no,
            order.customer_code,
            order.customer_name,
            order.invoice_date,
            float(order.base_amount),
            float(order.tax_rate),
            float(order.tax_amount),
            float(order.total_amount),
            order.status,
            order.cycle_id
        )
    )
    return {"status": "success", "invoice_no": order.invoice_no}

@router.post("/api/cdms/credit-notes")
def create_credit_note_endpoint(cn: CreditNoteCreate):
    """Ingests a CDMS credit note."""
    db.execute(
        """INSERT INTO credit_notes (cn_no, invoice_no, amount)
           VALUES (%s, %s, %s)""",
        (cn.cn_no, cn.invoice_no, float(cn.amount))
    )
    return {"status": "success", "cn_no": cn.cn_no}

@router.get("/api/cdms/orders")
def list_orders(limit: int = 50):
    """Lists CDMS orders in database."""
    return db.fetchall("SELECT * FROM orders ORDER BY invoice_date DESC LIMIT %s", (limit,))

@router.get("/api/settlements")
def list_settlements(limit: int = 50):
    """Lists normalized settlements."""
    return db.fetchall("SELECT * FROM settlements ORDER BY settled_at DESC LIMIT %s", (limit,))

@router.get("/api/events/raw")
def list_raw_events(limit: int = 50):
    """Lists raw Razorpay webhook payloads."""
    rows = db.fetchall("SELECT id, event_id, event_type, received_at, processed FROM razorpay_events_raw ORDER BY id DESC LIMIT %s", (limit,))
    return rows

@router.post("/api/seed-demo-data")
def seed_demo_data():
    """
    Seeds a realistic weekly reconciliation batch (approx 15-20 records):
    - Clean matches with direct Razorpay payments
    - Credit notes with adjusted net payments
    - Underpayments (short collections)
    - Tax rate discrepancies (12% vs 18% GST)
    - Duplicate credit notes
    - Orphan settlements (payments with no invoice tag)
    - Realistic memory contexts
    """
    # 1. Clear previous unassigned or demo data if needed
    # Seed Memory Contexts
    db.execute("DELETE FROM memory_context WHERE description LIKE '%[Demo]%'")
    add_memory_context(
        context_type="Tax Rate Change",
        description="[Demo] GST on Category B items revised from 12% to 18% effective Aug 15. Check invoices raised around transition date.",
        effective_date="2026-08-15"
    )
    add_memory_context(
        context_type="Discount Scheme",
        description="[Demo] Apollo Pharmacy eligible for approved 5% prompt settlement cash discount on W34 cycles.",
        effective_date="2026-08-01"
    )
    add_memory_context(
        context_type="Disputed Transaction",
        description="[Demo] INV-2026-DEMO-DISPUTE under dispute regarding damaged transit carton #4491.",
        effective_date="2026-08-20"
    )

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")

    # Helper function to seed an order
    def seed_order(inv, code, name, base, rate, status="PD"):
        base_d = Decimal(str(base))
        rate_d = Decimal(str(rate))
        tax_d = (base_d * rate_d).quantize(Decimal("0.01"))
        tot_d = base_d + tax_d
        db.execute(
            """INSERT OR REPLACE INTO orders (invoice_no, customer_code, customer_name, invoice_date,
                                  base_amount, tax_rate, tax_amount, total_amount, status, cycle_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)""",
            (inv, code, name, date_str, float(base_d), float(rate_d), float(tax_d), float(tot_d), status)
        )
        return tot_d

    # Helper to simulate incoming Razorpay webhook
    def seed_webhook_payment(inv_no, amt_inr, pay_id=None, utr=None):
        if not pay_id:
            pay_id = f"pay_{uuid.uuid4().hex[:12]}"
        if not utr:
            utr = f"UTR{uuid.uuid4().hex[:10].upper()}"
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
                        "amount": int(Decimal(str(amt_inr)) * 100), # paise
                        "currency": "INR",
                        "status": "captured",
                        "order_id": f"order_{uuid.uuid4().hex[:10]}",
                        "method": "netbanking",
                        "created_at": int(now.timestamp()),
                        "notes": {"invoice_no": inv_no} if inv_no else {},
                        "acquirer_data": {"utr": utr}
                    }
                }
            }
        }
        process_razorpay_event(event_payload, json.dumps(event_payload))

    # Helper to seed credit note
    def seed_cn(cn_id, inv_no, amt):
        db.execute(
            """INSERT OR REPLACE INTO credit_notes (cn_no, invoice_no, amount)
               VALUES (%s, %s, %s)""",
            (cn_id, inv_no, float(amt))
        )

    # 1. Clean Matches (5 orders)
    for i in range(1, 6):
        inv = f"INV-2026-MATCH-{i:03d}"
        tot = seed_order(inv, f"CUST-00{i}", f"Metro Retailer {i}", base=1000 * i, rate=0.18)
        seed_webhook_payment(inv, float(tot))

    # 2. Clean Match with Credit Note applied (Order 1180, CN 180, Payment 1000)
    inv_cn = "INV-2026-CN-MATCH-001"
    seed_order(inv_cn, "CUST-010", "MedPlus Distributors", base=1000.00, rate=0.18) # 1180 total
    seed_cn("CN-2026-001", inv_cn, 180.00)
    seed_webhook_payment(inv_cn, 1000.00)

    # 3. Underpayment / Short payment (Order 5900, Payment 5000 -> Delta 900)
    inv_under = "INV-2026-UNDER-001"
    seed_order(inv_under, "CUST-011", "Apollo Pharmacy", base=5000.00, rate=0.18) # 5900 total
    seed_webhook_payment(inv_under, 5605.00) # Short payment (5% prompt discount deduction = 295)

    # 4. Tax Mismatch (Invoiced at 12% instead of 18%)
    inv_tax = "INV-2026-TAX-001"
    seed_order(inv_tax, "CUST-012", "CareMax Healthcare", base=10000.00, rate=0.12) # 11200 total (12%)
    seed_webhook_payment(inv_tax, 11200.00)

    # 5. Duplicate Credit Note (Two credit notes of 500 each entered in error)
    inv_dup = "INV-2026-DUPCN-001"
    seed_order(inv_dup, "CUST-013", "Zenith Pharma", base=2000.00, rate=0.18) # 2360 total
    seed_cn("CN-2026-002A", inv_dup, 500.00)
    seed_cn("CN-2026-002B", inv_dup, 500.00) # Duplicate entry
    seed_webhook_payment(inv_dup, 1860.00) # Customer paid after deducting 1 CN (2360 - 500 = 1860)

    # 6. Disputed Transaction
    inv_disp = "INV-2026-DEMO-DISPUTE"
    seed_order(inv_disp, "CUST-014", "HealthCart Logistics", base=3000.00, rate=0.18) # 3540 total
    seed_webhook_payment(inv_disp, 3000.00) # 540 withheld due to damaged transit

    # 7. Orphan Payment (No invoice tag)
    seed_webhook_payment(None, 4500.00, pay_id=f"pay_orphan_{uuid.uuid4().hex[:8]}", utr="HDFC99881122")

    return {
        "status": "success",
        "message": "Demo data successfully seeded for reconciliation cycle!"
    }
