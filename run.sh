#!/bin/bash
# Start Finance Controller Reconciliation Agent
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
    ./.venv/bin/pip install -r requirements.txt
fi

echo "Starting Reconciliation Agent on http://127.0.0.1:8000 ..."
./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
