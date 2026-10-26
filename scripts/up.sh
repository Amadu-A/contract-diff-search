#!/usr/bin/env bash
# scripts/up.sh
# Однокомандный запуск с подготовкой учётной записи в существующем общем RabbitMQ.
set +x
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .cache
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
docker compose "${args[@]}" config --quiet
if ! docker compose "${args[@]}" build api > .cache/deploy-build.log 2>&1; then
  tail -n 80 .cache/deploy-build.log >&2
  exit 1
fi
# Settings читает dotenv как данные: source/eval здесь недопустимы.
if ! broker_config="$(docker compose "${args[@]}" run --rm --no-deps -T api python -m app.provisioning)"; then
  echo 'Подготовка RabbitMQ остановлена: не удалось проверить настройки.' >&2
  exit 1
fi
mapfile -t broker_values <<< "$broker_config"
trap 'unset broker_config broker_values broker_password' EXIT
if [[ ${#broker_values[@]} -ne 4 ]]; then
  echo 'Неверный ответ процесса настроек RabbitMQ.' >&2
  exit 1
fi
broker_container=${broker_values[0]}
broker_user=${broker_values[1]}
broker_vhost=${broker_values[2]}
broker_password=${broker_values[3]}
if [[ "$(docker inspect --format '{{.State.Running}}' "$broker_container")" != true ]]; then
  echo 'Существующий контейнер RabbitMQ не запущен. Проверьте shared-infrastructure.' >&2
  exit 1
fi
members="$(docker network inspect ai-shared --format '{{range .Containers}}{{println .Name}}{{end}}')"
if ! awk -v name="$broker_container" '$0 == name { found=1 } END { exit !found }' <<< "$members"; then
  echo 'Существующий RabbitMQ не подключён к сети ai-shared.' >&2
  exit 1
fi

rabbitmq_ctl() {
  # Выполняет только управление существующим брокером; общий контейнер не перезапускается.
  docker exec "$broker_container" rabbitmqctl --quiet --timeout 30 "$@"
}

users="$(rabbitmq_ctl list_users --no-table-headers)"
user_record="$(awk -v name="$broker_user" '$1 == name { print; exit }' <<< "$users")"
if [[ -n "$user_record" ]]; then
  if ! awk '$2 == "[]" { valid=1 } END { exit !valid }' <<< "$user_record"; then
    echo 'Выбранный пользователь RabbitMQ имеет служебные теги. Укажите отдельного пользователя приложения.' >&2
    exit 1
  fi
else
  if ! printf '%s\n' "$broker_password" | docker exec -i "$broker_container" rabbitmqctl --quiet --timeout 30 add_user "$broker_user" >/dev/null 2>&1; then
    echo 'Не удалось создать пользователя приложения в существующем RabbitMQ.' >&2
    exit 1
  fi
  echo 'Создан пользователь приложения RabbitMQ.'
fi
if ! printf '%s\n' "$broker_password" | docker exec -i "$broker_container" rabbitmqctl --quiet --timeout 30 authenticate_user "$broker_user" >/dev/null 2>&1; then
  echo 'RABBITMQ_PASSWORD не подходит существующему пользователю. Исправьте .env; пароль брокера автоматически не меняется.' >&2
  exit 1
fi
user_permissions="$(rabbitmq_ctl list_user_permissions "$broker_user" --no-table-headers)"
if ! awk -v name="$broker_vhost" 'NF && $1 != name { foreign=1 } END { exit foreign }' <<< "$user_permissions"; then
  echo 'У пользователя RabbitMQ есть права в других vhost. Укажите отдельного пользователя этого приложения.' >&2
  exit 1
fi
vhosts="$(rabbitmq_ctl list_vhosts name --no-table-headers)"
if ! awk -v name="$broker_vhost" '$0 == name { found=1 } END { exit !found }' <<< "$vhosts"; then
  rabbitmq_ctl add_vhost "$broker_vhost" >/dev/null
  echo 'Создан выделенный vhost приложения.'
fi
permissions="$(rabbitmq_ctl list_permissions -p "$broker_vhost" --no-table-headers)"
if ! awk -v name="$broker_user" '$1 == name && $2 == ".*" && $3 == ".*" && $4 == ".*" { valid=1 } END { exit !valid }' <<< "$permissions"; then
  rabbitmq_ctl set_permissions -p "$broker_vhost" "$broker_user" '.*' '.*' '.*' >/dev/null
  echo 'Настроены права приложения внутри выделенного vhost.'
fi
unset broker_config broker_values broker_password
echo 'Учётная запись RabbitMQ проверена.'
docker compose "${args[@]}" up -d --wait --wait-timeout 180
docker compose "${args[@]}" ps
echo 'Интерфейс: http://localhost:8080. Проверка готовности: /readyz'
