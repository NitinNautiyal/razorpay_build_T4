"""Core Reconciliation matching and exception classification engine."""
import uuid
from decimal import Decimal
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

from app.config import TOLERANCE, STANDARD_TAX_RATE
from app.database import db
from app.llm_agent import run_llm_remark_pass

def classify_exception(
    order: Dict[str, Any],
    cn_sum: Decimal,
    cn_count: int,
    stl_sum: Decimal,
    delta: Decimal
) -> str:
    """Classifies financial discrepancy into specific error types."""
    # Check for Duplicate Credit Note
    if cn_count > 1 and cn_sum > 0:
        return "Duplicate Credit Note"

    # Check for Tax Mismatch
    # E.g., invoice tax_rate differs from standard 18% (e.g. 12% or 5%)
    tax_rate = Decimal(str(order.get("tax_rate", 0)))
    base_amount = Decimal(str(order.get("base_amount", 0)))
    tax_amount = Decimal(str(order.get("tax_amount", 0)))
    
    expected_standard_tax = (base_amount * STANDARD_TAX_RATE).quantize(Decimal("0.01"))
    if tax_rate != STANDARD_TAX_RATE and abs(tax_amount - expected_standard_tax) > TOLERANCE:
        # Check if delta aligns with tax rate difference
        return "Tax Mismatch"

    # Delta classification
    if delta > TOLERANCE:
        return "Underpayment / Pending Collection"
    elif delta < -TOLERANCE:
        return "Overpayment / Excess Settlement"

    return "Unclassified Discrepancy"

def run_reconciliation(cycle_label: Optional[str] = None, skip_llm: bool = False) -> str:
    """
    Executes a reconciliation cycle over pending orders, credit notes, and settlements.
    Returns the reconciliation run_id (UUID string).
    """
    run_id = str(uuid.uuid4())
    if not cycle_label:
        cycle_label = f"W{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"

    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Create reconciliation run record
    db.execute(
        """INSERT INTO reconciliation_runs (id, cycle_label, started_at)
           VALUES (%s, %s, %s)""",
        (run_id, cycle_label, now_iso)
    )

    # 2. Attach unassigned orders to this cycle run
    db.execute(
        "UPDATE orders SET cycle_id = %s WHERE cycle_id IS NULL",
        (run_id,)
    )

    # 3. Core Match Query
    # Aggregates credit notes and settlements per order for this run
    query = """
    WITH cn_totals AS (
      SELECT invoice_no, SUM(amount) AS cn_sum, COUNT(*) AS cn_count
      FROM credit_notes GROUP BY invoice_no
    ),
    stl_totals AS (
      SELECT invoice_no, SUM(amount) AS stl_sum
      FROM settlements WHERE invoice_no IS NOT NULL GROUP BY invoice_no
    )
    SELECT o.invoice_no, o.customer_code, o.customer_name,
           o.base_amount, o.tax_rate, o.tax_amount, o.total_amount, o.status,
           COALESCE(cn.cn_sum, 0) AS cn_sum,
           COALESCE(cn.cn_count, 0) AS cn_count,
           COALESCE(s.stl_sum, 0) AS stl_sum
    FROM orders o
    LEFT JOIN cn_totals cn ON cn.invoice_no = o.invoice_no
    LEFT JOIN stl_totals s ON s.invoice_no = o.invoice_no
    WHERE o.cycle_id = %s
    """
    orders = db.fetchall(query, (run_id,))

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
            error_type = classify_exception(o, cn_sum, cn_count, stl_sum, delta)
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
            })

    # 4. Orphan Settlements Pass (Settlements with no invoice_no or unlinked to existing orders)
    # Check settlements that have not matched any known order
    orphan_query = """
    SELECT s.payment_id, s.utr, s.invoice_no, s.amount, s.settled_at
    FROM settlements s
    WHERE s.invoice_no IS NULL
       OR s.invoice_no NOT IN (SELECT invoice_no FROM orders)
    """
    orphans = db.fetchall(orphan_query)
    for orphan in orphans:
        orphan_amt = Decimal(str(orphan["amount"]))
        exceptions_to_insert.append({
            "run_id": run_id,
            "invoice_no": orphan.get("invoice_no"),
            "customer_name": "Unmatched Cash Receipt",
            "delta": float(-orphan_amt),
            "error_type": "Unmatched Settlement / Orphan Payment",
            "payment_id": orphan.get("payment_id"),
            "utr": orphan.get("utr"),
            "order_total": 0.0,
            "base_amount": 0.0,
            "tax_rate": float(STANDARD_TAX_RATE),
            "cn_sum": 0.0,
            "cn_count": 0,
            "stl_sum": float(orphan_amt),
        })

    # 5. Insert generated exceptions into database
    for exc in exceptions_to_insert:
        db.execute(
            """INSERT INTO exceptions (run_id, invoice_no, customer_name, delta, error_type, remark, resolved)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                exc["run_id"],
                exc["invoice_no"],
                exc["customer_name"],
                exc["delta"],
                exc["error_type"],
                None, # Will be filled by LLM pass
                0
            )
        )

    # 6. Calculate Run Metrics
    total_records = len(orders) + len(orphans)
    match_rate = Decimal("100.00")
    if total_records > 0:
        match_rate = (Decimal(str(matched_count)) / Decimal(str(total_records)) * Decimal("100")).quantize(Decimal("0.01"))

    finished_iso = datetime.now(timezone.utc).isoformat()
    db.execute(
        """UPDATE reconciliation_runs
           SET finished_at = %s, total_records = %s, matched_count = %s, match_rate = %s
           WHERE id = %s""",
        (finished_iso, total_records, matched_count, float(match_rate), run_id)
    )

    # 7. LLM Remark & Insights Pass
    if not skip_llm and exceptions_to_insert:
        try:
            run_llm_remark_pass(run_id, exceptions_to_insert)
        except Exception as e:
            # Fallback remark pass if anything fails
            print(f"Warning: LLM pass encountered error: {e}")

    return run_id

def get_exceptions(run_id: str) -> List[Dict[str, Any]]:
    """Retrieves all exceptions for a given run ID with delta as float/Decimal."""
    rows = db.fetchall(
        "SELECT * FROM exceptions WHERE run_id = %s ORDER BY id ASC",
        (run_id,)
    )
    # Cast delta to float for consistency with test assertions
    for r in rows:
        if r.get("delta") is not None:
            r["delta"] = float(r["delta"])
        r["resolved"] = bool(r.get("resolved"))
    return rows
