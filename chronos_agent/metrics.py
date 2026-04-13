from prometheus_client import (
    REGISTRY,
    Counter,
    Histogram,
    make_asgi_app,
)

_REGISTRY = REGISTRY

agent_requests_total = Counter(
    name="chronos_agent_requests_total",
    documentation="Total number of incoming Telegram messages dispatched to AgentCore",
    labelnames=["trigger"],
    registry=_REGISTRY,
)

agent_request_duration_seconds = Histogram(
    name="chronos_agent_request_duration_seconds",
    documentation="End-to-end AgentCore.run() duration",
    labelnames=["trigger"],
    buckets=(0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0),
    registry=_REGISTRY,
)

agent_actions_total = Counter(
    name="chronos_agent_actions_total",
    documentation="Tool Layer actions executed",
    labelnames=["action_type", "status"],
    registry=_REGISTRY,
)

agent_tool_errors_total = Counter(
    name="chronos_agent_tool_errors_total",
    documentation="Tool Layer errors by type",
    labelnames=["error_type"],
    registry=_REGISTRY,
)

metrics_app = make_asgi_app(registry=_REGISTRY)
