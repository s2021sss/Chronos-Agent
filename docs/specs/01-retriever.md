# Spec: Retriever — Calendar Events & Tasks

## Цель

Получение актуального состояния событий и задач пользователя для reasoning-контура агента.
Данные хранятся локально в PostgreSQL и синхронизируются с Google Calendar.


## Модели данных Google Calendar

Google Calendar предоставляет **два разных ресурса**:

| Ресурс | API | Семантика | Статус выполнения |
|---|---|---|---|
| **Event** | `google.calendar.events` | Временной блок: встреча, звонок, слот. Просто наступает. | Нет |
| **Task** | `google.tasks` | Элемент to-do с опциональной датой. | `needsAction` / `completed` |

**Events** не выполняются — агент напоминает пользователю о приближающихся событиях.
**Tasks** могут быть выполнены — агент отслеживает просроченные (due_date < now, status = needsAction).

---

## Локальное хранение и синхронизация

Агент хранит события и задачи в локальном PostgreSQL. Это позволяет избежать лишних API-вызовов, быстро проверять занятость слотов через SQL и запускать проактивные cron-проверки без обращения к Google API.

**Как данные попадают в локальную БД:**

1. **Прямое создание через агента** — пользователь отправляет запрос боту; агент создаёт Event/Task через API и сразу записывает в локальную БД.
2. **Google Calendar Events Webhook (Push Notifications)** — Google отправляет уведомление при создании, изменении или удалении события; агент получает webhook, запрашивает изменённые события и обновляет локальные записи.
3. **Fallback при промахе локальной БД** — если нужных событий или задач нет в PostgreSQL, tool делает прямой запрос к Google API и upsert'ит результат.
4. **Startup recovery после простоя** — при старте после длительного downtime агент догоняет локальную БД из Google API.

Webhook покрывает изменения в Calendar Events, сделанные **напрямую в Google Calendar** пользователем, без участия агента. Google Tasks API push-уведомления не поддерживает; задачи синхронизируются через write-through, fallback и startup recovery.

Регулярного полного polling-sync по расписанию сейчас нет. В нормальном режиме актуальность локальной БД поддерживается webhook'ами Calendar Events и прямыми записями агента.

---

## Источники данных (в контексте агента)

| Источник | Когда запрашивается | Что возвращает |
|---|---|---|
| PostgreSQL (`calendar_events`) | При проверке занятых слотов | Временные окна событий пользователя |
| PostgreSQL (`calendar_tasks`) | При старте cron-итерации и при ответе пользователя | Задачи с due_date и статусом |
| Google Calendar Events API | Fallback при промахе локальной БД и startup recovery | Актуальные события из Google |
| Google Tasks API | Fallback при промахе кэша | Актуальные задачи из Google |

---

## Интерфейсы

### `get_events(user_id, start, end) → list[CalendarEvent]`

- Читает из локальной БД за диапазон `[start, end]`
- Fallback: при пустом результате — вызвать Google Calendar Events API, обновить БД
- Диапазон по умолчанию: `requested_time ± 24h`
- Не более 50 событий за вызов

### `get_tasks(user_id, status="needsAction") → list[CalendarTask]`

- Читает задачи из локальной БД по `status`
- Fallback: если в БД нет задач с таким статусом — вызвать Google Tasks API, обновить БД
- Возвращает: `[{id, title, due_date, status, gcal_task_id}]`

### `get_overdue_tasks(user_id) → list[CalendarTask]`

- Читает из PostgreSQL: `status = needsAction` И `due_date < now()`
- Вызывается **только в cron-пути** (раз в час)

### `find_free_slots(user_id, duration_minutes, search_window_hours=24) → list[CalendarSlot]`

- Вызывает `get_events`, вычисляет свободные промежутки ≥ `duration_minutes`
- Рабочие часы: 08:00–22:00 по timezone пользователя
- Возвращает не более 3 слотов, отсортированных по `score`
- Если в 24h нет подходящих слотов — расширяет окно до 48h

---

## Webhook-обработчик

- Эндпоинт: `POST /webhooks/google/calendar`
- Google отправляет уведомление при создании / изменении / удалении Calendar Event
- Агент находит пользователя по `X-Goog-Channel-ID`
- Затем вызывает `sync_calendar_events(user_id, token)`
- Webhook-канал требует периодического обновления (Google: TTL до 7 дней) — обновление через APScheduler раз в `WEBHOOK_RENEWAL_INTERVAL_DAYS` дней, по умолчанию 6

`sync_calendar_events()` запрашивает у Google Calendar Events API все события, обновлённые за последние 10 минут (`updatedMin = now - 10 min`). Запас в 10 минут компенсирует задержку доставки webhook и рассинхрон часов. Удалённые события запрашиваются включительно: Google возвращает их со статусом `cancelled`, и локальная запись обновляется соответственно. Результат сохраняется через idempotent upsert по уникальному ограничению `(user_id, gcal_event_id)`.

При первой OAuth-авторизации пользователя агент сохраняет зашифрованный `refresh_token`, переводит пользователя в `pending_timezone` и, если задан `GOOGLE_WEBHOOK_BASE_URL`, регистрирует Calendar Events push-канал через `events.watch()`. Первичная полная загрузка календаря при OAuth сейчас не выполняется; данные подтягиваются через webhook, fallback-запросы и startup recovery.

### Google Tasks

Google Tasks API не поддерживает push-уведомления, поэтому для задач используются другие пути:

| Способ | Когда | Механизм |
|---|---|---|
| Write-through | Агент сам создаёт или завершает задачу | `create_task` / `complete_task` пишут и в Google Tasks API, и в `calendar_tasks` |
| Startup recovery | После простоя сервиса больше `RECOVERY_MIN_DOWNTIME_SECONDS` | Полный fetch `needsAction` + `completed`, затем upsert |
| Fallback | `get_tasks` при пустой БД для нужного статуса | Прямой запрос к Google Tasks API + upsert результата |

`get_overdue_tasks()` намеренно читает только локальную БД и не делает fallback в Google Tasks API, потому что вызывается из cron-пути для всех пользователей.

### Уведомления о просроченных задачах

APScheduler запускает `_cron_check_job` каждые `CRON_INTERVAL_MINUTES` минут, по умолчанию раз в час. Для каждого активного пользователя вызывается `get_overdue_tasks()`, которая читает из `calendar_tasks` задачи со статусом `needsAction` и `due_at < now()`. Если просроченные задачи найдены — пользователю отправляется Telegram-уведомление.

В Telegram-уведомление попадают первые 5 просроченных задач; если задач больше, добавляется счётчик остатка. Пользователи обрабатываются последовательно, чтобы не создавать всплеск запросов и не упираться в rate limits. После уведомления пользователь отвечает свободным текстом, и сообщение обрабатывается обычным ReAct flow. В текущей реализации для задач есть `complete_task`; отдельного tool для переноса `due_at` задачи пока нет.

### Startup recovery после простоя

Во время нормальной работы APScheduler каждые `HEARTBEAT_INTERVAL_SECONDS` секунд обновляет запись в таблице `service_heartbeat` с полями `last_alive_at = now()` и `shutdown_type = 'crash'`. При graceful shutdown сервис сначала останавливает scheduler, а затем перезаписывает `shutdown_type = 'graceful'`, чтобы следующий запуск мог различить нормальное завершение и аварийный крэш.

`run_startup_recovery()` вызывается при старте после инициализации `AgentCore` и до запуска APScheduler:

1. Читает `last_alive_at` из `service_heartbeat`.
2. Если записи нет — это первый запуск, recovery пропускается.
3. Считает `downtime = now - last_alive_at`.
4. Если downtime меньше `RECOVERY_MIN_DOWNTIME_SECONDS`, sync пропускается и heartbeat сдвигается на текущее время.
5. Если downtime больше порога, для каждого активного пользователя с Google token:
   - Calendar Events: `Events.list(updatedMin=last_alive_at)` и upsert в `calendar_events`;
   - Google Tasks: полный fetch `needsAction` + `completed` и upsert в `calendar_tasks`.
6. Если все пользователи успешно синхронизированы — `last_alive_at` обновляется.
7. Если хотя бы один пользователь упал — `last_alive_at` не обновляется, следующий рестарт повторит sync с той же точки.

Повторный catch-up безопасен, потому что записи сохраняются через идемпотентный upsert.

### Деградированный режим при сбое обновления канала

Если APScheduler не смог обновить канал (Google API недоступен, сетевой сбой):
- Google перестаёт присылать push-уведомления по истечении TTL старого канала (≤ 7 дней)
- **Агент продолжает работать** — `get_events` имеет fallback на прямой Google Calendar API-запрос при пустом результате из БД
- **Деградация:** изменения, сделанные пользователем напрямую в Google Calendar (вне агента), не отразятся в локальной БД до следующего прямого API-запроса (т.е. до первого пользовательского запроса, требующего `get_events`)
- **Определение деградированного режима:** `users.gcal_events_channel_expiry < now()` — канал истёк; логировать `WARNING` при обнаружении во время `get_events`
- **Следующая попытка обновления:** APScheduler повторит попытку через `WEBHOOK_RENEWAL_INTERVAL_DAYS` дней; при повторном успехе — канал восстанавливается автоматически

---

## Скоринг слотов

`score` — эвристика (без ML):
- Близость к запрошенному времени (вес 0.5)
- Время дня (утро предпочтительнее вечера, вес 0.3)
- Запас длительности ≥ 30 мин сверх запрошенной (вес 0.2)

---

## Ограничения

- Отслеживается только один (primary) календарь пользователя
- Нет кэша поверх БД — данные актуальны на момент последнего webhook или прямого запроса

---

## Обработка ошибок

| Ошибка | Поведение |
|---|---|
| `OAuthExpiredError` (401) | Передать в оркестратор → re-auth flow |
| `CalendarAPIError` (429/5xx) | Retry x3 с exponential backoff (1 s, 2 s, 4 s) |
| Webhook недоступен / не обновлён | APScheduler повторит через `WEBHOOK_RENEWAL_INTERVAL_DAYS` дней; деградированный режим: fallback на прямые API-запросы при `get_events` |
| Нет свободных слотов в 48h | Вернуть пустой список; оркестратор уведомляет пользователя |
| Невалидный диапазон дат | Ошибка валидации; API не вызывается |
