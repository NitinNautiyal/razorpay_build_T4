"""Batched LLM Remark and Insight Generator for Reconciliation Exceptions."""
import json
import os
from decimal import Decimal
from typing import Dict, Any, List, Optional
import httpx

from app.config import LLM_API_KEY, LLM_MODEL, STANDARD_TAX_RATE
from app.database import db
from app.memory import get_all_memory_context, add_memory_insight

def generate_fallback_remarks_and_insights(
    exceptions: List[Dict[str, Any]],
    memory_contexts: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Intelligent rule-assisted fallback reasoning engine.
    Cross-references memory context (tax changes, discount schemes, disputes)
    to generate realistic financial remarks, dual-plausible causes, and aggregate insights.
    """
    remarks_by_invoice = {}
    plausible_causes_by_invoice = {}
    pattern_keys_by_invoice = {}
    
    # Pre-parse memory contexts
    tax_contexts = [c for c in memory_contexts if "tax" in c.get("context_type", "").lower() or "tax" in c.get("description", "").lower()]
    discount_contexts = [c for c in memory_contexts if "discount" in c.get("context_type", "").lower() or "discount" in c.get("description", "").lower()]
    dispute_contexts = [c for c in memory_contexts if "dispute" in c.get("context_type", "").lower() or "dispute" in c.get("description", "").lower()]

    for exc in exceptions:
        inv = exc.get("invoice_no") or exc.get("payment_id") or "unknown"
        error_type = exc.get("error_type", "")
        delta = Decimal(str(exc.get("delta", 0)))
        cust = exc.get("customer_name", "Customer")
        order_total = Decimal(str(exc.get("order_total", 0)))
        base_amt = Decimal(str(exc.get("base_amount", 0)))
        tax_rate = Decimal(str(exc.get("tax_rate", 0)))

        remark = ""
        plausible_causes = []
        pattern_key = f"{cust}:{error_type}"

        # Check disputes
        matched_dispute = next((d for d in dispute_contexts if inv and inv in d.get("description", "")), None)
        if matched_dispute:
            plausible_causes = [
                {"title": "Transit Damage Dispute on File", "confidence": "High", "detail": matched_dispute['description']},
                {"title": "Withheld Short Payment", "confidence": "Medium", "detail": "Customer withheld payment awaiting replacement/credit note"}
            ]
            remark = f"Plausible Cause 1: Active dispute on file ({matched_dispute['description']}). Plausible Cause 2: Customer withheld ₹{abs(delta):,.2f} awaiting credit note resolution."

        elif error_type == "Tax Mismatch":
            tax_desc = tax_contexts[0].get("description", "") if tax_contexts else "GST rate transition from 12% to 18%"
            plausible_causes = [
                {"title": "CDMS Master Tax Divergence", "confidence": "High", "detail": f"Invoiced at {float(tax_rate)*100:.0f}% GST instead of standard {float(STANDARD_TAX_RATE)*100:.0f}%."},
                {"title": "Transition Cutover Lag", "confidence": "Medium", "detail": f"Order raised during GST revision window: {tax_desc}"}
            ]
            remark = f"Plausible Cause 1: CDMS tax master billed at {float(tax_rate)*100:.0f}% GST vs {float(STANDARD_TAX_RATE)*100:.0f}%. Plausible Cause 2: Cutover lag around policy effective date ({tax_desc})."

        elif error_type == "Duplicate Credit Note":
            cn_count = exc.get("cn_count", 0)
            cn_sum = exc.get("cn_sum", 0)
            plausible_causes = [
                {"title": "Double Ingestion / Retry Glitch", "confidence": "High", "detail": f"{cn_count} credit notes totaling ₹{cn_sum:,.2f} registered in CDMS."},
                {"title": "Redundant Return Authorization", "confidence": "Low", "detail": "Both return requests approved separately for the same original invoice"}
            ]
            remark = f"Plausible Cause 1: Upstream CDMS double-click created twin credit notes totaling ₹{cn_sum:,.2f}. Plausible Cause 2: Redundant RMA processed. Reverse duplicate CN in CDMS."

        elif error_type == "Unmatched Settlement / Orphan Payment":
            pid = exc.get("payment_id", "N/A")
            utr = exc.get("utr", "N/A")
            plausible_causes = [
                {"title": "Missing Checkout Note Tag", "confidence": "High", "detail": "Direct link payment captured without invoice_no in metadata notes."},
                {"title": "Advance / Over-the-counter Remittance", "confidence": "Medium", "detail": "Customer initiated direct payment prior to CDMS order generation."}
            ]
            remark = f"Orphan Razorpay payment {pid} (UTR: {utr}) for ₹{abs(delta):,.2f}. Plausible Cause 1: Missing invoice_no tag in notes. Plausible Cause 2: Advance customer deposit. Reconcile against bank receipts."

        elif error_type == "Unallocated Bulk Payment":
            pid = exc.get("payment_id", "N/A")
            plausible_causes = [
                {"title": "Bulk Settlement Covering Multiple Invoices", "confidence": "High", "detail": "Payment amount represents multi-order settlement without order allocation breakdown."},
                {"title": "Customer Lump-Sum Advance", "confidence": "Medium", "detail": "Lump sum transferred to clear rolling ledger balance."}
            ]
            remark = f"Bulk payment {pid} for ₹{abs(delta):,.2f} requires allocation breakdown across open customer orders."

        elif error_type == "Underpayment / Pending Collection":
            # Check if delta matches a known discount scheme
            matched_discount = None
            if base_amt > 0:
                discount_pct = (delta / (order_total or base_amt)) * Decimal("100")
                for d in discount_contexts:
                    if cust.lower() in d.get("description", "").lower() or f"{int(discount_pct)}%" in d.get("description", ""):
                        matched_discount = d
                        break

            if matched_discount:
                plausible_causes = [
                    {"title": "Authorized Prompt Payment Discount", "confidence": "High", "detail": matched_discount['description']},
                    {"title": "Customer Short-Remittance", "confidence": "Medium", "detail": f"Unexplained deduction of ₹{delta:,.2f} requiring debit note."}
                ]
                remark = f"Plausible Cause 1: Authorized 5% prompt settlement cash discount ({matched_discount['description']}). Plausible Cause 2: Unexplained short payment of ₹{delta:,.2f}. Recommend clearing if paid within terms."
            else:
                plausible_causes = [
                    {"title": "Customer Short Remittance", "confidence": "High", "detail": f"Customer underpaid by ₹{delta:,.2f}."},
                    {"title": "Unapplied Debit / TDS Deduction", "confidence": "Medium", "detail": "Customer may have withheld statutory TDS or transit debit."}
                ]
                remark = f"Plausible Cause 1: Customer underpaid by ₹{delta:,.2f}. Plausible Cause 2: Unrecorded TDS or debit note withheld. Follow up for balance collection."

        elif error_type == "Overpayment / Excess Settlement":
            plausible_causes = [
                {"title": "Advance Overpayment", "confidence": "High", "detail": f"Customer remitted ₹{abs(delta):,.2f} in excess of invoice total."},
                {"title": "Unrecorded Surcharge / Freight", "confidence": "Low", "detail": "Customer included courier or expedite charge not in CDMS base invoice."}
            ]
            remark = f"Plausible Cause 1: Advance customer overpayment of ₹{abs(delta):,.2f}. Plausible Cause 2: Extra freight or late surcharge paid. Hold in customer advance ledger."

        else:
            plausible_causes = [
                {"title": "Financial Variance", "confidence": "Medium", "detail": f"Discrepancy delta ₹{delta:,.2f}"}
            ]
            remark = f"Discrepancy of ₹{delta:,.2f} ({error_type}) requires controller review."

        remarks_by_invoice[inv] = remark
        plausible_causes_by_invoice[inv] = plausible_causes
        pattern_keys_by_invoice[inv] = pattern_key

    # Generate cycle aggregate pattern insights
    insights = []
    total_exc = len(exceptions)
    tax_count = sum(1 for e in exceptions if e.get("error_type") == "Tax Mismatch")
    orphan_count = sum(1 for e in exceptions if "Orphan" in e.get("error_type", ""))
    underpay_count = sum(1 for e in exceptions if "Underpayment" in e.get("error_type", ""))
    dup_cn_count = sum(1 for e in exceptions if "Duplicate" in e.get("error_type", ""))
    bulk_count = sum(1 for e in exceptions if "Bulk" in e.get("error_type", ""))

    if tax_count > 0:
        insights.append({
            "insight": f"{tax_count} of {total_exc} exceptions caused by tax rate mismatches; check CDMS tax master data against recent GST changes.",
            "pattern_key": "Tax Transition:Tax Mismatch",
            "frequency": tax_count,
            "severity": "High",
            "actionable_fix": "Update CDMS master tax tables for Product Category B from 12% to 18%."
        })
    if orphan_count > 0:
        insights.append({
            "insight": f"{orphan_count} orphan settlement(s) received without invoice notes tag; enforce invoice_no tagging in checkout links.",
            "pattern_key": "Process Hygiene:Orphan Payment",
            "frequency": orphan_count,
            "severity": "Medium",
            "actionable_fix": "Enforce mandatory notes.invoice_no validation in checkout links."
        })
    if underpay_count > 0:
        insights.append({
            "insight": f"{underpay_count} underpayment exception(s) detected; check customer prompt discount terms and pending collections.",
            "pattern_key": "Customer Terms:Underpayment",
            "frequency": underpay_count,
            "severity": "Medium",
            "actionable_fix": "Add prompt payment discount rule in memory context to auto-clear."
        })
    if dup_cn_count > 0:
        insights.append({
            "insight": f"{dup_cn_count} duplicate credit note(s) identified in CDMS export.",
            "pattern_key": "Upstream Glitch:Duplicate Credit Note",
            "frequency": dup_cn_count,
            "severity": "High",
            "actionable_fix": "Set up CDMS idempotency key on Credit Note creation API."
        })
    if bulk_count > 0:
        insights.append({
            "insight": f"{bulk_count} bulk settlement(s) received covering multiple customer orders.",
            "pattern_key": "Allocation:Unallocated Bulk Payment",
            "frequency": bulk_count,
            "severity": "Medium",
            "actionable_fix": "Use settlement allocation resolution engine to allocate across invoices."
        })

    if not insights:
        insights.append({
            "insight": "Cycle reconciliation completed with minor variance exceptions.",
            "pattern_key": "Cycle:Clean",
            "frequency": 1,
            "severity": "Low",
            "actionable_fix": "All records aligned with tolerance."
        })

    return {
        "remarks": remarks_by_invoice,
        "plausible_causes": plausible_causes_by_invoice,
        "pattern_keys": pattern_keys_by_invoice,
        "insights": insights
    }

def run_llm_remark_pass(run_id: str, exceptions: List[Dict[str, Any]]) -> None:
    """
    Batched LLM pass: sends all exceptions + memory context to the LLM
    and writes results back to `exceptions.remark`, `exceptions.plausible_causes`,
    `exceptions.pattern_key`, and `memory_insights`.
    """
    memory_contexts = get_all_memory_context()
    
    # Try calling external LLM API if key is available
    llm_result = None
    if LLM_API_KEY:
        try:
            llm_result = _call_external_llm(exceptions, memory_contexts)
        except Exception as err:
            print(f"External LLM call failed, falling back to rule-assisted engine: {err}")

    # Fallback if no LLM key or call failed
    if not llm_result:
        llm_result = generate_fallback_remarks_and_insights(exceptions, memory_contexts)

    remarks = llm_result.get("remarks", {})
    plausible_causes = llm_result.get("plausible_causes", {})
    pattern_keys = llm_result.get("pattern_keys", {})
    insights = llm_result.get("insights", [])

    # Update exceptions table with generated remarks and plausible causes
    for exc in exceptions:
        inv = exc.get("invoice_no") or exc.get("payment_id") or "unknown"
        remark = remarks.get(inv) or remarks.get(exc.get("invoice_no")) or "Review discrepancy against source invoice and settlements."
        causes = plausible_causes.get(inv) or plausible_causes.get(exc.get("invoice_no"))
        causes_json = json.dumps(causes) if causes else None
        pattern_key = pattern_keys.get(inv) or pattern_keys.get(exc.get("invoice_no")) or f"{exc.get('customer_name', 'Customer')}:{exc.get('error_type', 'Variance')}"

        if exc.get("invoice_no"):
            db.execute(
                """UPDATE exceptions
                   SET remark = %s, plausible_causes = %s, pattern_key = %s
                   WHERE run_id = %s AND invoice_no = %s""",
                (remark, causes_json, pattern_key, run_id, exc["invoice_no"])
            )
        elif exc.get("payment_id"):
            # Orphan settlement exception
            db.execute(
                """UPDATE exceptions
                   SET remark = %s, plausible_causes = %s, pattern_key = %s
                   WHERE run_id = %s AND invoice_no IS NULL""",
                (remark, causes_json, pattern_key, run_id)
            )

    # Store cycle pattern insights
    for ins in insights:
        if isinstance(ins, dict):
            add_memory_insight(
                run_id=run_id,
                insight=ins.get("insight", ""),
                pattern_key=ins.get("pattern_key"),
                frequency=ins.get("frequency", 1),
                severity=ins.get("severity", "Medium"),
                actionable_fix=ins.get("actionable_fix")
            )
        else:
            add_memory_insight(run_id=run_id, insight=str(ins))

def _call_external_llm(exceptions: List[Dict[str, Any]], memory_contexts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Calls Gemini or OpenAI compatible endpoint with batched prompt."""
    prompt = f"""
You are a senior Finance Controller Reconciliation Agent.
Analyze the following reconciliation discrepancy exceptions and cross-reference them with the company's memory context (tax changes, discount schemes, policies, disputes).
If multiple explanations are plausible, articulate both plausible causes rather than forcing a single classification.

Memory Context:
{json.dumps(memory_contexts, indent=2, default=str)}

Reconciliation Exceptions:
{json.dumps(exceptions, indent=2, default=str)}

Provide your output strictly in JSON format with two keys:
1. "remarks": a dictionary mapping each invoice_no (or payment_id for orphan payments) to a concise, professional 1-2 sentence finance controller remark stating the plausible cause(s) and recommended action.
2. "insights": a list of 1-3 high-level cycle pattern observations for the executive finance report.
"""
    # If using Gemini
    if "gemini" in LLM_MODEL or "AIza" in (LLM_API_KEY or ""):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{LLM_MODEL}:generateContent?key={LLM_API_KEY}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"}
        }
        resp = httpx.post(url, json=payload, timeout=20.0)
        if resp.status_code == 200:
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
    else:
        # OpenAI compatible
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": LLM_MODEL if "gpt" in LLM_MODEL else "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"}
        }
        resp = httpx.post(url, headers=headers, json=payload, timeout=20.0)
        if resp.status_code == 200:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            return json.loads(text)

    return None
