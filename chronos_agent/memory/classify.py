"""
Conversation classifier — определяет, является ли входящее сообщение продолжением
активного диалога или началом нового.

Два уровня:
  1. classify_deterministic — детерминированные правила без LLM, O(1)
  2. classify_llm           — LLM fallback для неоднозначных случаев
"""

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from chronos_agent.logging import get_logger

if TYPE_CHECKING:
    from chronos_agent.db.models import Conversation

logger = get_logger(__name__)

_TIMEOUT_NEW = timedelta(minutes=30)
_TIMEOUT_QUICK = timedelta(minutes=2)

_NEW_INTENT_RE = re.compile(
    r"^(создай|запланируй|добавь|поставь|перенеси|отметь|напомни|удали)\s",
    re.IGNORECASE,
)

CLASSIFY_PROMPT = """\
Определи: новое сообщение пользователя — это продолжение текущего разговора или начало нового?

Тема текущего разговора: {topic_summary}
Последние сообщения:
{recent_messages}

Время с последнего сообщения: {silence_minutes} мин.

Новое сообщение: "{raw_input}"

Правила:
- Короткие ответы ("да", "не подходит", "давай в 21:30", "создай событие") -> continue
- Ссылка на предыдущий контекст ("то событие", "этот созвон", "перенеси на час позже") -> continue
- Совершенно новая тема без связи с текущим разговором -> new

Ответь строго JSON без пояснений: \
{{"decision": "continue", "confidence": 0.85, "reason": "short_reply"}}
"""


@dataclass
class ClassifyResult:
    decision: str  # "new" | "continue"
    confidence: float
    reason: str
    used_llm: bool = field(default=False)


def classify_deterministic(
    active_conv: "Conversation | None",
    raw_input: str,
    silence: timedelta,
) -> ClassifyResult | None:
    """
    Возвращает ClassifyResult если уверен, иначе None (нужен LLM fallback).

    Порядок правил (первое совпадение wins):
    1. Нет активного conv -> new (confidence=1.0)
    2. Conv в awaiting_user (HITL/slot) -> continue (confidence=1.0, приоритет!)
    3. Conv completed/expired -> new (confidence=1.0)
    4. Тишина > TIMEOUT_NEW (30 мин) -> new (confidence=0.95)
    5. Сообщение короткое (<50 символов) + тишина < TIMEOUT_QUICK (2 мин) -> continue
    6. Длинное сообщение с явным глаголом нового действия + тишина > 30 с -> new
    7. Иначе -> None (LLM fallback)
    """
    if active_conv is None:
        return ClassifyResult("new", 1.0, "no_active_conv")

    if active_conv.status == "awaiting_user":
        return ClassifyResult("continue", 1.0, "hitl_pending")

    if active_conv.status in ("completed", "expired"):
        return ClassifyResult("new", 1.0, "prev_closed")

    if silence > _TIMEOUT_NEW:
        silence_min = int(silence.total_seconds() / 60)
        return ClassifyResult("new", 0.95, f"timeout_{silence_min}min")

    stripped = raw_input.strip()

    if len(stripped) < 50 and silence < _TIMEOUT_QUICK:
        return ClassifyResult("continue", 0.85, "quick_short_reply")

    if len(stripped) > 30 and _NEW_INTENT_RE.match(stripped):
        if silence > timedelta(seconds=30):
            return ClassifyResult("new", 0.80, "new_intent_keyword")

    return None


async def classify_llm(
    active_conv: "Conversation",
    recent_messages: list[dict],
    raw_input: str,
    silence: timedelta,
) -> ClassifyResult:
    """
    LLM fallback для неоднозначных случаев.
    """
    from openai import AsyncOpenAI

    from chronos_agent.config import settings

    topic = active_conv.topic_summary or "неизвестно"
    silence_minutes = int(silence.total_seconds() / 60)
    msgs_text = (
        "\n".join(f"{m['role'].capitalize()}: {m['content'][:100]}" for m in recent_messages[-3:])
        or "(нет)"
    )

    prompt = CLASSIFY_PROMPT.format(
        topic_summary=topic,
        recent_messages=msgs_text,
        silence_minutes=silence_minutes,
        raw_input=raw_input[:300],
    )

    try:
        if settings.llm_proxy_enabled and settings.llm_proxy_url:
            client = AsyncOpenAI(
                api_key=settings.llm_proxy_api_key,
                base_url=settings.llm_proxy_url,
            )
        else:
            client = AsyncOpenAI(
                api_key=settings.mistral_api_key,
                base_url=settings.mistral_base_url,
            )

        response = await client.chat.completions.create(
            model=settings.mistral_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=80,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content or "{}"
        data = json.loads(raw)
        decision = data.get("decision", "continue")
        if decision not in ("new", "continue"):
            decision = "continue"
        confidence = float(data.get("confidence", 0.5))
        reason = str(data.get("reason", "llm_classified"))

        logger.info(
            "classify_llm_result",
            decision=decision,
            confidence=confidence,
            reason=reason,
        )
        return ClassifyResult(decision, confidence, reason, used_llm=True)

    except Exception as exc:
        logger.warning("classify_llm_error", error=str(exc))
        return ClassifyResult("continue", 0.5, "llm_error_fallback", used_llm=True)
