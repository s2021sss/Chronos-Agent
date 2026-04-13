import base64
import hashlib
import hmac
import json
import time
from typing import NamedTuple

from cryptography.fernet import Fernet
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from chronos_agent.config import settings
from chronos_agent.logging import get_logger

logger = get_logger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
]

_STATE_TTL_SECONDS = 600


# ---------------------------------------------------------------------------
# CSRF state token
# ---------------------------------------------------------------------------


def generate_oauth_state(user_id: str) -> str:
    """
    Создаёт HMAC-SHA256-подписанный state токен для защиты от CSRF.
    Формат: base64url(json_payload).hex_signature
    """
    payload = json.dumps({"user_id": user_id, "exp": int(time.time()) + _STATE_TTL_SECONDS})
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(
        settings.oauth_state_secret.encode(),
        payload_b64.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload_b64}.{sig}"


def verify_oauth_state(state: str) -> str:
    """
    Проверяет подпись и TTL state-токена.
    Возвращает user_id при успехе, поднимает ValueError при ошибке.
    """
    try:
        payload_b64, sig = state.rsplit(".", 1)
    except ValueError:
        raise ValueError("Malformed state token")

    expected_sig = hmac.new(
        settings.oauth_state_secret.encode(),
        payload_b64.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(sig, expected_sig):
        raise ValueError("Invalid state signature")

    # Восстанавливаем base64-паддинг (мы убрали его при генерации через rstrip)
    padding = (4 - len(payload_b64) % 4) % 4
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * padding))

    if payload["exp"] < time.time():
        raise ValueError("State token expired")

    return payload["user_id"]


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------


def _build_flow() -> Flow:
    client_config = {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uris": [settings.google_redirect_uri],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    flow = Flow.from_client_config(client_config, scopes=SCOPES)
    flow.redirect_uri = settings.google_redirect_uri
    return flow


def build_oauth_url(state: str) -> str:
    """Формирует URL авторизации Google с CSRF-защитой через state."""
    flow = _build_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state,
        include_granted_scopes="true",
    )
    return auth_url


class TokenPair(NamedTuple):
    access_token: str
    refresh_token: str


def exchange_code(code: str) -> TokenPair:
    """
    Обменивает authorization code на токены.
    Синхронный — вызывать через asyncio.to_thread().

    Поднимает ValueError если Google не вернул refresh_token
    (пользователь уже авторизовывался без prompt=consent).
    """
    flow = _build_flow()
    flow.fetch_token(code=code)
    creds: Credentials = flow.credentials

    if not creds.refresh_token:
        raise ValueError(
            "Google did not return a refresh_token. "
            "User may have previously authorized this app. "
            "Revoke access at https://myaccount.google.com/permissions and retry."
        )

    return TokenPair(access_token=creds.token or "", refresh_token=creds.refresh_token)


# ---------------------------------------------------------------------------
# Fernet encryption
# ---------------------------------------------------------------------------


def encrypt_refresh_token(refresh_token: str) -> bytes:
    """Шифрует refresh_token симметричным шифрованием Fernet (AES-128-CBC + HMAC)."""
    return Fernet(settings.encryption_key.encode()).encrypt(refresh_token.encode())


def decrypt_refresh_token(encrypted: bytes) -> str:
    """Расшифровывает Fernet-зашифрованный refresh_token."""
    return Fernet(settings.encryption_key.encode()).decrypt(encrypted).decode()
