"""
react_hitl — HITL-узлы для ReAct агента.

tool_router_node:
  Диспетчер pending_tool_call:
  - READ_ONLY_TOOLS  -> возвращает route "read_tool_executor"
  - SIDE_EFFECT_TOOLS -> отправляет confirmation keyboard, устанавливает
                        awaiting_confirmation=True, route -> "hitl_wait"
  - TERMINAL_TOOLS   -> устанавливает final_answer из аргумента question, route -> "respond"
  - Неизвестный инструмент -> устанавливает final_answer с ошибкой

react_hitl_wait_node:
  Исполняется только при resume (граф прерывается ПЕРЕД этим узлом).
  Если confirmed=False — добавляет cancellation tool message в messages,
  чтобы reasoner знал что действие было отменено.
  Если confirmed=True — просто проходит, граф идёт к write_tool_executor.
"""

import json
from datetime import UTC, datetime

from chronos_agent.agent.react_state import ReactAgentState
from chronos_agent.agent.react_tools import READ_ONLY_TOOLS, SIDE_EFFECT_TOOLS, TERMINAL_TOOLS
from chronos_agent.formatting import fmt_local
from chronos_agent.logging import get_logger
from chronos_agent.observability import get_current_trace_id, node_span
from chronos_agent.tools.notify_user import request_confirmation

logger = get_logger(__name__)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


def _build_confirmation_text(name: str, args: dict, user_timezone: str) -> str:
    """Формирует понятный текст подтверждения для пользователя."""
    match name:
        case "create_event":
            title = args.get("title", "(без названия)")
            start = _parse_dt(args.get("start"))
            end = _parse_dt(args.get("end"))
            start_str = fmt_local(start, user_timezone) if start else "?"
            end_str = fmt_local(end, user_timezone) if end else "?"
            return (
                f"📅 <b>Создать событие?</b>\n\n"
                f"«{title}»\n"
                f"🕐 {start_str} — {end_str}\n\n"
                f"Подтвердить создание?"
            )

        case "create_task":
            title = args.get("title", "(без названия)")
            due = _parse_dt(args.get("due_date"))
            due_str = f"\n📆 Срок: {fmt_local(due, user_timezone)}" if due else ""
            return f"📋 <b>Создать задачу?</b>\n\n«{title}»{due_str}\n\nПодтвердить создание?"

        case "move_event":
            title = args.get("event_title", "(без названия)")
            new_start = _parse_dt(args.get("new_start"))
            new_end = _parse_dt(args.get("new_end"))
            start_str = fmt_local(new_start, user_timezone) if new_start else "?"
            end_str = f" — {fmt_local(new_end, user_timezone)}" if new_end else ""
            return (
                f"🔄 <b>Перенести событие?</b>\n\n"
                f"📅 «{title}»\n"
                f"➡️ Новое время: {start_str}{end_str}\n\n"
                f"Подтвердить перенос?"
            )

        case "complete_task":
            title = args.get("task_title", "(без названия)")
            return f"✅ <b>Отметить как выполненную?</b>\n\n📋 «{title}»\n\nПодтвердить выполнение?"

        case _:
            return f"Подтвердить действие «{name}»?"


async def tool_router_node(state: ReactAgentState) -> dict:
    """
    Диспетчеризует tool_call после reasoner_node.

    - READ_ONLY -> без изменений в state (маршрут определяется в route_from_tool_router)
    - SIDE_EFFECT -> отправляет confirmation keyboard, awaiting_confirmation=True
    - TERMINAL (ask_user) -> переводит вопрос в final_answer
    - Неизвестный -> устанавливает final_answer с сообщением об ошибке
    """
    tool_call = state.get("pending_tool_call") or {}
    name = tool_call.get("name", "")
    args = tool_call.get("arguments") or {}
    user_id = state["user_id"]
    trace_id = state.get("langfuse_trace_id") or get_current_trace_id()

    span = node_span(
        trace_id,
        "tool_router",
        {"tool": name, "args": args, "iteration": state.get("iteration_count")},
    )

    if name in READ_ONLY_TOOLS:
        if span:
            try:
                span.end(output={"route": "read_tool_executor"})
            except Exception:
                pass
        logger.info("tool_router_read_only", user_id=user_id, tool=name)
        return {"pending_tool_call": tool_call}

    if name in SIDE_EFFECT_TOOLS:
        user_timezone = state.get("user_timezone") or "UTC"
        conversation_id = state.get("conversation_id")

        if conversation_id is not None:
            thread_id = f"user:{user_id}:conv:{conversation_id}"
        else:
            thread_id = f"user:{user_id}"

        confirmation_text = _build_confirmation_text(name, args, user_timezone)

        await request_confirmation(
            user_id=user_id,
            text=confirmation_text,
            thread_id=thread_id,
        )

        if span:
            try:
                span.end(output={"route": "hitl_wait", "confirmation_sent": True})
            except Exception:
                pass
        logger.info(
            "tool_router_hitl_sent",
            user_id=user_id,
            tool=name,
            thread_id=thread_id,
        )
        return {"awaiting_confirmation": True}

    if name in TERMINAL_TOOLS:
        question = args.get("question", "Нужна дополнительная информация.")
        if span:
            try:
                span.end(output={"route": "respond", "question_preview": question[:100]})
            except Exception:
                pass
        logger.info("tool_router_ask_user", user_id=user_id, question_preview=question[:80])
        return {"final_answer": question, "pending_tool_call": None}

    if span:
        try:
            span.end(output={"route": "respond", "error": f"unknown_tool:{name}"}, level="WARNING")
        except Exception:
            pass
    logger.warning("tool_router_unknown_tool", user_id=user_id, tool=name)
    return {
        "final_answer": f"Не могу выполнить неизвестный инструмент «{name}».",
        "pending_tool_call": None,
    }


def _build_cancellation_followup(name: str, args: dict) -> str:
    """
    Фиксированный шаблон ответа после того, как пользователь нажал ❌.

    Отвечает на вопрос «что делать дальше?» в зависимости от отменённого действия.
    Результат уже прошёл через md_to_html в respond_node, поэтому здесь пишем
    обычный текст без HTML-тегов.
    """
    match name:
        case "create_event":
            title = args.get("title", "Событие")
            return (
                f"«{title}» не создано.\n"
                "Хочешь указать другое время? Напиши, например: «в 21:30» или «завтра утром»."
            )
        case "create_task":
            title = args.get("title", "Задача")
            return (
                f"«{title}» не добавлена.\n"
                "Хочешь изменить срок? Напиши, например: «к пятнице» или «на следующей неделе»."
            )
        case "move_event":
            title = args.get("event_title", "Событие")
            return f"Перенос «{title}» отменён.\nХочешь выбрать другое время? Напиши когда."
        case "complete_task":
            title = args.get("task_title", "Задача")
            return f"«{title}» остаётся активной."
        case _:
            return "Действие не выполнено. Напиши что нужно сделать."


async def react_hitl_wait_node(state: ReactAgentState) -> dict:
    """
    Выполняется только при resume (граф прерывается ПЕРЕД этим узлом).

    confirmed=True  -> граф идёт к write_tool_executor.
    confirmed=False -> добавляем cancellation tool message в messages + фиксированный
                      текст-шаблон в final_answer -> граф идёт к respond (не к reasoner).
                      Это обеспечивает единый формат ответа при отмене.
    """
    user_id = state["user_id"]
    tool_call = state.get("pending_tool_call") or {}
    tool_name = tool_call.get("name", "unknown")
    tool_args = tool_call.get("arguments") or {}
    confirmed = state.get("confirmed")
    trace_id = state.get("langfuse_trace_id") or get_current_trace_id()

    span = node_span(
        trace_id,
        "react_hitl_resume",
        {"tool": tool_name, "confirmed": confirmed},
    )

    if confirmed is not True:
        tool_call_id = state.get("tool_call_id") or ""

        cancel_message = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": json.dumps({"status": "cancelled_by_user"}),
        }

        followup_text = _build_cancellation_followup(tool_name, tool_args)

        assistant_followup = {"role": "assistant", "content": followup_text}

        if span:
            try:
                span.end(output={"route": "respond", "confirmed": False, "tool": tool_name})
            except Exception:
                pass
        logger.info("react_hitl_rejected", user_id=user_id, tool=tool_name)

        return {
            "messages": list(state.get("messages") or []) + [cancel_message, assistant_followup],
            "final_answer": followup_text,
            "pending_tool_call": None,
            "awaiting_confirmation": False,
            "confirmed": None,
        }

    if span:
        try:
            span.end(output={"route": "write_tool_executor", "confirmed": True})
        except Exception:
            pass
    logger.info("react_hitl_confirmed", user_id=user_id, tool=tool_name)
    return {"awaiting_confirmation": False}
