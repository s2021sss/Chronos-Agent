# Spec: Serving / Config

## Цель

Запуск, конфигурация, управление секретами и версиями моделей для PoC-среды.


## Компоненты и способ запуска

| Компонент | Способ запуска | Порт |
|---|---|---|
| Telegram Bot (aiogram, webhook) | Python process / Docker container | — (исходящий webhook) |
| Webhook-сервер (FastAPI) | Docker container | 8000 |
| APScheduler (cron) | Встроен в процесс агента | — |
| PostgreSQL | Docker container | 5432 |
| Langfuse (self-hosted, PoC) | Docker Compose | 3000 |

В PoC всё поднимается через `docker-compose`. Отдельного Kubernetes не требуется.

---

## Health Check

`GET /health` — эндпоинт для Docker healthcheck и внешнего мониторинга.

**Проверяет:**
1. Соединение с PostgreSQL (простой `SELECT 1`)
2. Доступность OpenRouter API (HEAD-запрос или cached status, не вызывает LLM)

**Ответы:**

```json
// 200 OK — всё в порядке
{ "status": "ok", "postgres": "ok", "openrouter": "ok" }

// 503 Service Unavailable — хотя бы один компонент недоступен
{ "status": "degraded", "postgres": "ok", "openrouter": "unreachable" }
```

- Langfuse недоступность **не влияет** на статус (не критично для работы агента)
- Используется в `docker-compose` как `healthcheck` для контейнера агента
- Таймаут проверки каждого компонента: 3 секунды

---

## Database Migrations (Alembic)

Инструмент: [Alembic](https://alembic.sqlalchemy.org/).

**Структура:**
```
alembic/
  versions/       # миграции
  env.py          # подключение к БД из POSTGRES_URL
alembic.ini
```

**Процедура при деплое:**
```bash
# Накатить все pending-миграции
alembic upgrade head
# Затем запустить сервис
docker-compose up -d agent
```

**Правила работы с миграциями:**
- Каждое изменение схемы БД — отдельная миграция, закоммиченная в git
- Миграции только forward (rollback — через новую миграцию, не `downgrade`)
- `alembic upgrade head` запускается автоматически в entrypoint контейнера агента до старта сервиса

**Начальные таблицы (initial migration):**
- `users` — профили пользователей
- `calendar_events` — локальная копия событий Google Calendar
- `calendar_tasks` — локальная копия задач Google Tasks
- `service_logs` — ERROR/CRITICAL события app-логов (для поиска без доступа к файлам хоста)

LangGraph checkpointer создаёт свои таблицы самостоятельно через `create_all` при первом запуске — они не управляются Alembic.

---

## Конфигурация (переменные окружения)

Все параметры задаются через `.env`. Секреты не хардкодятся.

```dotenv
# Telegram
TELEGRAM_BOT_TOKEN=

# Google OAuth
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REDIRECT_URI=

# Шифрование OAuth-токенов пользователей
ENCRYPTION_KEY=                     # Fernet key, base64

# LLM
OPENROUTER_API_KEY=
OPENROUTER_MODEL=mistral/mistral-large-latest

# PostgreSQL
POSTGRES_URL=postgresql://user:pass@localhost:5432/chronos

# Langfuse
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=http://localhost:3000

# Whisper
WHISPER_MODEL=base                  # base / small / medium
WHISPER_DEVICE=cpu                  # cpu / cuda

# Агент
CONFIDENCE_THRESHOLD=0.6
CONFIRMATION_TIMEOUT_SECONDS=300    # 5 мин
AGENT_ITERATION_TIMEOUT_SECONDS=30  # макс. время ReAct-цикла
MAX_TOOL_CALLS_PER_ITERATION=3
SEARCH_WINDOW_HOURS=24
CRON_INTERVAL_MINUTES=60
WEBHOOK_RENEWAL_INTERVAL_DAYS=6
ORPHAN_SESSION_TIMEOUT_MINUTES=10   # сессии старше N мин при старте = orphaned

# Rate limiting
RATE_LIMIT_MSG_PER_MINUTE=5         # макс. сообщений от одного пользователя в минуту

# Логирование
LOG_LEVEL=INFO                      # DEBUG / INFO / WARNING / ERROR

# Ограничения ввода
MAX_TEXT_LENGTH=4000
MAX_AUDIO_SIZE_MB=20
AUDIO_RETENTION_DAYS=14
```

---

## Версии моделей

| Модель | Параметр | Текущее значение |
|---|---|---|
| LLM | `OPENROUTER_MODEL` | `mistral/mistral-large-latest` |
| Whisper | `WHISPER_MODEL` | `base` (PoC); при необходимости — `small` |

Смена модели — через переменную окружения, без пересборки образа.

---

## Google OAuth Flow

1. Пользователь впервые пишет боту → агент отправляет ссылку авторизации Google OAuth 2.0
2. Пользователь разрешает доступ → Google редиректит на `GOOGLE_REDIRECT_URI` с кодом
3. Сервер обменивает код на `access_token` + `refresh_token`
4. `refresh_token` шифруется Fernet и сохраняется в `users.gcal_refresh_token`
5. `access_token` не хранится — обновляется через `refresh_token` при каждом вызове

Scope: `https://www.googleapis.com/auth/calendar`, `https://www.googleapis.com/auth/tasks`

---

## Webhook регистрация (Telegram)

Telegram Bot работает в режиме webhook:
```
POST https://api.telegram.org/bot{TOKEN}/setWebhook
  url: https://{domain}/webhook/telegram
```

Требует публичного HTTPS-домена. В PoC — `ngrok` для локальной разработки.

---

## Безопасность

### Секреты и конфигурация
- `.env` не коммитится в git (добавлен в `.gitignore`)
- Секреты в production — через Docker secrets или переменные среды CI/CD
- `ENCRYPTION_KEY` ротируется вручную; при ротации — перешифровать все токены в `users.gcal_refresh_token`

### Аутентификация webhook-эндпоинтов
- Telegram webhook проверяет `X-Telegram-Bot-Api-Secret-Token` (параметр `setWebhook`); запросы без токена → 403
- Google Calendar Webhook проверяет `X-Goog-Channel-Token`; запросы без совпадающего токена → 403
- Оба эндпоинта принимают запросы только от ожидаемых источников; IP-whitelist не применяется (Google и Telegram не имеют фиксированных диапазонов)

### Изоляция данных пользователей
- Каждый инструментальный вызов получает `user_id` явно; перед чтением/записью в БД и перед Google API-вызовом проверяется, что ресурс принадлежит этому `user_id`
- LangGraph `thread_id = user_id` — checkpoint изолирован по пользователю
- Данные одного пользователя никогда не передаются в контекст LLM другого пользователя

### Обработка отозванных OAuth-токенов
- `401 Unauthorized` от Google → попытка обновить через `refresh_token`
- Если обновление даёт `invalid_grant` (токен отозван пользователем в Google Account) → флаг `gcal_refresh_token = NULL` в `users`, уведомить пользователя о необходимости повторной авторизации, инициировать onboarding OAuth flow
- Отличие от истёкшего токена: `invalid_grant` — постоянный отзыв, не временная ошибка; retry не поможет

### Санация пользовательского ввода
- Заголовки событий и задач санируются в Pydantic-валидаторах (удаление управляющих символов, обрезка длины) — подробно в [02-tools-apis.md](02-tools-apis.md)
- Текст пользователя не передаётся в tool-вызовы напрямую — только через структурированный `TaskIntent` после LLM-парсинга

---

## Надёжность и мониторинг запуска

### Старт сервиса
- Все контейнеры имеют `restart: unless-stopped` в docker-compose
- PostgreSQL: `healthcheck` перед стартом агента (wait-for-it)
- При недоступности PostgreSQL при старте — агент не запускается, логирует `CRITICAL`
- При недоступности OpenRouter при старте — агент запускается, но LLM-запросы падают с retry
- Langfuse недоступен — агент работает, трейсы теряются (не критично для PoC)

### Надёжность во время работы

**Whisper (ASR):**
- Запускается в том же процессе, что и агент (in-process)
- Ограничен таймаутом `asyncio.wait_for` в 30 с (общий `AGENT_ITERATION_TIMEOUT_SECONDS`)
- При зависании inference (редко, но возможно на CPU) — timeout прерывает итерацию; пользователю предлагается прислать текстом
- Whisper занимает 1 CPU core во время inference; параллельные голосовые сообщения выстраиваются в очередь (FastAPI обрабатывает один голосовой запрос за раз в силу GIL)

**Cron и APScheduler:**
- `max_instances=1` на каждую задачу — если предыдущий прогон не завершился, новый пропускается (misfire)
- Сбой cron-задачи (необработанное исключение) логируется как `ERROR`; APScheduler продолжает работу, следующий тик запускается по расписанию
- Нет алертинга при систематических сбоях cron в PoC — мониторинг вручную через app-логи

**PostgreSQL недоступен в runtime (не при старте):**
- LangGraph checkpointer выбросит исключение при попытке сохранить checkpoint
- Итерация графа падает; пользователь получает сообщение об ошибке (через Telegram, если Telegram доступен)
- Агент не выходит из строя целиком — следующий запрос будет обработан при восстановлении БД
- Логировать `ERROR` с трассировкой

**Атомарность `execute_action` + `state_writer`:**
- Эти два шага не атомарны: инструмент может успешно создать событие в Google Calendar, а запись в локальную БД — упасть
- При сбое `state_writer`: событие существует в Google Calendar, но отсутствует в `calendar_events`; при следующем webhook-уведомлении от Google — запись будет создана через `ON CONFLICT DO UPDATE`
- Дублирование не возникнет: идемпотентность `create_event` проверяет `calendar_events` перед API-вызовом

---

## Минимальные требования к хосту (PoC)

Ориентировочные требования для запуска всего стека на одной машине:

| Компонент | RAM | Примечание |
|---|---|---|
| Agent + Bot (FastAPI + LangGraph) | ~512 MB | |
| Whisper `base` | ~1 GB | При `small` — ~2 GB |
| PostgreSQL | ~512 MB | |
| Langfuse (Docker, self-hosted) | ~2–4 GB | Включает ClickHouse и Redis |
| ОС + Docker overhead | ~1 GB | |
| **Итого** | **~5–7 GB RAM** | Рекомендуется хост с ≥ 8 GB RAM |
