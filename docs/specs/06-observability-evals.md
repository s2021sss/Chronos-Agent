# Spec: Observability / Evals

## Цель

Создание двух независимых инструментов логирования:
- **Structured App Logs** — JSON-логи; инфраструктурные события, состояния системы, ошибки сервиса
- **Langfuse** — трассировка LLM-решений: вызовы модели, confidence, reason_text, latency

Они дополняют друг друга и не дублируют: app-логи отвечают на вопрос «что делал сервис», Langfuse — «как агент принял решение».


## Структурированное App-логирование

**Формат:** JSON, одна строка на событие. Выводятся в `stdout` (docker-compose собирает).

```json
{
  "timestamp": "2026-04-01T12:34:56.789Z",
  "level": "INFO",
  "service": "chronos-agent",
  "agent_version": "0.1.0",
  "user_id": "u_1234",
  "event": "agent_iteration_started",
  "trigger": "user_message",
  "thread_id": "thread_u_1234"
}
```

**Обязательные поля каждого лог-события:**

| Поле | Описание |
|---|---|
| `timestamp` | ISO 8601 UTC |
| `level` | DEBUG / INFO / WARNING / ERROR / CRITICAL |
| `service` | `"chronos-agent"` |
| `agent_version` | Из `AGENT_VERSION` |
| `event` | Машинно-читаемый идентификатор события (snake_case) |
| `user_id` | Опционально; не включать если недоступен (инфраструктурные события) |

**Уровни логирования:**

| Уровень | Когда использовать | Примеры |
|---|---|---|
| `DEBUG` | Детали для разработки (в prod отключён) | Содержимое промптов, raw API ответы, webhook payload |
| `INFO` | Нормальные операции | Создание события, onboarding-шаги, старт cron |
| `WARNING` | Нестандартные ситуации, не ломающие flow | 429 от Google API, orphaned session, rate limit hit, webhook-канал истёк |
| `ERROR` | Сбой после retry, неожиданное исключение | LLM недоступен после 3 попыток, OAuth revoked, DB connection failed в runtime |
| `CRITICAL` | Сервис не может запуститься | PostgreSQL недоступен при старте |

**Настройка уровня:** через `LOG_LEVEL` в `.env` (default: `INFO` в prod, `DEBUG` в dev).

**PII в логах:** имена, email, текст задач не логировать в полях верхнего уровня. Идентификаторы (`user_id`, `gcal_event_id`) — допустимы.

---

## Хранение App-логов

Двухуровневая схема хранения — без дополнительных сервисов:

### Уровень 1 — Docker json-file (все уровни)

Весь `stdout` агента автоматически сохраняется Docker-демоном в JSON-файлы на диске хоста. Настраивается в `docker-compose.yml`:

```yaml
services:
  agent:
    logging:
      driver: "json-file"
      options:
        max-size: "100m"   # ротация при достижении 100 МБ
        max-file: "5"      # хранить последние 5 файлов (~500 МБ итого)
```

Просмотр логов:
```bash
docker logs chronos-agent --follow
docker logs chronos-agent --since 1h | grep '"level":"ERROR"'
```

### Уровень 2 — PostgreSQL `service_logs` (только ERROR и CRITICAL)

Критические события дополнительно записываются в таблицу `service_logs` для удобного поиска без доступа к файлам хоста.

```sql
CREATE TABLE service_logs (
    id          BIGSERIAL PRIMARY KEY,
    timestamp   TIMESTAMPTZ NOT NULL,
    level       TEXT NOT NULL,          -- ERROR / CRITICAL
    event       TEXT NOT NULL,
    user_id     TEXT,
    error       TEXT,
    extra       JSONB,
    agent_version TEXT
);
```

- Запись выполняется асинхронно (fire-and-forget) — сбой записи в `service_logs` не влияет на основной flow
- Таблица управляется Alembic (initial migration)
- Retention: 90 дней (pg_cron: `DELETE FROM service_logs WHERE timestamp < now() - INTERVAL '90 days'`)
- Для DEBUG/INFO/WARNING — только Docker json-file, в `service_logs` не пишется


---

## Каталог событий

Полный перечень событий, которые должны логироваться. Дополнительные поля (`extra`) указаны для каждого события.

### Жизненный цикл сервиса

| Событие | Уровень | Дополнительные поля |
|---|---|---|
| `service_started` | INFO | `host`, `port` |
| `service_stopping` | INFO | — |
| `db_unavailable_at_startup` | CRITICAL | `error` |
| `orphaned_session_recovery_started` | INFO | `sessions_found` |
| `orphaned_session_found` | WARNING | `user_id`, `age_minutes` |
| `orphaned_session_recovered` | INFO | `user_id` |

### Onboarding

| Событие | Уровень | Дополнительные поля |
|---|---|---|
| `onboarding_started` | INFO | `user_id` |
| `oauth_flow_initiated` | INFO | `user_id` |
| `oauth_completed` | INFO | `user_id` |
| `timezone_set` | INFO | `user_id`, `timezone` |
| `onboarding_completed` | INFO | `user_id` |
| `onboarding_blocked_message` | INFO | `user_id`, `status` |

### Итерация агента

| Событие | Уровень | Дополнительные поля |
|---|---|---|
| `agent_iteration_started` | INFO | `user_id`, `trigger`, `thread_id` |
| `agent_iteration_completed` | INFO | `user_id`, `action`, `latency_ms` |
| `agent_iteration_timeout` | WARNING | `user_id`, `elapsed_ms` |
| `agent_iteration_failed` | ERROR | `user_id`, `error`, `node` |
| `rate_limit_hit` | WARNING | `user_id`, `messages_in_window` |
| `confidence_below_threshold` | INFO | `user_id`, `confidence` |
| `confirmation_sent` | INFO | `user_id` |
| `confirmation_received` | INFO | `user_id`, `confirmed` |
| `confirmation_timeout` | WARNING | `user_id` |
| `session_cancelled` | INFO | `user_id` |

### Инструменты (Tool Layer)

| Событие | Уровень | Дополнительные поля |
|---|---|---|
| `event_created` | INFO | `user_id`, `gcal_event_id` |
| `event_creation_deduplicated` | INFO | `user_id`, `gcal_event_id` |
| `event_moved` | INFO | `user_id`, `gcal_event_id` |
| `task_created` | INFO | `user_id`, `gcal_task_id` |
| `task_creation_deduplicated` | INFO | `user_id`, `gcal_task_id` |
| `task_completed` | INFO | `user_id`, `gcal_task_id` |
| `tool_validation_failed` | WARNING | `user_id`, `tool`, `reason` |
| `api_retry` | WARNING | `user_id`, `api`, `attempt`, `status_code` |
| `api_failed_after_retries` | ERROR | `user_id`, `api`, `attempts`, `error` |

### OAuth и авторизация

| Событие | Уровень | Дополнительные поля |
|---|---|---|
| `oauth_token_refreshed` | DEBUG | `user_id` |
| `oauth_token_expired` | WARNING | `user_id` |
| `oauth_token_revoked` | ERROR | `user_id` |
| `reauth_flow_initiated` | INFO | `user_id` |

### Cron и Scheduler

| Событие | Уровень | Дополнительные поля |
|---|---|---|
| `cron_check_started` | INFO | `active_users_count` |
| `cron_check_completed` | INFO | `processed`, `skipped`, `failed`, `duration_ms` |
| `cron_user_no_overdue_tasks` | DEBUG | `user_id` |
| `cron_user_failed` | ERROR | `user_id`, `error` |
| `cron_misfire` | WARNING | `job_id`, `scheduled_at` |
| `webhook_renewal_started` | INFO | `channels_to_renew` |
| `webhook_renewal_completed` | INFO | `renewed`, `failed`, `duration_ms` |
| `webhook_renewal_failed` | WARNING | `user_id`, `error` |

### Google Webhook (входящие push-уведомления)

| Событие | Уровень | Дополнительные поля |
|---|---|---|
| `webhook_received` | DEBUG | `user_id`, `resource_type`, `resource_id` |
| `webhook_auth_failed` | WARNING | `reason` |
| `webhook_db_updated` | DEBUG | `user_id`, `resource_type`, `gcal_id` |
| `webhook_deduplicated` | DEBUG | `user_id`, `gcal_id` |
| `webhook_channel_expired` | WARNING | `user_id` |

### Инфраструктура (runtime)

| Событие | Уровень | Дополнительные поля |
|---|---|---|
| `db_connection_failed` | ERROR | `error` |
| `langfuse_unavailable` | WARNING | `error` |
| `whisper_inference_timeout` | WARNING | `user_id`, `elapsed_ms` |

---

## Трассировка (Langfuse)

Каждая итерация LangGraph-графа создаёт один **trace** в Langfuse. Внутри trace — spans для каждого ключевого шага.

### Структура trace

```
trace: agent_iteration
  span: transcribe              (если голосовой запрос)
    input: audio_file_path
    output: transcript_text
    latency: ms

  span: extract_intent
    input: user_text, conversation_history
    output: TaskIntent JSON
    metadata: model_id, confidence, tokens_used

  span: slot_analyzer           (если вызывался)
    input: requested_time, duration
    output: list[CalendarSlot]
    latency: ms

  span: action_decider
    input: TaskIntent, slots, conflict_info
    output: AgentAction
    metadata: reason_text, agent_version

  span: execute_action          (если вызывался)
    input: AgentAction
    output: gcal_event_id / error
    latency: ms
```

### Обязательные поля каждого trace

| Поле | Описание |
|---|---|
| `user_id` | Идентификатор пользователя (не PII) |
| `session_id` | `thread_id` LangGraph |
| `trigger` | `user_message` / `cron_check` / `user_confirmation` |
| `agent_version` | Версия агента из конфига |
| `model_id` | ID модели LLM |
| `confidence` | Значение из `TaskIntent` |
| `action` | Итоговое действие: `create` / `suggest` / `ask_user` / `noop` |
| `reason_text` | Краткое объяснение решения агента на естественном языке |
| `total_latency_ms` | Время от начала итерации до отправки ответа |

---

## Метрики (считаются по трейсам)

### Технические

| Метрика | Как считать | Цель |
|---|---|---|
| `e2e_latency_p95` | p95 по `total_latency_ms` всех трейсов | < 7 000 ms |
| `llm_latency_p95` | p95 по span `extract_intent` | < 3 000 ms |
| `error_rate` | Доля трейсов с `status = error` | < 5% |
| `retry_rate` | Доля трейсов с retry-попытками | Мониторинг тренда |

### Продуктовые

| Метрика | Как считать | Цель |
|---|---|---|
| `nlu_success_rate` | Доля трейсов, где `confidence >= 0.6` и `TaskIntent` прошёл валидацию | > 80% |
| `binary_acceptance` | Доля `request_confirmation` → `confirmed = true` | > 60% |
| `hallucination_rate` | Доля трейсов с аномальным `action` (вне разрешённого набора) или провалившейся валидацией Pydantic | < 10% |

---

## Алерты

В PoC — мониторинг вручную через Langfuse Dashboard и app-логи. Автоматических алертов нет.

Пороги для ручного осмотра:
- `error_rate` > 10% за последний час (Langfuse)
- `e2e_latency_p95` > 10 s за последние 30 мин (Langfuse)
- `binary_acceptance` < 40% за день (Langfuse)
- `cron_user_failed` повторяется для одного `user_id` более 3 раз подряд (app-логи / `service_logs`)
- `webhook_renewal_failed` для пользователя 2+ итерации подряд (app-логи / `service_logs`)

---

## Оценка качества (Evals)

### Unit-eval: NLU (Intent Extraction)

- Набор: 20–30 эталонных фраз с ожидаемым `TaskIntent` JSON
- Проверяется: корректность `scheduled_at`, `duration_minutes`, `priority`, `deadline`
- Включает «мусорные» запросы (вне зоны ответственности агента) для оценки `hallucination_rate`
- Запускается: вручную перед каждым релизом изменений в промпте

### Scenario-eval: End-to-end

- Набор: 10 сквозных сценариев (UC-001 — UC-008 из product-proposal)
- Входы: текстовые и голосовые сообщения
- Результат: фактическое изменение в Google Calendar (тестовый аккаунт)
- Оценка: LLM-as-a-judge сравнивает итоговое состояние календаря с эталоном
- Параллельно замеряется `total_latency_ms`

### Binary Acceptance (in-production)

- Считается по данным из Langfuse: `confirmed / total_confirmations_sent`
- Обновляется по мере накопления реальных взаимодействий

---

## Политика логирования

- PII (имена, email, номера телефонов) маскируются перед записью и в app-логи, и в Langfuse
- В `reason_text` не пишутся персональные данные — только описание логики решения
- Аудиофайлы не логируются нигде — только `transcript_text` после Whisper
- Langfuse retention: 14 дней (настройка в UI)
- `service_logs` retention: 90 дней (pg_cron)
- Docker json-file rotation: `max-size: 100m, max-file: 5` (~500 МБ на диске)
