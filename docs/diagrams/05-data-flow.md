# Диаграмма 5 — Data Flow Diagram

## Цель

Показывает **как данные проходят через систему**: что является входом, как трансформируется,
что сохраняется, что логируется и какие данные возвращаются пользователю.

## Ключевые трансформации данных

| Вход | Трансформация | Выход |
|---|---|---|
| Аудиофайл (.ogg) | Whisper ASR | Текстовый транскрипт |
| Текст + prior_messages + REACT_TOOL_DEFINITIONS | Mistral (reasoner) | tool_call или final_answer |
| tool_call (READ_ONLY) | read_tool_executor | tool result → messages |
| tool_call (SIDE_EFFECT) | tool_router → HITL | inline keyboard ✅/❌ |
| confirmed=True + pending_tool_call | write_tool_executor | Запись в Google Calendar / Tasks API |
| Google push notification | Webhook Handler | Обновление calendar_events в БД |

## Диаграмма

```mermaid
flowchart LR
    subgraph IN ["Входные данные"]
        direction TB
        TEXT["Текстовое<br/>сообщение"]
        AUDIO["Аудиофайл<br/>(.ogg)"]
        WEBHOOK_IN["Google Calendar<br/>Webhook"]
    end

    subgraph PROC ["ReAct StateGraph (LangGraph)"]
        direction TB
        ASR["Whisper ASR<br/>Транскрипция"]
        REASON["reasoner<br/>Mistral + REACT_TOOL_DEFINITIONS<br/>→ tool_call / final_answer"]
        ROUTER["tool_router<br/>READ_ONLY / SIDE_EFFECT / ask_user"]
        READ_EX["read_tool_executor<br/>get_calendar_events<br/>find_free_slots<br/>get_pending_tasks<br/>get_conversation_history"]
        HITL["hitl_wait<br/>inline keyboard ✅/❌"]
        WRITE_EX["write_tool_executor<br/>create_event / create_task<br/>move_event / complete_task"]
        RESPOND["respond<br/>→ Telegram"]
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
        LF["Langfuse<br/>generations / spans<br/>tool_calls, latency"]
    end

    TEXT --> REASON
    AUDIO --> ASR
    ASR -- "транскрипт" --> REASON

    WEBHOOK_IN --> WHHANDLER
    WHHANDLER -- "upsert события" --> PG_EV

    REASON -- "tool_call" --> ROUTER
    REASON -- "final_answer" --> RESPOND
    ROUTER -- "READ_ONLY" --> READ_EX
    READ_EX -- "tool result → messages" --> REASON
    ROUTER -- "SIDE_EFFECT" --> HITL
    HITL -- "✅ confirmed" --> WRITE_EX
    HITL -- "❌ cancel" --> RESPOND
    WRITE_EX --> RESPOND

    READ_EX -- "get_events / get_tasks" --> PG_EV
    READ_EX -- "get_tasks" --> PG_TASK
    PG_EV -. "fallback" .-> GCAL_EV

    WRITE_EX -- "create / move event" --> GCAL_EV
    WRITE_EX -- "create / complete task" --> GCAL_TASK
    WRITE_EX -- "write-through" --> PG_EV
    WRITE_EX -- "write-through" --> PG_TASK

    REASON -. "checkpoint" .-> PG_STATE
    HITL -. "checkpoint" .-> PG_STATE

    REASON -. "generation" .-> LF
    READ_EX -. "span" .-> LF
    WRITE_EX -. "span" .-> LF
```
