#!/bin/bash
echo "=== Starting Nalo.Ai ==="
echo "Python: $(python3 --version)"
echo "PORT: $PORT"
echo "Checking imports..."
python3 -c "from main import app; print('All imports OK')" || { echo "IMPORT FAILED"; exit 1; }
echo "Starting uvicorn..."
exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --timeout-keep-alive 120
