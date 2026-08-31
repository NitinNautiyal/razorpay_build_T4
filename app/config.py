"""Configuration settings for the Reconciliation Agent."""
import os
from decimal import Decimal
from typing import Optional

# Database
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./recon.db")

# Razorpay Webhook Configuration
WEBHOOK_SECRET: str = os.getenv("RAZORPAY_WEBHOOK_SECRET", "test_webhook_secret_key_recon_2026")

# Reconciliation Tolerance & Tax Defaults (Config constants per Spec §4 and §8)
TOLERANCE: Decimal = Decimal(os.getenv("RECON_TOLERANCE", "0.09"))
STANDARD_TAX_RATE: Decimal = Decimal(os.getenv("STANDARD_TAX_RATE", "0.18"))

# LLM Configuration
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "auto")
LLM_API_KEY: Optional[str] = os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("OPENAI_API_KEY")
LLM_MODEL: str = os.getenv("LLM_MODEL", "gemini-1.5-flash")
