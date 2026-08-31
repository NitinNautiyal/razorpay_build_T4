"""Unit tests for Razorpay webhook ingestion, signature verification, and idempotency."""
import json
import hmac
import hashlib
import pytest
from app.config import WEBHOOK_SECRET
from app.database import db
from app.webhook import verify_signature, process_razorpay_event
from tests.helpers import fake_payment_captured_event

@pytest.fixture(autouse=True)
def clean_db():
    db.init_db()
    db.clear_tables()
    yield

def test_signature_verification():
    secret = "test_webhook_secret_key"
    payload = b'{"event":"payment.captured"}'
    
    # Correct signature
    valid_sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    assert verify_signature(payload, valid_sig, secret) is True

    # Invalid signature
    invalid_sig = "incorrect_signature_hash"
    assert verify_signature(payload, invalid_sig, secret) is False

    # Missing signature or secret
    assert verify_signature(payload, None, secret) is False
    assert verify_signature(payload, valid_sig, "") is False

def test_webhook_idempotency():
    """Duplicate delivery of same event_id should no-op and return 200 without duplication."""
    event = fake_payment_captured_event(invoice_no="INV-101", amount=1500.00, event_id="evt_idempotency_1")
    event_str = json.dumps(event)

    # First delivery
    success1, msg1, raw_id1 = process_razorpay_event(event, event_str)
    assert success1 is True
    assert raw_id1 is not None

    # Second delivery (same event_id)
    success2, msg2, raw_id2 = process_razorpay_event(event, event_str)
    assert success2 is True
    assert raw_id2 == raw_id1
    assert "already" in msg2.lower()

    # Verify only 1 row in razorpay_events_raw and 1 row in settlements
    raw_rows = db.fetchall("SELECT * FROM razorpay_events_raw WHERE event_id = 'evt_idempotency_1'")
    assert len(raw_rows) == 1

    settlement_rows = db.fetchall("SELECT * FROM settlements WHERE invoice_no = 'INV-101'")
    assert len(settlement_rows) == 1
    assert float(settlement_rows[0]["amount"]) == 1500.00

def test_settlement_normalization_and_notes():
    """Verifies that paise is correctly converted to rupees and invoice_no is captured from notes."""
    event = fake_payment_captured_event(
        invoice_no="INV-NORM-01",
        amount=2450.75,
        payment_id="pay_norm_123"
    )
    success, msg, raw_id = process_razorpay_event(event, json.dumps(event))
    assert success is True

    stl = db.fetchone("SELECT * FROM settlements WHERE payment_id = 'pay_norm_123'")
    assert stl is not None
    assert stl["invoice_no"] == "INV-NORM-01"
    assert float(stl["amount"]) == 2450.75
    assert stl["raw_event_id"] == raw_id
