# Spec: Tools / APIs

## Цель

Tool Layer — набор вызываемых агентом инструментов. Каждый инструмент имеет строго типизированный вход/выход (Pydantic), обрабатывает ошибки и не выполняет действий без прохождения валидации.


## Список инструментов

### `create_event`

**Цель:** Создать событие в Google Calendar + записать в локальную БД.

**Вход:**
```python
class CreateEventInput(BaseModel):
    user_id: str
    title: str
    start: datetime
    end: datetime
    description: Optional[str] = None
```

**Выход:** `gcal_event_id: str`

**Side effects:**
- Запись в Google Calendar Events API
- Запись в локальную БД (`calendar_events`)

**Ограничения:**
- `end > start` — обязательно
- `start` не может быть в прошлом более чем на 1 час (sanity check)
- Без подтверждения пользователя (автоматическое создание при `confidence >= 0.6` и свободном слоте)

**Идемпотентность:**
- Перед вызовом Google Calendar API проверить `calendar_events` на наличие записи с тем же `user_id`, `title`, `start` (±1 мин)
- Если запись уже существует — вернуть существующий `gcal_event_id`, не создавать повторно
- Повторные webhook-уведомления от Google: при получении `push notification` с уже известным `gcal_event_id` — обновить запись в БД, не создавать новую (`INSERT ... ON CONFLICT (gcal_event_id) DO UPDATE`)

---

### `create_task`

**Цель:** Создать задачу в Google Tasks + записать в локальную БД.

**Вход:**
```python
class CreateTaskInput(BaseModel):
    user_id: str
    title: str
    due_date: Optional[datetime] = None
    notes: Optional[str] = None
```

**Выход:** `gcal_task_id: str`

**Side effects:**
- Запись в Google Tasks API
- Запись в локальную БД (`calendar_tasks`, `status = needsAction`)

**Идемпотентность:**
- Проверить `calendar_tasks` на наличие записи с тем же `user_id`, `title`, `due_date` (±1 день)
- При совпадении — вернуть существующий `gcal_task_id`
- Webhook-дедупликация аналогична `create_event`: `ON CONFLICT (gcal_task_id) DO UPDATE`

---

### `move_event`

**Цель:** Перенести событие на другой слот.

**Вход:**
```python
class MoveEventInput(BaseModel):
    user_id: str
    event_id: str
    new_start: datetime
    new_end: datetime
```

**Выход:** обновлённый `gcal_event_id`

**Guardrail:** Требует явного подтверждения пользователя (`HITL`) перед вызовом, если длительность события > 2 ч.

---

### `complete_task`

**Цель:** Отметить задачу выполненной.

**Вход:** `user_id: str`, `task_id: str`

**Side effects:**
- Обновление статуса в Google Tasks API (`status = completed`)
- Обновление локальной БД

---

### `notify_user`

**Цель:** Отправить сообщение пользователю через Telegram.

**Вход:**
```python
class NotifyInput(BaseModel):
    user_id: str
    text: str
    buttons: Optional[list[InlineButton]] = None  # OK / Cancel
```

**Выход:** `message_id: str`

**Используется для:** подтверждений, предложений переноса, запросов уточнения, re-auth ссылок.

---

### `request_confirmation`

**Цель:** Отправить предложение с кнопками и дождаться ответа пользователя.

**Вход:** `user_id`, `text`, `action_payload` (сериализованный `AgentAction`)

**Выход:** `confirmed: bool`

**Логика:**
- Сохраняет `awaiting_confirmation = True` в стейте LangGraph
- Telegram отправляет сообщение с кнопками `✅ Подтвердить` / `❌ Отменить`
- При ответе пользователя → LangGraph возобновляет граф из сохранённого checkpoint
- При отсутствии ответа > 5 мин → `confirmed = False`, сессия → `stale`

---

## Политика валидации

Все инструменты получают вход через Pydantic. Если валидация не пройдена — инструмент выбрасывает `ToolValidationError` и не выполняет никаких внешних вызовов.

Дополнительные sanity checks перед записью в API:
- Дата не в прошлом более чем на 1 час
- Дата не в будущем более чем на 2 года
- `end > start`
- Заголовок не пустой, длина ≤ 256 символов

### Санация пользовательского ввода

Заголовок события / задачи и поле `description` формируются из текста пользователя и записываются напрямую в Google Calendar. Перед записью необходима санация:

| Поле | Правило |
|---|---|
| `title` | Обрезать пробелы по краям; удалить управляющие символы `\x00–\x1F` (кроме `\n`); отклонить если пустой или состоит только из пробелов; ограничить 256 символами |
| `description` | Обрезать пробелы по краям; удалить управляющие символы `\x00–\x1F` (кроме `\n`); ограничить 8 000 символами |
| `notes` (create_task) | Те же правила, что для `description` |

**Почему важно:** заголовок из пользовательского ввода попадает в Google Calendar, откуда может быть прочитан сторонними интеграциями, экспортирован в iCal или отображён в UI. Управляющие символы могут нарушить парсинг; чрезмерно длинный текст — превысить лимиты Google API.

**Что NOT делается:** HTML-экранирование не требуется — Google Calendar API принимает plain text и самостоятельно обрабатывает экранирование при отображении.

Санация реализуется в Pydantic-валидаторах моделей входных данных (`@field_validator`), до любых внешних вызовов.

---

## Ограниченный набор действий

В PoC агент **не имеет** инструмента `delete_event` / `delete_task`. Удаление доступно только напрямую через Google Calendar UI.

Разрешённые действия агента: `create_event`, `create_task`, `move_event`, `complete_task`, `notify_user`, `request_confirmation`.

---

## Обработка ошибок

| Ошибка | Инструмент | Поведение |
|---|---|---|
| `OAuthExpiredError` | create/move/complete | Вернуть ошибку в оркестратор → re-auth flow |
| `CalendarAPIError` 429/5xx | create/move | Retry x3 exponential backoff; после 3 → `notify_user` об ошибке |
| `ToolValidationError` | все | Вернуть ошибку валидации; внешних вызовов нет |
| Telegram send failure | notify_user | Retry x2; при неудаче — логировать, не падать |
| Дублирующийся event | create_event | Проверить по `gcal_event_id` в локальной БД; не создавать повторно |

---

## Квота Google API

Google Calendar Events API: 100 req/min (per user token). Google Tasks API: 50 req/s (per user token).

В PoC — один пользователь, квота не исчерпывается при нормальной работе. Тем не менее Tool Layer обязан корректно обрабатывать 429.

**Поведение при 429:**
- Первый 429 → ждать время из заголовка `Retry-After` (если есть) или 5 секунд, повторить
- Повторный 429 → exponential backoff: 10 s, 20 s
- После 3 попыток → прервать операцию, уведомить пользователя: «Google Calendar временно недоступен, попробуй позже»
- Логировать каждый 429 как `WARNING` в app-логи с указанием `user_id` и типа операции

**Предотвращение избыточных вызовов:**
- `get_events` при проверке слотов вызывается один раз за итерацию; результат не кэшируется, но повторный вызов в рамках той же итерации не допускается

---

## Timeout

| Инструмент | Timeout |
|---|---|
| Google Calendar API (create/move) | 10 s |
| Google Calendar API (get_events) | 5 s |
| Telegram sendMessage | 5 s |
| request_confirmation (ожидание ответа) | 5 мин |
