# Spec: Agent / Orchestrator

## Цель

LangGraph ReAct-граф — ядро системы. LLM итеративно рассуждает и вызывает инструменты. Граф управляет порядком шагов, HITL-подтверждениями и fallback-поведением при ошибках.

---

## Онбординг пользователя

```
Пользователь: /start
  → Создать запись users(user_id, status=pending_oauth)
  → Отправить приветственное сообщение + ссылку Google OAuth

[OAuth callback получен]
  → Сохранить refresh_token (зашифровать Fernet)
  → Обновить users(status=pending_timezone)
  → Отправить: «Укажи свой часовой пояс командой /timezone Europe/Moscow»

[Timezone установлен]
  → Обновить users(timezone=..., status=active)
  → Зарегистрировать Google webhook-канал для пользователя
  → Отправить: «Готово! Можешь написать что нужно запланировать.»
```

**Состояния пользователя:** `pending_oauth` → `pending_timezone` → `active`

Если пользователь пишет что-то до завершения онбординга — отвечать: «Сначала завершим настройку».

---

## Команды бота

| Команда | Действие |
|---|---|
| `/start` | Начать onboarding или показать приветствие |
| `/cancel` | Отменить текущую сессию / ожидающее подтверждение |
| `/help` | Список команд и примеры запросов |
| `/status` | Активные задачи из Google Tasks |
| `/timezone <tz>` | Установить timezone (пример: `/timezone Europe/Moscow`) |

---

## Timezone

1. **Явная команда** `/timezone <tz_name>` — принимает IANA timezone
2. **Свободный текст при онбординге** — «Москва» → резолвится в `Europe/Moscow`
3. **Изменение после онбординга** — `/timezone` доступна в любое время

---

## Orphaned Session Recovery

**Проблема:** процесс агента упал в момент, когда `awaiting_confirmation = True`. При рестарте сессия зависает.

**Поведение при старте:**
```
При инициализации AgentCore:
  → Запросить из PostgreSQL все checkpoints где next_node = "hitl_wait"
  → Для каждого:
       Отправить пользователю: «Предыдущее действие не завершилось.»
       AgentCore.resume(confirmed=False) → граф завершается
```

---

## Триггеры запуска

| Триггер | Источник | Действие |
|---|---|---|
| `text_message` | Telegram webhook | Запуск ReAct flow |
| `voice_message` | Telegram webhook | Whisper ASR → Запуск ReAct flow |
| `user_confirmation` | Telegram callback (кнопка) | `AgentCore.resume(confirmed)` |
| `cron_check` | APScheduler, раз в час | Проактивный flow для просроченных задач |
| `webhook_renewal` | APScheduler, раз в 6 дней | Обновление Google webhook-каналов |
| `startup` | Инициализация сервиса | Orphaned session recovery |

---

## ReAct граф — узлы

| Узел | Описание |
|---|---|
| `reasoner` | LLM с system prompt + messages → tool_call или final_answer |
| `tool_router` | Маршрутизирует tool_call: read → executor, write → HITL, ask_user → respond |
| `read_tool_executor` | Выполняет read-only инструмент → добавляет tool result в messages → reasoner |
| `hitl_wait` | interrupt_before: граф паузируется, ждёт callback от пользователя |
| `write_tool_executor` | Выполняет side-effect инструмент после confirmed=True → фиксированный ответ |
| `respond` | Отправляет final_answer пользователю через notify_user |

### Классификация инструментов

**READ_ONLY** (выполняются автоматически, результат возвращается в reasoner):
- `get_calendar_events` — события за период из локальной БД
- `find_free_slots` — свободные окна со scoring (8:00–22:00)
- `get_pending_tasks` — активные задачи
- `get_conversation_history` — история диалога из checkpoint

**SIDE_EFFECT** (требуют HITL-подтверждения):
- `create_event` — создать событие в Google Calendar
- `create_task` — создать задачу в Google Tasks
- `move_event` — перенести событие (lookup по названию → перед HITL)
- `complete_task` — отметить задачу выполненной (lookup по названию → перед HITL)

**TERMINAL**:
- `ask_user` — задать уточняющий вопрос / сообщить о недоступном действии

---

## Правила переходов

```
START → reasoner

reasoner
  ├─ final_answer установлен   → respond → END
  └─ pending_tool_call          → tool_router

tool_router
  ├─ READ_ONLY tool             → read_tool_executor → reasoner (loop)
  ├─ SIDE_EFFECT tool           → [interrupt] hitl_wait
  └─ ask_user (TERMINAL)        → respond → END

hitl_wait (resume)
  ├─ confirmed=True             → write_tool_executor → respond → END
  └─ confirmed=False            → respond → END   (фиксированный шаблон отмены)
```

---

## Контекст диалога

- Агент получает `prior_messages` — последние 30 сообщений из текущего и предыдущего диалогов
- System prompt пересоздаётся каждый раз (содержит текущую дату и timezone пользователя)
- После обрезки контекста: срезаем до первого `user` сообщения (нельзя начинать с `tool`)
- Conversation classification: deterministic rules + LLM fallback → `get_or_create_conversation()`

---

## Проактивный flow (cron)

```
cron_check (раз в час):
  → Получить всех active-пользователей
  → Для каждого пользователя:
       get_pending_tasks(user_id) — ищем просроченные задачи
       Если есть → отправить напоминание в Telegram
  → Завершить итерацию
```

Последовательная обработка (не параллельная). Каждая итерация ограничена `AGENT_ITERATION_TIMEOUT_SECONDS`.

---

## Rate Limiting

- **Лимит:** `RATE_LIMIT_MSG_PER_MINUTE` сообщений в минуту на пользователя (default: 5)
- **Хранение:** in-memory скользящее окно
- **При превышении:** отклонить без вызова агента
- **Команды бота** (`/start`, `/cancel`, `/help`, `/status`) не входят в лимит

---

## Retry / Fallback политика

| Компонент | Retry | Fallback |
|---|---|---|
| LLM (reasoner) | x3, exponential backoff 1/2/4 s | Уведомить пользователя |
| Google Calendar API | x3, exponential backoff 1/2/4 s | Уведомить; re-auth при 401 |
| Whisper ASR | x1 | Попросить прислать текстом |
| Telegram sendMessage | x2 | Логировать, не падать |

---

## Защита от prompt injection

- Off-topic pre-filter: regex в `messages.py` блокирует до вызова LLM
- System prompt запрещает ответы вне области Calendar/Tasks
- HITL-подтверждение перед каждым side-effect инструментом исключает несанкционированные изменения
- Side-effect инструменты всегда требуют HITL-подтверждения

---

## Ограничения PoC

- Один активный диалог на пользователя
- Максимум `MAX_TOOL_CALLS_PER_ITERATION` инструмент-вызовов за итерацию (default: 5)
- `delete_event` / `delete_task` — не реализованы (делается вручную в Google Calendar)
