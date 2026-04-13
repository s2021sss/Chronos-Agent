"""
ReAct Agent Graph

Граф:
  START -> reasoner
  reasoner -> [route_from_reasoner] -> tool_router | respond
  tool_router -> [route_from_tool_router] -> read_tool_executor | hitl_wait | respond
  read_tool_executor -> reasoner          (loop: read -> reason -> read -> ...)
  hitl_wait ← interrupt_before          (граф прерывается здесь)
  hitl_wait -> [route_from_hitl_wait] -> write_tool_executor | respond | reasoner
  write_tool_executor -> respond          (фиксированный ответ)
  respond -> END

HITL-механизм:
  1. reasoner выбирает side-effect tool -> tool_router отправляет confirmation keyboard
  2. Граф прерывается ПЕРЕД hitl_wait, state сохраняется в PostgreSQL checkpoint.
  3. Пользователь нажимает ✅ или ❌ -> AgentCore.resume_react(thread_id, confirmed).
  4. При confirmed=True -> write_tool_executor выполняет действие -> reasoner получает результат.
  5. При confirmed=False -> react_hitl_wait добавляет "cancelled_by_user" в messages -> reasoner.
"""

from langgraph.graph import END, StateGraph

from chronos_agent.agent.nodes.react_hitl import react_hitl_wait_node, tool_router_node
from chronos_agent.agent.nodes.react_reasoner import reasoner_node, respond_node
from chronos_agent.agent.nodes.react_tool_executor import (
    read_tool_executor_node,
    write_tool_executor_node,
)
from chronos_agent.agent.react_state import ReactAgentState
from chronos_agent.agent.react_tools import READ_ONLY_TOOLS, SIDE_EFFECT_TOOLS
from chronos_agent.logging import get_logger

logger = get_logger(__name__)


def route_from_reasoner(state: ReactAgentState) -> str:
    """
    После reasoner:
      - final_answer установлен -> respond
      - pending_tool_call установлен -> tool_router
      - иначе -> respond (fallback)
    """
    if state.get("final_answer"):
        return "respond"
    if state.get("pending_tool_call"):
        return "tool_router"
    return "respond"


def route_from_tool_router(state: ReactAgentState) -> str:
    """
    После tool_router:
      - final_answer установлен (ask_user или ошибка) -> respond
      - pending_tool_call.name в READ_ONLY_TOOLS -> read_tool_executor
      - pending_tool_call.name в SIDE_EFFECT_TOOLS -> hitl_wait (interrupt перед ним)
      - иначе -> respond (fallback)
    """
    if state.get("final_answer"):
        return "respond"

    tool_call = state.get("pending_tool_call")
    if not tool_call:
        return "respond"

    name = tool_call.get("name", "")
    if name in READ_ONLY_TOOLS:
        return "read_tool_executor"
    if name in SIDE_EFFECT_TOOLS:
        return "hitl_wait"

    return "respond"


def route_from_hitl_wait(state: ReactAgentState) -> str:
    """
    После hitl_wait (только при resume):
      - confirmed=True  -> write_tool_executor
      - confirmed=False + final_answer установлен -> respond (фиксированный шаблон отмены)
      - иначе -> reasoner (fallback)
    """
    if state.get("confirmed") is True:
        return "write_tool_executor"
    if state.get("final_answer"):
        return "respond"
    return "reasoner"


def build_react_graph(checkpointer):
    """
    Строит и компилирует ReAct LangGraph StateGraph.

    checkpointer: AsyncPostgresSaver — PostgreSQL checkpoint.
    Возвращает CompiledStateGraph.
    """
    graph = StateGraph(ReactAgentState)

    graph.add_node("reasoner", reasoner_node)
    graph.add_node("tool_router", tool_router_node)
    graph.add_node("read_tool_executor", read_tool_executor_node)
    graph.add_node("hitl_wait", react_hitl_wait_node)
    graph.add_node("write_tool_executor", write_tool_executor_node)
    graph.add_node("respond", respond_node)

    graph.set_entry_point("reasoner")

    # reasoner -> tool_router | respond
    graph.add_conditional_edges(
        "reasoner",
        route_from_reasoner,
        {"tool_router": "tool_router", "respond": "respond"},
    )

    # tool_router -> read_tool_executor | hitl_wait | respond
    graph.add_conditional_edges(
        "tool_router",
        route_from_tool_router,
        {
            "read_tool_executor": "read_tool_executor",
            "hitl_wait": "hitl_wait",
            "respond": "respond",
        },
    )

    # read_tool_executor -> reasoner (loop)
    graph.add_edge("read_tool_executor", "reasoner")

    # hitl_wait -> write_tool_executor | respond (отмена) | reasoner (fallback)
    graph.add_conditional_edges(
        "hitl_wait",
        route_from_hitl_wait,
        {
            "write_tool_executor": "write_tool_executor",
            "respond": "respond",
            "reasoner": "reasoner",
        },
    )

    # write_tool_executor -> respond (фиксированный ответ)
    graph.add_edge("write_tool_executor", "respond")

    # respond -> END
    graph.add_edge("respond", END)

    compiled = graph.compile(
        checkpointer=checkpointer,
        interrupt_before=["hitl_wait"],
    )

    logger.info("react_graph_compiled")
    return compiled
