#!/usr/bin/env bash
# scripts/check-quality.sh
# Ruff, форматирование и архитектура: вывод виден, если не передан --log.
set -euo pipefail
cd "$(dirname "$0")/.."
log_to_file=false
case "${1-}" in
  '') ;;
  --log) log_to_file=true ;;
  --help) echo 'Качество: bash scripts/check-quality.sh [--log].'; exit 0 ;;
  *) echo 'Неизвестный аргумент. Используйте bash scripts/check-quality.sh [--log].' >&2; exit 2 ;;
esac
if [[ $# -gt 1 ]]; then echo 'Лишние аргументы проверки.' >&2; exit 2; fi
check_quality() {
  # Сохраняет код первого отказа в обоих режимах вывода.
  uv run --frozen ruff check app tests &&
  uv run --frozen ruff format --check app tests &&
  uv run --frozen pytest -q tests/architecture &&
  git diff --check
}
echo 'Проверка Ruff, форматирования, архитектуры и git diff.'
if $log_to_file; then
  mkdir -p .cache
  echo 'Подробный вывод: .cache/quality-linux.log'
  if check_quality > .cache/quality-linux.log 2>&1; then tail -n 6 .cache/quality-linux.log
  else code=$?; tail -n 100 .cache/quality-linux.log; exit "$code"; fi
else check_quality; fi
echo 'Проверки качества пройдены.'
