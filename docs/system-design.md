# System Design — Chronos Agent (PoC)

## Обзор системы

Chronos Agent — проактивный AI-планировщик, который принимает голосовые и текстовые запросы через Telegram, извлекает структурированную информацию о задачах и событиях и автономно управляет Google Calendar. В отличие от пассивных инструментов, агент сам инициирует переносы, отслеживает незавершённые задачи и разрешает конфликты расписания.

Система построена на **LangGraph** (управление состоянием + ReAct-цикл). Каждое входное событие запускает итерацию: observe → reason → act → observe. Цикл завершается, когда задача запланирована или пользователю отправлено предложение.

---

## Содержание

- [Ключевые архитектурные решения](#ключевые-архитектурные-решения)
- [Модули и их ответственность](#модули-и-их-ответственность)
- [Workflow выполнения задачи](#workflow-выполнения-задачи)
- [State / Memory / Context](#state--memory--context)
- [Обмен данными между компонентами](#обмен-данными-между-компонентами)
- [Tool / API интеграции](#tool--api-интеграции)
- [Failure Modes, Fallback, Guardrails](#failure-modes-fallback-guardrails)
- [Context Architecture](#context-architecture)
- [Sync Strategy](#sync-strategy)
- [State Management](#state-management)
- [Trade-offs (PoC)](#trade-offs-poc)
- [Ограничения (SLO)](#ограничения-slo)

---

## Ключевые архитектурные решения

| Решение | Обоснование |
|---|---|
| LangGraph как orchestrator | Явный граф состояний вместо монолитного промпта; поддержка персистентного стейта через checkpointer (сохранение состояния агента и контекста задач для продолжения работы после перезапуска или сбоя) |
| Structured Output (Pydantic) | LLM выдаёт строго типизированный JSON |
| Локальный Whisper | Нет платы за ASR; данные не покидают сервер |
| Human-in-the-loop для деструктивных действий | Удаление / перенос события > 2 ч требует явного `✅` в Telegram от пользователя |
| Langfuse для observability | Логирование `reason_text`, `confidence`, `agent_version` для анализа решений агента |

---

## Модули и их ответственность

```mermaid
flowchart LR
    %% Направление слева направо
    User(["<b>Пользователь</b>"])

    subgraph SYSTEM ["<b>Chronos Agent</b>"]
        direction TB
        ModuleA["<b>Module A</b><br/>Perception<br/><b>Telegram Bot</b> / <b>Whisper ASR</b>"]
        ModuleB["<b>Module B</b><br/>Reasoning (LangGraph/ReAct)<br/><b>Intent Extract</b> / <b>Slot Analysis</b> / <b>Conflict Resolve</b>"]
        ModuleC["<b>Module C</b><br/>Action (Calendar API)<br/><b>create_event</b> / <b>move_event</b> / <b>get_events</b>"]
        ModuleD["<b>Module D</b><br/>Memory<br/><b>PostgreSQL</b> / <b>LangGraph state</b> / <b>checkpointer</b>"]
    end

    %% Внешний пользовательский ввод
    User -- "сообщение (текст / голос)" --> ModuleA

    %% Потоки данных между модулями Chronos Agent
    ModuleA -- "Perception и Events" --> ModuleB
    ModuleB -- "команды и действия" --> ModuleC
    ModuleB -- "чтение / запись состояния" --> ModuleD
    ModuleD -- "состояние / контекст" --> ModuleB
```

| Модуль | Компоненты | Ответственность |
|---|---|---|
| **A — Perception** | Telegram Bot, Whisper (local) | Приём текста/аудио; транскрипция аудиофайла → текст; нормализация входа |
| **B — Reasoning** | LLM (Mistral via OpenRouter), LangGraph | ReAct-цикл; извлечение структурированного Intent (JSON); анализ слотов; разрешение конфликтов |
| **C — Action** | Google Calendar API, Telegram Bot | Выполнение calendar-операций; отправка подтверждений и предложений пользователю |
| **D — Memory** | PostgreSQL (стандартный checkpointer) | Персистентный стейт LangGraph; `calendar_events` + `calendar_tasks` (локальная копия); OAuth-токены (зашифрованы) |

---

## Workflow выполнения задачи

### Триггеры запуска агента

```
User message ──────────────────────────────┐
Calendar conflict ──────────────────────── ▶ LangGraph Entry
Cron (hourly check) ───────────────────────┘
```

### Основной flow (happy path)

```
[Telegram] text / voice
       │
       ▼
[Whisper] (если аудио) → транскрипт
       │
       ▼
[LLM] Intent Extraction
  → TaskIntent {title, datetime, duration, deadline, priority, confidence}
       │
       ├─ confidence < 0.6 → спросить уточнение → STOP
       │
       ▼
Resource Type Gate
  ├─ Task (to-do) → create_task → уведомление → STOP
  └─ Event (встреча, звонок)
       ▼
[Slot Analyzer] читает calendar_events из локальной БД или запрашивает данные через Google API (при необходимости)
  → window: ±24h от запрошенного слота
       │
       ├─ слот свободен → create_event → уведомление → STOP
       │
       └─ конфликт → генерация Top-3 альтернатив (с score) → предложение
                          │
                          ├─ ✅ пользователь → create_event → STOP
                          └─ ❌ / timeout 5 мин → session stale → STOP
```

### Проактивный flow (hourly cron)

```
[Cron] каждый час
  → get_overdue_tasks() из calendar_tasks (локальная БД)
    (status = needsAction И due_date < now())
  → для каждой просроченной задачи:
       найти свободный слот (next 24h) в calendar_events
       → предложить перенос/выполнение пользователю в Telegram
```

### Webhook flow (фоновый, Google → агент)

```
[Google Calendar Events API / Tasks API]
  → push notification → POST /webhook/google
  → Webhook Handler обновляет calendar_events или calendar_tasks в локальной БД
  → (агент не запускается; только синхронизация данных)
```

---

## State / Memory / Context

| Слой | Хранилище | Содержимое | TTL |
|---|---|---|---|
| **Session state** | LangGraph (in-memory) | Текущий шаг графа, pending confirmation | до конца сессии |
| **Persistent state** | PostgreSQL (checkpointer) | Стейт LangGraph между запусками | 90 дней |
| **calendar_events** | PostgreSQL | Локальная копия событий Google Calendar Events | 90 дней |
| **calendar_tasks** | PostgreSQL | Локальная копия задач Google Tasks (status: needsAction/completed) | 90 дней |
| **OAuth tokens** | PostgreSQL (encrypted) | Google refresh token | до отзыва |
| **service_logs** | PostgreSQL | ERROR/CRITICAL app-события (поиск без доступа к файлам хоста) | 90 дней |
| **Audio files** | Локальный диск | Входящие аудиофайлы | 14 дней |
| **App logs (full)** | Docker json-file (диск хоста) | Все уровни, JSON, ротация 5 × 100 МБ | ~500 МБ rolling |
Контекст агента на каждом шаге содержит: `user_id`, `timezone`, `current_tasks[]`, `calendar_window`, `conversation_history` (последние 5 сообщений).

---

## Обмен данными между компонентами

### TaskIntent (A → B)

```python
class TaskIntent(BaseModel):
    title: str
    scheduled_at: Optional[datetime]      # None если не указано
    duration_minutes: int
    deadline: Optional[datetime]
    priority: Literal["low", "medium", "high"]
    confidence: float                     # 0.0 – 1.0
    raw_text: str
```

### CalendarSlot (B → C)

```python
class CalendarSlot(BaseModel):
    start: datetime
    end: datetime
    score: float          # релевантность слота
    conflict_reason: Optional[str]
```

### AgentAction (B → C)

```python
class AgentAction(BaseModel):
    action: Literal["create_event", "create_task", "move_event", "complete_task",
                    "suggest", "ask_user", "noop"]
    event_id: Optional[str]     # для move_event
    task_id: Optional[str]      # для complete_task
    slot: Optional[CalendarSlot]
    message_to_user: str
    reason_text: str            # логируется в Langfuse
    agent_version: str
    model_id: str
```

---

## Tool / API интеграции

| Инструмент | Операции | Ограничения |
|---|---|---|
| **Google Calendar Events API** | `get_events`, `create_event`, `move_event` | 100 req/min; только primary-календарь |
| **Google Tasks API** | `get_tasks`, `create_task`, `complete_task` | 50 req/s (квота Google Tasks) |
| **Google Calendar Webhook** | push notification → `POST /webhook/google` | TTL канала до 7 дней; продление раз в 6 дней |
| **Telegram Bot API** | `send_message`, `send_inline_keyboard` | — |
| **Whisper (local)** | `transcribe(audio)` | Файлы до 20 МБ; поддерживает `.ogg` |
| **Langfuse** | `log_trace`, `log_span` | Только observability, не влияет на flow |

`delete_event` / `delete_task` — намеренно не реализованы в PoC. Доступны только через прямой Google Calendar/Tasks UI.

---

## Failure Modes, Fallback, Guardrails

| Сценарий | Детекция | Поведение |
|---|---|---|
| **LLM confidence < 0.6** | Поле `confidence` в TaskIntent | Агент спрашивает уточнение; не создаёт событий |
| **Галлюцинация даты** | Pydantic datetime validation + sanity check (дата не в прошлом на > 1 год) | Отклонить, запросить уточнение |
| **Конфликт в календаре** | Сравнение запрошенного слота с `get_events` | Предложить Top-3 альтернативы с объяснением |
| **LLM API 429 / 5xx** | HTTP status code | Exponential backoff (1 s, 2 s, 4 s); после 3 попыток → сообщение пользователю |
| **Google OAuth expired** | 401 от Calendar API | Отправить ссылку переавторизации; продолжить после auth |
| **Деструктивное действие** | `action in ["delete", "move"]` + duration > 2h | Обязательное подтверждение `✅ / ❌` в Telegram |
| **Массовые изменения** | > 3 событий за сессию | Требует явного подтверждения всего пакета |
| **Prompt injection** | Pydantic restricted schema | Игнорировать; логировать; не выполнять |
| **Таймаут подтверждения** | > 5 мин без ответа | Сессия → stale; пользователь может повторить запрос |
| **Невалидный ввод** | Текст > 4000 символов / аудио > 20 МБ | Отклонить с сообщением об ошибке |

---

## Context Architecture

Агент разделяет входные данные на два класса: **eager** (загружается всегда при старте итерации) и **lazy** (запрашивается только если нужно).

**Eager-контекст** — минимальный набор, без которого нельзя начать reasoning:
- `user_id`, `timezone`
- Текущий запрос пользователя (текст или транскрипт)
- Последние 5 сообщений диалога (sliding window)
- Список задач в статусе `pending` / `overdue` (из PostgreSQL)

**Lazy-контекст** — запрашивается только при необходимости:
- `calendar_events` из локальной БД — только при переходе на Slot Analyzer (только для Event-запросов)
- Детали конкретного события — только при разрешении конфликта

**Принцип:** агент не загружает весь календарь при старте. Слоты читаются из локальной БД или через Google API; окно ограничено ± 24 ч. Для Task-запросов Slot Analyzer не вызывается вовсе.

---

## Sync Strategy

События и задачи Google Calendar **хранятся локально в PostgreSQL** и синхронизируются с Google через два канала: push webhook и write-through при прямом создании агентом. При reasoning агент читает данные из локальной БД, а не из Google API напрямую. Это снижает число внешних вызовов и позволяет cron работать без API-запросов к Google.

| Источник | Стратегия | Частота | Компромисс PoC |
|---|---|---|---|
| **Google Calendar Events** | Push webhook → локальная БД | При каждом изменении в Google | Небольшая задержка webhook; зато нет лишних API-вызовов при чтении |
| **Google Tasks** | Push webhook → локальная БД | При каждом изменении в Google | Аналогично Events |
| **Прямое создание агентом** | Write-through | Сразу при создании события/задачи | Запись и в Google API, и в локальную БД одновременно |
| **Cron-триггер** | Pull из локальной БД | Раз в час | Максимальная задержка реакции на просроченную задачу — 1 час |
| **Fallback при пустом результате** | Прямой Google API-запрос | При промахе локальной БД | Актуализирует БД; используется при первом запуске или сбое webhook |

Webhook-канал Google имеет TTL до 7 дней и обновляется APScheduler раз в 6 дней. При сбое обновления агент переходит в деградированный режим: читает из Google API напрямую через fallback, пока канал не восстановится.

---

## State Management

LangGraph хранит стейт в **PostgreSQL через стандартный `langgraph-checkpoint-postgres`**. Кастомной схемы нет — используется схема из коробки.

**Что хранится в стейте:**
- Текущий шаг графа (node)
- `TaskIntent` и `AgentAction` текущей сессии
- Флаг ожидания подтверждения (`awaiting_confirmation`)
- `thread_id` — изолирует сессии разных пользователей

**Что НЕ хранится в стейте (лежит отдельно в БД):**
- История задач пользователя (отдельная таблица)
- OAuth-токены (отдельно, зашифрованы)

**Жизненный цикл стейта:**
- Сессия стартует при получении сообщения или cron-триггере
- Ожидание подтверждения: стейт сохраняется; агент "спит" до ответа пользователя
- Таймаут 5 мин без ответа → сессия помечается `stale`; стейт сохраняется для аудита, но не возобновляется автоматически

---


## Trade-offs (PoC)

| Компромисс | Что выбрано | Что жертвуем | Почему допустимо для PoC |
|---|---|---|---|
| **Актуальность vs сложность** | Локальная БД + webhook sync | Данные могут отставать от Google на время доставки webhook (секунды) | Задержка незначима для PoC; fallback на прямой API при пустой БД |
| **Cost vs качество LLM** | Mistral (OpenRouter) | Потенциально ниже качество extraction vs GPT-4 | Достаточно для > 80% NLU Success Rate |
| **Complexity vs гибкость** | Стандартный LangGraph checkpointer | Нет тонкой настройки retention / TTL | Упрощает запуск; кастомизация — после PoC |
| **Memory vs простота** | Только PostgreSQL, без VectorDB | Нет долгосрочных паттернов предпочтений | Паттерны поведения не нужны для базовых сценариев PoC |
| **Autonomy vs safety** | HITL для деструктивных действий | Дополнительный round-trip с пользователем | Для PoC важнее доверие, чем скорость |

---

## Ограничения (SLO)

| Параметр | Целевое значение |
|---|---|
| **End-to-end latency p95** | < 7 сек (от запроса до ответа бота) |
| **NLU Success Rate** | > 80% корректно извлечённых дата/время/тип |
| **Hallucination Rate** | < 10% некорректных/лишних действий |
| **Binary Acceptance** | > 60% предложений подтверждается пользователем |
| **Google Calendar API** | ≤ 100 req/min |
| **LLM бюджет** | $10–20 на период PoC |
| **Аудио retention** | 14 дней |
| **Метаданные задач** | 90 дней (по согласию) |
