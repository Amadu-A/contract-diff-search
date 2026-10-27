#!/usr/bin/env bash
# scripts/test.sh
# Linux-тесты с видимым выводом; --log явно сохраняет подробности в файл.
set -euo pipefail
cd "$(dirname "$0")/.."
log_to_file=false
case "${1-}" in
  '') ;;
  --log) log_to_file=true ;;
  --help) echo 'Проверка: bash scripts/test.sh [--log]. Сборка и тесты по умолчанию видны в терминале.'; exit 0 ;;
  *) echo 'Неизвестный аргумент. Используйте bash scripts/test.sh [--log].' >&2; exit 2 ;;
esac
if [[ $# -gt 1 ]]; then echo 'Лишние аргументы проверки.' >&2; exit 2; fi
args=(--env-file .env.example)
if [[ -f .env ]]; then args+=(--env-file .env); fi
echo 'Сборка тестового образа и запуск тестов (OCR, LibreOffice, браузер).'
if $log_to_file; then
  mkdir -p .cache
  echo 'Подробный вывод: .cache/tests-linux.log'
  if docker compose --progress plain "${args[@]}" --profile test run --build --rm --no-deps -T tests > .cache/tests-linux.log 2>&1; then
    tail -n 8 .cache/tests-linux.log
  else code=$?; tail -n 100 .cache/tests-linux.log; exit "$code"; fi
else
  docker compose --progress plain "${args[@]}" --profile test run --build --rm --no-deps -T tests
fi
printf 'Linux-тесты завершены за %sс.\n' "$SECONDS"
