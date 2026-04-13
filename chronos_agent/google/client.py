from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from chronos_agent.config import settings
from chronos_agent.google.auth import SCOPES, decrypt_refresh_token


def build_credentials(encrypted_refresh_token: bytes) -> Credentials:
    """
    Строит OAuth2 Credentials из зашифрованного refresh_token.
    Сразу выполняет refresh — получает валидный access_token.
    Синхронный.
    """
    refresh_token = decrypt_refresh_token(encrypted_refresh_token)
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return creds


def build_calendar_service(encrypted_refresh_token: bytes):
    """Возвращает аутентифицированный Google Calendar v3 API service. Синхронный."""
    creds = build_credentials(encrypted_refresh_token)
    return build("calendar", "v3", credentials=creds)


def build_tasks_service(encrypted_refresh_token: bytes):
    """Возвращает аутентифицированный Google Tasks v1 API service. Синхронный."""
    creds = build_credentials(encrypted_refresh_token)
    return build("tasks", "v1", credentials=creds)
