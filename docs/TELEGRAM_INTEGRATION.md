<!-- docs/TELEGRAM_INTEGRATION.md -->
# Интеграция внешнего Telegram-бота

Бот разрабатывается отдельно. Сервис принимает документы в существующий фоновый
конвейер, предоставляет технический статус и одноразовый переход в готовый
веб-интерфейс. Telegram API, RabbitMQ, SQLite, VLM и файловое хранилище боту не нужны.

## Адрес и авторизация

Бот обращается к `http://192.168.55.3:8082` из разрешённой локальной сети/VPN.
Для другой среды оператор явно задаёт `PUBLIC_BASE_URL` в `.env.example` или
окружении процесса; адрес ссылки не вычисляется из Host запроса или Docker DNS.
Для внешнего HTTPS используйте действующий reverse proxy и `COOKIE_SECURE=true`.
Открытие API в интернет в эту доработку не входит.

Все три метода интеграции требуют `Authorization: Bearer <секрет>`.
Секрет `TELEGRAM_INTEGRATION_TOKEN` находится только на backend сервиса и
в защищённой конфигурации сервера бота. Не помещайте его в браузер, Telegram-сообщения,
Git, аргументы журналируемых команд, логи HTTP-клиента или скриншоты.
Пустой секрет отключает интеграцию с HTTP 503. Минимальная длина — 32 символа.

Создание секрета на сервере, без вывода значения и без перезаписи существующего:

```bash
cd ~/projects/contract-diff-search
umask 077
python3 - <<'PY'
from pathlib import Path
import secrets

path = Path('.env')
text = path.read_text(encoding='utf-8') if path.exists() else ''
if any(line.startswith('TELEGRAM_INTEGRATION_TOKEN=') for line in text.splitlines()):
    raise SystemExit('Секрет уже задан; для ротации используйте процедуру ниже')
with path.open('a', encoding='utf-8') as file:
    file.write(('\n' if text and not text.endswith('\n') else '')
               + 'TELEGRAM_INTEGRATION_TOKEN=' + secrets.token_urlsafe(48) + '\n')
print('Секрет записан в .env без вывода значения')
PY
chmod 600 .env
bash scripts/up.sh
```

На Windows эквивалентное безопасное добавление:

```powershell
@'
from pathlib import Path
import secrets
p = Path('.env')
s = p.read_text(encoding='utf-8') if p.exists() else ''
assert not any(x.startswith('TELEGRAM_INTEGRATION_TOKEN=') for x in s.splitlines()), 'Секрет уже задан'
with p.open('a', encoding='utf-8') as f:
    f.write(('\n' if s and not s.endswith('\n') else '') + 'TELEGRAM_INTEGRATION_TOKEN=' + secrets.token_urlsafe(48) + '\n')
'@ | uv run --frozen python -
```

Администратор передаёт значение владельцу backend бота по принятому защищённому
каналу. На стороне бота его можно читать из переменной `CONTRACT_DIFF_TOKEN`.
`TELEGRAM_CLIENT_ID=telegram-bot` — стабильное имя интеграции, не секрет и не Telegram ID.
Его не следует менять при ротации: существующие задания принадлежат этому клиенту.
Сервис не предоставляет перечисление всех заданий; UUID чужого браузерного
или другого интеграционного задания не даёт боту доступа.

Ротация: сгенерировать новый случайный секрет, заменить только
`TELEGRAM_INTEGRATION_TOKEN` в `.env`, передать новый секрет backend бота,
обновить его конфигурацию и выполнить `bash scripts/up.sh`. После обновления API
старый токен получает 401. `TELEGRAM_CLIENT_ID` сохраняется, текущие задания
доступны с новым токеном. `.env` содержит только секреты, обычные настройки — в baseline.

## 1. Создание сравнения

`POST /api/v1/integrations/telegram/comparisons`, `multipart/form-data`.

| Поле/заголовок | Требование |
|---|---|
| `old` | Обязательный файл: **наша редакция**, всегда первая сторона |
| `new` | Обязательный файл: **редакция контрагента**, всегда вторая сторона |
| `ai_full` | Необязательный boolean, по умолчанию `false`: полный ИИ-проход |
| `ai_unverified` | Необязательный boolean, по умолчанию `false`: адресная ИИ-проверка |
| `Idempotency-Key` | Обязательный уникальный ключ одной операции, 8–128 символов `[A-Za-z0-9._:-]` |

Флаги независимы; допустимы все четыре сочетания. Алгоритмы выполняются всегда.
Назначение сторон сервис не угадывает. Поддерживаются DOC, DOCX, PDF в любой паре;
сканы распознаются существующим OCR. Лимит по baseline — 100 МиБ на файл,
300 страниц на документ, 300 МиБ распаковки DOCX, 20 МиБ одной XML-части.
Настройки могут быть уменьшены оператором. Формат проверяется по содержимому;
парольные PDF, макросы, небезопасные архивы и внешние ресурсы отклоняются.

Повтор с тем же ключом, именами файлов, содержимым и флагами возвращает прежний
`job_id`, включая конкурентную отправку. Порядок `old/new` входит в отпечаток.
Изменение любого параметра под прежним ключом даёт 409. Новый ключ означает новое
сравнение. После истечения результата прежний ключ даёт 410, пока хранится
обезличенная запись; она удаляется штатной уборкой примерно через час.
Ключ нельзя использовать как постоянное хранилище истории.

HTTP 202, пример структуры, не результат тестирования:

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued",
  "stage": "В очереди",
  "ai_full": false,
  "ai_unverified": true,
  "result_ready": false,
  "ai": {"full": {"status": "not_requested"}, "unverified": {"status": "queued"}},
  "statistics": null,
  "expires_at": null,
  "error_code": null
}
```

Ключ сохраняется ботом до завершения операции. При сетевом обрыве повторяйте
те же файлы и параметры с тем же ключом. Не создавайте автоматически новые ключи
при каждом таймауте.

## 2. Опрос состояния

`GET /api/v1/integrations/telegram/comparisons/{job_id}`.
Рекомендуемый интервал — 2–3 секунды. Ответ HTTP 200:

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "completed_with_warnings",
  "stage": "completed",
  "ai_full": true,
  "ai_unverified": true,
  "result_ready": true,
  "ai": {
    "full": {"status": "completed", "planned": 40, "processed": 40, "succeeded": 40, "failed": 0, "remaining": 0},
    "unverified": {"status": "partial", "planned": 9, "processed": 9, "succeeded": 8, "failed": 1, "remaining": 0}
  },
  "statistics": {"total": 31, "unverified": 8},
  "expires_at": "2026-10-28T14:00:00Z",
  "error_code": null
}
```

Это пример контракта, не измерение качества. Статистика может дополнительно
содержать `added`, `deleted`, `ambiguous`, `ai_confirmed`, `algorithms_confirmed`,
`both_confirmed`, `ai_needs_review`. `total` — уникальные подтверждённые изменения.
`both_confirmed` — детализация алгоритмических фактов, а не добавка к total.
Совпадение, подтверждённое ИИ, уменьшает `unverified` без увеличения total.
AI-прогресс также содержит фактически обработанные страницы, время и категории ошибок.

Статусы: `queued`, `processing`, `completed`, `completed_with_warnings`, `failed`,
`cancelled`, `expired`. `stage` во время обработки — текущая человекочитаемая стадия,
для готового результата — `completed`. `result_ready=true` означает готовый
доступный результат, а не отсутствие предупреждений. Пока работа продолжается,
`expires_at=null`: предварительный технический срок не выдаётся за окончательный.
По baseline результат хранится час **после завершения**. Истёкший собственный
UUID возвращает `status=expired`, `result_ready=false`, пока запись ещё не удалена.
После удаления записи — 404.

Ответ боту не содержит текста, имён файлов, путей, cookie, билетов или внутренних логов.

## 3. Получение защищённой ссылки

`POST /api/v1/integrations/telegram/comparisons/{job_id}/access-link`.
Вызывается только после `result_ready=true`; тело не требуется. HTTP 200:

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "url": "https://contracts.example.test/open#ticket=<opaque-token>",
  "link_expires_at": "2026-10-28T13:15:00Z",
  "result_expires_at": "2026-10-28T14:00:00Z"
}
```

Домен в примере не настроен автоматически. Используется именно `PUBLIC_BASE_URL`.
Билет криптографически случаен; в SQLite хранится только SHA256. Срок активации
по умолчанию 900 секунд (`ACCESS_LINK_SECONDS`), не дольше TTL результата.
Выдача новой ссылки этому заданию отзывает предыдущую ещё не активированную ссылку.
Уже активированные сессии действуют до истечения результата.

Передавайте URL непосредственно пользователю, не записывайте в лог.
Это **переносимое право доступа**: пересланную ссылку потенциально может открыть
другой сотрудник. Персональная авторизация по Telegram ID не реализована.

GET `/open` только отдаёт страницу; GET не расходует билет. Fragment не отправляется
серверу в URL и не попадает в access log. JavaScript сразу очищает fragment и
обменивает билет внутренним POST `/api/v1/result-access` на отдельную HttpOnly-cookie.
Этот endpoint не входит в API бота. После обмена адрес содержит только публичный
UUID, а право доступа остаётся в cookie. `Referrer-Policy: no-referrer`, CSP и
запрет кеширования действуют на страницу и API. Билет активируется только один раз.

Браузер сразу загружает существующие просмотрщики, различия, статистику,
AI-прогресс и экспорт. Перезагрузка страницы работает с той же cookie.
Никакой повторной загрузки, OCR или сравнения при переходе нет. Разрешены только
чтение этого результата и доступный экспорт. Удаление, отмена, повтор ИИ,
создание заданий от имени интеграционного владельца и чтение других UUID запрещены.
Исходная `contract_diff_session` не заменяется и не получает право собственности.
После TTL результат закрывается независимо от cookie и работы фоновой уборки.
Обычный браузерный сценарий и его CSRF-защита сохраняются.

## Примеры curl

Читатель, активировавший билет через `POST /api/v1/result-access`, может скачать
табличный PDF по `GET /api/v1/comparisons/{job_id}/report` с полученной сессионной
cookie. В интерфейсе это кнопка «Скачать отчёт» под списком различий.
Действуют те же права и TTL; интеграционный Bearer-токен не заменяет cookie
читателя. Отчёт включает все сохранённые статусы независимо от переключателя
показа непроверенных областей и не запускает обработку документов.

Ввести секрет без сохранения значения в shell history:

```bash
BASE=http://192.168.55.3:8082
read -rsp 'Integration token: ' CONTRACT_DIFF_TOKEN; printf '\n'
KEY=$(python3 -c 'import uuid; print(uuid.uuid4())')
curl --fail-with-body --config - \
  -F 'old=@our.docx' -F 'new=@partner.pdf' \
  -F 'ai_full=false' -F 'ai_unverified=true' \
  "$BASE/api/v1/integrations/telegram/comparisons" <<EOF
header = "Authorization: Bearer $CONTRACT_DIFF_TOKEN"
header = "Idempotency-Key: $KEY"
EOF
```

Секрет передаётся curl через stdin, без значения в аргументах процесса.
Для polling и получения ссылки используйте полученный UUID:

```bash
JOB_ID=550e8400-e29b-41d4-a716-446655440000
curl --fail-with-body --config - "$BASE/api/v1/integrations/telegram/comparisons/$JOB_ID" <<EOF
header = "Authorization: Bearer $CONTRACT_DIFF_TOKEN"
EOF
# Ответ содержит секретную ссылку: сохранить в закрытый временный файл, не в общий лог.
umask 077
mkdir -p .cache
curl --fail-with-body --config - -X POST \
  "$BASE/api/v1/integrations/telegram/comparisons/$JOB_ID/access-link" \
  > .cache/telegram-access.json <<EOF
header = "Authorization: Bearer $CONTRACT_DIFF_TOKEN"
EOF
unset CONTRACT_DIFF_TOKEN
```

## Пример Python httpx для backend бота

```python
import os
import time
from pathlib import Path

import httpx


def compare_documents(our: Path, partner: Path, key: str, *, full=False, targeted=False) -> str:
    """Возвращает одноразовый URL для отправки пользователю; секреты и URL не журналирует."""
    with httpx.Client(
        base_url=os.environ['CONTRACT_DIFF_URL'],
        headers={'Authorization': 'Bearer ' + os.environ['CONTRACT_DIFF_TOKEN']},
        timeout=120,
    ) as client:
        with our.open('rb') as old, partner.open('rb') as new:
            response = client.post(
                '/api/v1/integrations/telegram/comparisons',
                headers={'Idempotency-Key': key},
                files={'old': (our.name, old), 'new': (partner.name, new)},
                data={'ai_full': str(full).lower(), 'ai_unverified': str(targeted).lower()},
            )
        response.raise_for_status()
        job_id = response.json()['job_id']
        endpoint = '/api/v1/integrations/telegram/comparisons/' + job_id
        while True:
            response = client.get(endpoint)
            if response.status_code == 429:
                time.sleep(int(response.headers.get('Retry-After', '60')))
                continue
            response.raise_for_status()
            state = response.json()
            if state['result_ready']:
                break
            if state['status'] in {'failed', 'cancelled', 'expired'}:
                raise RuntimeError(state.get('error_code') or state['status'])
            time.sleep(3)
        response = client.post(endpoint + '/access-link')
        response.raise_for_status()
        return response.json()['url']
```

`key` генерируется один раз на пользовательскую операцию и сохраняется для
сетевых повторов. Возвращённый URL отправляется пользователю; не печатайте его
через logger и не сохраняйте вместе с секретом в аналитике.

## Ошибки и ограничения

Ошибки содержат безопасный `detail` и, где применимо, машинный `code`.

| HTTP | Смысл |
|---|---|
| 401 | Нет Bearer-токена или секрет неверен |
| 403 | Браузерный Origin для серверного API либо нарушение CSRF/прав изменения |
| 404 | UUID не существует, принадлежит другому клиенту или запись удалена |
| 409 | `idempotency_conflict`, результат/экспорт ещё не готов |
| 410 | `result_expired`, билет истёк/отозван/уже использован |
| 413 | Слишком большой файл или multipart-запрос |
| 422 | Некорректные поля, ключ, UUID, формат или структура документа |
| 429 | Частота запросов или число активных заданий; для частоты есть `Retry-After` |
| 503 | Интеграция/публичный URL не настроены либо очередь временно недоступна |

Baseline: 6 загрузок в минуту, 120 прочих интеграционных запросов в минуту,
4 активных задания интеграции, 20 заданий всего в сервисе.
Обмен билетов ограничен 20 запросами в минуту на фактический адрес соединения;
произвольному `X-Forwarded-For` сервис не доверяет. Счётчики атомарны для процессов API.
Идемпотентные повторы также расходуют лимит запросов. Поддержки массового перечисления,
удаления и управления заданиями со стороны бота нет. Никакие токены не отменяют TTL.

Публичная схема: `/openapi.json`, интерфейс: `/docs`, секция `Telegram integration`.
Автоматические проверки включают конкурентную отправку, авторизацию до чтения тела,
однократную активацию, новый браузер, ротацию, TTL, CSRF, права чтения и отсутствие секретов в логах.
