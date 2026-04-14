# Диаграмма 1 — C4 Context

## Цель

Показывает систему как единый «чёрный ящик» в окружении пользователей и внешних сервисов.
Ответ на вопрос: **кто взаимодействует с системой и через что?**

## Обязательные элементы

| Элемент | Тип | Описание |
|---|---|---|
| Пользователь | Person | Отправляет текстовые и голосовые сообщения |
| Chronos Agent | System | Граница системы — единый блок |
| Telegram | External | Канал ввода/вывода |
| Google Calendar Events API | External | Хранение и управление временными блоками (встречи, звонки) |
| Google Tasks API | External | Хранение задач с возможностью отметки выполнения |
| Mistral API | External | LLM-инференс |
| Langfuse | External | Observability и трассировка |

## Ключевые связи

- Пользователь ↔ Telegram ↔ Chronos Agent (запросы и ответы)
- Chronos Agent ↔ Google Calendar Events API (создание / перенос / чтение событий)
- Chronos Agent ↔ Google Tasks API (создание / выполнение / чтение задач)
- Google Calendar Events API → Chronos Agent (webhook: уведомление об изменениях)
- Google Tasks API — push-уведомления не поддерживает; задачи синхронизируются через write-through и startup recovery
- Chronos Agent ↔ Mistral API (запрос с tool definitions → tool_call / final_answer)
- Chronos Agent → Langfuse (логи трейсов, reason_text)

## Диаграмма

```mermaid
flowchart TB
    User(["Пользователь<br/>(текст / голос)"])

    subgraph SYSTEM ["Chronos Agent"]
        Agent["Chronos Agent<br/>Проактивный AI-планировщик"]
    end

    subgraph GOOGLE ["Google APIs"]
        direction TB
        GCalEv["Google Calendar Events API<br/>Временные блоки (встречи, звонки)"]
        GTask["Google Tasks API<br/>Задачи с статусом выполнения"]
    end

    subgraph EXT ["Прочие внешние системы"]
        direction TB
        TG["Telegram<br/>Канал ввода / вывода"]
        LLM["Mistral API<br/>LLM-инференс (function calling / ReAct)"]
        LF["Langfuse<br/>Observability"]
    end

    User -- "сообщение (текст / аудио)" --> TG
    TG -- "webhook update" --> Agent
    Agent -- "ответ / кнопки" --> TG
    TG -- "уведомление" --> User

    Agent -- "get / create / move event" --> GCalEv
    GCalEv -- "данные событий" --> Agent
    GCalEv -- "webhook: изменение события" --> Agent

    Agent -- "get / create / complete task" --> GTask
    GTask -- "данные задач" --> Agent

    Agent -- "запрос с tool definitions" --> LLM
    LLM -- "tool_call / final_answer" --> Agent

    Agent -- "трейсы / reason_text" --> LF
```
