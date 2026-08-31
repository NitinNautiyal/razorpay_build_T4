"""Razorpay webhook signature verification and ingestion logic."""
import hmac
import hashlib
import json
from decimal import Decimal
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple

from app.config import WEBHOOK_SECRET
from app.database import db

def verify_signature(body: bytes, signature: Optional[str], secret: str = WEBHOOK_SECRET) -> bool:
    """Verifies Razorpay HMAC-SHA256 signature against webhook secret."""
    if not signature or not secret:
        return False
    
    expected_sig = hmac.new(
        key=secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(expected_sig, signature)

def process_razorpay_event(event: Dict[str, Any], raw_body_str: str) -> Tuple[bool, str, Optional[int]]:
    """
    Ingests raw Razorpay event and normalizes into settlements table.
    Returns (success, message, raw_event_id).
    """
    event_id = event.get("id") or event.get("event_id")
    event_type = event.get("event") or event.get("event_type")
    
    if not event_id or not event_type:
        return False, "Missing event ID or event type", None

    # Idempotent insert into razorpay_events_raw
    # Check if event already exists
    existing = db.fetchone(
        "SELECT id, processed FROM razorpay_events_raw WHERE event_id = %s",
        (event_id,)
    )
    
    if existing:
        return True, "Event already received (idempotent no-op)", existing["id"]

    try:
        raw_event_id = db.execute(
            """INSERT INTO razorpay_events_raw (event_id, event_type, payload, processed)
               VALUES (%s, %s, %s, %s)""",
            (event_id, event_type, raw_body_str, 0)
        )
    except Exception as e:
        # SQLite / Postgres unique constraint race condition
        existing = db.fetchone("SELECT id FROM razorpay_events_raw WHERE event_id = %s", (event_id,))
        if existing:
            return True, "Event already received", existing["id"]
        return False, f"Failed to record raw event: {str(e)}", None

    # Process supported event types
    # Supported: payment.captured, refund.created, refund.processed, settlement.processed
    if event_type in ("payment.captured", "refund.created", "refund.processed", "settlement.processed"):
        payload_data = event.get("payload", {})
        
        # Payment captured event
        if "payment" in payload_data:
            p = payload_data["payment"].get("entity", {})
            payment_id = p.get("id")
            if payment_id:
                utr = p.get("acquirer_data", {}).get("utr")
                invoice_no = p.get("notes", {}).get("invoice_no") if p.get("notes") else None
                # Razorpay amount is in paise (1 INR = 100 paise)
                raw_amt = p.get("amount", 0)
                amount = Decimal(str(raw_amt)) / Decimal("100")
                
                # Timestamp conversion
                created_at_ts = p.get("created_at")
                if created_at_ts:
                    settled_at = datetime.fromtimestamp(created_at_ts, tz=timezone.utc)
                else:
                    settled_at = datetime.now(timezone.utc)

                # Insert or update settlements
                existing_stl = db.fetchone("SELECT payment_id FROM settlements WHERE payment_id = %s", (payment_id,))
                if not existing_stl:
                    db.execute(
                        """INSERT INTO settlements (payment_id, utr, invoice_no, amount, settled_at, raw_event_id)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (payment_id, utr, invoice_no, float(amount), settled_at.isoformat(), raw_event_id)
                    )
                else:
                    # Update invoice_no or utr if newly available
                    if invoice_no:
                        db.execute(
                            "UPDATE settlements SET invoice_no = %s WHERE payment_id = %s AND invoice_no IS NULL",
                            (invoice_no, payment_id)
                        )

        # Mark raw event as processed
        db.execute("UPDATE razorpay_events_raw SET processed = 1 WHERE id = %s", (raw_event_id,))

    return True, "Webhook processed successfully", raw_event_id
