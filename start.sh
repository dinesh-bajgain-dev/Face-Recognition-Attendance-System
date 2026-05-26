#!/bin/bash
# वेदनेत्रम् - Face Recognition System startup script

set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "  ┌─────────────────────────────────────┐"
echo "  │   वेदनेत्रम् · Face Recognition System     │"
echo "  └─────────────────────────────────────┘"
echo ""

# ── Backend ──────────────────────────────────────────────────────────────────
echo "▸ Starting backend (Flask) on port 5050..."
cd "$ROOT/backend"

if [ ! -d "venv" ]; then
  echo "  Creating virtual environment..."
  python3 -m venv venv
fi

source venv/bin/activate
pip install -q -r requirements.txt
python app.py &
BACKEND_PID=$!
echo "  Backend PID: $BACKEND_PID"
sleep 2

# ── Frontend ──────────────────────────────────────────────────────────────────
echo ""
echo "▸ Starting frontend on port 8080..."
cd "$ROOT/frontend"
python3 -m http.server 8080 &
FRONTEND_PID=$!
echo "  Frontend PID: $FRONTEND_PID"

echo ""
echo "  ✓ System running!"
echo "  Open: http://localhost:8080"
echo ""
echo "  Press Ctrl+C to stop all services."
echo ""

# ── Cleanup on exit ───────────────────────────────────────────────────────────
trap "echo ''; echo 'Stopping...'; kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit" INT TERM
wait
