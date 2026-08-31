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
    to generate realistic financial remarks and aggregate insights.
    """
    remarks_by_invoice = {}
    
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

        # Check disputes
        matched_dispute = next((d for d in dispute_contexts if inv and inv in d.get("description", "")), None)
        if matched_dispute:
            remark = f"Dispute on file: {matched_dispute['description']}. Delta of ₹{abs(delta):,.2f} pending dispute resolution."

        elif error_type == "Tax Mismatch":
            if tax_contexts:
                ctx_desc = tax_contexts[0].get("description", "")
                remark = f"Tax calculation discrepancy: Order billed at {float(tax_rate)*100:.0f}% GST vs standard {float(STANDARD_TAX_RATE)*100:.0f}%. Note: {ctx_desc}."
            else:
                remark = f"Tax rate mismatch: Invoiced at {float(tax_rate)*100:.0f}% GST instead of standard {float(STANDARD_TAX_RATE)*100:.0f}% GST. Delta: ₹{delta:,.2f}."

        elif error_type == "Duplicate Credit Note":
            cn_count = exc.get("cn_count", 0)
            cn_sum = exc.get("cn_sum", 0)
            remark = f"{cn_count} credit notes totaling ₹{cn_sum:,.2f} applied to invoice. Check for duplicate CN entry in CDMS."

        elif error_type == "Unmatched Settlement / Orphan Payment":
            pid = exc.get("payment_id", "N/A")
            utr = exc.get("utr", "N/A")
            remark = f"Orphan Razorpay payment {pid} (UTR: {utr}) received for ₹{abs(delta):,.2f} without invoice tag in notes. Reconcile against manual receipts."

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
                remark = f"Short payment of ₹{delta:,.2f} likely corresponds to authorized scheme: {matched_discount['description']}."
            else:
                remark = f"Customer underpaid by ₹{delta:,.2f}. Balance remaining for collection or pending debit note."

        elif error_type == "Overpayment / Excess Settlement":
            remark = f"Excess payment of ₹{abs(delta):,.2f} captured. Verify if advance payment or excess credit note refund."

        else:
            remark = f"Discrepancy of ₹{delta:,.2f} ({error_type}) requires controller manual review."

        remarks_by_invoice[inv] = remark

    # Generate cycle aggregate pattern insights
    insights = []
    total_exc = len(exceptions)
    tax_count = sum(1 for e in exceptions if e.get("error_type") == "Tax Mismatch")
    orphan_count = sum(1 for e in exceptions if "Orphan" in e.get("error_type", ""))
    underpay_count = sum(1 for e in exceptions if "Underpayment" in e.get("error_type", ""))
    dup_cn_count = sum(1 for e in exceptions if "Duplicate" in e.get("error_type", ""))

    if tax_count > 0:
        insights.append(f"{tax_count} of {total_exc} exceptions caused by tax rate mismatches; check CDMS tax master data against recent GST changes.")
    if orphan_count > 0:
        insights.append(f"{orphan_count} orphan settlement(s) received without invoice notes tag; enforce invoice_no tagging in checkout links.")
    if underpay_count > 0:
        insights.append(f"{underpay_count} underpayment exception(s) detected; check customer prompt discount terms and pending collections.")
    if dup_cn_count > 0:
        insights.append(f"{dup_cn_count} duplicate credit note(s) identified in CDMS export.")

    if not insights:
        insights.append("Cycle reconciliation completed with minor variance exceptions.")

    return {
        "remarks": remarks_by_invoice,
        "insights": insights
    }

def run_llm_remark_pass(run_id: str, exceptions: List[Dict[str, Any]]) -> None:
    """
    Batched LLM pass: sends all exceptions + memory context to the LLM
    and writes results back to `exceptions.remark` and `memory_insights`.
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
    insights = llm_result.get("insights", [])

    # Update exceptions table with generated remarks
    for exc in exceptions:
        inv = exc.get("invoice_no") or exc.get("payment_id") or "unknown"
        remark = remarks.get(inv) or remarks.get(exc.get("invoice_no")) or "Review discrepancy against source invoice and settlements."
        
        if exc.get("invoice_no"):
            db.execute(
                "UPDATE exceptions SET remark = %s WHERE run_id = %s AND invoice_no = %s",
                (remark, run_id, exc["invoice_no"])
            )
        elif exc.get("payment_id"):
            # Orphan settlement exception
            db.execute(
                "UPDATE exceptions SET remark = %s WHERE run_id = %s AND invoice_no IS NULL",
                (remark, run_id)
            )

    # Store cycle pattern insights
    for ins in insights:
        add_memory_insight(run_id=run_id, insight=ins)

def _call_external_llm(exceptions: List[Dict[str, Any]], memory_contexts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Calls Gemini or OpenAI compatible endpoint with batched prompt."""
    prompt = f"""
You are a senior Finance Controller Reconciliation Agent.
Analyze the following reconciliation discrepancy exceptions and cross-reference them with the company's memory context (tax changes, discount schemes, policies, disputes).

Memory Context:
{json.dumps(memory_contexts, indent=2, default=str)}

Reconciliation Exceptions:
{json.dumps(exceptions, indent=2, default=str)}

Provide your output strictly in JSON format with two keys:
1. "remarks": a dictionary mapping each invoice_no (or payment_id for orphan payments) to a concise, professional 1-2 sentence finance controller remark explaining the cause and recommended action.
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
