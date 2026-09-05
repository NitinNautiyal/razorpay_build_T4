"""Unit and integration tests for v2 enhancements: allocations, run lock, lifecycle, audit log."""
import json
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import db
from app.reconciliation import run_reconciliation, get_exceptions, check_cycle_readiness
from tests.helpers import seed_order, seed_credit_note, fake_payment_captured_event, handle_razorpay_webhook

@pytest.fixture(autouse=True)
def clean_db():
    db.init_db()
    db.clear_tables()
    yield

@pytest.mark.asyncio
async def test_bulk_and_installment_settlement_allocations():
    # Bulk payment: 1 settlement for 2 invoices
    seed_order("INV-BLK-1", total_amount=2000.00, tax_rate=0.18)
    seed_order("INV-BLK-2", total_amount=3000.00, tax_rate=0.18)
    handle_razorpay_webhook(fake_payment_captured_event(
        invoice_no="INV-BLK-1, INV-BLK-2",
        amount=5000.00,
        payment_id="pay_bulk_test"
    ))

    # Installment payment: 2 settlements for 1 invoice
    seed_order("INV-INST-1", total_amount=4000.00, tax_rate=0.18)
    handle_razorpay_webhook(fake_payment_captured_event(invoice_no="INV-INST-1", amount=2500.00, payment_id="pay_inst_1"))
    handle_razorpay_webhook(fake_payment_captured_event(invoice_no="INV-INST-1", amount=1500.00, payment_id="pay_inst_2"))

    run_id = run_reconciliation()
    exceptions = get_exceptions(run_id)
    assert len(exceptions) == 0  # Both should be cleanly matched via allocations!

    allocs = db.fetchall("SELECT * FROM settlement_allocations ORDER BY id ASC")
    assert len(allocs) >= 3
    # Check bulk allocations
    bulk_allocs = [a for a in allocs if a["settlement_id"] == "pay_bulk_test"]
    assert len(bulk_allocs) == 2
    assert sum(float(a["allocated_amount"]) for a in bulk_allocs) == 5000.00

    # Check installment allocations
    inst_allocs = [a for a in allocs if a["order_id"] == "INV-INST-1"]
    assert len(inst_allocs) == 2
    assert sum(float(a["allocated_amount"]) for a in inst_allocs) == 4000.00

@pytest.mark.asyncio
async def test_run_lock_and_queue():
    # Simulate a run in progress
    db.execute(
        "INSERT INTO reconciliation_runs (id, cycle_label, started_at, status, lock_acquired) VALUES ('run_active', 'W-ACTIVE', '2026-09-01T00:00:00', 'running', 1)"
    )

    queued_id = run_reconciliation(cycle_label="W-QUEUED")
    queued_run = db.fetchone("SELECT * FROM reconciliation_runs WHERE id = %s", (queued_id,))
    assert queued_run["status"] == "queued"
    assert "already in progress" in queued_run["queued_reason"].lower()

@pytest.mark.asyncio
async def test_exception_lifecycle_and_audit_trail():
    seed_order("INV-UNDER-TEST", total_amount=2000.00, tax_rate=0.18)
    handle_razorpay_webhook(fake_payment_captured_event("INV-UNDER-TEST", 1600.00))

    run_id = run_reconciliation()
    exceptions = get_exceptions(run_id)
    assert len(exceptions) == 1
    exc_id = exceptions[0]["id"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # 1. Escalate with SLA aging
        res_esc = await ac.patch(
            f"/reconciliation/exceptions/{exc_id}",
            json={"action": "escalate", "note": "Customer disputed freight charge.", "actor": "analyst_ram"}
        )
        assert res_esc.status_code == 200
        exc_data = res_esc.json()["exception"]
        assert exc_data["status"] == "escalated"
        assert exc_data["aging_hours"] >= 0.1

        # 2. Accept remark
        res_acc = await ac.patch(
            f"/reconciliation/exceptions/{exc_id}",
            json={"action": "accept", "note": "Approved 5% discount settlement.", "actor": "controller_nitin"}
        )
        assert res_acc.status_code == 200
        assert res_acc.json()["exception"]["status"] == "resolved"

        # 3. Reopen resolution
        res_reopen = await ac.patch(
            f"/reconciliation/exceptions/{exc_id}",
            json={"action": "reopen", "note": "Reversed acceptance due to late credit note.", "actor": "auditor_sneha"}
        )
        assert res_reopen.status_code == 200
        assert res_reopen.json()["exception"]["status"] == "reopened"

        # 4. Check audit log
        res_audit = await ac.get("/audit-log")
        assert res_audit.status_code == 200
        logs = res_audit.json()["audit_logs"]
        actions = [l["action"] for l in logs if l["entity_id"] == str(exc_id)]
        assert "ESCALATE" in actions
        assert "ACCEPT_REMARK" in actions
        assert "REOPEN" in actions

@pytest.mark.asyncio
async def test_batch_resolve_pattern():
    seed_order("INV-PAT-1", total_amount=1000.00, customer_name="Apollo Pharmacy", tax_rate=0.18)
    seed_order("INV-PAT-2", total_amount=2000.00, customer_name="Apollo Pharmacy", tax_rate=0.18)
    handle_razorpay_webhook(fake_payment_captured_event("INV-PAT-1", 950.00))
    handle_razorpay_webhook(fake_payment_captured_event("INV-PAT-2", 1900.00))

    run_id = run_reconciliation()
    exceptions = get_exceptions(run_id)
    assert len(exceptions) == 2

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        pat_key = exceptions[0]["pattern_key"] or "Apollo Pharmacy:Underpayment / Pending Collection"
        res_batch = await ac.post(
            "/reconciliation/exceptions/batch-resolve-pattern",
            json={"pattern_key": pat_key, "note": "Authorized 5% prompt discount applied to all", "actor": "controller_nitin"}
        )
        assert res_batch.status_code == 200
        assert res_batch.json()["resolved_count"] == 2

        # Verify exceptions are now resolved
        res_updated = await ac.get(f"/reconciliation/runs/{run_id}/exceptions")
        for e in res_updated.json()["exceptions"]:
            assert e["status"] == "resolved"

@pytest.mark.asyncio
async def test_cdms_upload_strict_rejection():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Invalid CSV with non-numeric amount at row 2
        bad_csv = """invoice_no,customer_name,total_amount,tax_rate
INV-GOOD,Customer A,1000,0.18
INV-BAD,Customer B,NOT_A_NUMBER,0.18"""
        files = {"file": ("bad_orders.csv", bad_csv.encode("utf-8"), "text/csv")}
        res = await ac.post("/ingest/cdms", files=files)
        assert res.status_code == 400
        assert "row 2" in res.json()["detail"].lower()
        assert "quarantined" in res.json()["detail"].lower()

        # Verify no rows were partially ingested
        orders = db.fetchall("SELECT * FROM orders WHERE invoice_no = 'INV-GOOD'")
        assert len(orders) == 0

@pytest.mark.asyncio
async def test_readiness_status_and_role_gated_config():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # 1. Check readiness when only CDMS orders exist
        seed_order("INV-ALONE", total_amount=1000.00, tax_rate=0.18)
        res_status = await ac.get("/reconciliation/status")
        assert res_status.status_code == 200
        assert res_status.json()["status"] == "awaiting_settlements"

        # 2. Role-gated config: unauthorized role should fail
        res_bad_role = await ac.patch(
            "/reconciliation/config",
            json={"tolerance": 0.15, "role": "viewer"}
        )
        assert res_bad_role.status_code == 403

        # 3. Authorized role should succeed
        res_ok_role = await ac.patch(
            "/reconciliation/config",
            json={"tolerance": 0.12, "role": "finance_controller", "actor": "controller_nitin"}
        )
        assert res_ok_role.status_code == 200
        assert res_ok_role.json()["config"]["tolerance"] == 0.12
