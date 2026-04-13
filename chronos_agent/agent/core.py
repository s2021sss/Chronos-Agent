"""
AgentCore — фасад для запуска и возобновления ReAct LangGraph графа.

Публичный API:
  AgentCore.init(conn_string)        — инициализация checkpointer + граф (startup)
  AgentCore.close()                  — закрытие соединения с PostgreSQL (shutdown)
  AgentCore.run(user_id, trigger, raw_input, conversation_id) — запуск прохода
  AgentCore.resume(thread_id, confirmed)      — возобновление после HITL

thread_id:
  Формат "user:{user_id}:conv:{conversation_id}" — один поток на диалог.
"""

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import select

from chronos_agent.agent.react_graph import build_react_graph
from chronos_agent.agent.react_state import ReactAgentState
from chronos_agent.db.engine import get_session
from chronos_agent.db.models import User
from chronos_agent.logging import get_logger
from chronos_agent.observability import get_langfuse, set_current_trace_id

_MAX_PRIOR_CONTEXT_MESSAGES = 30

logger = get_logger(__name__)


def _parse_thread_id(thread_id: str) -> tuple[str, int | None]:
    """
    Разбирает thread_id на (user_id, conversation_id).

    Форматы:
      "user:{uid}:conv:{cid}" -> (uid, cid)   — формат с conversation
      "user:{uid}"            -> (uid, None)   — legacy формат
    """
    if ":conv:" in thread_id:
        uid_part, conv_part = thread_id[len("user:") :].split(":conv:", 1)
        try:
            return uid_part, int(conv_part)
        except ValueError:
            return uid_part, None
    return thread_id[len("user:") :], None


class AgentCore:
    _react_graph = None
    _pg_conn: psycopg.AsyncConnection | None = None
    _checkpointer: AsyncPostgresSaver | None = None

    @classmethod
    async def init(cls, conn_string: str) -> None:
        """
        Создаёт psycopg-соединение, инициализирует AsyncPostgresSaver,
        создаёт таблицы checkpoint (если не существуют), компилирует граф.

        conn_string: postgresql://user:pass@host:port/dbname (без asyncpg driver)
        """
        if cls._react_graph is not None:
            logger.warning("agent_core_already_initialized")
            return

        cls._pg_conn = await psycopg.AsyncConnection.connect(
            conn_string,
            autocommit=True,
            prepare_threshold=0,
        )
        cls._checkpointer = AsyncPostgresSaver(cls._pg_conn)

        await cls._checkpointer.setup()

        cls._react_graph = build_react_graph(cls._checkpointer)

        logger.info(
            "agent_core_initialized",
            conn_string=conn_string[:40] + "...",
        )

    @classmethod
    async def close(cls) -> None:
        """Закрывает PostgreSQL-соединение при shutdown."""
        if cls._pg_conn is not None:
            await cls._pg_conn.close()
            cls._pg_conn = None
            cls._react_graph = None
            cls._checkpointer = None
            logger.info("agent_core_closed")

    @classmethod
    async def run(
        cls,
        user_id: str,
        trigger: str,
        raw_input: str,
        conversation_id: int | None = None,
    ) -> str:
        """
        Запускает новый проход ReAct графа для сообщения пользователя.

        user_id:         Telegram user ID
        trigger:         "text_message" | "voice_message"
        raw_input:       текст (или транскрипт голосового)
        conversation_id: ID диалога из таблицы conversations (None — legacy)

        Возвращает статус: "completed" | "awaiting_confirmation" | "error"
        """
        if cls._react_graph is None:
            raise RuntimeError("AgentCore not initialized. Call AgentCore.init() at startup.")
        return await cls._run_react(user_id, trigger, raw_input, conversation_id)

    @classmethod
    async def _run_react(
        cls,
        user_id: str,
        trigger: str,
        raw_input: str,
        conversation_id: int | None,
    ) -> str:
        """Запускает ReAct граф (reasoner -> tool_router -> ...)."""
        if conversation_id is not None:
            thread_id = f"user:{user_id}:conv:{conversation_id}"
            langfuse_session_id = f"conv:{conversation_id}"
        else:
            thread_id = f"user:{user_id}"
            langfuse_session_id = f"user:{user_id}"

        user_timezone = await cls._get_user_timezone(user_id)

        lf = get_langfuse()
        trace = None
        ctx_token = None
        if lf is not None:
            trace = lf.trace(
                name="agent_run",
                user_id=user_id,
                session_id=langfuse_session_id,
                input={"text": raw_input[:500], "trigger": trigger},
                tags=[trigger, "react"],
                metadata={
                    "thread_id": thread_id,
                    "conversation_id": conversation_id,
                },
            )
            ctx_token = set_current_trace_id(trace.id)

        # Восстановление контекста из checkpoint
        config = {"configurable": {"thread_id": thread_id}}

        def _extract_messages(vals: dict) -> list[dict]:
            """Из checkpoint state возвращает сообщения без system prompt.
            Если ход прерван (awaiting_confirmation/pending_tool_call) — пустой список."""
            if vals.get("awaiting_confirmation") or vals.get("pending_tool_call"):
                return []
            return [m for m in (vals.get("messages") or []) if m.get("role") != "system"]

        # Текущий диалог
        current_msgs: list[dict] = []
        try:
            cur_state = await cls._react_graph.aget_state(config)
            if cur_state and cur_state.values:
                current_msgs = _extract_messages(cur_state.values)
        except Exception as exc:
            logger.warning("react_prior_context_load_failed", user_id=user_id, error=str(exc))

        # Предыдущий диалог
        prev_msgs: list[dict] = []
        if conversation_id is not None:
            try:
                from chronos_agent.db.models import Conversation

                async with get_session() as session:
                    prev_result = await session.execute(
                        select(Conversation)
                        .where(
                            Conversation.user_id == user_id,
                            Conversation.id < conversation_id,
                        )
                        .order_by(Conversation.id.desc())
                        .limit(1)
                    )
                    prev_conv = prev_result.scalar_one_or_none()

                if prev_conv is not None:
                    prev_thread_id = f"user:{user_id}:conv:{prev_conv.id}"
                    prev_config = {"configurable": {"thread_id": prev_thread_id}}
                    prev_state = await cls._react_graph.aget_state(prev_config)
                    if prev_state and prev_state.values:
                        prev_msgs = _extract_messages(prev_state.values)
            except Exception as exc:
                logger.warning(
                    "react_prior_prev_context_load_failed", user_id=user_id, error=str(exc)
                )

        sliced = (prev_msgs + current_msgs)[-_MAX_PRIOR_CONTEXT_MESSAGES:]
        while sliced and sliced[0].get("role") != "user":
            sliced = sliced[1:]
        prior_messages = sliced

        initial_state: ReactAgentState = {
            "user_id": user_id,
            "trigger": trigger,
            "raw_input": raw_input,
            "user_timezone": user_timezone,
            "conversation_id": conversation_id,
            "messages": [],
            "prior_messages": prior_messages,
            "pending_tool_call": None,
            "tool_call_id": None,
            "awaiting_confirmation": False,
            "confirmed": None,
            "iteration_count": 0,
            "final_answer": None,
            "langfuse_trace_id": trace.id if trace is not None else None,
            "error": None,
        }

        logger.info(
            "agent_run_started",
            user_id=user_id,
            trigger=trigger,
            conversation_id=conversation_id,
            prior_context=len(prior_messages),
            text_preview=raw_input[:60],
        )

        return await cls._invoke_graph(
            initial_state,
            config,
            trace,
            lf,
            ctx_token,
            user_id,
            conversation_id,
        )

    @classmethod
    async def _invoke_graph(
        cls,
        initial_state,
        config: dict,
        trace,
        lf,
        ctx_token,
        user_id: str,
        conversation_id: int | None,
    ) -> str:
        """Общий код запуска графа с обработкой Langfuse и conversation status."""
        result = None
        run_status = "completed"
        try:
            result = await cls._react_graph.ainvoke(initial_state, config=config)
            if result and result.get("awaiting_confirmation"):
                run_status = "awaiting_confirmation"
                if conversation_id is not None:
                    try:
                        from chronos_agent.memory.session import update_conversation_status

                        await update_conversation_status(conversation_id, "awaiting_user")
                    except Exception as exc:
                        logger.warning(
                            "agent_run_status_update_failed",
                            user_id=user_id,
                            error=str(exc),
                        )
            elif result and result.get("error"):
                run_status = "error"
        finally:
            if trace is not None and lf is not None:
                try:
                    if run_status == "error":
                        trace.update(
                            output={"status": "error", "error": (result or {}).get("error")},
                            level="ERROR",
                        )
                    elif run_status == "awaiting_confirmation":
                        trace.update(output={"status": "awaiting_confirmation"})
                    else:
                        trace.update(output={"status": "completed"})
                    lf.flush()
                except Exception as exc:
                    logger.warning(
                        "agent_run_observability_update_failed",
                        user_id=user_id,
                        error=str(exc),
                    )
            if ctx_token is not None:
                from chronos_agent.observability import _current_trace_id

                _current_trace_id.reset(ctx_token)

        logger.info("agent_run_finished", user_id=user_id, status=run_status)
        return run_status

    @classmethod
    async def resume(cls, thread_id: str, confirmed: bool) -> None:
        """
        Возобновляет ReAct граф после HITL-подтверждения.

        thread_id: "user:{user_id}:conv:{conv_id}" — из callback_data кнопки
        confirmed: True (✅) или False (❌)
        """
        if cls._react_graph is None:
            raise RuntimeError("AgentCore not initialized. Call AgentCore.init() at startup.")

        config = {"configurable": {"thread_id": thread_id}}
        user_id, conversation_id = _parse_thread_id(thread_id)

        await cls._react_graph.aupdate_state(config, {"confirmed": confirmed, "iteration_count": 0})

        if conversation_id is not None:
            try:
                from chronos_agent.memory.session import update_conversation_status

                await update_conversation_status(conversation_id, "active")
            except Exception as exc:
                logger.warning(
                    "agent_resume_status_update_failed",
                    user_id=user_id,
                    error=str(exc),
                )
        logger.info(
            "agent_resume_started",
            user_id=user_id,
            thread_id=thread_id,
            confirmed=confirmed,
        )

        lf = get_langfuse()
        resume_span = None
        ctx_token = None
        if lf is not None:
            checkpoint_state = await cls._react_graph.aget_state(config)
            original_trace_id: str | None = (checkpoint_state.values or {}).get("langfuse_trace_id")

            if original_trace_id:
                resume_span = lf.span(
                    trace_id=original_trace_id,
                    name="hitl_resume",
                    input={"confirmed": confirmed},
                    metadata={"thread_id": thread_id},
                )
                ctx_token = set_current_trace_id(original_trace_id)
            else:
                fallback_trace = lf.trace(
                    name="agent_resume",
                    user_id=user_id,
                    session_id=f"user:{user_id}",
                    input={"confirmed": confirmed},
                    tags=["hitl_resume"],
                    metadata={"thread_id": thread_id},
                )
                resume_span = fallback_trace
                ctx_token = set_current_trace_id(fallback_trace.id)

        result = None
        try:
            result = await cls._react_graph.ainvoke(None, config=config)
        finally:
            if resume_span is not None and lf is not None:
                try:
                    if result and result.get("error"):
                        resume_span.end(
                            output={
                                "status": "error",
                                "confirmed": confirmed,
                                "error": result["error"],
                            }
                        )
                    else:
                        resume_span.end(output={"status": "resumed", "confirmed": confirmed})
                    lf.flush()
                except Exception as exc:
                    logger.warning(
                        "agent_resume_observability_update_failed",
                        user_id=user_id,
                        error=str(exc),
                    )
            if ctx_token is not None:
                from chronos_agent.observability import _current_trace_id

                _current_trace_id.reset(ctx_token)

        logger.info("agent_resume_finished", user_id=user_id, confirmed=confirmed)

    @classmethod
    async def recover_orphaned_sessions(cls) -> None:
        """
        Находит HITL-сессии, прерванные до перезапуска сервиса, и завершает их.

        Ищет thread_id в checkpoint, ожидающие hitl_wait.
        """
        if cls._react_graph is None or cls._pg_conn is None:
            logger.warning("orphan_recovery_skipped", reason="AgentCore not initialized")
            return

        try:
            async with cls._pg_conn.cursor() as cur:
                await cur.execute(
                    "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE 'user:%'"
                )
                rows = await cur.fetchall()
        except Exception as exc:
            logger.warning("orphan_recovery_query_failed", error=str(exc))
            return

        if not rows:
            logger.info("orphan_recovery_no_threads")
            return

        recovered = 0
        for (thread_id,) in rows:
            try:
                config = {"configurable": {"thread_id": thread_id}}
                state = await cls._react_graph.aget_state(config)

                next_nodes = state.next or ()
                if "hitl_wait" not in next_nodes:
                    continue

                user_id, conversation_id = _parse_thread_id(thread_id)
                logger.info("orphan_session_found", thread_id=thread_id, user_id=user_id)

                try:
                    from chronos_agent.bot.texts import T
                    from chronos_agent.tools.notify_user import NotifyInput, notify_user

                    await notify_user(NotifyInput(user_id=user_id, text=T.confirmation_expired))
                except Exception as notify_exc:
                    logger.warning(
                        "orphan_recovery_notify_failed",
                        user_id=user_id,
                        error=str(notify_exc),
                    )

                if conversation_id is not None:
                    try:
                        from chronos_agent.memory.session import update_conversation_status

                        await update_conversation_status(conversation_id, "expired")
                    except Exception as conv_exc:
                        logger.warning(
                            "orphan_recovery_conv_close_failed",
                            conversation_id=conversation_id,
                            error=str(conv_exc),
                        )

                await cls.resume(thread_id=thread_id, confirmed=False)
                recovered += 1

            except Exception as exc:
                logger.warning("orphan_recovery_failed", thread_id=thread_id, error=str(exc))

        logger.info("orphan_recovery_done", recovered=recovered, checked=len(rows))

    @classmethod
    async def _get_user_timezone(cls, user_id: str) -> str:
        """Читает timezone пользователя из БД. Fallback: UTC."""
        try:
            async with get_session() as session:
                result = await session.execute(select(User.timezone).where(User.user_id == user_id))
                tz = result.scalar_one_or_none()
                return tz or "UTC"
        except Exception as exc:
            logger.warning("agent_get_timezone_failed", user_id=user_id, error=str(exc))
            return "UTC"
