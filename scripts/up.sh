#!/usr/bin/env bash
# scripts/up.sh
# Видимый запуск и ограниченная по времени подготовка существующего общего RabbitMQ.
set +x
set -euo pipefail
cd "$(dirname "$0")/.."
log_to_file=false
case "${1-}" in
  '') ;;
  --log) log_to_file=true ;;
  --help) echo 'Запуск: bash scripts/up.sh [--log]. По умолчанию подробности видны в терминале.'; exit 0 ;;
  *) echo 'Неизвестный аргумент. Используйте bash scripts/up.sh [--log].' >&2; exit 2 ;;
esac
if [[ $# -gt 1 ]]; then echo 'Лишние аргументы запуска.' >&2; exit 2; fi
mkdir -p .cache
deploy_log=.cache/deploy.log
log_started=false
current_stage='подготовка'
finish() {
  # Показывает место отказа без вывода командной строки с секретами.
  local code=$?
  unset broker_config broker_values broker_password
  if [[ $code -ne 0 ]]; then
    echo "Ошибка: $current_stage (код $code)." >&2
    if $log_started; then tail -n 30 "$deploy_log" >&2; fi
  fi
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
stage() {
  # Отображает текущий шаг и прошедшее время в обоих режимах логирования.
  current_stage=$1
  printf '[%s; +%ss] %s\n' "$(date +%T)" "$SECONDS" "$current_stage"
}
run_command() {
  # Скрывает подробности только по явно переданному --log.
  if $log_to_file; then "$@" >> "$deploy_log" 2>&1; else "$@"; fi
}
stage 'Проверка инструментов и блокировки запуска'
for tool in docker timeout flock; do
  if ! command -v "$tool" >/dev/null; then echo "Не найдена команда $tool (нужны Docker, coreutils и util-linux)." >&2; exit 1; fi
done
exec 9>.cache/deploy.lock
if ! flock -n 9; then echo 'Уже выполняется другой scripts/up.sh. Дождитесь его завершения или остановите его.' >&2; exit 1; fi
if $log_to_file; then : > "$deploy_log"; log_started=true; echo "Подробный вывод: $deploy_log"; fi
if ! docker info >/dev/null 2>&1; then
  echo 'Docker недоступен. Запустите Docker Engine / Docker Desktop.' >&2
  exit 1
fi
if ! docker network inspect ai-shared >/dev/null 2>&1; then
  echo 'Нет сети ai-shared. Сначала запустите существующий shared-infrastructure.' >&2
  exit 1
fi
args=(--env-file .env.example)
if [[ -f .env ]]; then args+=(--env-file .env); fi
stage 'Проверка Compose'
docker compose "${args[@]}" config --quiet
stage 'Сборка образа приложения'
if ! run_command docker compose --progress plain "${args[@]}" build api; then
  stage 'Основная сборка не удалась; повтор с базовым образом из mirror.gcr.io'
  run_command docker compose --progress plain "${args[@]}" build --build-arg PYTHON_BASE_IMAGE=mirror.gcr.io/library/python:3.12-slim-bookworm api
fi
# Settings читает dotenv как данные: source/eval здесь недопустимы.
stage 'Чтение настроек через Pydantic (одноразовый контейнер)'
if ! broker_config="$(docker compose "${args[@]}" run --rm --no-deps -T api python -m app.provisioning)"; then
  echo 'Подготовка RabbitMQ остановлена: не удалось проверить настройки.' >&2
  exit 1
fi
mapfile -t broker_values <<< "$broker_config"
if [[ ${#broker_values[@]} -ne 6 || ! ${broker_values[4]} =~ ^[0-9]+$ ]]; then
  echo 'Неверный ответ процесса настроек RabbitMQ.' >&2
  exit 1
fi
broker_container=${broker_values[0]}
broker_user=${broker_values[1]}
broker_vhost=${broker_values[2]}
broker_password=${broker_values[3]}
broker_timeout=${broker_values[4]}
public_url=${broker_values[5]}
bounded_docker() {
  # Ограничивает Docker-клиент, включая случай зависшего соединения с daemon.
  timeout --signal=TERM --kill-after=5s "$((broker_timeout + 15))s" bash -c 'docker "$@"' -- "$@"
}
stage 'Проверка существующего контейнера RabbitMQ'
if [[ "$(bounded_docker inspect --format '{{.State.Running}}' "$broker_container")" != true ]]; then
  echo 'Существующий контейнер RabbitMQ не запущен. Проверьте shared-infrastructure.' >&2
  exit 1
fi
members="$(bounded_docker network inspect ai-shared --format '{{range .Containers}}{{println .Name}}{{end}}')"
if ! awk -v name="$broker_container" '$0 == name { found=1 } END { exit !found }' <<< "$members"; then
  echo 'Существующий RabbitMQ не подключён к сети ai-shared.' >&2
  exit 1
fi

rabbitmq_ctl() {
  # Ограничивает весь процесс CLI внутри контейнера, а не только его RPC.
  bounded_docker exec --user rabbitmq "$broker_container" timeout --signal=TERM --kill-after=3s "${broker_timeout}s" rabbitmqctl --quiet --timeout "$broker_timeout" "$@"
}
rabbitmq_secret() {
  # CLI читает секретный файл в памяти через RPC, минуя интерактивный stdin Erlang.
  local expression
  case "$1" in
    add_user) expression='begin case file:read_file(_2) of {ok, P} -> case catch rabbit_auth_backend_internal:add_user(_1, P, <<"contract-diff-bootstrap">>) of ok -> ok; _ -> throw(user_creation_failed) end; _ -> throw(secret_read_failed) end end.' ;;
    authenticate_user) expression='begin case file:read_file(_2) of {ok, P} -> case catch rabbit_access_control:check_user_pass_login(_1, P) of {ok, _} -> ok; _ -> throw(authentication_failed) end; _ -> throw(secret_read_failed) end end.' ;;
    *) return 2 ;;
  esac
  printf '%s' "$broker_password" | bounded_docker exec -i --user rabbitmq "$broker_container" \
    timeout --signal=TERM --kill-after=3s "$((broker_timeout + 8))s" sh -c '
      set -eu
      umask 077
      secret_file=$(mktemp /dev/shm/contract-diff-password.XXXXXX)
      trap '\''rm -f "$secret_file"'\'' EXIT
      trap '\''exit 143'\'' TERM
      trap '\''exit 130'\'' INT
      cat > "$secret_file"
      timeout --signal=TERM --kill-after=3s "${1}s" rabbitmqctl --quiet --timeout "$1" eval "$2" "$3" "$secret_file"
    ' -- "$broker_timeout" "$expression" "$broker_user"
}
stage "Проверка пользователя RabbitMQ (лимит CLI: ${broker_timeout}с)"
users="$(rabbitmq_ctl list_users --no-table-headers)"
user_record="$(awk -v name="$broker_user" '$1 == name { print; exit }' <<< "$users")"
if [[ -n "$user_record" ]]; then
  if ! awk '$2 == "[]" { valid=1 } END { exit !valid }' <<< "$user_record"; then
    echo 'Выбранный пользователь RabbitMQ имеет служебные теги. Укажите отдельного пользователя приложения.' >&2
    exit 1
  fi
else
  stage "Создание пользователя RabbitMQ (лимит CLI: ${broker_timeout}с)"
  run_command rabbitmq_secret add_user
  echo 'Создан пользователь приложения RabbitMQ.'
fi
stage "Проверка пароля RabbitMQ (лимит CLI: ${broker_timeout}с)"
if run_command rabbitmq_secret authenticate_user; then :; else
  code=$?
  if [[ $code == 124 || $code == 137 ]]; then echo 'Превышено время ожидания RabbitMQ; процесс CLI ограничен timeout.' >&2
  else echo 'Не удалось подтвердить пароль RabbitMQ. Проверьте RABBITMQ_PASSWORD в .env; существующий пароль не меняется.' >&2; fi
  exit "$code"
fi
stage 'Проверка vhost и прав RabbitMQ'
user_permissions="$(rabbitmq_ctl list_user_permissions "$broker_user" --no-table-headers)"
if ! awk -v name="$broker_vhost" 'NF && $1 != name { foreign=1 } END { exit foreign }' <<< "$user_permissions"; then
  echo 'У пользователя RabbitMQ есть права в других vhost. Укажите отдельного пользователя этого приложения.' >&2
  exit 1
fi
vhosts="$(rabbitmq_ctl list_vhosts name --no-table-headers)"
if ! awk -v name="$broker_vhost" '$0 == name { found=1 } END { exit !found }' <<< "$vhosts"; then
  stage 'Создание выделенного vhost RabbitMQ'
  run_command rabbitmq_ctl add_vhost "$broker_vhost"
  echo 'Создан выделенный vhost приложения.'
fi
permissions="$(rabbitmq_ctl list_permissions -p "$broker_vhost" --no-table-headers)"
if ! awk -v name="$broker_user" '$1 == name && $2 == ".*" && $3 == ".*" && $4 == ".*" { valid=1 } END { exit !valid }' <<< "$permissions"; then
  stage 'Настройка прав внутри выделенного vhost'
  run_command rabbitmq_ctl set_permissions -p "$broker_vhost" "$broker_user" '.*' '.*' '.*'
  echo 'Настроены права приложения внутри выделенного vhost.'
fi
unset broker_config broker_values broker_password
echo 'Учётная запись RabbitMQ проверена.'
stage 'Запуск API и worker; ожидание готовности до 180с'
run_command docker compose "${args[@]}" up -d --wait --wait-timeout 180
docker compose "${args[@]}" ps
echo "Интерфейс: $public_url. Проверка готовности: $public_url/readyz"
