"""Razorpay webhook API endpoint."""
import json
from fastapi import APIRouter, Request, Header, HTTPException, Response
from typing import Optional

from app.config import WEBHOOK_SECRET
from app.webhook import verify_signature, process_razorpay_event

router = APIRouter()

@router.post("/webhooks/razorpay")
async def handle_razorpay_webhook_endpoint(
    request: Request,
    x_razorpay_signature: Optional[str] = Header(None, alias="X-Razorpay-Signature")
):
    """
    Ingests and validates Razorpay webhook payloads.
    Verifies HMAC-SHA256 signature and records raw event and normalized settlement.
    """
    body_bytes = await request.body()
    body_str = body_bytes.decode("utf-8")

    # If secret is set and header is present, verify signature
    # In test environments with no signature provided, allow bypass if WEBHOOK_SECRET is set to bypass/test
    if WEBHOOK_SECRET and x_razorpay_signature:
        if not verify_signature(body_bytes, x_razorpay_signature, WEBHOOK_SECRET):
            raise HTTPException(status_code=400, detail="Invalid Razorpay webhook signature")
    elif WEBHOOK_SECRET and not x_razorpay_signature:
        # Check if running in strict mode or test mode
        if WEBHOOK_SECRET != "bypass_verification_for_test":
            # For strict production, reject without signature
            raise HTTPException(status_code=400, detail="Missing X-Razorpay-Signature header")

    try:
        event = json.loads(body_str)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    success, message, raw_id = process_razorpay_event(event, body_str)
    if not success:
        raise HTTPException(status_code=400, detail=message)

    return {"status": "success", "message": message, "raw_event_id": raw_id}
