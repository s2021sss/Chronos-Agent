"""
react_tool_executor — узлы выполнения инструментов ReAct агента.

read_tool_executor_node:
  Выполняет read-only tool (get_conversation_history, get_calendar_events,
  find_free_slots, get_pending_tasks). Добавляет результат в messages.
  Всегда передаёт управление обратно reasoner_node.

write_tool_executor_node:
  Выполняет side-effect tool после подтверждения пользователем (confirmed=True).
  Добавляет результат в messages и передаёт управление reasoner_node.
  Reasoner видит результат и отправляет финальный ответ пользователю.
"""

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from chronos_agent.agent.react_state import ReactAgentState
from chronos_agent.db.engine import get_session
from chronos_agent.db.models import CalendarEvent, CalendarTask
from chronos_agent.formatting import fmt_local
from chronos_agent.logging import get_logger
from chronos_agent.memory.conversation import get_conversation_history
from chronos_agent.observability import get_current_trace_id, node_span
from chronos_agent.tools.complete_task import complete_task
from chronos_agent.tools.create_event import CreateEventInput, create_event
from chronos_agent.tools.create_task import CreateTaskInput, create_task
from chronos_agent.tools.exceptions import CalendarAPIError, OAuthExpiredError, ToolValidationError
from chronos_agent.tools.find_free_slots import find_free_slots
from chronos_agent.tools.get_events import get_events
from chronos_agent.tools.get_tasks import get_tasks
from chronos_agent.tools.move_event import MoveEventInput, move_event

logger = get_logger(__name__)

_MAX_FREE_SLOTS = 3


def _build_success_answer(name: str, result: dict, user_timezone: str) -> str:
    """Фиксированный шаблон ответа после успешного выполнения write-инструмента."""
    match name:
        case "create_event":
            title = result.get("title", "Событие")
            start = _parse_dt(result.get("start"))
            end = _parse_dt(result.get("end"))
            start_str = fmt_local(start, user_timezone) if start else "?"
            end_str = fmt_local(end, user_timezone) if end else "?"
            return f"✅ Событие создано!\n\n📅 **{title}**\n🕐 {start_str} — {end_str}"
        case "create_task":
            title = result.get("title", "Задача")
            due = _parse_dt(result.get("due_date"))
            due_str = f"\n📆 Срок: {fmt_local(due, user_timezone)}" if due else ""
            return f"✅ Задача создана!\n\n📋 **{title}**{due_str}"
        case "move_event":
            title = result.get("title", "Событие")
            new_start = _parse_dt(result.get("new_start"))
            new_end = _parse_dt(result.get("new_end"))
            start_str = fmt_local(new_start, user_timezone) if new_start else "?"
            end_str = f" — {fmt_local(new_end, user_timezone)}" if new_end else ""
            return f"✅ Событие перенесено!\n\n📅 **{title}**\n🕐 {start_str}{end_str}"
        case "complete_task":
            title = result.get("title", "Задача")
            return f"✅ Задача выполнена!\n\n📋 **{title}**"
        case _:
            return "✅ Готово!"


# ---------------------------------------------------------------------------
# Read-only executor
# ---------------------------------------------------------------------------


async def read_tool_executor_node(state: ReactAgentState) -> dict:
    """
    Выполняет read-only tool call и добавляет результат в messages.
    Всегда передаёт управление обратно reasoner_node (loop).
    """
    tool_call = state.get("pending_tool_call") or {}
    name = tool_call.get("name", "")
    args = tool_call.get("arguments") or {}
    tool_call_id = tool_call.get("id") or state.get("tool_call_id") or ""
    user_id = state["user_id"]
    trace_id = state.get("langfuse_trace_id") or get_current_trace_id()

    span = node_span(
        trace_id,
        f"read_tool:{name}",
        {"tool": name, "args": args, "iteration": state.get("iteration_count")},
    )

    try:
        result = await _execute_read_tool(name, args, user_id, state)
    except Exception as exc:
        logger.warning(
            "read_tool_executor_error",
            user_id=user_id,
            tool=name,
            error=str(exc),
        )
        result = {"error": str(exc)[:200]}

    tool_message = {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": name,
        "content": json.dumps(result, ensure_ascii=False, default=str),
    }

    if span:
        try:
            span.end(output={"result_keys": list(result.keys()), "has_error": "error" in result})
        except Exception:
            pass

    logger.info(
        "read_tool_executed",
        user_id=user_id,
        tool=name,
        result_keys=list(result.keys()),
    )

    return {
        "messages": list(state.get("messages") or []) + [tool_message],
        "pending_tool_call": None,
        "tool_call_id": None,
    }


async def _execute_read_tool(name: str, args: dict, user_id: str, state: ReactAgentState) -> dict:
    """Диспетчеризация read-only инструментов."""
    match name:
        case "get_conversation_history":
            limit = min(int(args.get("limit") or 5), 10)
            history = await get_conversation_history(
                user_id,
                limit=limit,
                conversation_id=state.get("conversation_id"),
            )
            return {"messages": history}

        case "get_calendar_events":
            time_min = datetime.fromisoformat(args["time_min"].replace("Z", "+00:00"))
            time_max = datetime.fromisoformat(args["time_max"].replace("Z", "+00:00"))
            if time_min.tzinfo is None:
                time_min = time_min.replace(tzinfo=UTC)
            if time_max.tzinfo is None:
                time_max = time_max.replace(tzinfo=UTC)

            events = await get_events(user_id, time_min, time_max)
            return {
                "events": [
                    {
                        "id": e.gcal_event_id,
                        "title": e.title,
                        "start": e.start_at.isoformat(),
                        "end": e.end_at.isoformat(),
                        "description": e.description,
                    }
                    for e in events
                ]
            }

        case "find_free_slots":
            duration_minutes = int(args["duration_minutes"])
            preferred_start: datetime | None = None
            if args.get("preferred_start"):
                preferred_start = datetime.fromisoformat(
                    args["preferred_start"].replace("Z", "+00:00")
                )
                if preferred_start.tzinfo is None:
                    preferred_start = preferred_start.replace(tzinfo=UTC)

            slots = await find_free_slots(
                user_id=user_id,
                duration_minutes=duration_minutes,
                preferred_start=preferred_start,
            )
            return {
                "slots": [
                    {
                        "start": s.start.isoformat(),
                        "end": s.end.isoformat(),
                        "duration_minutes": s.duration_minutes,
                        "score": round(s.score, 3),
                    }
                    for s in slots[:_MAX_FREE_SLOTS]
                ]
            }

        case "get_pending_tasks":
            task_list = await get_tasks(user_id, status="needsAction")
            return {
                "tasks": [
                    {
                        "id": t.gcal_task_id,
                        "title": t.title,
                        "due_date": t.due_at.isoformat() if t.due_at else None,
                        "notes": t.notes,
                    }
                    for t in task_list
                ]
            }

        case _:
            return {"error": f"Unknown read tool: {name}"}


# ---------------------------------------------------------------------------
# Write (side-effect) executor
# ---------------------------------------------------------------------------


async def write_tool_executor_node(state: ReactAgentState) -> dict:
    """
    Выполняет side-effect tool после подтверждения пользователем.
    Добавляет результат в messages и устанавливает фиксированный final_answer —
    граф идёт напрямую к respond, минуя LLM.
    """
    tool_call = state.get("pending_tool_call") or {}
    name = tool_call.get("name", "")
    args = tool_call.get("arguments") or {}
    tool_call_id = tool_call.get("id") or state.get("tool_call_id") or ""
    user_id = state["user_id"]
    user_timezone = state.get("user_timezone") or "UTC"
    trace_id = state.get("langfuse_trace_id") or get_current_trace_id()

    span = node_span(
        trace_id,
        f"write_tool:{name}",
        {"tool": name, "args": args, "confirmed": state.get("confirmed")},
    )

    error_msg: str | None = None
    try:
        result = await _execute_write_tool(name, args, user_id)
    except ToolValidationError as exc:
        logger.warning("write_tool_validation_error", user_id=user_id, tool=name, error=str(exc))
        result = {"success": False, "error": str(exc)}
        error_msg = str(exc)
    except OAuthExpiredError:
        logger.warning("write_tool_oauth_expired", user_id=user_id)
        msg = "Нет доступа к Google Calendar. Переподключи аккаунт командой /start."
        result = {"success": False, "error": msg}
        error_msg = msg
    except CalendarAPIError as exc:
        logger.error("write_tool_calendar_api_error", user_id=user_id, tool=name, error=str(exc))
        msg = "Ошибка Google API. Попробуй ещё раз."
        result = {"success": False, "error": msg}
        error_msg = msg
    except Exception as exc:
        logger.error("write_tool_unexpected_error", user_id=user_id, tool=name, error=str(exc))
        msg = "Не удалось выполнить действие. Попробуй ещё раз."
        result = {"success": False, "error": msg}
        error_msg = msg

    tool_message = {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": name,
        "content": json.dumps(result, ensure_ascii=False, default=str),
    }

    if result.get("success"):
        final_answer = _build_success_answer(name, result, user_timezone)
    else:
        final_answer = error_msg or "Не удалось выполнить действие."

    if span:
        try:
            span.end(
                output={"success": result.get("success", False), "error": result.get("error")},
                level="ERROR" if not result.get("success") else "DEFAULT",
            )
        except Exception:
            pass

    logger.info(
        "write_tool_executed",
        user_id=user_id,
        tool=name,
        success=result.get("success", False),
    )

    return {
        "messages": list(state.get("messages") or []) + [tool_message],
        "pending_tool_call": None,
        "tool_call_id": None,
        "awaiting_confirmation": False,
        "confirmed": None,
        "final_answer": final_answer,
    }


async def _execute_write_tool(name: str, args: dict, user_id: str) -> dict:
    """Диспетчеризация side-effect инструментов."""
    match name:
        case "create_event":
            start = _parse_dt(args.get("start"))
            end = _parse_dt(args.get("end"))
            if start is None:
                raise ToolValidationError("Не указано время начала события")
            if end is None:
                end = start + timedelta(hours=1)

            gcal_id = await create_event(
                CreateEventInput(
                    user_id=user_id,
                    title=args.get("title", ""),
                    start=start,
                    end=end,
                    description=args.get("description"),
                )
            )
            return {
                "success": True,
                "action": "create_event",
                "gcal_id": gcal_id,
                "title": args.get("title"),
                "start": start.isoformat(),
                "end": end.isoformat(),
            }

        case "create_task":
            due_date = _parse_dt(args.get("due_date"))
            gcal_id = await create_task(
                CreateTaskInput(
                    user_id=user_id,
                    title=args.get("title", ""),
                    due_date=due_date,
                    notes=args.get("notes"),
                )
            )
            return {
                "success": True,
                "action": "create_task",
                "gcal_id": gcal_id,
                "title": args.get("title"),
                "due_date": due_date.isoformat() if due_date else None,
            }

        case "move_event":
            event = await _find_event_by_title(user_id, args.get("event_title", ""))
            if event is None:
                raise ToolValidationError(
                    f"Событие «{args.get('event_title')}» не найдено. "
                    "Проверь название или уточни дату."
                )

            new_start = _parse_dt(args.get("new_start"))
            if new_start is None:
                raise ToolValidationError("Не указано новое время начала")

            if args.get("new_end"):
                new_end = _parse_dt(args["new_end"])
            else:
                duration = int((event.end_at - event.start_at).total_seconds() / 60)
                new_end = new_start + timedelta(minutes=duration)

            await move_event(
                MoveEventInput(
                    user_id=user_id,
                    event_id=event.gcal_event_id,
                    new_start=new_start,
                    new_end=new_end,
                )
            )
            return {
                "success": True,
                "action": "move_event",
                "title": event.title,
                "new_start": new_start.isoformat(),
                "new_end": new_end.isoformat() if new_end else None,
            }

        case "complete_task":
            task = await _find_task_by_title(user_id, args.get("task_title", ""))
            if task is None:
                raise ToolValidationError(
                    f"Задача «{args.get('task_title')}» не найдена. "
                    "Проверь название или список задач командой /status."
                )

            await complete_task(user_id=user_id, task_id=task.gcal_task_id)
            return {
                "success": True,
                "action": "complete_task",
                "title": task.title,
            }

        case _:
            raise ToolValidationError(f"Unknown write tool: {name}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


async def _find_event_by_title(user_id: str, title: str) -> CalendarEvent | None:
    """Ищет ближайшее предстоящее событие по заголовку (case-insensitive LIKE)."""
    if not title:
        return None
    now = datetime.now(UTC)
    window_start = now - timedelta(days=1)
    async with get_session() as session:
        result = await session.execute(
            select(CalendarEvent)
            .where(
                CalendarEvent.user_id == user_id,
                CalendarEvent.title.ilike(f"%{title}%"),
                CalendarEvent.status != "cancelled",
                CalendarEvent.start_at >= window_start,
            )
            .order_by(CalendarEvent.start_at.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def _find_task_by_title(user_id: str, title: str) -> CalendarTask | None:
    """Ищет активную задачу по заголовку (case-insensitive LIKE)."""
    if not title:
        return None
    async with get_session() as session:
        result = await session.execute(
            select(CalendarTask)
            .where(
                CalendarTask.user_id == user_id,
                CalendarTask.title.ilike(f"%{title}%"),
                CalendarTask.status == "needsAction",
            )
            .order_by(CalendarTask.due_at.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()
