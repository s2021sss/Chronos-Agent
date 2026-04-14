# Spec: Observability / Evals

## Цель

Создание двух независимых инструментов логирования:
- **Structured App Logs** — JSON-логи; инфраструктурные события, состояния системы, ошибки сервиса
- **Langfuse** — трассировка LLM-решений: вызовы модели, tool calls, latency, spans

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
- Retention: 90 дней (pg_cron: `DELETE FROM service_logs WHERE timestamp < now() - INTERVAL '90 days'`)

---

## Каталог событий

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

### Итерация агента (ReAct)

| Событие | Уровень | Дополнительные поля |
|---|---|---|
| `agent_iteration_started` | INFO | `user_id`, `trigger`, `thread_id` |
| `agent_iteration_completed` | INFO | `user_id`, `latency_ms` |
| `agent_iteration_timeout` | WARNING | `user_id`, `elapsed_ms` |
| `agent_iteration_failed` | ERROR | `user_id`, `error`, `node` |
| `rate_limit_hit` | WARNING | `user_id`, `messages_in_window` |
| `confirmation_sent` | INFO | `user_id` |
| `confirmation_received` | INFO | `user_id`, `confirmed` |
| `session_cancelled` | INFO | `user_id` |

### ReAct Graph узлы

| Событие | Уровень | Дополнительные поля |
|---|---|---|
| `react_reasoner_tool_call` | INFO | `user_id`, `tool`, `iteration` |
| `react_reasoner_final_answer` | INFO | `user_id`, `iteration`, `answer_preview` |
| `react_reasoner_max_iterations` | WARNING | `user_id`, `iteration_count` |
| `react_reasoner_llm_error` | ERROR | `user_id`, `error` |
| `react_reasoner_context_restored` | INFO | `user_id`, `prior_messages` |

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

Каждая итерация LangGraph-графа создаёт один **trace** в Langfuse. Внутри trace — generation/span для каждого ключевого шага.

### Структура trace (ReAct агент)

```
trace: agent_run
  generation: react_reasoner_iter_1  (LLM generation — первый вызов)
    input: messages[system, user]
    output: tool_call или final_answer
    metadata: model_id, iteration, tools_count

  span: tool_router                  (диспетчеризация tool_call)
    metadata: tool, args, iteration

  span: read_tool:<name>             (если read-only tool, например read_tool:get_calendar_events)
    input: tool_name, args
    output: result_keys, has_error

  generation: react_reasoner_iter_2  (если read-only tool был выполнен)
    input: messages[..., tool_result]
    output: side-effect tool_call

  span: react_hitl_resume            (при resume после HITL-подтверждения)
    metadata: tool_name, confirmed

  span: write_tool:<name>            (после подтверждения, например write_tool:create_event)
    input: tool_name, args, confirmed
    output: success, error

  span: react_respond
    output: answer_preview, delivered: true
```

### Обязательные поля каждого trace

| Поле | Описание |
|---|---|
| `user_id` | Идентификатор пользователя (не PII) |
| `session_id` | `thread_id` LangGraph |
| `trigger` | `text_message` / `voice_message` / `cron_check` / `user_confirmation` |
| `agent_version` | Версия агента из конфига |
| `model_id` | ID модели LLM |
| `total_latency_ms` | Время от начала итерации до отправки ответа |

---

## Метрики (считаются по трейсам)

### Технические

| Метрика | Как считать | Цель |
|---|---|---|
| `e2e_latency_p95` | p95 по `total_latency_ms` всех трейсов | < 7 000 ms |
| `llm_latency_p95` | p95 по span `react_reasoner_iter_1` | < 3 000 ms |
| `error_rate` | Доля трейсов с `status = error` | < 5% |
| `retry_rate` | Доля трейсов с retry-попытками | Мониторинг тренда |

### Продуктовые

| Метрика | Как считать | Цель |
|---|---|---|
| `binary_acceptance` | Доля `confirmation_sent` → `confirmed = true` | > 60% |
| `tool_selection_accuracy` | Доля трейсов, где выбранный tool соответствует ожидаемому действию (eval) | > 85% |

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

### ReAct Eval: Tool Selection & Arguments

- Набор: 32 эталонных кейса с ожидаемым tool call + аргументами (`tests/evals/dataset/nlu_dataset.yaml`)
- Методология: запуск LLM с `REACT_SYSTEM_PROMPT` + `REACT_TOOL_DEFINITIONS` → проверка выбранного tool name и аргументов (datetime, title, duration)
- Read-only tool calls симулируются (пустой ответ), loop продолжается до SIDE_EFFECT/TERMINAL tool
- Включает «мусорные» запросы (вне зоны ответственности) для оценки `ask_user` flow
- Запускается: `python tests/evals/run_nlu_eval.py --out results.json`
- Рекомендуемая задержка: `--delay 2.0` для избежания rate limit Mistral API

### Scenario-eval: End-to-end (ручной)

- Набор: 11 сквозных сценариев (`tests/evals/SCENARIO_CHECKLIST.md`)
- Входы: текстовые и голосовые сообщения в реальный Telegram-бот
- Результат: фактическое изменение в Google Calendar (тестовый аккаунт)
- Наблюдение за шагами агента в Langfuse Dashboard

### Binary Acceptance (in-production)

- Считается по данным из Langfuse: `confirmed / total_confirmations_sent`
- Обновляется по мере накопления реальных взаимодействий

---

## Политика логирования

- PII (имена, email, номера телефонов) маскируются перед записью и в app-логи, и в Langfuse
- Аудиофайлы не логируются нигде — только `transcript_text` после Whisper
- Langfuse retention: 14 дней (настройка в UI)
- `service_logs` retention: 90 дней (pg_cron)
- Docker json-file rotation: `max-size: 100m, max-file: 5` (~500 МБ на диске)
