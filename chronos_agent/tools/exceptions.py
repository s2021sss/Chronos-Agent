class ToolError(Exception):
    """Базовый класс для всех ошибок Tool Layer."""


class ToolValidationError(ToolError):
    """
    Невалидный входной параметр.
    Поднимается до любых внешних вызовов — API не вызывался.
    """


class OAuthExpiredError(ToolError):
    """
    Google OAuth токен отсутствует (пользователь не авторизован)
    или истёк (Google API вернул 401).
    Оркестратор должен инициировать повторную авторизацию.
    """


class CalendarAPIError(ToolError):
    """
    Ошибка Google Calendar / Tasks API.
    Включает HTTP-статус для принятия решений о retry.
    """

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code

    def is_retryable(self) -> bool:
        """429 и 5xx — retryable; остальные — нет."""
        return self.status_code == 429 or self.status_code >= 500


class TaskNotFoundError(ToolError):
    """Задача или событие не найдено в локальной БД."""
