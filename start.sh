#!/bin/sh
# アンケート集計ツールを起動する（http://127.0.0.1:8000 をブラウザで開く）
cd "$(dirname "$0")"
exec python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
