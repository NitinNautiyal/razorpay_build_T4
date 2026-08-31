"""Test helper fixtures and seeders for tests and smoke runs."""
import json
import uuid
from decimal import Decimal
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from app.database import db
from app.webhook import process_razorpay_event
from app.reconciliation import run_reconciliation, get_exceptions

def seed_order(
    invoice_no: str,
    total_amount: float,
    tax_rate: float = 0.18,
    customer_code: str = "CUST-SMOKE",
    customer_name: str = "Smoke Test Customer",
    status: str = "PD"
) -> str:
    """Inserts a test order into the database."""
    total_d = Decimal(str(total_amount))
    rate_d = Decimal(str(tax_rate))
    # Calculate base and tax from total and tax_rate: total = base * (1 + rate)
    base_d = (total_d / (Decimal("1.0") + rate_d)).quantize(Decimal("0.01"))
    tax_d = (total_d - base_d).quantize(Decimal("0.01"))

    db.execute(
        """INSERT OR REPLACE INTO orders (invoice_no, customer_code, customer_name, invoice_date,
                              base_amount, tax_rate, tax_amount, total_amount, status, cycle_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)""",
        (
            invoice_no,
            customer_code,
            customer_name,
            datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            float(base_d),
            float(rate_d),
            float(tax_d),
            float(total_d),
            status
        )
    )
    return invoice_no

def seed_credit_note(invoice_no: str, amount: float, cn_no: Optional[str] = None) -> str:
    """Inserts a test credit note into the database."""
    if not cn_no:
        cn_no = f"CN-{uuid.uuid4().hex[:8]}"
    db.execute(
        """INSERT OR REPLACE INTO credit_notes (cn_no, invoice_no, amount)
           VALUES (%s, %s, %s)""",
        (cn_no, invoice_no, float(amount))
    )
    return cn_no

def fake_payment_captured_event(
    invoice_no: Optional[str],
    amount: float,
    payment_id: Optional[str] = None,
    event_id: Optional[str] = None
) -> Dict[str, Any]:
    """Builds a realistic Razorpay payment.captured webhook payload."""
    if not payment_id:
        payment_id = f"pay_{uuid.uuid4().hex[:12]}"
    if not event_id:
        event_id = f"evt_{uuid.uuid4().hex[:12]}"

    now_ts = int(datetime.now(timezone.utc).timestamp())
    amount_in_paise = int(Decimal(str(amount)) * 100)

    return {
        "id": event_id,
        "entity": "event",
        "event": "payment.captured",
        "contains": ["payment"],
        "created_at": now_ts,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": amount_in_paise,
                    "currency": "INR",
                    "status": "captured",
                    "order_id": f"order_{uuid.uuid4().hex[:10]}",
                    "method": "card",
                    "created_at": now_ts,
                    "notes": {"invoice_no": invoice_no} if invoice_no else {},
                    "acquirer_data": {
                        "utr": f"UTR{uuid.uuid4().hex[:8].upper()}"
                    }
                }
            }
        }
    }

def handle_razorpay_webhook(event: Dict[str, Any]) -> Tuple[bool, str, Optional[int]]:
    """Direct helper to process webhook event."""
    return process_razorpay_event(event, json.dumps(event))
