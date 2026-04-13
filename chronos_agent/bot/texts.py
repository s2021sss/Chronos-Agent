from dataclasses import dataclass


@dataclass(frozen=True)
class _Texts:
    welcome: str = (
        "Привет! Я Chronos — твой ИИ-планировщик.\n\n"
        "Я помогу создавать события и задачи в Google Calendar голосом или текстом.\n\n"
        "Для начала нужно подключить Google-аккаунт:"
    )
    oauth_prompt: str = (
        '<a href="{url}">Подключить Google Calendar</a>\n\nНажми на ссылку и разреши доступ.'
    )
    oauth_reconnect_prompt: str = (
        "Если Google Calendar перестал подключаться, то перейдите по ссылке ниже:\n\n"
        '<a href="{url}">Переподключить Google Calendar</a>\n\n'
        "После авторизации повтори запрос."
    )
    oauth_pending: str = "Ожидаю подключения Google-аккаунта.\nПожалуйста, перейди по ссылке выше."
    oauth_done_set_timezone: str = (
        "Google Calendar подключён!\n\n"
        "Теперь укажи свой часовой пояс. Примеры:\n"
        "  /timezone Europe/Moscow\n"
        "  /timezone Asia/Almaty\n"
        "  /timezone UTC"
    )
    timezone_pending: str = (
        "Осталось указать часовой пояс.\nИспользуй команду: /timezone Europe/Moscow"
    )
    onboarding_complete: str = (
        "Готово! Можешь написать или надиктовать что нужно запланировать.\n\n"
        "Примеры:\n"
        "  «Встреча с командой завтра в 14:00 на час»\n"
        "  «Напомни сдать отчёт в пятницу»"
    )
    onboarding_blocked: str = "Сначала завершим настройку.\n{hint}"

    help_text: str = (
        "<b>Доступные команды:</b>\n\n"
        "/start — начать настройку или показать приветствие\n"
        "/cancel — отменить текущее действие\n"
        "/help — эта справка\n"
        "/status — список активных задач\n"
        "/reconnect — переподключить Google Calendar\n"
        "/timezone &lt;tz&gt; — установить часовой пояс\n\n"
        "<b>Примеры запросов:</b>\n"
        "  «Встреча с Иваном в пятницу в 10:00»\n"
        "  «Задача: подготовить презентацию до конца недели»\n"
        "  «Перенеси встречу с командой на вторник»"
    )
    cancel_no_session: str = "Нет активных действий для отмены."
    cancel_done: str = "Действие отменено."
    status_no_tasks: str = "Нет активных задач."
    status_header: str = "<b>Активные задачи:</b>\n\n"
    status_task_row: str = "• {title}{due}{priority}\n"
    timezone_usage: str = (
        "Укажи timezone в формате IANA.\nПримеры: /timezone Europe/Moscow, /timezone UTC"
    )
    timezone_invalid: str = (
        "Неизвестный timezone: <code>{tz}</code>\n"
        "Используй формат IANA: Europe/Moscow, Asia/Almaty, UTC"
    )
    timezone_set: str = "Часовой пояс установлен: <b>{tz}</b>"

    confirmation_confirmed: str = "Выполняю..."
    confirmation_rejected: str = "Действие отменено."
    confirmation_expired: str = "Время ожидания истекло. Действие отменено."

    voice_transcribing: str = "Распознаю голосовое сообщение..."
    voice_transcript: str = "Распознал: «{text}»"
    voice_transcription_failed: str = (
        "Не удалось распознать голосовое сообщение.\nПопробуй написать текстом или повтори запись."
    )

    agent_stub: str = "Обработка запросов в разработке. Скоро буду готов!"

    rate_limit: str = "Слишком много запросов. Подожди немного."

    generic_error: str = "Что-то пошло не так. Попробуй ещё раз."
    agent_still_processing: str = (
        "Запрос ещё обрабатывается. Я пришлю результат сюда, пожалуйста, не повторяй его пока."
    )

    off_topic: str = (
        "Я планировщик: создаю события в Google Calendar и задачи в Google Tasks.\n"
        "Напиши что нужно запланировать."
    )

    hitl_pending_reminder: str = (
        "Жду твоего ответа — нажми кнопки выше.\nИли используй /cancel чтобы отменить действие."
    )


T = _Texts()
