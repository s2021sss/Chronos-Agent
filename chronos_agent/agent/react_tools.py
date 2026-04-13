"""
ReAct Tool Definitions — инструменты для LLM function calling.

REACT_TOOL_DEFINITIONS: список tool schemas в формате OpenAI/Mistral.
READ_ONLY_TOOLS:   инструменты без side-effect — выполняются автоматически.
SIDE_EFFECT_TOOLS: инструменты с записью — требуют HITL-подтверждения.
TERMINAL_TOOLS:    инструменты завершения — ask_user, заканчивают текущий шаг.
"""

REACT_TOOL_DEFINITIONS = [
    # ── Read-only tools ───────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_conversation_history",
            "description": (
                "Получить историю недавних сообщений с пользователем. "
                "Используй когда сообщение пользователя короткое и ссылается "
                "на предыдущий контекст, или когда нужно восстановить детали "
                "(название события, время) из предыдущего диалога."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Количество сообщений (1–10)",
                        "default": 5,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_calendar_events",
            "description": "Получить события Google Calendar в диапазоне дат.",
            "parameters": {
                "type": "object",
                "properties": {
                    "time_min": {"type": "string", "description": "ISO 8601 datetime"},
                    "time_max": {"type": "string", "description": "ISO 8601 datetime"},
                },
                "required": ["time_min", "time_max"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_free_slots",
            "description": "Найти свободные временные слоты для нового события.",
            "parameters": {
                "type": "object",
                "properties": {
                    "duration_minutes": {
                        "type": "integer",
                        "description": "Требуемая длительность события в минутах",
                    },
                    "preferred_start": {
                        "type": "string",
                        "description": "Предпочтительное время начала ISO 8601 (опционально)",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Количество вариантов (по умолчанию 3)",
                        "default": 3,
                    },
                },
                "required": ["duration_minutes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pending_tasks",
            "description": "Получить список активных (невыполненных) задач пользователя.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    # ── Side-effect tools (требуют HITL) ──────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "create_event",
            "description": (
                "Создать событие в Google Calendar. "
                "Перед вызовом проверь конфликты через get_calendar_events. "
                "Требует подтверждения пользователя."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Название события"},
                    "start": {"type": "string", "description": "Время начала ISO 8601"},
                    "end": {"type": "string", "description": "Время конца ISO 8601"},
                    "description": {
                        "type": "string",
                        "description": "Описание события (опционально)",
                    },
                },
                "required": ["title", "start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": "Создать задачу в Google Tasks. Требует подтверждения пользователя.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Название задачи"},
                    "due_date": {
                        "type": "string",
                        "description": "Срок выполнения ISO 8601 (опционально)",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Заметки к задаче (опционально)",
                    },
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_event",
            "description": (
                "Перенести существующее событие на другое время. "
                "Требует подтверждения пользователя."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "event_title": {
                        "type": "string",
                        "description": "Название события для поиска (частичное совпадение)",
                    },
                    "new_start": {
                        "type": "string",
                        "description": "Новое время начала ISO 8601",
                    },
                    "new_end": {
                        "type": "string",
                        "description": (
                            "Новое время конца ISO 8601 (если не указано — прежняя длительность)"
                        ),
                    },
                },
                "required": ["event_title", "new_start"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": ("Отметить задачу выполненной. Требует подтверждения пользователя."),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_title": {
                        "type": "string",
                        "description": "Название задачи для поиска (частичное совпадение)",
                    },
                },
                "required": ["task_title"],
            },
        },
    },
    # ── Terminal tools ────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Задать уточняющий вопрос пользователю и завершить текущий шаг. "
                "Используй когда не хватает информации для выполнения действия "
                "(неизвестное время, название, и т.д.)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "Конкретный уточняющий вопрос на русском языке",
                    },
                },
                "required": ["question"],
            },
        },
    },
]


READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "get_conversation_history",
        "get_calendar_events",
        "find_free_slots",
        "get_pending_tasks",
    }
)

SIDE_EFFECT_TOOLS: frozenset[str] = frozenset(
    {
        "create_event",
        "create_task",
        "move_event",
        "complete_task",
    }
)

TERMINAL_TOOLS: frozenset[str] = frozenset(
    {
        "ask_user",
    }
)
