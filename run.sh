#!/usr/bin/env bash
# 삼호푸드 OSMU 스튜디오 실행 (데스크톱 앱). 브라우저로 보려면: FLET_VIEW=web ./run.sh
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$DIR"
if [ -f ".venv/bin/python3" ]; then PYTHON_BIN=".venv/bin/python3"; else PYTHON_BIN="python3"; fi
"$PYTHON_BIN" main.py
