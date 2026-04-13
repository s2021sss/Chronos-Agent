"""
Санация пользовательского ввода перед записью в Google Calendar API.

Правила:
  title       — strip, удалить управляющие символы \x00-\x1f (кроме \n), не пустой, ≤ 256 символов
  description — strip, удалить управляющие символы \x00-\x1f (кроме \n), ≤ 8000 символов
  notes       — те же правила, что description

Санация реализуется в Pydantic @field_validator до любых внешних вызовов.
"""

import re

from chronos_agent.tools.exceptions import ToolValidationError

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f]")


def sanitize_title(value: str) -> str:
    """
    Очищает заголовок события/задачи.
    Поднимает ToolValidationError если результат пустой.
    """
    value = _CONTROL_CHARS.sub("", value).strip()
    if not value:
        raise ToolValidationError("Title cannot be empty or consist of whitespace only")
    return value[:256]


def sanitize_text(value: str | None, max_length: int = 8000) -> str | None:
    """
    Очищает description / notes.
    Возвращает None если значение None или пустое после очистки.
    """
    if value is None:
        return None
    value = _CONTROL_CHARS.sub("", value).strip()
    return value[:max_length] if value else None
