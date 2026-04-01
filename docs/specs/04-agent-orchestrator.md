# Spec: Agent / Orchestrator

## Цель

LangGraph-граф — ядро системы. Управляет порядком шагов, правилами переходов между узлами, stop conditions и fallback-поведением при ошибках.


## Триггеры запуска

Срабатывает при первом сообщении нового пользователя (нет записи в таблице `users`).

```
Пользователь: /start
  → Создать запись users(user_id, status=pending_oauth)
  → Отправить приветственное сообщение + ссылку Google OAuth
  → Ждать завершения OAuth (пользователь открывает ссылку)

[OAuth callback получен]
  → Сохранить refresh_token (зашифровать Fernet)
  → Обновить users(status=pending_timezone)
  → Отправить: «Укажи свой часовой пояс командой /timezone Europe/Moscow
    или напиши название города»

[Timezone установлен]
  → Обновить users(timezone=..., status=active)
  → Зарегистрировать Google webhook-канал для пользователя
  → Отправить: «Готово! Можешь написать что нужно запланировать.»

[Первый рабочий запрос]
  → Запуск основного flow агента
```

**Состояния пользователя:** `pending_oauth` → `pending_timezone` → `active`

Если пользователь пишет что-то до завершения onboarding — отвечать: «Сначала завершим настройку» и повторять нужный шаг.

---

## Команды бота

| Команда | Действие |
|---|---|
| `/start` | Начать onboarding или показать приветствие повторно |
| `/cancel` | Отменить текущую сессию; сбросить `awaiting_confirmation`; уведомить о сбросе |
| `/help` | Показать список доступных команд и примеры запросов |
| `/status` | Показать список pending-задач из `calendar_tasks` с due_date и приоритетом |
| `/timezone <tz>` | Установить или обновить timezone пользователя (пример: `/timezone Europe/Moscow`) |

**Обработка `/cancel`:**
- Если `awaiting_confirmation = True` → снять флаг, пометить сессию `stale`, сообщить: «Действие отменено»
- Если итерация активна → дождаться завершения текущего узла графа, затем не продолжать
- Если сессии нет → ответить: «Нет активных действий для отмены»

---

## Timezone: способ установки

1. **Явная команда** `/timezone <tz_name>` — принимает [IANA timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) (например `Europe/Moscow`, `Asia/Almaty`)
2. **Свободный текст при onboarding** — «Москва», «Берлин» → LLM резолвит в IANA timezone, спрашивает подтверждение
3. **Изменение после onboarding** — `/timezone` доступна в любое время; обновляет `users.timezone`

Если timezone не установлен (status ≠ active) — `slot_analyzer` не запускается, агент просит завершить настройку.

---

## Orphaned Session Recovery

**Проблема:** процесс агента упал в момент, когда `awaiting_confirmation = True`. При рестарте сессия зависает — пользователь не получит ответа на нажатие кнопки.

**Поведение при старте сервиса:**
```
При инициализации AgentCore:
  → Запросить из PostgreSQL все checkpoints где awaiting_confirmation = True
    AND updated_at < now() - ORPHAN_SESSION_TIMEOUT (default: 10 мин)
  → Для каждого:
       Отправить пользователю: «Предыдущее действие не завершилось.
         Хочешь повторить? [Да / Нет]»
       Установить awaiting_confirmation = False в checkpoint
```

`ORPHAN_SESSION_TIMEOUT` задаётся через `.env` (default 10 мин). Сессии, зависшие < 10 мин назад, не трогаются — возможно, процесс перезапустился быстро.

---

## Триггеры запуска

| Триггер | Источник | Действие |
|---|---|---|
| `user_message` | Telegram webhook | Запуск основного flow |
| `user_confirmation` | Telegram callback (кнопка) | Возобновление графа из checkpoint |
| `cron_check` | APScheduler, раз в час | Запуск проактивного flow |
| `webhook_renewal` | APScheduler, раз в 6 дней | Обновление Google webhook-каналов всех пользователей |
| `startup` | Инициализация сервиса | Orphaned session recovery |

---

## Модель обработки Cron

**Cron-итерация выполняется последовательно** — без параллельной обработки пользователей.

```
cron_check (раз в час):
  → Получить всех active-пользователей из users
  → Для каждого пользователя (по одному):
       get_overdue_tasks(user_id)
       Если есть просроченные задачи:
         Запустить проактивный flow (slot_analyzer → suggest → request_confirmation)
       Иначе: пропустить
  → Завершить итерацию
```

**Последствия последовательной модели:**
- При N пользователях с просроченными задачами: общее время ≈ N × avg_flow_time
- Каждый пользовательский flow ограничен 30 с (`AGENT_ITERATION_TIMEOUT_SECONDS`), итого потолок ≈ N × 30 с
- Для PoC (≤ 10–20 пользователей) это приемлемо: общий прогон ≤ 10 мин при полной загрузке
- При сбое на одном пользователе — исключение логируется как `ERROR`, итерация переходит к следующему пользователю (no-stop policy)
- Если cron-прогон не завершился до следующего тика — APScheduler пропускает следующий запуск (misfire policy: `max_instances=1`)

**Ограничение PoC:** параллельная обработка (asyncio gather / worker pool) не реализована. При росте числа пользователей (> 50) — необходимо переработать.


---

## Узлы графа

### Основной flow (user-triggered)

| Узел | Описание |
|---|---|
| `entry_router` | Определяет тип триггера; валидирует входные данные (размер, формат) |
| `transcribe` | Вызывает Whisper ASR; только для голосовых сообщений |
| `extract_intent` | LLM-вызов → `TaskIntent` JSON; валидация Pydantic |
| `confidence_gate` | `confidence >= 0.6`? Да → `slot_analyzer`. Нет → `ask_user` |
| `slot_analyzer` | Вызывает `get_events`; ищет свободные окна; формирует `list[CalendarSlot]` |
| `conflict_resolver` | Генерирует Top-3 альтернативных слота при конфликте |
| `action_decider` | Выбирает `AgentAction` на основе контекста |
| `hitl_gate` | Нужно ли подтверждение? Да → `request_confirmation`. Нет → `execute_action` |
| `request_confirmation` | Вызывает инструмент `request_confirmation`; сохраняет стейт; ждёт ответа |
| `execute_action` | Вызывает Tool Layer; применяет `AgentAction` |
| `state_writer` | Обновляет локальную БД; логирует в Langfuse |
| `ask_user` | Отправляет запрос уточнения; завершает итерацию |

### Проактивный flow (cron-triggered)

| Узел | Описание |
|---|---|
| `proactive_entry` | Вызывает `get_overdue_tasks`; итерирует по задачам |
| `slot_analyzer` | Тот же узел; ищет слоты для каждой просроченной задачи |
| `action_decider` | Формирует `action = "suggest"` с предложением переноса |
| `request_confirmation` | Отправляет предложение в Telegram; ждёт ответа пользователя |
| `execute_action` | При подтверждении — вызывает `move_event` или `create_event` |

---

## Правила переходов

```
entry_router
  ├─ invalid_input         → notify_user(error) → END
  ├─ voice_message         → transcribe
  ├─ text_message          → extract_intent
  └─ cron_trigger          → proactive_entry

transcribe
  ├─ success               → extract_intent
  └─ error                 → notify_user(retry_request) → END

extract_intent
  ├─ success               → confidence_gate
  └─ llm_error             → retry (x3) → notify_user(error) → END

confidence_gate
  ├─ confidence >= 0.6     → slot_analyzer
  └─ confidence < 0.6      → ask_user → END

slot_analyzer
  ├─ oauth_expired         → reauth_flow → END
  ├─ no_slots_found        → notify_user(no_slots) → END
  ├─ conflict              → conflict_resolver
  └─ slot_free             → action_decider

conflict_resolver          → action_decider

action_decider             → hitl_gate

hitl_gate
  ├─ safe_create           → execute_action
  └─ destructive_or_bulk   → request_confirmation

request_confirmation
  ├─ confirmed             → execute_action
  ├─ rejected              → END
  └─ timeout (5 min)       → mark_stale → END

execute_action
  ├─ success               → state_writer
  └─ api_error             → retry (x3) → notify_user(error) → END

state_writer               → notify_user(success) → END
```

---

## Stop Conditions

Граф завершает итерацию (`END`) при:
- Успешном создании / переносе события
- Отправке предложения пользователю
- Запросе уточнения
- Отказе пользователя или таймауте подтверждения
- Критической ошибке после исчерпания retry

---

## Правило немедленного действия (Auto-execute)

Агент выполняет `create_event` без подтверждения только если одновременно:
1. `confidence >= 0.6`
2. Запрошенный слот свободен
3. `action = "create"` (не move, не delete)

Во всех остальных случаях — `request_confirmation`.

---

## Таймаут итерации агента

Максимальное время выполнения одного ReAct-цикла (от получения сообщения до отправки ответа): **30 секунд** (`AGENT_ITERATION_TIMEOUT_SECONDS`).

- Реализуется через `asyncio.wait_for` на уровне вызова `AgentCore.run()`
- При превышении: прервать граф, отправить пользователю «Не успел обработать запрос, попробуй ещё раз», записать `status=timeout` в Langfuse trace
- Не применяется к `request_confirmation` — там таймаут отдельный (5 мин, управляется `CONFIRMATION_TIMEOUT_SECONDS`)

---

## Per-User Rate Limiting

Защита от флуда и неконтролируемого расхода LLM-бюджета.

- **Лимит:** `RATE_LIMIT_MSG_PER_MINUTE` сообщений в минуту на пользователя (default: 5)
- **Хранение счётчика:** in-memory скользящее окно в `entry_router` (не требует Redis в PoC)
- **При превышении:** не запускать граф, ответить: «Слишком много запросов, подожди немного» — без логирования в Langfuse (не засорять трейсы)
- **Команды бота** (`/start`, `/cancel`, `/help`, `/status`) не входят в лимит

---

## Retry / Fallback политика

| Компонент | Retry | Fallback |
|---|---|---|
| LLM (extract_intent) | x3, exponential backoff 1/2/4 s | Уведомить пользователя |
| Google Calendar API | x3, exponential backoff 1/2/4 s | Уведомить пользователя; re-auth при 401 |
| Whisper ASR | x1 | Попросить прислать текстом |
| Telegram sendMessage | x2 | Логировать, не падать |

---

## Изоляция пользователей

Каждый пользователь имеет отдельный `thread_id` в LangGraph. Checkpoint хранит стейт только для этого thread. Параллельные сессии одного пользователя не поддерживаются в PoC — новое сообщение продолжает существующий thread.

---

## Защита от prompt injection

- `extract_intent` возвращает только `TaskIntent` через structured output (Pydantic)
- Свободный текст пользователя не передаётся напрямую в tool-вызовы
- Если LLM выдаёт `action` вне разрешённого набора — Pydantic отклоняет; логируется как аномалия

---

## Ограничения агента в PoC

- Один активный диалог на пользователя
- Максимум 3 инструмент-вызова за одну итерацию
- Массовые изменения (> 3 событий за сессию) требуют явного подтверждения пакета
- `delete_event` / `delete_task` — не реализованы
