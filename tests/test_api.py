"""Integration tests for REST API endpoints."""
import json
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.database import db

@pytest.fixture(autouse=True)
def clean_db():
    db.init_db()
    db.clear_tables()
    yield

@pytest.mark.asyncio
async def test_healthcheck_and_ui_root():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/health")
        assert res.status_code == 200
        assert res.json()["status"] == "ok"

        res_ui = await ac.get("/")
        assert res_ui.status_code == 200
        assert "Finance Controller" in res_ui.text

@pytest.mark.asyncio
async def test_seed_and_reconciliation_flow():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # 1. Seed demo data
        res_seed = await ac.post("/api/seed-demo-data")
        assert res_seed.status_code == 200

        # 2. Trigger reconciliation
        res_recon = await ac.post("/internal/run-reconciliation")
        assert res_recon.status_code == 200
        recon_data = res_recon.json()
        assert recon_data["status"] == "completed"
        run_id = recon_data["run"]["id"]

        # 3. Fetch runs
        res_runs = await ac.get("/api/runs")
        assert res_runs.status_code == 200
        assert len(res_runs.json()) >= 1

        # 4. Fetch run details
        res_run_detail = await ac.get(f"/api/runs/{run_id}")
        assert res_run_detail.status_code == 200
        assert len(res_run_detail.json()["exceptions"]) > 0

        # 5. Resolve an exception
        exc_id = res_run_detail.json()["exceptions"][0]["id"]
        res_resolve = await ac.patch(
            f"/api/exceptions/{exc_id}/resolve",
            json={"resolved": True, "resolved_note": "Verified customer payment in bank ledger."}
        )
        assert res_resolve.status_code == 200
        assert res_resolve.json()["exception"]["resolved"] is True
        assert res_resolve.json()["exception"]["resolved_note"] == "Verified customer payment in bank ledger."

@pytest.mark.asyncio
async def test_memory_context_crud():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Create memory context
        res_create = await ac.post(
            "/api/memory-context",
            json={
                "context_type": "Policy Change",
                "description": "2% late payment penalty waived for August.",
                "effective_date": "2026-08-01"
            }
        )
        assert res_create.status_code == 200
        ctx_id = res_create.json()["context"]["id"]

        # List memory contexts
        res_list = await ac.get("/api/memory-context")
        assert res_list.status_code == 200
        assert any(c["id"] == ctx_id for c in res_list.json())

        # Delete memory context
        res_del = await ac.delete(f"/api/memory-context/{ctx_id}")
        assert res_del.status_code == 200
