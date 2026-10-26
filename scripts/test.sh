#!/usr/bin/env bash
# scripts/test.sh
# Полный Linux-прогон в образе с реальными OCR и LibreOffice; успешные логи остаются в файле.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .cache
args=(--env-file .env.example)
if [[ -f .env ]]; then args+=(--env-file .env); fi
if docker compose "${args[@]}" --profile test run --build --rm --no-deps tests > .cache/tests-linux.log 2>&1; then
  tail -n 5 .cache/tests-linux.log
else
  tail -n 100 .cache/tests-linux.log
  exit 1
fi
