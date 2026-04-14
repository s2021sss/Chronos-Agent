# Диаграмма 3 — C4 Component

## Цель

Раскрывает внутреннее устройство **Agent Core** (LangGraph ReAct-граф).
Показывает узлы графа, переходы между ними и точки ветвления.

## Узлы графа

| Узел | Описание |
|---|---|
| `reasoner` | LLM-вызов: system prompt + messages + REACT_TOOL_DEFINITIONS → tool_call или final_answer |
| `tool_router` | Маршрутизирует tool_call: READ_ONLY → executor, SIDE_EFFECT → HITL, TERMINAL → respond |
| `read_tool_executor` | Выполняет read-only tool (get_events, find_free_slots и др.) → добавляет tool result → reasoner |
| `hitl_wait` | Graph pauses (interrupt_before); ждёт callback кнопок ✅/❌; resume через AgentCore.resume() |
| `write_tool_executor` | После confirmed=True: выполняет side-effect tool (Google API); устанавливает fixed final_answer |
| `respond` | Конвертирует final_answer → Telegram HTML → notify_user → END |

## Ключевые связи

- `reasoner` → (tool_call) → `tool_router`
- `reasoner` → (final_answer) → `respond`
- `tool_router` → READ_ONLY tool → `read_tool_executor` → `reasoner` (loop)
- `tool_router` → SIDE_EFFECT tool → `hitl_wait` (interrupt)
- `tool_router` → `ask_user` → `respond`
- `hitl_wait` → confirmed=True → `write_tool_executor` → `respond`
- `hitl_wait` → confirmed=False → `respond`

## Диаграмма

```mermaid
flowchart TD
    subgraph ENTRY ["Точка входа"]
        ST(["START<br/>user_id, raw_input<br/>trigger, user_timezone"])
    end

    subgraph REACT ["LangGraph ReAct StateGraph"]
        RN["🧠 reasoner<br/>─────────────────<br/>System prompt + prior_messages + messages<br/>Mistral: reasoning → tool_call или final_answer<br/><br/>итерации ≤ max_tool_calls_per_iteration"]

        TR["🔀 tool_router<br/>─────────────────<br/>READ_ONLY → read_tool_executor<br/>SIDE_EFFECT → [interrupt] hitl_wait<br/>ask_user → respond"]

        RTE["📖 read_tool_executor<br/>─────────────────<br/>get_calendar_events<br/>find_free_slots<br/>get_pending_tasks<br/>get_conversation_history<br/><br/>result → messages → reasoner"]

        HW["⏸ hitl_wait<br/>─────────────────<br/>GRAPH PAUSES<br/>Checkpoint → PostgreSQL<br/>Отправляет inline keyboard ✅ / ❌<br/>Ждёт AgentCore.resume()"]

        WTE["✏️ write_tool_executor<br/>─────────────────<br/>create_event / create_task<br/>move_event / complete_task<br/><br/>Google Calendar / Tasks API<br/>Fixed final_answer (LLM не вызывается)"]

        RESP["💬 respond<br/>─────────────────<br/>md_to_html(final_answer)<br/>notify_user → Telegram<br/>add_conversation_message"]
    end

    END(["END"])

    ST --> RN

    RN -->|"final_answer"| RESP
    RN -->|"pending_tool_call"| TR

    TR -->|"READ_ONLY"| RTE
    TR -->|"SIDE_EFFECT"| HW
    TR -->|"ask_user"| RESP

    RTE -->|"tool result → messages"| RN

    HW -->|"confirmed = True"| WTE
    HW -->|"confirmed = False"| RESP

    WTE --> RESP
    RESP --> END
```
