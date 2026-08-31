"""Self-Check Smoke Test conforming to Engineering Spec v1."""
import pytest
from app.database import db
from app.reconciliation import run_reconciliation, get_exceptions
from tests.helpers import seed_order, seed_credit_note, fake_payment_captured_event, handle_razorpay_webhook

@pytest.fixture(autouse=True)
def setup_db():
    db.init_db()
    db.clear_tables()
    yield

def test_recon_pipeline_smoke():
    """One invoice, one credit note, one settlement, one webhook replay —
    asserts the full path produces the expected match/exception."""
    seed_order(invoice_no="INV1", total_amount=1180.00, tax_rate=0.18)
    seed_credit_note(invoice_no="INV1", amount=180.00)
    handle_razorpay_webhook(fake_payment_captured_event(invoice_no="INV1", amount=1000.00))
    run_id = run_reconciliation()
    result = get_exceptions(run_id)
    assert result == []  # fully matched: 1180 - 180 - 1000 = 0

    # underpayment case
    seed_order(invoice_no="INV2", total_amount=1000.00, tax_rate=0.18)
    handle_razorpay_webhook(fake_payment_captured_event(invoice_no="INV2", amount=700.00))
    run_id2 = run_reconciliation()
    exc = get_exceptions(run_id2)
    assert exc[0]["error_type"] == "Underpayment / Pending Collection"
    assert exc[0]["delta"] == 300.00
