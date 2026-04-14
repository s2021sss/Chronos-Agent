# Chronos Agent

**Проактивный менеджер когнитивной нагрузки на базе ReAct агентной архитектуры.**

## Описание проекта

**Цель:** Автономное управление персональным расписанием и приоритетами.  
**Целевая аудитория:** Студенты, исследователи и специалисты с высокой когнитивной нагрузкой, которые тратят большое количество времени на микроменеджмент задач или вовсе не могут организовать эффективное планирование своей работы из-за высокого порога входа существующих решений (большой объем ручной работы: создание / изменение / отслеживание задач, управление категориями и списками).

**Проблематика существующих решений:**
- **Отсутствие оптимизаций рутинных действий:** Существующие решения предлагают вводить события/задачи с указанием времени, дедлайнов, категорий и напоминаний, что занимает немалое количество времени. Из-за этого многие отказываются от использования планировщиков или вводят данные неполно, снижая эффективность планирования.
- **Статичность:** Обычные календари статичны. Если ты пропустил задачу, она просто висит «красным» или исчезает. 
- **Порог входа для планирования.** Большое количество полей для ввода вынуждают пользователя отказаться от использования планировщиков.


## Что именно сделает PoC на демо

Цель PoC — показать, как агент может оптимизировать процесс планирования, сохраняя за пользователем возможность контролировать и корректировать принимаемые решения.

1. **Multimodal Intake:** пользователь может отправлять короткие голосовые заметки или текстовые сообщения в Telegram‑бот. Локальный Whisper распознаёт речь, ReAct-агент итеративно извлекает нужную информацию: задача, длительность, дедлайн, предпочтения по времени.
2. **ReAct Agent:** LLM рассуждает и последовательно вызывает инструменты — проверяет календарь, находит свободные окна, предлагает варианты. Каждое действие с побочным эффектом (создание, перенос, завершение) требует явного подтверждения.
3. **Calendar Check & Conflict Resolution:** агент сверяет полученные слоты с календарём, при конфликте предлагает до 3 вариантов.
4. **HITL Confirmations:** перед любым изменением в календаре агент отправляет inline-кнопки ✅/❌ и ждёт явного подтверждения пользователя.
5. **Observability & Explainability:** все шаги агента (reasoning, tool calls, results) видны в Langfuse — можно инспектировать причины и откатывать решения.

### Что НЕ делает PoC (Out-of-scope)

* Бронирование внешних сервисов (столики, билеты).
* Работа с групповыми календарями и приглашение других участников.
* Сложная аналитика продуктивности за месяцы.
* Удаление событий/задач (делается вручную в Google Calendar).

---

## Архитектура

```
Telegram Message
      ↓
messages.py
  • rate limit
  • onboarding gate
  • off-topic pre-filter
  • voice → Whisper ASR → text
  • conversation classification
      ↓
AgentCore.run()
      ↓
LangGraph ReAct StateGraph
  ┌─ reasoner (Mistral Large)
  │    LLM рассуждает и выбирает tool call
  │
  ├─ tool_router
  │    ├── READ_ONLY → read_tool_executor → reasoner (loop)
  │    │     get_calendar_events, find_free_slots,
  │    │     get_pending_tasks, get_conversation_history
  │    │
  │    └── SIDE_EFFECT → [interrupt] hitl_wait → write_tool_executor
  │          create_event, create_task, move_event, complete_task
  │
  └─ respond_node → notify_user → Telegram reply
```

---

## Технологический стек

| Компонент | Технология |
|---|---|
| Telegram Bot | Aiogram 3, webhook |
| Агент | LangGraph StateGraph (ReAct) |
| HITL подтверждения | interrupt_before + AsyncPostgresSaver |
| Голосовой ввод | faster-whisper |
| LLM | Mistral Large |
| OAuth | OAuth 2.0 + Fernet шифрование |
| Calendar/Tasks API | google-api-python-client |
| БД | PostgreSQL + SQLAlchemy async |
| Миграции | Alembic |
| Observability | Langfuse v2 + structlog |
| Деплой | Docker Compose |

---

## Быстрый старт

Подробная пошаговая инструкция: **[docs/quick-start.md](docs/quick-start.md)**

### Требования

- Docker + Docker Compose
- Google Cloud Project с включёнными Calendar API и Tasks API
- [Mistral API](https://console.mistral.ai/) ключ
- Telegram Bot Token ([@BotFather](https://t.me/BotFather))
- HTTPS URL для Telegram webhook (ngrok для разработки)

### Запуск

```bash
# 1. Скопируй конфиг
cp .env.example .env
# Заполни все переменные в .env (см. docs/quick-start.md)

# 2. Запусти
docker-compose up --build

# 3. Примени миграции (только первый раз)
docker-compose run --rm agent alembic upgrade head
```

### Ключевые переменные окружения

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен бота |
| `TELEGRAM_WEBHOOK_URL` | Публичный HTTPS URL (ngrok / домен) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | OAuth credentials |
| `GOOGLE_REDIRECT_URI` | `{WEBHOOK_URL}/auth/google/callback` |
| `MISTRAL_API_KEY` | Ключ Mistral API |
| `POSTGRES_URL` | `postgresql+asyncpg://user:pass@host:port/db` |
| `ENCRYPTION_KEY` | Fernet key (32 bytes, base64) |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Langfuse observability |

Полный список: [.env.example](.env.example)

---

## Команды бота

| Команда | Описание |
|---|---|
| `/start` | Начать настройку или показать приветствие |
| `/cancel` | Отменить текущее действие |
| `/help` | Справка и примеры запросов |
| `/status` | Список активных задач |
| `/timezone Europe/Moscow` | Установить часовой пояс |

**Примеры запросов:**
- «Встреча с командой завтра в 14:00 на час»
- «Напомни сдать отчёт в пятницу»
- «Перенеси встречу с Иваном на вторник»
- «Отметь задачу "подготовить презентацию" выполненной»

---

## Разработка

```bash
# Линт + форматирование
ruff check --fix .
ruff format .

# ReAct eval (тест выбора инструментов агентом)
# --delay 2.0 — пауза между кейсами для избежания rate limit Mistral API
python tests/evals/run_nlu_eval.py --delay 2.0 --out tests/evals/results/results.json

# ASR eval
python tests/evals/run_asr_eval.py --delay 2.0 --out tests/evals/results/results_asr.json

# Rate limit stress test
python tests/evals/run_ratelimit_stress.py

# Создать новую миграцию БД
alembic revision --autogenerate -m "description"
```

---

## Документация

| Документ | Описание |
|---|---|
| [docs/quick-start.md](docs/quick-start.md) | Пошаговая инструкция по запуску (Google, Telegram, ngrok) |
| [docs/llm-proxy.md](docs/llm-proxy.md) | LiteLLM прокси: конфигурация, guardrails и метрики |
| [tests/evals/RESULTS.md](tests/evals/RESULTS.md) | Результаты eval


### Спецификации

| Документ | Описание |
|---|---|
| [docs/specs/01-retriever.md](docs/specs/01-retriever.md) | Синхронизация событий и задач из Google Calendar, локальное хранилище |
| [docs/specs/02-tools-apis.md](docs/specs/02-tools-apis.md) | Tool Layer спецификация |
| [docs/specs/03-memory-context.md](docs/specs/03-memory-context.md) | Память и контекст диалога |
| [docs/specs/04-agent-orchestrator.md](docs/specs/04-agent-orchestrator.md) | Детальная спецификация ReAct агента |
| [docs/specs/05-serving-config.md](docs/specs/05-serving-config.md) | Конфигурация, деплой, безопасность |
| [docs/specs/06-observability-evals.md](docs/specs/06-observability-evals.md) | Langfuse, метрики, eval-сценарии |

### Диаграммы архитектуры

| Диаграмма | Описание |
|---|---|
| [docs/diagrams/01-c4-context.md](docs/diagrams/01-c4-context.md) | C4 Context — система в окружении пользователей и сервисов |
| [docs/diagrams/02-c4-container.md](docs/diagrams/02-c4-container.md) | C4 Container — компоненты (Bot, ASR, Agent Core, PostgreSQL, etc.) |
| [docs/diagrams/03-c4-component.md](docs/diagrams/03-c4-component.md) | C4 Component — узлы ReAct графа (reasoner, router, executors) |
| [docs/diagrams/04-workflow.md](docs/diagrams/04-workflow.md) | Workflow — полный процесс обработки запроса |

