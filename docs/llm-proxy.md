# LLM Proxy

Агент использует LiteLLM Proxy — OpenAI-совместимый прокси для маршрутизации запросов к LLM.

## Как это работает

```
Chronos Agent
  → AsyncOpenAI(base_url="http://llm-proxy:8001/v1")
  → LiteLLM Proxy
     → Mistral AI API
```

При `LLM_PROXY_ENABLED=true` агент обращается к прокси вместо прямого подключения к Mistral.

## Конфигурация

Переменные окружения (`.env`):

```env
LLM_PROXY_ENABLED=true
LLM_PROXY_URL=http://llm-proxy:8001/v1
LLM_PROXY_API_KEY=dev-key-1
```

Конфиг прокси: `services/litellm_proxy/config.yaml`
- Маппинг моделей: `mistral-large-latest` → `mistral/mistral-large-latest`
- Retries: 3 попытки с интервалом 5 сек
- Timeout: 60 сек

## Запуск

```bash
docker compose up -d llm-proxy
```

Проверить доступность:

```bash
curl http://localhost:8001/health \
  -H "Authorization: Bearer dev-key-1"
```

Тестовый запрос:

```bash
curl http://localhost:8001/v1/chat/completions \
  -H "Authorization: Bearer dev-key-1" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mistral-large-latest",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

## Отключение прокси

Если `LLM_PROXY_ENABLED=false`, агент обращается напрямую к Mistral API через переменные:
- `MISTRAL_API_KEY`
- `MISTRAL_BASE_URL`

## Guardrails

Каждый запрос к прокси проходит через встроенную валидацию (`guardrails_hook.py`) перед отправкой к LLM.

Проверяются только сообщения с `role="user"`; системные сообщения и tool responses не проверяются.

**Политики блокировки:**

| Политика | Примеры |
|---|---|
| Prompt Injection | `ignore all previous instructions`, `forget the system prompt` |
| Secrets / Credentials | API ключи (`sk-...`, `AKIA...`), PEM приватные ключи |
| PII | email, номер телефона, номер карты, паспорт, СНИЛС, ИНН |
| Вредоносный контент | malware, взлом, фишинг, наркотики, оружие |

При нарушении прокси возвращает HTTP 400 с `BadRequestError`.

## Метрики

LiteLLM экспортирует Prometheus метрики на `/metrics`:

```
GET http://localhost:8001/metrics
```

**Основные метрики:**

- `litellm_deployment_success_responses_total` — количество успешных запросов
- `litellm_deployment_failure_responses_total` — количество ошибок
- `litellm_input_tokens_metric_total` — входные токены
- `litellm_output_tokens_metric_total` — выходные токены
- `litellm_spend_metric_total` — затраты
- `litellm_request_total_latency_metric` — общая задержка запроса
- `litellm_llm_api_latency_metric` — задержка самого LLM API

