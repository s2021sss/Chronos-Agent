import contextvars
from datetime import UTC

from chronos_agent.config import settings
from chronos_agent.logging import get_logger

logger = get_logger(__name__)

_langfuse = None

_current_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_trace_id", default=None
)


def get_langfuse():
    """
    Возвращает инициализированный Langfuse-клиент или None если не настроен.
    """
    global _langfuse
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return None

    if _langfuse is None:
        try:
            from langfuse import Langfuse

            _langfuse = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                host=settings.langfuse_host,
            )
            logger.info("langfuse_initialized", host=settings.langfuse_host)
        except Exception as exc:
            logger.warning("langfuse_init_failed", error=str(exc))
            return None

    return _langfuse


def get_current_trace_id() -> str | None:
    """Возвращает trace_id текущего запроса из contextvar (None вне запроса)."""
    return _current_trace_id.get()


def set_current_trace_id(trace_id: str | None) -> contextvars.Token:
    """
    Устанавливает trace_id в contextvar.
    Возвращает Token для последующего сброса через reset().
    """
    return _current_trace_id.set(trace_id)


def node_span(trace_id: str | None, name: str, input_data: dict | None = None):
    """
    Создаёт дочерний Langfuse span для узла LangGraph-графа.
    Возвращает объект span (с методом .end()) или None если Langfuse не настроен.
    """
    if not trace_id:
        return None
    lf = get_langfuse()
    if lf is None:
        return None
    try:
        from datetime import datetime

        return lf.span(
            trace_id=trace_id,
            name=name,
            input=input_data or {},
            start_time=datetime.now(UTC),
        )
    except Exception as exc:
        logger.warning("langfuse_span_create_failed", name=name, error=str(exc))
        return None


def node_generation(
    trace_id: str | None,
    name: str,
    model: str,
    model_parameters: dict | None = None,
    messages: list[dict] | None = None,
    metadata: dict | None = None,
):
    """
    Создаёт Langfuse generation span для LLM вызова (отображается с токенами и prompt).
    """
    if not trace_id:
        return None
    lf = get_langfuse()
    if lf is None:
        return None
    try:
        from datetime import datetime

        return lf.generation(
            trace_id=trace_id,
            name=name,
            model=model,
            model_parameters=model_parameters or {},
            input=messages or [],
            metadata=metadata or {},
            start_time=datetime.now(UTC),
        )
    except Exception as exc:
        logger.warning("langfuse_generation_create_failed", name=name, error=str(exc))
        return None
