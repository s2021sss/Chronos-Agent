import asyncio
import json
import logging
import sys
from datetime import UTC
from typing import Any

import structlog

_STRUCTLOG_INTERNAL_KEYS = frozenset(
    {"level", "event", "user_id", "error", "timestamp", "service", "agent_version"}
)


def _json_dumps(event_dict: dict[str, Any], **kwargs: Any) -> str:
    return json.dumps(event_dict, ensure_ascii=False, **kwargs)


def _get_level_from_env() -> str:
    import os

    return os.getenv("LOG_LEVEL", "INFO").upper()


def _get_agent_version() -> str:
    import os

    return os.getenv("AGENT_VERSION", "0.1.0")


def configure_logging() -> None:
    log_level = _get_level_from_env()
    agent_version = _get_agent_version()

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level),
    )

    shared_processors: list[Any] = [
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_service_context(agent_version),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _service_log_writer,
    ]

    structlog.configure(
        processors=shared_processors
        + [
            structlog.processors.JSONRenderer(serializer=_json_dumps),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, log_level)),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def _add_service_context(agent_version: str):
    def processor(logger: Any, method: str, event_dict: dict) -> dict:
        event_dict.setdefault("service", "chronos-agent")
        event_dict.setdefault("agent_version", agent_version)
        return event_dict

    return processor


def _service_log_writer(logger: Any, method: str, event_dict: dict) -> dict:
    level = event_dict.get("level", "").upper()
    if level not in ("ERROR", "CRITICAL"):
        return event_dict

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return event_dict

    extra = {k: v for k, v in event_dict.items() if k not in _STRUCTLOG_INTERNAL_KEYS} or None

    loop.create_task(
        _async_write_service_log(
            level=level,
            event=str(event_dict.get("event", "")),
            user_id=str(event_dict.get("user_id", "")) or None,
            error=str(event_dict.get("error", ""))[:500] or None,
            extra=extra,
            agent_version=str(event_dict.get("agent_version", "")) or None,
        )
    )

    return event_dict


async def _async_write_service_log(
    level: str,
    event: str,
    user_id: str | None,
    error: str | None,
    extra: dict | None,
    agent_version: str | None,
) -> None:
    from datetime import datetime

    try:
        from chronos_agent.db.engine import get_session
        from chronos_agent.db.models import ServiceLog

        async with get_session() as session:
            log = ServiceLog(
                timestamp=datetime.now(UTC),
                level=level,
                event=event,
                user_id=user_id,
                error=error,
                extra=extra,
                agent_version=agent_version,
            )
            session.add(log)
            await session.commit()
    except Exception:
        pass


def get_logger(name: str) -> structlog.BoundLogger:
    return structlog.get_logger(name)
