"""Main FastAPI Application Entrypoint."""
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import db
from app.routes.webhook import router as webhook_router
from app.routes.reconciliation import router as recon_router
from app.routes.exceptions import router as exceptions_router
from app.routes.memory import router as memory_router
from app.routes.cdms import router as cdms_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize DB schema on startup
    db.init_db()
    yield

app = FastAPI(
    title="Finance Controller — Reconciliation Agent",
    description="Automated CDMS ↔ Razorpay Reconciliation Engine and Review Dashboard",
    version="1.0.0",
    lifespan=lifespan
)

# Template engine
templates_dir = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_dir)

# Register routes
app.include_router(webhook_router)
app.include_router(recon_router)
app.include_router(exceptions_router)
app.include_router(memory_router)
app.include_router(cdms_router)

@app.get("/", response_class=HTMLResponse)
def get_dashboard(request: Request):
    """Renders the Finance Controller Review UI."""
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/health")
def healthcheck():
    return {"status": "ok", "service": "finance-reconciliation-agent"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
