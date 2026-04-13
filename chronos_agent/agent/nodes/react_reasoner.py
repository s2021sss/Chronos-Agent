"""
react_reasoner_node — основной LLM-узел ReAct агента.

Вызывает Mistral с REACT_TOOL_DEFINITIONS через нативный function calling API.

Логика:
  - На первой итерации: строит messages из system_prompt + user_message.
  - На последующих итерациях: messages уже содержат tool results от предыдущих шагов.
  - finish_reason="tool_calls" -> устанавливает pending_tool_call, увеличивает iteration_count.
  - finish_reason="stop" -> устанавливает final_answer.
  - Если iteration_count >= max -> force stop с сообщением об ошибке.

respond_node — финальный узел отправки ответа пользователю.

Отправляет final_answer через notify_user и сохраняет в историю диалога.
"""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from chronos_agent.agent.react_state import ReactAgentState
from chronos_agent.agent.react_tools import REACT_TOOL_DEFINITIONS
from chronos_agent.config import settings
from chronos_agent.formatting import md_to_html
from chronos_agent.llm.client import get_raw_llm_client
from chronos_agent.llm.prompts import REACT_SYSTEM_PROMPT
from chronos_agent.logging import get_logger
from chronos_agent.memory.conversation import add_conversation_message
from chronos_agent.observability import get_current_trace_id, node_generation, node_span
from chronos_agent.tools.notify_user import NotifyInput, notify_user

logger = get_logger(__name__)

_WEEKDAY_RU = [
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
]


def _build_system_prompt(state: ReactAgentState) -> str:
    """Строит system prompt с контекстом даты/времени пользователя."""
    user_timezone = state.get("user_timezone") or "UTC"
    try:
        tz = ZoneInfo(user_timezone)
    except (ZoneInfoNotFoundError, KeyError):
        tz = ZoneInfo("UTC")

    now = datetime.now(tz)
    today = now.date()
    tomorrow = today + timedelta(days=1)

    # +0300 -> +03:00
    dt_str = now.strftime("%Y-%m-%dT%H:%M:%S%z")
    if len(dt_str) >= 5 and dt_str[-5] in ("+", "-") and ":" not in dt_str[-5:]:
        dt_str = dt_str[:-2] + ":" + dt_str[-2:]

    next_weekdays_lines = []
    for delta in range(1, 8):
        day = today + timedelta(days=delta)
        weekday = _WEEKDAY_RU[day.weekday()]
        next_weekdays_lines.append(f"  {weekday}: {day.isoformat()}")
    next_weekdays = "\n".join(next_weekdays_lines)

    return REACT_SYSTEM_PROMPT.format(
        current_datetime=dt_str,
        user_timezone=user_timezone,
        today_date=today.isoformat(),
        tomorrow_date=tomorrow.isoformat(),
        weekday_name=_WEEKDAY_RU[today.weekday()],
        next_weekdays=next_weekdays,
        max_iterations=settings.max_tool_calls_per_iteration,
    )


async def reasoner_node(state: ReactAgentState) -> dict:
    """
    Узел графа: вызывает LLM с tool definitions.

    На первом вызове строит messages из system + user.
    На последующих — messages уже содержат tool results.
    При превышении max_iterations — force stop.
    """
    iteration_count = state.get("iteration_count") or 0
    user_id = state["user_id"]
    trace_id = state.get("langfuse_trace_id") or get_current_trace_id()

    if iteration_count >= settings.max_tool_calls_per_iteration:
        logger.warning(
            "react_reasoner_max_iterations",
            user_id=user_id,
            iteration_count=iteration_count,
        )
        return {
            "final_answer": (
                "Извини, не смог выполнить запрос за отведённые шаги. Попробуй переформулировать."
            ),
            "error": "max_iterations_exceeded",
        }

    messages: list[dict] = list(state.get("messages") or [])

    # Первая итерация — строим messages из нуля
    if not messages:
        system_content = _build_system_prompt(state)
        prior: list[dict] = list(state.get("prior_messages") or [])

        if prior:
            # Продолжение диалога
            messages = [
                {"role": "system", "content": system_content},
                *prior,
                {"role": "user", "content": state["raw_input"]},
            ]
            logger.info(
                "react_reasoner_context_restored",
                user_id=user_id,
                prior_messages=len(prior),
            )
        else:
            # Новый диалог
            messages = [
                {"role": "system", "content": system_content},
                {"role": "user", "content": state["raw_input"]},
            ]

    client = get_raw_llm_client()

    generation = node_generation(
        trace_id=trace_id,
        name=f"react_reasoner_iter_{iteration_count + 1}",
        model=settings.mistral_model,
        model_parameters={
            "temperature": 0.1,
            "max_tokens": settings.llm_max_tokens,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
        },
        messages=messages,
        metadata={
            "iteration": iteration_count + 1,
            "tools_count": len(REACT_TOOL_DEFINITIONS),
            "user_id": user_id,
        },
    )

    response = None
    try:
        response = await client.chat.completions.create(
            model=settings.mistral_model,
            messages=messages,
            tools=REACT_TOOL_DEFINITIONS,
            tool_choice="auto",
            parallel_tool_calls=False,
            temperature=0.1,
            max_tokens=settings.llm_max_tokens,
        )
    except Exception as exc:
        logger.error("react_reasoner_llm_error", user_id=user_id, error=str(exc))
        if generation:
            try:
                generation.end(level="ERROR", status_message=str(exc))
            except Exception:
                pass
        return {
            "final_answer": "Не удалось обработать запрос. Попробуй ещё раз.",
            "error": str(exc),
        }

    choice = response.choices[0]

    usage_data: dict | None = None
    if hasattr(response, "usage") and response.usage:
        usage_data = {
            "input": response.usage.prompt_tokens,
            "output": response.usage.completion_tokens,
            "total": response.usage.total_tokens,
            "unit": "TOKENS",
        }

    # Tool call
    if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
        tool_call = choice.message.tool_calls[0]

        assistant_message: dict = {
            "role": "assistant",
            "content": choice.message.content,
            "tool_calls": [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                },
            ],
        }

        try:
            arguments = json.loads(tool_call.function.arguments)
        except (json.JSONDecodeError, TypeError):
            arguments = {}

        if generation:
            try:
                generation.end(
                    output=assistant_message,
                    usage=usage_data,
                    metadata={
                        "finish_reason": "tool_calls",
                        "tool_name": tool_call.function.name,
                        "tool_calls_returned": len(choice.message.tool_calls),
                        "tool_calls_recorded": 1,
                    },
                )
            except Exception:
                pass

        logger.info(
            "react_reasoner_tool_call",
            user_id=user_id,
            tool=tool_call.function.name,
            iteration=iteration_count + 1,
        )

        return {
            "messages": messages + [assistant_message],
            "pending_tool_call": {
                "id": tool_call.id,
                "name": tool_call.function.name,
                "arguments": arguments,
            },
            "tool_call_id": tool_call.id,
            "iteration_count": iteration_count + 1,
            "final_answer": None,
            "error": None,
        }

    final_text = choice.message.content or "Готово!"

    assistant_message = {
        "role": "assistant",
        "content": final_text,
    }

    if generation:
        try:
            generation.end(
                output={"content": final_text},
                usage=usage_data,
                metadata={"finish_reason": "stop", "iterations_total": iteration_count},
            )
        except Exception:
            pass

    logger.info(
        "react_reasoner_final_answer",
        user_id=user_id,
        iteration=iteration_count,
        answer_preview=final_text[:80],
    )

    return {
        "final_answer": final_text,
        "messages": messages + [assistant_message],
        "pending_tool_call": None,
        "error": None,
    }


async def respond_node(state: ReactAgentState) -> dict:
    """
    Финальный узел: отправляет final_answer пользователю и сохраняет в историю.
    Вызывается когда LLM дал ответ без tool_call (или после ask_user).
    """
    user_id = state["user_id"]
    conversation_id = state.get("conversation_id")
    text = md_to_html(state.get("final_answer") or "Готово!")
    trace_id = state.get("langfuse_trace_id") or get_current_trace_id()

    span = node_span(
        trace_id,
        "react_respond",
        {
            "answer_preview": text[:200],
            "iteration_count": state.get("iteration_count"),
            "error": state.get("error"),
        },
    )

    await notify_user(NotifyInput(user_id=user_id, text=text))

    try:
        await add_conversation_message(user_id, "assistant", text, conversation_id=conversation_id)
    except Exception as exc:
        logger.warning("respond_node_history_save_failed", user_id=user_id, error=str(exc))

    if span:
        try:
            span.end(output={"delivered": True})
        except Exception:
            pass

    logger.info("respond_node_done", user_id=user_id, answer_preview=text[:80])
    return {"final_answer": text}
