"""Pydantic models and schemas for the Reconciliation Agent."""
from decimal import Decimal
from typing import Optional, List, Dict, Any
from datetime import datetime, date
from pydantic import BaseModel, Field

class OrderCreate(BaseModel):
    invoice_no: str
    customer_code: str
    customer_name: str
    invoice_date: str # YYYY-MM-DD
    base_amount: Decimal
    tax_rate: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    status: str = "PD"
    cycle_id: Optional[str] = None

class Order(OrderCreate):
    pass

class CreditNoteCreate(BaseModel):
    cn_no: str
    invoice_no: str
    amount: Decimal

class CreditNote(CreditNoteCreate):
    created_at: Optional[datetime] = None

class SettlementCreate(BaseModel):
    payment_id: str
    utr: Optional[str] = None
    invoice_no: Optional[str] = None
    amount: Decimal
    settled_at: datetime
    raw_event_id: Optional[int] = None

class Settlement(SettlementCreate):
    pass

class ReconciliationRun(BaseModel):
    id: str
    cycle_label: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    total_records: int = 0
    matched_count: int = 0
    match_rate: Decimal = Decimal("0.00")

class ExceptionItem(BaseModel):
    id: int
    run_id: str
    invoice_no: Optional[str] = None
    customer_name: Optional[str] = None
    delta: Decimal
    error_type: str
    remark: Optional[str] = None
    resolved: bool = False
    resolved_note: Optional[str] = None

class ExceptionResolveRequest(BaseModel):
    resolved: bool = True
    resolved_note: Optional[str] = None

class MemoryContextCreate(BaseModel):
    context_type: str # 'Tax Rate Change' | 'Policy Change' | 'Discount Scheme' | 'Disputed Transaction'
    description: str
    effective_date: Optional[str] = None

class MemoryContext(MemoryContextCreate):
    id: int
    created_at: Optional[datetime] = None

class MemoryInsight(BaseModel):
    id: int
    run_id: Optional[str] = None
    insight: str
    created_at: Optional[datetime] = None

class ReconciliationTriggerRequest(BaseModel):
    cycle_label: Optional[str] = None
    skip_llm: bool = False
