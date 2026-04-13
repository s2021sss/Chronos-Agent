"""
LLM-клиент для ReAct reasoner.

Возвращает сырой OpenAI-совместимый AsyncOpenAI клиент. Он нужен ReAct-графу
для нативных tool_calls. Если LLM_PROXY_ENABLED=true, трафик идёт через
LiteLLM Proxy; иначе агент обращается напрямую к Mistral.
"""

from openai import AsyncOpenAI

from chronos_agent.config import settings
from chronos_agent.logging import get_logger

logger = get_logger(__name__)

_raw_client: AsyncOpenAI | None = None


def get_raw_llm_client() -> AsyncOpenAI:
    """
    Возвращает AsyncOpenAI клиент для ReAct function calling API.
    """
    global _raw_client
    if _raw_client is None:
        if settings.llm_proxy_enabled and settings.llm_proxy_url:
            _raw_client = AsyncOpenAI(
                api_key=settings.llm_proxy_api_key,
                base_url=settings.llm_proxy_url,
            )
        else:
            _raw_client = AsyncOpenAI(
                api_key=settings.mistral_api_key,
                base_url=settings.mistral_base_url,
            )
        logger.info(
            "raw_llm_client_initialized",
            provider="llm_proxy" if settings.llm_proxy_enabled else "mistral",
        )
    return _raw_client
