# Spec: Memory / Context

## Цель

Управление состоянием агента между запросами, сборка контекста для LLM и политика хранения данных.

## Слои памяти

### 1. Session State (in-memory, LangGraph)

Живёт в рамках одной итерации графа. При получении следующего сообщения восстанавливается из PostgreSQL checkpoint.

Содержит:
- `current_node` — текущий узел графа
- `task_intent` — извлечённый `TaskIntent` текущей сессии
- `agent_action` — выбранное действие
- `awaiting_confirmation: bool` — ожидается ли ответ пользователя
- `pending_action_payload` — сериализованный `AgentAction` для восстановления после подтверждения

---

### 2. Persistent State (PostgreSQL, LangGraph checkpointer)

Стандартная схема `langgraph-checkpoint-postgres`. Каждая итерация графа сохраняет snapshot стейта.

- Ключ изоляции: `thread_id = f"{user_id}"` — один тред на пользователя
- Позволяет возобновить граф из точки ожидания подтверждения после ответа пользователя
- Retention: 90 дней (после — удалять через pg cron job)

---

### 3. Task / Event Storage (PostgreSQL, отдельные таблицы)

Не является частью LangGraph-стейта. Хранит бизнес-данные:

- `calendar_events` — события, созданные или синхронизированные из Google Calendar
- `calendar_tasks` — задачи из Google Tasks со статусом `needsAction / completed`
- `users` — `user_id`, `timezone`, `gcal_oauth_token_encrypted`

Данные попадают через:
- Прямое создание агентом
- Google Calendar Webhook (при изменениях вне агента)

Таблица `users` содержит:
- `user_id` — Telegram user ID
- `timezone` — IANA timezone string (например `Europe/Moscow`)
- `gcal_refresh_token` — зашифрован Fernet
- `status` — `pending_oauth` | `pending_timezone` | `active`
- `gcal_webhook_channel_id` — ID активного Google webhook-канала пользователя
- `gcal_webhook_expiry` — дата истечения канала (для планового обновления)

---

### 4. OAuth Tokens (PostgreSQL, зашифрованы)

- Хранятся в таблице `users`, поле `gcal_refresh_token`
- Зашифрованы через Fernet (симметричное шифрование)
- Ключ шифрования — в переменной окружения `ENCRYPTION_KEY`, не в БД
- При `OAuthExpiredError` → агент инициирует re-auth flow и обновляет токен

---

## Контекст для LLM

Каждый вызов LLM получает собранный контекст. Контекст делится на обязательный и опциональный.

**Обязательный (передаётся всегда):**
- `user_id`, `timezone`, `current_datetime`
- Текст текущего запроса пользователя (или транскрипт)
- Последние 5 сообщений диалога (sliding window)
- Список `pending` задач пользователя (заголовок, due_date, приоритет)

**Опциональный (добавляется только если нужен):**
- Список событий из `get_events` — только при переходе на узел Slot Analyzer
- Детали конфликтующего события — только при Conflict Resolution

**Принцип:** агент не передаёт весь календарь в LLM. Контекст формируется минимально достаточным для текущего шага графа.

---

## Context Budget

Целевой лимит контекста на один LLM-вызов: **≤ 3 000 токенов**.

| Компонент | Примерный объём |
|---|---|
| System prompt | ~300 токенов |
| Диалог (5 сообщений) | ~500 токенов |
| Pending tasks (до 10) | ~400 токенов |
| Calendar events (до 10) | ~600 токенов |
| Текущий запрос | ~100 токенов |
| Запас на structured output | ~500 токенов |
| **Итого** | **~2 400 токенов** |

Если объём `pending_tasks` или `calendar_events` превышает лимит — обрезать до 10 записей (по приоритету и близости даты).

---

## Conversation History (Sliding Window)

- Хранится в LangGraph-стейте, персистируется в checkpoint
- Размер окна: **последние 5 сообщений** (user + assistant)
- При превышении — старые сообщения выпадают из контекста, но остаются в checkpoint для аудита
- Сессия считается новой (окно сбрасывается) если: последнее сообщение > 30 мин назад

---

## Политика хранения и очистки

| Данные | TTL | Механизм удаления |
|---|---|---|
| LangGraph checkpoint | 90 дней | pg_cron: удалять записи старше 90 дней |
| `calendar_events` | 90 дней | pg_cron |
| `calendar_tasks` (completed) | 90 дней | pg_cron |
| Аудиофайлы (.ogg) | 14 дней | cron-скрипт на диске |
| OAuth tokens | до отзыва | Удаляются при явном logout пользователя |
| Langfuse трейсы | 14 дней | Настройка retention в Langfuse UI |
