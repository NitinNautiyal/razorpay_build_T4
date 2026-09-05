"""Core Reconciliation matching, allocation resolution, and exception classification engine."""
import uuid
import json
from decimal import Decimal
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

from app.config import TOLERANCE, STANDARD_TAX_RATE
from app.database import db
from app.llm_agent import run_llm_remark_pass

def check_cycle_readiness() -> Dict[str, Any]:
    """
    Evaluates ingestion state (§1 A7).
    Determines if cycle is ready, awaiting settlements, awaiting CDMS data, or running.
    """
    # Check if run currently executing
    active_run = db.fetchone(
        "SELECT id, cycle_label, started_at FROM reconciliation_runs WHERE status = 'running' OR lock_acquired = 1"
    )
    if active_run:
        return {
            "status": "running",
            "message": "Reconciliation run in progress for active window.",
            "active_run_id": active_run["id"]
        }

    # Check pending/unassigned orders and settlements
    orders_count_row = db.fetchone("SELECT COUNT(*) as cnt FROM orders") or {"cnt": 0}
    settlements_count_row = db.fetchone("SELECT COUNT(*) as cnt FROM settlements") or {"cnt": 0}
    
    orders_cnt = orders_count_row.get("cnt", 0)
    stl_cnt = settlements_count_row.get("cnt", 0)

    if orders_cnt > 0 and stl_cnt == 0:
        return {
            "status": "awaiting_settlements",
            "message": "Awaiting Settlement Data: CDMS orders ingested but Razorpay settlements pending.",
            "orders_count": orders_cnt,
            "settlements_count": 0
        }
    elif stl_cnt > 0 and orders_cnt == 0:
        return {
            "status": "awaiting_cdms",
            "message": "Awaiting CDMS Data: Razorpay settlements present but CDMS orders not uploaded.",
            "orders_count": 0,
            "settlements_count": stl_cnt
        }
    elif orders_cnt == 0 and stl_cnt == 0:
        return {
            "status": "empty",
            "message": "No reconciliation data found. Upload CDMS data or seed demo cycle.",
            "orders_count": 0,
            "settlements_count": 0
        }
    
    return {
        "status": "ready",
        "message": "Ready to run",
        "orders_count": orders_cnt,
        "settlements_count": stl_cnt
    }

def classify_exception(
    order: Dict[str, Any],
    cn_sum: Decimal,
    cn_count: int,
    stl_sum: Decimal,
    delta: Decimal,
    memory_contexts: Optional[List[Dict[str, Any]]] = None
) -> str:
    """Classifies financial discrepancy into specific error types."""
    inv_no = order.get("invoice_no", "")

    # Check for Disputed Invoice in memory context (PRD §7.5)
    if memory_contexts:
        for ctx in memory_contexts:
            if "dispute" in ctx.get("context_type", "").lower() or "dispute" in ctx.get("description", "").lower():
                if inv_no and inv_no.lower() in ctx.get("description", "").lower():
                    return "Disputed Invoice"

    # Check for Duplicate Credit Note
    if cn_count > 1 and cn_sum > 0:
        return "Duplicate Credit Note"

    # Check for Tax Mismatch (e.g. invoice tax_rate differs from standard 18%)
    tax_rate = Decimal(str(order.get("tax_rate", 0)))
    base_amount = Decimal(str(order.get("base_amount", 0)))
    tax_amount = Decimal(str(order.get("tax_amount", 0)))
    
    expected_standard_tax = (base_amount * STANDARD_TAX_RATE).quantize(Decimal("0.01"))
    if tax_rate != STANDARD_TAX_RATE and abs(tax_amount - expected_standard_tax) > TOLERANCE:
        return "Tax Mismatch"

    # Delta classification
    if delta > TOLERANCE:
        return "Underpayment / Pending Collection"
    elif delta < -TOLERANCE:
        return "Overpayment / Excess Settlement"

    return "Unclassified Discrepancy"

def resolve_settlement_allocations() -> None:
    """
    Allocates settlements across orders (§3 & §4).
    Resolves bulk payments (many-to-one) and installments (one-to-many).
    Populates `settlement_allocations` table.
    """
    # Fetch unallocated settlements
    settlements = db.fetchall(
        """SELECT s.payment_id, s.utr, s.invoice_no, s.amount
           FROM settlements s
           WHERE s.payment_id NOT IN (SELECT DISTINCT settlement_id FROM settlement_allocations)"""
    )
    if not settlements:
        return

    orders_by_inv = {
        o["invoice_no"]: o
        for o in db.fetchall("SELECT invoice_no, customer_code, customer_name, total_amount FROM orders")
    }

    for stl in settlements:
        pid = stl["payment_id"]
        inv_str = stl.get("invoice_no") or ""
        stl_amt = Decimal(str(stl["amount"]))

        if not inv_str:
            continue

        # Handle multiple comma/semicolon-separated invoices (Bulk Payment)
        if "," in inv_str or ";" in inv_str:
            tokens = [t.strip() for t in inv_str.replace(";", ",").split(",") if t.strip()]
            valid_orders = [orders_by_inv[t] for t in tokens if t in orders_by_inv]
            
            if valid_orders:
                remaining_amt = stl_amt
                for vo in valid_orders:
                    ord_tot = Decimal(str(vo["total_amount"]))
                    alloc = min(ord_tot, remaining_amt)
                    if alloc > 0:
                        db.execute(
                            """INSERT INTO settlement_allocations (settlement_id, order_id, allocated_amount, allocation_type)
                               VALUES (%s, %s, %s, 'auto')""",
                            (pid, vo["invoice_no"], float(alloc))
                        )
                        remaining_amt -= alloc
                continue

        # Single invoice tag: direct or installment allocation
        if inv_str in orders_by_inv:
            db.execute(
                """INSERT INTO settlement_allocations (settlement_id, order_id, allocated_amount, allocation_type)
                   VALUES (%s, %s, %s, 'auto')""",
                (pid, inv_str, float(stl_amt))
            )

def run_reconciliation(cycle_label: Optional[str] = None, skip_llm: bool = False) -> str:
    """
    Executes a reconciliation cycle with run locking, settlement allocations,
    deterministic matching, and batched LLM remarks.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    if not cycle_label:
        cycle_label = f"W{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"

    # 1. Run Lock Check (§1 B4 & §6)
    active_run = db.fetchone(
        "SELECT id, cycle_label, started_at FROM reconciliation_runs WHERE status = 'running' OR lock_acquired = 1"
    )
    if active_run:
        # Another run in progress: queue behind it
        queued_run_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO reconciliation_runs (id, cycle_label, started_at, status, lock_acquired, queued_reason)
               VALUES (%s, %s, %s, 'queued', 0, %s)""",
            (
                queued_run_id,
                cycle_label,
                now_iso,
                f"Run already in progress ({active_run['id']}); queued behind active run"
            )
        )
        return queued_run_id

    run_id = str(uuid.uuid4())

    # 2. Acquire Lock and Create Run Record with status 'running'
    db.execute(
        """INSERT INTO reconciliation_runs (id, cycle_label, started_at, status, lock_acquired)
           VALUES (%s, %s, %s, 'running', 1)""",
        (run_id, cycle_label, now_iso)
    )

    try:
        # 3. Attach unassigned orders to this cycle run
        db.execute(
            "UPDATE orders SET cycle_id = %s WHERE cycle_id IS NULL",
            (run_id,)
        )

        # 4. Resolve settlement allocations (bulk / installments)
        resolve_settlement_allocations()

        # 5. Core Match Query
        # Aggregates credit notes and allocated settlements per order for this run
        query = """
        WITH cn_totals AS (
          SELECT invoice_no, SUM(amount) AS cn_sum, COUNT(*) AS cn_count
          FROM credit_notes GROUP BY invoice_no
        ),
        alloc_totals AS (
          SELECT order_id AS invoice_no, SUM(allocated_amount) AS stl_sum
          FROM settlement_allocations GROUP BY order_id
        ),
        direct_stl_totals AS (
          SELECT invoice_no, SUM(amount) AS direct_stl_sum
          FROM settlements
          WHERE invoice_no IS NOT NULL AND payment_id NOT IN (SELECT settlement_id FROM settlement_allocations)
          GROUP BY invoice_no
        )
        SELECT o.invoice_no, o.customer_code, o.customer_name,
               o.base_amount, o.tax_rate, o.tax_amount, o.total_amount, o.status,
               COALESCE(cn.cn_sum, 0) AS cn_sum,
               COALESCE(cn.cn_count, 0) AS cn_count,
               COALESCE(al.stl_sum, 0) + COALESCE(dstl.direct_stl_sum, 0) AS stl_sum
        FROM orders o
        LEFT JOIN cn_totals cn ON cn.invoice_no = o.invoice_no
        LEFT JOIN alloc_totals al ON al.invoice_no = o.invoice_no
        LEFT JOIN direct_stl_totals dstl ON dstl.invoice_no = o.invoice_no
        WHERE o.cycle_id = %s
        """
        orders = db.fetchall(query, (run_id,))

        # Load active memory contexts for domain classification (PRD §7.5)
        memory_contexts = db.fetchall("SELECT * FROM memory_context ORDER BY created_at DESC")

        exceptions_to_insert = []
        matched_count = 0

        for o in orders:
            total_amt = Decimal(str(o["total_amount"]))
            cn_sum = Decimal(str(o["cn_sum"]))
            cn_count = int(o["cn_count"])
            stl_sum = Decimal(str(o["stl_sum"]))

            # delta = order.total_amount - credit_notes - settlements
            delta = (total_amt - cn_sum - stl_sum).quantize(Decimal("0.01"))

            if abs(delta) <= TOLERANCE:
                # Order is cleanly matched within tolerance
                matched_count += 1
            else:
                error_type = classify_exception(o, cn_sum, cn_count, stl_sum, delta, memory_contexts)
                exceptions_to_insert.append({
                    "run_id": run_id,
                    "invoice_no": o["invoice_no"],
                    "customer_name": o["customer_name"],
                    "delta": float(delta),
                    "error_type": error_type,
                    "order_total": float(total_amt),
                    "base_amount": float(o["base_amount"]),
                    "tax_rate": float(o["tax_rate"]),
                    "cn_sum": float(cn_sum),
                    "cn_count": cn_count,
                    "stl_sum": float(stl_sum),
                    "pattern_key": f"{o['customer_name']}:{error_type}"
                })

        # 6. Orphan and Unallocated Settlements Pass
        orphan_query = """
        SELECT s.payment_id, s.utr, s.invoice_no, s.amount, s.settled_at
        FROM settlements s
        WHERE s.payment_id NOT IN (SELECT DISTINCT settlement_id FROM settlement_allocations)
          AND (s.invoice_no IS NULL OR s.invoice_no NOT IN (SELECT invoice_no FROM orders))
        """
        orphans = db.fetchall(orphan_query)
        for orphan in orphans:
            orphan_amt = Decimal(str(orphan["amount"]))
            is_bulk = "bulk" in (orphan.get("invoice_no") or "").lower() or (orphan_amt > 15000 and orphan.get("invoice_no"))
            err_type = "Unallocated Bulk Payment" if is_bulk else "Unmatched Settlement / Orphan Payment"
            cust_name = "Bulk Remittance Cust" if is_bulk else "Unmatched Cash Receipt"

            exceptions_to_insert.append({
                "run_id": run_id,
                "invoice_no": orphan.get("invoice_no"),
                "customer_name": cust_name,
                "delta": float(-orphan_amt),
                "error_type": err_type,
                "payment_id": orphan.get("payment_id"),
                "utr": orphan.get("utr"),
                "order_total": 0.0,
                "base_amount": 0.0,
                "tax_rate": float(STANDARD_TAX_RATE),
                "cn_sum": 0.0,
                "cn_count": 0,
                "stl_sum": float(orphan_amt),
                "pattern_key": f"{cust_name}:{err_type}"
            })

        # 7. Insert generated exceptions into database
        for exc in exceptions_to_insert:
            db.execute(
                """INSERT INTO exceptions (run_id, invoice_no, customer_name, delta, error_type, remark, resolved, status, pattern_key)
                   VALUES (%s, %s, %s, %s, %s, %s, 0, 'open', %s)""",
                (
                    exc["run_id"],
                    exc["invoice_no"],
                    exc["customer_name"],
                    exc["delta"],
                    exc["error_type"],
                    None, # Will be filled by LLM pass
                    exc["pattern_key"]
                )
            )

        # 8. Calculate Run Metrics
        total_records = len(orders) + len(orphans)
        match_rate = Decimal("100.00")
        if total_records > 0:
            match_rate = (Decimal(str(matched_count)) / Decimal(str(total_records)) * Decimal("100")).quantize(Decimal("0.01"))

        finished_iso = datetime.now(timezone.utc).isoformat()
        db.execute(
            """UPDATE reconciliation_runs
               SET finished_at = %s, total_records = %s, matched_count = %s, match_rate = %s, status = 'complete', lock_acquired = 0
               WHERE id = %s""",
            (finished_iso, total_records, matched_count, float(match_rate), run_id)
        )

        # 9. Batched LLM Remark & Insights Pass
        if not skip_llm and exceptions_to_insert:
            try:
                run_llm_remark_pass(run_id, exceptions_to_insert)
            except Exception as e:
                print(f"Warning: LLM pass encountered error: {e}")

        # Log completion to audit_log
        db.log_audit(
            actor="system_scheduler",
            action="COMPLETE_RUN",
            entity_type="run",
            entity_id=run_id,
            after_state={"total_records": total_records, "matched_count": matched_count, "match_rate": float(match_rate)}
        )

        return run_id

    except Exception as e:
        # Record failure and release lock
        db.execute(
            "UPDATE reconciliation_runs SET status = 'failed', error_message = %s, lock_acquired = 0 WHERE id = %s",
            (str(e), run_id)
        )
        raise

def get_exceptions(run_id: str) -> List[Dict[str, Any]]:
    """Retrieves all exceptions for a given run ID with casted fields."""
    rows = db.fetchall(
        "SELECT * FROM exceptions WHERE run_id = %s ORDER BY id ASC",
        (run_id,)
    )
    for r in rows:
        if r.get("delta") is not None:
            r["delta"] = float(r["delta"])
        r["resolved"] = bool(r.get("resolved"))
        if not r.get("status"):
            r["status"] = "resolved" if r["resolved"] else "open"
    return rows

