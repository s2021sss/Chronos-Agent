# Мониторинг и наблюдаемость

## Обзор стека

| Инструмент | Задача | URL |
|---|---|---|
| **Prometheus** | Сбор метрик (числа, latency, счётчики) | http://localhost:9090 |
| **Grafana** | Визуализация метрик из Prometheus | http://localhost:3001 (admin/admin) |
| **Langfuse** | Трейсинг LLM-вызовов и шагов агента | http://localhost:3000 |

```
LiteLLM Proxy ──/metrics──▶ Prometheus ──▶ Grafana (dashboards)
Chronos Agent ──/metrics──┘
cAdvisor ───────/metrics──┘ (CPU/Memory контейнеров)
Node Exporter ──/metrics──┘ (CPU/Memory/Disk хоста)

Chronos Agent ──langfuse SDK──▶ Langfuse (traces, generations, spans)
```

> Панели Grafana пустые пока стек не запущен и не обрабатывает запросы — это нормально.

---

## Prometheus

Конфиг: [`monitoring/prometheus.yml`](../monitoring/prometheus.yml)

| Job | Target | Что собирает |
|---|---|---|
| `llm-proxy` | `llm-proxy:8001/metrics` | LLM latency, tokens, cost, errors |
| `agent` | `agent:8000/metrics` | Agent requests, actions, errors |
| `cadvisor` | `cadvisor:8080/metrics` | CPU/Memory/Network контейнеров |
| `node-exporter` | `node-exporter:9100/metrics` | CPU/Memory/Disk хоста |

---

## Grafana Dashboard

Файл: [`monitoring/grafana/dashboards/llm_proxy.json`](../monitoring/grafana/dashboards/llm_proxy.json)

Dashboard `LLM Proxy (LiteLLM)` — шесть секций.

### Requests

| Панель | Описание |
|---|---|
| Requests Since Restart | Stat — всего запросов через прокси |
| Failures Since Restart | Stat — всего ошибок |
| Traffic by Model | Pie chart — распределение запросов по модели |
| Requests by Model (req/s) | Timeseries — RPS по модели |
| Success vs Failure by Provider | Timeseries — успехи/ошибки по провайдеру |
| Error Rate by Provider (%) | Timeseries — % ошибок по провайдеру |

### Latency

| Панель | Описание |
|---|---|
| End-to-End Latency (s) | Среднее время от запроса до ответа клиенту |
| Queue Time (s) | Время в очереди прокси |
| Upstream API Latency (s) | Время ответа у провайдера |
| Proxy Overhead (s) | Накладные расходы прокси |

### Cost

| Панель | Описание |
|---|---|
| Spend Since Restart ($) | Суммарные расходы |
| Avg Cost / Request ($) | Средняя стоимость запроса по модели |
| Tokens In vs Out | Входящие/исходящие токены по модели (rate) |
| Spend by Model ($) | Расходы по модели (rate) |

### Agent (метрики Chronos Agent)

| Панель | Описание |
|---|---|
| Requests/s by Trigger | RPS: text_message / voice_message |
| Agent Request Duration | Histogram p50/p95 E2E времени AgentCore.run() |
| Tool Errors/s by Type | Ошибки по типу (validation, oauth, api) |
| Actions/s by Type & Status | Действия агента: create_event/task success/error |

### Streaming (TTFT / TPOT)

| Панель | Описание |
|---|---|
| TTFT — Time to First Token (s) | p50/p95/p99 до первого токена (только при stream=true) |
| TPOT — Time per Output Token (ms/token) | Скорость генерации мс/токен |

### Infra

| Панель | Описание |
|---|---|
| CPU Usage by cgroup | CPU % контейнеров (Docker) |
| Host Memory Breakdown | Разбивка памяти хоста |
| Host CPU Usage (%) | CPU % хоста |

---

## Метрики Chronos Agent

Файл: [`chronos_agent/metrics.py`](../chronos_agent/metrics.py)

Экспортируются на `GET /metrics`:

```python
# Счётчик входящих запросов (labels: trigger = text_message | voice_message)
chronos_agent_requests_total

# Гистограмма E2E latency AgentCore.run() (labels: trigger)
# Buckets: 0.5, 1, 2, 5, 10, 20, 30, 60 с
chronos_agent_request_duration_seconds

# Выполненные действия (labels: action_type, status = success | error)
chronos_agent_actions_total

# Ошибки Tool Layer (labels: error_type = validation | oauth_expired | calendar_api | unknown)
chronos_agent_tool_errors_total
```

---

## Langfuse — трейсинг агента

Файл: [`chronos_agent/observability.py`](../chronos_agent/observability.py)

Langfuse (http://localhost:3000) показывает путь обработки каждого запроса через ReAct граф:

```
trace: agent_run  (user_id, session_id, raw_input)
  └─ generation: reasoner       (LLM вызов с tools, входящий prompt + tool definitions)
  └─ span: tool_router          (маршрутизация: read / write / hitl / respond)
  └─ span: read_tool_executor   (get_calendar_events, get_pending_tasks, ...)
  └─ generation: reasoner       (следующая итерация ReAct loop)
  └─ span: write_tool_executor  (create_event, create_task, move_event, ...)
  └─ span: respond              (финальный ответ пользователю)
```

Langfuse показывает:
- Какой prompt отправлен в LLM и какие tools предложены
- Сколько токенов потрачено на каждый LLM вызов
- Какой tool вызвал агент и с какими аргументами
- E2E время обработки запроса

Если `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` не заданы — трейсинг отключается (no-op).

---

## Health-check endpoints

```bash
# Chronos Agent — проверяет PostgreSQL + LLM API:
curl http://localhost:8000/health
# → {"status": "ok", "postgres": "ok", "llm": "ok"}

# LiteLLM Proxy:
curl http://localhost:8001/health -H "Authorization: Bearer dev-key-1"

# Langfuse:
curl http://localhost:3000/api/public/health
```
