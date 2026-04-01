# Диаграмма 4 — Workflow / Graph Diagram

## Цель

Показывает **пошаговое выполнение запроса** от входящего сообщения до финального результата,
включая все ветки ошибок, fallback-пути и проактивный flow.

## Обязательные элементы

- Два точки входа: пользовательский запрос и cron-триггер
- Ветка транскрипции (только для голосовых сообщений)
- Ветвление по confidence (< 0.6 → уточнение)
- Ветвление по типу ресурса: Event (анализ слотов) vs Task (прямое создание)
- HITL-ветка для деструктивных и массовых действий
- Обработка timeout подтверждения
- Cron-путь: читает overdue tasks из локальной БД → предложение переноса
- Webhook-путь: входящее уведомление от Google → обновление локальной БД
- Failure-пути: LLM API error, Google OAuth error, невалидный ввод

## Диаграмма

```mermaid
flowchart TD
    START_USER(["Входящее сообщение<br/>от пользователя"])
    START_CRON(["Cron-триггер<br/>каждый час"])
    START_WEBHOOK(["Google Webhook<br/>push notification"])

    VALIDATE{"Валидация входа<br/>размер / формат"}
    REJECT["Отклонить запрос<br/>Сообщить пользователю"]

    AUDIO{"Голосовое<br/>сообщение?"}
    WHISPER["Whisper ASR<br/>Транскрипция .ogg → текст"]
    WHISPER_ERR["Ошибка транскрипции<br/>Попросить повторить"]

    EXTRACT["Intent Extractor<br/>LLM → TaskIntent JSON"]
    LLM_ERR["LLM API Error<br/>Retry x3 с backoff<br/>Затем: уведомить пользователя"]

    CONF{"confidence >= 0.6?"}
    ASK["Запросить уточнение<br/>у пользователя"]

    RESTYPE{"Тип ресурса?"}

    CHECK_OVERDUE["Proactive Checker<br/>Читает overdue tasks<br/>из calendar_tasks (БД)"]
    NO_OVERDUE(["Нет просроченных<br/>задач — END"])

    SLOTS["Slot Analyzer<br/>Читает calendar_events из БД"]
    GCAL_ERR{"Google OAuth<br/>ошибка?"}
    REAUTH["Отправить ссылку<br/>переавторизации"]

    CONFLICT{"Конфликт<br/>в слоте?"}
    RESOLVE["Conflict Resolver<br/>Top-3 альтернативы"]

    DECIDE["Action Decider<br/>create_event / create_task<br/>move_event / complete_task / suggest"]

    HITL{"Нужно<br/>подтверждение?"}
    CONFIRM["Отправить предложение<br/>с кнопками OK / Cancel"]
    WAIT{"Ответ<br/>в течение 5 мин?"}
    STALE(["Сессия stale — END<br/>Пользователь может повторить"])
    CANCEL(["Действие отменено — END"])

    EXECUTE["Action Executor<br/>Tool Layer + Pydantic<br/>(Events API или Tasks API)"]
    SAVE["State Writer<br/>Обновить БД + checkpoint"]
    NOTIFY["Уведомить пользователя<br/>о результате"]

    WEBHOOK_UPDATE["Webhook Handler<br/>Обновить calendar_events<br/>или calendar_tasks в БД"]
    END(["END"])

    START_WEBHOOK --> WEBHOOK_UPDATE
    WEBHOOK_UPDATE --> END

    START_USER --> VALIDATE
    VALIDATE -- "невалидный ввод" --> REJECT
    REJECT --> END
    VALIDATE -- "OK" --> AUDIO

    AUDIO -- "да" --> WHISPER
    WHISPER -- "ошибка" --> WHISPER_ERR
    WHISPER_ERR --> END
    WHISPER -- "транскрипт" --> EXTRACT
    AUDIO -- "нет (текст)" --> EXTRACT

    EXTRACT -- "ошибка API" --> LLM_ERR
    LLM_ERR --> END
    EXTRACT -- "TaskIntent" --> CONF

    CONF -- "нет (< 0.6)" --> ASK
    ASK --> END
    CONF -- "да" --> RESTYPE

    RESTYPE -- "Task (to-do)" --> DECIDE
    RESTYPE -- "Event (встреча, звонок)" --> SLOTS

    START_CRON --> CHECK_OVERDUE
    CHECK_OVERDUE -- "нет задач" --> NO_OVERDUE
    CHECK_OVERDUE -- "есть overdue" --> DECIDE

    SLOTS --> GCAL_ERR
    GCAL_ERR -- "да" --> REAUTH
    REAUTH --> END
    GCAL_ERR -- "нет" --> CONFLICT

    CONFLICT -- "есть" --> RESOLVE
    RESOLVE --> DECIDE
    CONFLICT -- "нет" --> DECIDE

    DECIDE --> HITL
    HITL -- "нет (безопасное создание)" --> EXECUTE
    HITL -- "да (move / массово > 3)" --> CONFIRM

    CONFIRM --> WAIT
    WAIT -- "timeout" --> STALE
    WAIT -- "Cancel" --> CANCEL
    WAIT -- "OK" --> EXECUTE

    EXECUTE --> SAVE
    SAVE --> NOTIFY
    NOTIFY --> END
```
