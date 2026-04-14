# Диаграмма 4 — Workflow / Graph Diagram

## Цель

Показывает **пошаговое выполнение запроса** от входящего сообщения до финального результата,
включая все ветки ошибок, fallback-пути и проактивный flow.

## Обязательные элементы

- Два точки входа: пользовательский запрос и cron-триггер
- Ветка транскрипции (только для голосовых сообщений)
- Предварительная фильтрация (rate limit, onboarding gate, off-topic regex)
- ReAct loop: reasoner → tool_router → [read executor | hitl_wait | respond]
- HITL-ветка для всех side-effect операций (create, move, complete)
- Проактивный cron-путь: читает overdue tasks → Telegram reminder
- Webhook-путь: входящее уведомление от Google → обновление локальной БД
- Failure-пути: LLM API error, Google OAuth error, невалидный ввод

## Диаграмма

```mermaid
flowchart TD
    START_USER(["Входящее сообщение<br/>от пользователя"])
    START_CRON(["Cron-триггер<br/>раз в час"])
    START_WEBHOOK(["Google Webhook<br/>push notification"])

    VALIDATE{"Валидация входа<br/>rate limit / размер"}
    REJECT["Отклонить запрос<br/>Сообщить пользователю"]

    ONBOARD{"Онбординг<br/>завершён?"}
    ONBOARD_MSG["Ответить: сначала<br/>завершим настройку"]

    AUDIO{"Голосовое<br/>сообщение?"}
    WHISPER["Whisper ASR<br/>Транскрипция .ogg → текст"]
    WHISPER_ERR["Ошибка ASR<br/>Попросить прислать текстом"]

    REACT_START["AgentCore.run()<br/>Starting ReAct graph"]

    REASONER["🧠 reasoner_node<br/>Mistral + REACT_TOOL_DEFINITIONS<br/>reasoning → tool_call или final_answer"]

    TOOL_ROUTER{"tool_router_node<br/>Какой тип tool?"}

    READ_EXEC["📖 read_tool_executor_node<br/>get_calendar_events<br/>find_free_slots<br/>get_pending_tasks<br/>get_conversation_history<br/>→ result добавляется в messages"]

    ASK_USER["💬 respond_node<br/>ask_user → Telegram<br/>(уточнение / не в теме)"]

    HITL_SEND["Отправить inline keyboard ✅ / ❌<br/>Через tool_router перед pause"]

    HITL_WAIT["⏸ hitl_wait_node<br/>GRAPH PAUSES<br/>Checkpoint сохранён в PostgreSQL<br/>Ожидание callback_query"]

    CONFIRMED{"confirmed?"}

    WRITE_EXEC["✏️ write_tool_executor_node<br/>create_event / create_task<br/>move_event / complete_task<br/>Google Calendar / Tasks API<br/>Fixed answer (LLM не вызывается)"]

    RESPOND["💬 respond_node<br/>md_to_html → notify_user → Telegram"]

    LLM_ERR["LLM Error<br/>Retry x3 → уведомить пользователя"]

    CHECK_OVERDUE["Proactive Checker<br/>get overdue tasks<br/>из calendar_tasks (БД)"]
    NO_OVERDUE(["Нет просроченных<br/>задач — END"])
    CRON_NOTIFY["Отправить reminder<br/>в Telegram"]

    WEBHOOK_UPDATE["Webhook Handler<br/>Обновить calendar_events<br/>или calendar_tasks в БД<br/>(ON CONFLICT DO UPDATE)"]

    END(["END"])

    START_WEBHOOK --> WEBHOOK_UPDATE
    WEBHOOK_UPDATE --> END

    START_USER --> VALIDATE
    VALIDATE -- "rate limit / невалидный" --> REJECT
    REJECT --> END

    VALIDATE -- "OK" --> ONBOARD
    ONBOARD -- "нет (pending_oauth / pending_timezone)" --> ONBOARD_MSG
    ONBOARD_MSG --> END

    ONBOARD -- "да (active)" --> AUDIO
    AUDIO -- "да" --> WHISPER
    WHISPER -- "ошибка" --> WHISPER_ERR
    WHISPER_ERR --> END
    WHISPER -- "транскрипт" --> REACT_START
    AUDIO -- "нет (текст)" --> REACT_START

    REACT_START --> REASONER

    REASONER -- "ошибка LLM" --> LLM_ERR
    LLM_ERR --> END
    REASONER -- "final_answer" --> RESPOND
    REASONER -- "tool_call" --> TOOL_ROUTER

    TOOL_ROUTER -- "READ_ONLY" --> READ_EXEC
    READ_EXEC -->|"loop"| REASONER

    TOOL_ROUTER -- "ask_user" --> ASK_USER
    ASK_USER --> END

    TOOL_ROUTER -- "SIDE_EFFECT" --> HITL_SEND
    HITL_SEND --> HITL_WAIT

    HITL_WAIT --> CONFIRMED
    CONFIRMED -- "True ✅" --> WRITE_EXEC
    CONFIRMED -- "False ❌" --> RESPOND

    WRITE_EXEC --> RESPOND
    RESPOND --> END

    START_CRON --> CHECK_OVERDUE
    CHECK_OVERDUE -- "нет задач" --> NO_OVERDUE
    CHECK_OVERDUE -- "есть overdue" --> CRON_NOTIFY
    CRON_NOTIFY --> END
```
