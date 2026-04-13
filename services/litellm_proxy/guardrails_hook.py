"""
Guardrails — кастомный CustomLogger для LiteLLM Proxy.

Запускается как pre-call hook перед каждым запросом к LLM.
Блокирует запросы, нарушающие политики безопасности.

Проверяются только сообщения с role="user" — системный промпт
не проверяется (зона ответственности агента).
"""

from __future__ import annotations

import re

from litellm.integrations.custom_logger import CustomLogger

# ------------------------------------------------------------------
# 1. Паттерны prompt injection
# ------------------------------------------------------------------
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+your\s+(system\s+)?prompt", re.IGNORECASE),
    re.compile(r"forget\s+(all\s+)?(previous\s+)?instructions", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(DAN|jailbroken|in\s+developer\s+mode)", re.IGNORECASE),
    re.compile(r"print\s+(your\s+)?(system\s+prompt|instructions)", re.IGNORECASE),
    re.compile(
        r"reveal\s+(your\s+)?(system\s+prompt|instructions|training\s+data)",
        re.IGNORECASE,
    ),
    re.compile(
        r"act\s+as\s+if\s+(you\s+have\s+no|there\s+are\s+no)\s+(restrictions|limits)",
        re.IGNORECASE,
    ),
    re.compile(
        r"pretend\s+(you\s+are|to\s+be)\s+(an?\s+)?(unrestricted|unfiltered)",
        re.IGNORECASE,
    ),
    re.compile(r"override\s+(your\s+)?(safety|ethics|guidelines)", re.IGNORECASE),
]

# ------------------------------------------------------------------
# 2. Паттерны утечки секретов / учётных данных
# ------------------------------------------------------------------
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-[A-Za-z0-9]{32,}"),  # OpenAI keys
    re.compile(r"sk-ant-[A-Za-z0-9\-]{40,}"),  # Anthropic keys
    re.compile(r"(AKIA|ASIA)[A-Z0-9]{16}"),  # AWS access keys
    re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}"),  # GitHub tokens
    re.compile(r"-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----"),  # PEM private keys
    re.compile(
        r"(?:password|passwd|secret)\s*[:=]\s*['\"][^'\"]{8,}['\"]",
        re.IGNORECASE,
    ),
    re.compile(
        r"api[_-]?key\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]",
        re.IGNORECASE,
    ),
]

# ------------------------------------------------------------------
# 3. Паттерны PII (Personally Identifiable Information)
#
# Цель: предотвратить отправку персональных данных пользователей в LLM.
# Сигнализируем об обнаружении, не пытаемся быть абсолютно точными —
# лучше false positive, чем утечка реального PII.
# ------------------------------------------------------------------
_PII_PATTERNS: list[re.Pattern[str]] = [
    # Email-адреса
    re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    # Российские мобильные номера: +7 (9XX) XXX-XX-XX, 8-9XX-XXX-XX-XX и др.
    re.compile(r"(?:\+7|8)[\s\-]?\(?9\d{2}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"),
    # Международные телефоны в формате +CC XXXXXXXXXX (CC = 1–3 цифры)
    re.compile(r"\+\d{1,3}[\s\-]\d{3,5}[\s\-]\d{3,5}[\s\-]\d{2,4}\b"),
    # Номера банковских карт (Luhn не проверяем — достаточно структуры 4×4)
    re.compile(r"\b(?:\d{4}[\s\-]){3}\d{4}\b"),
    # Российский паспорт: серия XXXX номер XXXXXX (с пробелом или без)
    re.compile(r"\b\d{4}[\s]?\d{6}\b"),
    # СНИЛС: XXX-XXX-XXX XX или XXXXXXXXXXX
    re.compile(r"\b\d{3}[\-]\d{3}[\-]\d{3}\s\d{2}\b"),
    # ИНН физического лица (12 цифр) / юридического (10 цифр)
    re.compile(r"\bИНН\s*[:№]?\s*\d{10,12}\b", re.IGNORECASE),
]

# ------------------------------------------------------------------
# 4. Паттерны topic restriction
#
# Chronos-Agent — ассистент для управления календарём и задачами.
# Блокируем явно внетематические запросы, которые не имеют отношения
# к планированию, а также запросы на генерацию вредоносного контента.
# ------------------------------------------------------------------
_TOPIC_RESTRICTION_PATTERNS: list[re.Pattern[str]] = [
    # Запросы на создание вредоносного ПО / хакинг
    re.compile(
        r"\b(write|create|generate|build|code)\b.{0,40}\b(malware|virus|trojan|ransomware|keylogger|exploit|payload|backdoor)\b",
        re.IGNORECASE,
    ),
    # Запросы на взлом / получение несанкционированного доступа
    re.compile(
        r"\b(hack|crack|bypass|brute.?force)\b.{0,30}\b(password|account|system|server|database)\b",
        re.IGNORECASE,
    ),
    # Генерация контента для фишинга / социальной инженерии
    re.compile(
        r"\b(phishing|spear.?phishing|social\s+engineering)\b.{0,40}\b(email|message|letter|template)\b",
        re.IGNORECASE,
    ),
    # Запросы на синтез / получение наркотиков, оружия
    re.compile(
        r"\b(synthesize|manufacture|produce|make)\b.{0,30}\b(drugs?|narcotics?|explosives?|weapon)\b",
        re.IGNORECASE,
    ),
]


def _extract_user_text(messages: list[dict]) -> str:
    parts: list[str] = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(part.get("text", ""))
    return "\n".join(parts)


class GuardrailsHook(CustomLogger):
    """
    LiteLLM CustomLogger: блокирует запросы нарушающие политики.

    Порядок проверок:
      1. Prompt injection  → HTTP 400
      2. Secret/credential → HTTP 400
      3. PII               → HTTP 400
      4. Topic restriction → HTTP 400

    Raises litellm.BadRequestError (→ HTTP 400) при нарушении.
    """

    async def async_pre_call_hook(
        self,
        user_api_key_dict,
        cache,
        data: dict,
        call_type: str,
    ) -> dict:
        messages: list[dict] = data.get("messages", [])
        if not messages:
            return data

        text = _extract_user_text(messages)
        if not text:
            return data

        import litellm

        model = data.get("model", "unknown")

        # ── 1. Prompt injection ─────────────────────────────────────────────────
        for pattern in _INJECTION_PATTERNS:
            if pattern.search(text):
                raise litellm.BadRequestError(
                    message="Guardrail violation: potential prompt injection detected.",
                    model=model,
                    llm_provider="",
                )

        # ── 2. Secret / credential leak ─────────────────────────────────────────
        for pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                raise litellm.BadRequestError(
                    message=(
                        "Guardrail violation: potential secret or credential detected in message."
                    ),
                    model=model,
                    llm_provider="",
                )

        # ── 3. PII detection ────────────────────────────────────────────────────
        for pattern in _PII_PATTERNS:
            if pattern.search(text):
                raise litellm.BadRequestError(
                    message="Guardrail violation: personal data (PII) detected in message. "
                    "Do not send personal identifiable information to the assistant.",
                    model=model,
                    llm_provider="",
                )

        # ── 4. Topic restriction ────────────────────────────────────────────────
        for pattern in _TOPIC_RESTRICTION_PATTERNS:
            if pattern.search(text):
                raise litellm.BadRequestError(
                    message="Guardrail violation: request is outside the permitted topic scope "
                    "of this assistant (calendar and task management only).",
                    model=model,
                    llm_provider="",
                )

        return data
