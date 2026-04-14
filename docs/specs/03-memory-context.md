# Spec: Memory / Context

## Цель

Управление состоянием агента между запросами, сборка контекста для LLM и политика хранения данных.

## Слои памяти

### 1. Session State (in-memory, LangGraph)

Живёт в рамках одной итерации графа. При получении следующего сообщения восстанавливается из PostgreSQL checkpoint.

Содержит (`ReactAgentState`):
- `messages` — полная история function calling API текущего хода (system/user/assistant/tool)
- `prior_messages` — последние 30 сообщений из предыдущих ходов (без system prompt)
- `pending_tool_call` — последний tool_call от LLM (name + arguments), ожидающий исполнения
- `tool_call_id` — ID текущего pending tool_call
- `awaiting_confirmation` — ожидается ли HITL-ответ пользователя
- `confirmed` — результат HITL (заполняется callback handler при resume)
- `iteration_count` — счётчик шагов reasoner (защита от бесконечного цикла)
- `final_answer` — финальный текст ответа агента (устанавливается reasoner)

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

## Контекст для LLM (ReAct агент)

Каждый вызов LLM (`reasoner_node`) получает собранный контекст.

**На первой итерации хода:**
- System prompt: текущая дата/время/timezone + инструкции + tool descriptions
- `prior_messages`: последние 30 сообщений из предыдущих ходов (trimmed до первого `user`)
- Текущий запрос пользователя (или транскрипт голосового)

**На последующих итерациях (после tool call):**
- Тот же system prompt
- Накопленные messages текущего хода: user → assistant(tool_calls) → tool(result) → ...

**Принцип:** агент сам вызывает `get_calendar_events` или `get_pending_tasks` при необходимости. Контекст не передаётся заранее — только по запросу через tool calls.

---

## Context trimming

После обрезки prior_messages: `(prev_msgs + current_msgs)[-30:]`

Обрезаем начало до первого `user` сообщения — чтобы избежать ситуации, когда срез начинается с `tool` сообщения, иначе будет ошибка.

---

## Conversation History

- Хранится в таблице `conversation_messages` (отдельная от LangGraph checkpoint)
- Используется для класификации диалогов и восстановления `prior_messages`
- Сессия считается новой если: последнее сообщение > 30 мин назад (новый `conversation_id`)

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
