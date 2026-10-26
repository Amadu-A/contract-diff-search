#!/usr/bin/env bash
# scripts/check-quality.sh
# Единая проверка Ruff, форматирования и архитектурных инвариантов на Linux.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .cache
if {
  uv run --frozen ruff check app tests &&
  uv run --frozen ruff format --check app tests &&
  uv run --frozen pytest -q tests/architecture &&
  git diff --check
} > .cache/quality-linux.log 2>&1; then
  tail -n 6 .cache/quality-linux.log
  echo 'Проверки качества пройдены.'
else
  tail -n 100 .cache/quality-linux.log
  exit 1
fi
