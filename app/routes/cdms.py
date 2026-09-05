"""CDMS ingestion endpoints, test data seeding, and raw data exploration."""
import json
import uuid
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException, UploadFile, File
import csv
from io import StringIO

from app.database import db
from app.models import OrderCreate, CreditNoteCreate
from app.webhook import process_razorpay_event
from app.memory import add_memory_context

router = APIRouter()

@router.post("/ingest/cdms")
@router.post("/api/cdms/upload")
async def upload_cdms_data(file: UploadFile = File(...)):
    """
    Accepts CSV upload of CDMS orders/credit notes (§1 Path B & §5).
    Pre-validates headers and row integrity; rejects malformed files completely
    rather than partially ingesting (§1 B2).
    """
    content = await file.read()
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=400,
            detail=f"Malformed file encoding for '{file.filename}'. Must be UTF-8 encoded CSV."
        )

    lines = decoded.strip().splitlines()
    if not lines:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    reader = csv.DictReader(StringIO(decoded))
    headers = [h.strip().lower() for h in (reader.fieldnames or [])]

    # Validate header presence
    has_invoice = any("invoice" in h for h in headers)
    has_cn = any("cn" in h for h in headers)
    if not has_invoice and not has_cn:
        raise HTTPException(
            status_code=400,
            detail=f"Header validation failed: File must contain 'invoice_no' or 'cn_no'. Found headers: {reader.fieldnames}"
        )

    rows = list(reader)
    if not rows:
        raise HTTPException(status_code=400, detail="CSV contains headers but zero data rows.")

    # Strict row integrity pre-check (No partial ingest)
    staged_records = []
    now_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for idx, row in enumerate(rows, start=1):
        # Case 1: Credit Note
        if row.get("cn_no"):
            cn_no = str(row["cn_no"]).strip()
            inv_no = str(row.get("invoice_no", "")).strip()
            if not inv_no:
                raise HTTPException(
                    status_code=400,
                    detail=f"Row integrity error at row {idx}: Credit note '{cn_no}' missing mandatory 'invoice_no'. File quarantined without partial ingest."
                )
            try:
                raw_amt = row.get("amount") or row.get("total_amount") or "0"
                amt = float(Decimal(str(raw_amt)))
                if amt <= 0:
                    raise ValueError("Amount must be positive")
            except (ValueError, InvalidOperation):
                raise HTTPException(
                    status_code=400,
                    detail=f"Row integrity error at row {idx}: Invalid credit note amount '{row.get('amount') or row.get('total_amount')}'. File quarantined without partial ingest."
                )
            staged_records.append(("credit_note", {
                "cn_no": cn_no,
                "invoice_no": inv_no,
                "amount": amt
            }))

        # Case 2: Order / Invoice
        elif row.get("invoice_no"):
            inv_no = str(row["invoice_no"]).strip()
            raw_amt = row.get("total_amount") or row.get("amount") or "0"
            try:
                tot = float(Decimal(str(raw_amt)))
                if tot <= 0:
                    raise ValueError("Amount must be positive")
            except (ValueError, InvalidOperation):
                raise HTTPException(
                    status_code=400,
                    detail=f"Row integrity error at row {idx}: Invalid order amount '{raw_amt}'. File quarantined without partial ingest."
                )

            try:
                rate = float(Decimal(str(row.get("tax_rate") or "0.18")))
            except (ValueError, InvalidOperation):
                raise HTTPException(
                    status_code=400,
                    detail=f"Row integrity error at row {idx}: Invalid tax rate '{row.get('tax_rate')}'. File quarantined without partial ingest."
                )

            base = float(Decimal(str(row.get("base_amount") or (tot / (1 + rate)))).quantize(Decimal("0.01")))
            tax = float(Decimal(str(row.get("tax_amount") or (tot - base))).quantize(Decimal("0.01")))

            staged_records.append(("order", {
                "invoice_no": inv_no,
                "customer_code": str(row.get("customer_code") or "CUST-UP").strip(),
                "customer_name": str(row.get("customer_name") or "Direct Ingestion Customer").strip(),
                "invoice_date": str(row.get("invoice_date") or now_date).strip(),
                "base_amount": base,
                "tax_rate": rate,
                "tax_amount": tax,
                "total_amount": tot,
                "status": str(row.get("status") or "PD").strip()
            }))
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Row integrity error at row {idx}: Missing both 'invoice_no' and 'cn_no'. File quarantined without partial ingest."
            )

    # Commit staged records atomically
    rows_ingested = 0
    for rtype, rdata in staged_records:
        if rtype == "credit_note":
            db.execute(
                """INSERT OR REPLACE INTO credit_notes (cn_no, invoice_no, amount)
                   VALUES (%s, %s, %s)""",
                (rdata["cn_no"], rdata["invoice_no"], rdata["amount"])
            )
            rows_ingested += 1
        elif rtype == "order":
            db.execute(
                """INSERT OR REPLACE INTO orders (invoice_no, customer_code, customer_name, invoice_date,
                                      base_amount, tax_rate, tax_amount, total_amount, status, cycle_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)""",
                (
                    rdata["invoice_no"],
                    rdata["customer_code"],
                    rdata["customer_name"],
                    rdata["invoice_date"],
                    rdata["base_amount"],
                    rdata["tax_rate"],
                    rdata["tax_amount"],
                    rdata["total_amount"],
                    rdata["status"]
                )
            )
            rows_ingested += 1

    # Audit log entry
    db.log_audit(
        actor="manual_uploader",
        action="INGEST_CDMS",
        entity_type="cdms_file",
        entity_id=file.filename or "cdms_upload.csv",
        after_state={"rows_ingested": rows_ingested, "filename": file.filename}
    )

    return {
        "status": "success",
        "message": f"Successfully validated and ingested {rows_ingested} records from {file.filename}.",
        "records_ingested": rows_ingested
    }

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
    return db.fetchall("SELECT id, event_id, event_type, received_at, processed FROM razorpay_events_raw ORDER BY id DESC LIMIT %s", (limit,))

@router.post("/api/seed-demo-data")
def seed_demo_data():
    """
    Seeds a realistic weekly reconciliation batch covering all edge cases:
    1. Clean 1:1 matches
    2. Clean matches with Credit Notes
    3. Bulk Payment Allocation (one payment covering multiple invoices)
    4. Installment Allocation (one invoice covered by multiple payments)
    5. Underpayment with 5% prompt discount (Apollo Pharmacy)
    6. Tax Mismatch (CareMax Healthcare 12% vs 18% GST)
    7. Duplicate Credit Note (Zenith Pharma)
    8. Disputed Invoice (HealthCart Logistics)
    9. Orphan Payment (no notes)
    10. Unallocated Bulk Payment
    """
    # Seed Memory Contexts
    db.execute("DELETE FROM memory_context WHERE description LIKE '%[Demo]%'")
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

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")

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

    # 3. Bulk Payment Allocation (One settlement paying two invoices)
    inv_b1 = "INV-2026-BULK-001"
    inv_b2 = "INV-2026-BULK-002"
    tot_b1 = seed_order(inv_b1, "CUST-BULK", "Reliance Retail Group", base=2000.00, rate=0.18) # 2360
    tot_b2 = seed_order(inv_b2, "CUST-BULK", "Reliance Retail Group", base=3000.00, rate=0.18) # 3540
    # Single lump sum payment covering both invoices: 2360 + 3540 = 5900
    seed_webhook_payment(f"{inv_b1}, {inv_b2}", 5900.00, pay_id="pay_bulk_rel_5900")

    # 4. Installment Allocation (One invoice paid via two separate payments)
    inv_inst = "INV-2026-INST-001"
    tot_inst = seed_order(inv_inst, "CUST-INST", "Tata 1mg Health", base=4000.00, rate=0.18) # 4720
    # Payment 1 of 2500, Payment 2 of 2220 = 4720
    seed_webhook_payment(inv_inst, 2500.00, pay_id="pay_inst_part_1")
    seed_webhook_payment(inv_inst, 2220.00, pay_id="pay_inst_part_2")

    # 5. Underpayment / Short payment (Order 5900, Payment 5605 -> 5% prompt discount = 295)
    inv_under = "INV-2026-UNDER-001"
    seed_order(inv_under, "CUST-011", "Apollo Pharmacy", base=5000.00, rate=0.18) # 5900 total
    seed_webhook_payment(inv_under, 5605.00)

    # 6. Tax Mismatch (Invoiced at 12% instead of 18%)
    inv_tax = "INV-2026-TAX-001"
    seed_order(inv_tax, "CUST-012", "CareMax Healthcare", base=10000.00, rate=0.12) # 11200 total (12%)
    seed_webhook_payment(inv_tax, 11200.00)

    # 7. Duplicate Credit Note (Two credit notes of 500 each entered in error)
    inv_dup = "INV-2026-DUPCN-001"
    seed_order(inv_dup, "CUST-013", "Zenith Pharma", base=2000.00, rate=0.18) # 2360 total
    seed_cn("CN-2026-002A", inv_dup, 500.00)
    seed_cn("CN-2026-002B", inv_dup, 500.00)
    seed_webhook_payment(inv_dup, 1860.00)

    # 8. Disputed Transaction
    inv_disp = "INV-2026-DEMO-DISPUTE"
    seed_order(inv_disp, "CUST-014", "HealthCart Logistics", base=3000.00, rate=0.18) # 3540 total
    seed_webhook_payment(inv_disp, 3000.00)

    # 9. Orphan Payment (No invoice tag)
    seed_webhook_payment(None, 4500.00, pay_id=f"pay_orphan_{uuid.uuid4().hex[:8]}", utr="HDFC99881122")

    # 10. Unallocated Bulk Payment
    seed_webhook_payment("bulk_advance_q3", 18500.00, pay_id=f"pay_unalloc_bulk_{uuid.uuid4().hex[:6]}", utr="KKBK776655")

    # Log seed to audit log
    db.log_audit(
        actor="system_demo_seeder",
        action="SEED_DEMO_CYCLE",
        entity_type="reconciliation",
        entity_id="demo_w_current",
        after_state={"orders_count": 13, "settlements_count": 14}
    )

    return {
        "status": "success",
        "message": "Demo data successfully seeded with bulk allocations, installments, and edge cases!"
    }

