import re
from datetime import datetime
from zoneinfo import ZoneInfo

_RU_MONTHS_GEN = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}

_RU_WEEKDAYS = {
    0: "Пн",
    1: "Вт",
    2: "Ср",
    3: "Чт",
    4: "Пт",
    5: "Сб",
    6: "Вс",
}


def md_to_html(text: str) -> str:
    """
    Конвертирует Markdown-форматирование в Telegram HTML (ParseMode.HTML).

    Поддерживаемые конструкции:
      **текст**  -> <b>текст</b>
      *текст*    -> <i>текст</i>  (только одиночные звёздочки)

    Telegram использует HTML parse mode, поэтому Markdown-звёздочки отображаются
    буквально. Функция применяется к тексту агента перед отправкой в Telegram.
    """
    # **bold** -> <b>bold</b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    # *italic* -> <i>italic</i>
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", text)
    return text


def fmt_local(dt: datetime | None, user_timezone: str) -> str:
    """
    Форматирует datetime в читаемый вид в timezone пользователя.

    Пример: "Чт, 10 апреля в 13:00"
    Fallback при ошибке: "10.04.2026 13:00 UTC"
    """
    if dt is None:
        return "?"
    try:
        tz = ZoneInfo(user_timezone or "UTC")
        local = dt.astimezone(tz)
        weekday = _RU_WEEKDAYS[local.weekday()]
        day = local.day
        month = _RU_MONTHS_GEN[local.month]
        time_str = local.strftime("%H:%M")
        return f"{weekday}, {day} {month} в {time_str}"
    except Exception:
        return dt.strftime("%d.%m.%Y %H:%M UTC")
