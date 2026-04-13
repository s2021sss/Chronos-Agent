"""
ReactAgentState — состояние LangGraph-графа ReAct агента.

Все поля JSON-сериализуемы.

Ключевые поля:
  - messages: полная история function calling API (system/user/assistant/tool)
  - pending_tool_call: последний tool_call от LLM (имя + аргументы)
  - tool_call_id: ID текущего pending tool_call (для tool result message)
  - iteration_count: счётчик шагов reasoner (защита от бесконечного цикла)
  - final_answer: финальный текст ответа агента (устанавливается reasoner)
"""

from typing import TypedDict


class ReactAgentState(TypedDict):
    user_id: str
    trigger: str  # "text_message" / "voice_message"
    raw_input: str
    user_timezone: str

    # id диалога из таблицы conversations
    conversation_id: int | None

    # system, user, assistant (с tool_calls), tool (results)
    messages: list[dict]

    # сообщения завершённого предыдущего хода (без system prompt)
    prior_messages: list[dict]

    # последний tool_call от LLM (name + arguments dict)
    pending_tool_call: dict | None
    # id текущего tool_call
    tool_call_id: str | None

    awaiting_confirmation: bool
    confirmed: bool | None

    iteration_count: int
    final_answer: str | None

    langfuse_trace_id: str | None
    error: str | None
