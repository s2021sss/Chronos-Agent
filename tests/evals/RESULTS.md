# Eval Results — Chronos Agent

**Дата:** 2026-04-13
**Модель:** mistral-large-latest
**Архитектура:** ReAct StateGraph (LangGraph)

---

## NLU Eval (ReAct tool calling)

**Файл:** `run_nlu_eval.py`
**Датасет:** `dataset/nlu_dataset.yaml` — 32 кейса
**Команда:**
```bash
python tests/evals/run_nlu_eval.py --delay 2.0 --out tests/evals/results/results.json
```

### Сводные метрики

| Метрика | Значение |
|---|---|
| NLU Success (action + все поля) | **32/32 (100%)** |
| Action Accuracy | **32/32 (100%)** |
| Errors | 0 |
| Latency p50 | 1428 ms |
| Latency p95 | 2851 ms ✅ < 7s |
| Latency p99 | 3037 ms |

### По категориям

| Категория | Результат | Кейсов |
|---|---|---|
| happy_path | ✅ 10/10 | 10 |
| relative_dates | ✅ 6/6 | 6 |
| task_vs_event | ✅ 4/4 | 4 |
| modify_existing | ✅ 4/4 | 4 |
| out_of_scope | ✅ 4/4 | 4 |
| edge_cases | ✅ 4/4 | 4 |

### Методология

Eval тестирует реальное поведение ReAct агента — тот же `REACT_SYSTEM_PROMPT` и `REACT_TOOL_DEFINITIONS`, что используются в продакшне:

1. LLM вызывается с полным system prompt (текущая дата/время/часовой пояс) и 9 tool definitions
2. **ReAct loop:** read-only tools симулируются с mock-ответами, цикл продолжается до первого SIDE_EFFECT или TERMINAL tool (до 5 итераций)
3. **Контекстные mock-ответы:** для кейсов `move_event`/`complete_task` возвращаются фиктивные данные (событие или задача с нужным названием и датой +7 дней), чтобы агент мог найти цель и вызвать нужный tool
4. `confidence_min`/`confidence_max` — не проверяются (ReAct агент не производит confidence score, это осталось от старого pipeline)
5. `--delay 2.0` — пауза между кейсами для обхода rate limit Mistral API

**Почему симуляция, а не реальный граф:** симуляция точно воспроизводит то, что видит LLM при принятии NLU-решения — тот же system prompt, те же tool definitions, те же параметры вызова (`temperature=0.1`, `max_tokens=512`, `tool_choice="auto"`, `parallel_tool_calls=False`). «Симуляция» означает только то, что реальные side effects не выполняются. Это и есть цель eval: проверить NLU без изменения реального календаря пользователя.

Eval **не использует** реальный Google Calendar/Tasks и не изменяет данные пользователя.

---

## ASR Eval (Whisper + ReAct E2E)

**Файл:** `run_asr_eval.py`
**Датасет:** `dataset/voice/asr_dataset.yaml` — 11 кейсов
**Команда:**
```bash
python tests/evals/run_asr_eval.py --delay 2.0 --out tests/evals/results/results_asr.json
```

### Сводные метрики

| Метрика | Значение |
|---|---|
| ASR accuracy (транскрипция) | **11/11 (100%)** |
| NLU accuracy (action) | **11/11 (100%)** |
| E2E passed | **11/11 (100%)** |
| Errors | 0 |
| ASR latency p50 | 917 ms |
| ASR latency p95 | 1163 ms |
| NLU latency p50 | 2578 ms |
| NLU latency p95 | 3834 ms ✅ < 7s |

### По категориям

| Категория | Результат | Кейсов |
|---|---|---|
| create_event | ✅ 5/5 | 5 |
| create_task | ✅ 3/3 | 3 |
| modify_existing | ✅ 2/2 | 2 |
| edge_cases | ✅ 1/1 | 1 |

### Условия запуска

- **Whisper модель:** `base`, `language=ru`, `beam_size=5`, `vad_filter=True`
- **Аудио:** 11 `.ogg` файлов записаны фразами из поля `say_exactly` датасета
- **faster-whisper** запускается локально (`pip install faster-whisper`)
- NLU-часть использует тот же ReAct подход, что и `run_nlu_eval.py`: `REACT_SYSTEM_PROMPT` + `REACT_TOOL_DEFINITIONS` + Mistral function calling + контекстные mock-ответы

---

## Rate Limit Stress Test

**Файл:** `run_ratelimit_stress.py`
**Команда:**
```bash
python tests/evals/run_ratelimit_stress.py
```

### Результат

| Тест | Результат |
|---|---|
| Первые 5 запросов разрешены | ✅ |
| Запрос 6+ блокируется (429) | ✅ |
| `remaining()` = 0 после исчерпания | ✅ |
| Лимит user_a не влияет на user_b | ✅ |
| `reset()` позволяет снова отправлять | ✅ |
| После истечения окна лимит сбрасывается | ✅ |
| Скользящее окно: ранние запросы вытекают | ✅ |
| **Итого** | **21/21 (100%)** |

Тест проверяет `SlidingWindowRateLimiter` напрямую (без LLM API). Параметры теста: `max_calls=5`, `window=2s` — соответствует продакшн-логике `max_calls=5`, `window=60s`.

---

## Scenario Checklist (UC-001 — UC-008)

Ручная проверка use-case сценариев на тестовом Google-аккаунте с поднятым полным стеком (`docker-compose up -d`). Файл сценариев: [tests/evals/SCENARIO_CHECKLIST.md](SCENARIO_CHECKLIST.md).

| Use Case | Результат |
|---|---|
| UC-001 Текстовый ввод → создание события (HITL ✅) | ✅ |
| UC-002 Голосовой ввод → создание события | ✅ |
| UC-003 Конфликт расписания → свободные слоты | ✅ |
| UC-004 Задача с дедлайном → Google Tasks | ✅ |
| UC-005 Низкая уверенность → ask_user → уточнение | ✅ |
| UC-006 Истёкший OAuth → переавторизация → повтор | ✅ |
| UC-007 Ошибка LLM + retry × 3 → graceful degradation | ✅ |
| UC-008a Сообщение > 4000 символов → отклонение | ✅ |
| UC-008b Rate limit 6-е сообщение → 429 + сброс через 60s | ✅ |

Все 9 сценариев прошли без замечаний.

---

## Итог

| Тест | Результат |
|---|---|
| NLU Eval (32 кейса) | ✅ **32/32 (100%)** |
| ASR + NLU E2E Eval (11 кейсов) | ✅ **11/11 (100%)** |
| Rate Limit Stress Test (21 проверка) | ✅ **21/21 (100%)** |
| Scenario Checklist (9 сценариев) | ✅ **9/9 (100%)** |
