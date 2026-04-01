# Диаграмма 3 — C4 Component

## Цель

Раскрывает внутреннее устройство **Agent Core** (LangGraph-граф).
Показывает узлы графа, переходы между ними и точки ветвления.

## Обязательные элементы

| Компонент | Описание |
|---|---|
| Entry Router | Определяет тип триггера: user message / cron |
| Intent Extractor | LLM-вызов: текст → TaskIntent (JSON + confidence) |
| Confidence Gate | Ветвление: confidence >= 0.6 → continue, иначе → ask_user |
| Resource Type Gate | Ветвление: пользователь имеет в виду событие (Event) или задачу (Task)? |
| Slot Analyzer | Читает calendar_events из локальной БД; ищет свободные окна |
| Conflict Resolver | При конфликте — генерирует Top-3 альтернативных слота |
| Action Decider | Выбирает action: create_event / create_task / move_event / complete_task / suggest / ask_user |
| Action Executor | Вызывает Tool Layer; применяет Pydantic-валидацию |
| HITL Gate | Ветвление: нужно ли подтверждение пользователя |
| Confirmation Waiter | Ждёт ответа (≤ 5 мин); при timeout → stale |
| State Writer | Обновляет локальную БД; пишет checkpoint в PostgreSQL |
| Proactive Checker | Cron-путь: читает overdue tasks из локальной calendar_tasks |

## Ключевые связи

- Entry Router → Intent Extractor (user path) или Proactive Checker (cron path)
- Intent Extractor → Confidence Gate → Resource Type Gate
- Resource Type Gate → Slot Analyzer (для Event) или Action Decider напрямую (для Task)
- Slot Analyzer → Conflict Resolver (конфликт) или Action Decider (слот свободен)
- Conflict Resolver → Action Decider
- Action Decider → HITL Gate
- HITL Gate → Confirmation Waiter (деструктивное / move) или Action Executor (безопасное)
- Confirmation Waiter → Action Executor (OK) или END (отказ / timeout)
- Action Executor → State Writer → END

## Диаграмма

```mermaid
flowchart TD
    subgraph ENTRY ["Точка входа"]
        ER["Entry Router<br/>Определяет тип триггера"]
    end

    subgraph USER_PATH ["User-triggered путь"]
        IE["Intent Extractor<br/>LLM → TaskIntent JSON"]
        CG{"Confidence Gate<br/>confidence >= 0.6?"}
        AU["ask_user<br/>Запросить уточнение"]
        RT{"Resource Type Gate<br/>Event или Task?"}
    end

    subgraph CALENDAR_PATH ["Анализ событий (Event)"]
        SA["Slot Analyzer<br/>Читает calendar_events из БД"]
        CR["Conflict Resolver<br/>Генерация Top-3 альтернатив"]
    end

    subgraph DECISION ["Принятие решения"]
        AD["Action Decider<br/>create_event / create_task<br/>move_event / complete_task / suggest"]
    end

    subgraph EXECUTION ["Исполнение"]
        HG{"HITL Gate<br/>Нужно подтверждение?"}
        CW["Confirmation Waiter<br/>Ожидание ≤ 5 мин"]
        AE["Action Executor<br/>Tool Layer + Pydantic"]
        SW["State Writer<br/>Обновить БД + checkpoint"]
    end

    subgraph CRON_PATH ["Cron-triggered путь"]
        PC["Proactive Checker<br/>Читает overdue tasks<br/>из calendar_tasks (БД)"]
    end

    END(["END"])

    ER -- "user message" --> IE
    ER -- "cron trigger" --> PC
    PC -- "найдены overdue задачи" --> AD
    PC -- "нет overdue задач" --> END

    IE --> CG
    CG -- "нет (< 0.6)" --> AU
    AU --> END
    CG -- "да" --> RT

    RT -- "Event (встреча, звонок)" --> SA
    RT -- "Task (to-do)" --> AD

    SA -- "слот свободен" --> AD
    SA -- "конфликт" --> CR
    CR --> AD

    AD --> HG
    HG -- "безопасное (create)" --> AE
    HG -- "move или массово (>3)" --> CW

    CW -- "пользователь подтвердил" --> AE
    CW -- "отказ / timeout" --> END

    AE --> SW
    SW --> END
```
