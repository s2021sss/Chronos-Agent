# Диаграмма 5 — Data Flow Diagram

## Цель

Показывает **как данные проходят через систему**: что является входом, как трансформируется,
что сохраняется, что логируется и какие данные возвращаются пользователю.

## Обязательные элементы

- Входные данные: текст / аудио от пользователя
- Трансформации: транскрипция, извлечение намерений, анализ слотов
- Хранилища: PostgreSQL (стейт + calendar_events + calendar_tasks + токены), Google APIs
- Webhook: входящий поток от Google для синхронизации локальной БД
- Логирование: Langfuse (трейсы, reason_text, confidence)
- Выходные данные: ответ / предложение пользователю

## Ключевые трансформации данных

| Вход | Трансформация | Выход |
|---|---|---|
| Аудиофайл (.ogg) | Whisper ASR | Текстовый транскрипт |
| Текст пользователя | LLM Intent Extraction | TaskIntent (JSON) |
| TaskIntent (Event) + calendar_events (БД) | Slot Analysis | Список свободных слотов |
| Свободные слоты | Conflict Resolution | Top-3 альтернативы с score |
| AgentAction | Tool Execution | Запись в Events API или Tasks API |
| Google push notification | Webhook Handler | Обновление calendar_events / calendar_tasks в БД |

## Диаграмма

```mermaid
flowchart LR
    subgraph IN ["Входные данные"]
        direction TB
        TEXT["Текстовое<br/>сообщение"]
        AUDIO["Аудиофайл<br/>(.ogg)"]
        WEBHOOK_IN["Google Webhook<br/>push notification"]
    end

    subgraph PROC ["Обработка"]
        direction TB
        ASR["Whisper ASR<br/>Транскрипция"]
        EXTRACT["LLM<br/>Intent Extraction<br/>→ TaskIntent JSON"]
        SLOTS["Slot Analyzer<br/>→ свободные окна<br/>(читает calendar_events)"]
        RESOLVE["Conflict Resolver<br/>→ Top-3 альтернативы"]
        DECIDE["Action Decider<br/>→ AgentAction"]
        EXEC["Tool Layer<br/>Pydantic-валидация"]
        WHHANDLER["Webhook Handler<br/>→ обновление локальной БД"]
    end

    subgraph STORE ["Хранилища (локальные)"]
        direction TB
        PG_STATE[("PostgreSQL<br/>LangGraph checkpointer<br/>OAuth-токены")]
        PG_EV[("PostgreSQL<br/>calendar_events")]
        PG_TASK[("PostgreSQL<br/>calendar_tasks")]
    end

    subgraph GOOGLE ["Google APIs"]
        direction TB
        GCAL_EV[("Google Calendar<br/>Events API")]
        GCAL_TASK[("Google Tasks API")]
    end

    subgraph OBS ["Observability"]
        LF["Langfuse<br/>трейсы<br/>reason_text<br/>confidence"]
    end

    subgraph OUT ["Выходные данные"]
        direction TB
        NOTIF["Уведомление<br/>пользователю"]
        SUGGEST["Предложение<br/>с кнопками OK / Cancel"]
    end

    TEXT --> EXTRACT
    AUDIO --> ASR
    ASR -- "транскрипт" --> EXTRACT

    WEBHOOK_IN --> WHHANDLER
    WHHANDLER -- "обновить событие" --> PG_EV
    WHHANDLER -- "обновить задачу" --> PG_TASK

    EXTRACT -- "TaskIntent (Event)" --> SLOTS
    EXTRACT -- "TaskIntent (Task)" --> DECIDE
    PG_EV -- "занятые слоты" --> SLOTS
    SLOTS -- "слоты + конфликты" --> RESOLVE
    RESOLVE -- "альтернативы" --> DECIDE

    PG_TASK -- "overdue задачи (cron)" --> DECIDE
    PG_STATE -- "стейт итерации" --> EXTRACT

    DECIDE -- "AgentAction" --> EXEC

    EXEC -- "create / move event" --> GCAL_EV
    GCAL_EV -- "gcal_event_id" --> EXEC
    EXEC -- "обновить запись" --> PG_EV

    EXEC -- "create / complete task" --> GCAL_TASK
    GCAL_TASK -- "gcal_task_id" --> EXEC
    EXEC -- "обновить запись" --> PG_TASK

    EXEC -- "сохранить checkpoint" --> PG_STATE

    EXTRACT -. "трейс" .-> LF
    DECIDE -. "reason_text<br/>confidence" .-> LF
    EXEC -. "результат действия" .-> LF

    EXEC -- "подтверждено автоматически" --> NOTIF
    DECIDE -- "требует подтверждения" --> SUGGEST
```
