#!/bin/bash
set -e

echo "⚡ CryptoBot starting..."

# Generate secrets if not set
export JWT_SECRET_KEY="${JWT_SECRET_KEY:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}"
export JWT_REFRESH_SECRET="${JWT_REFRESH_SECRET:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}"
export DEMO_MODE="${DEMO_MODE:-true}"

echo "📦 Building frontend..."
cd frontend && npm install --silent && npm run build && cd ..

echo "🚀 Starting server on port 8080..."
exec uvicorn main:app --host 0.0.0.0 --port 8080
