"""
Whisper ASR — транскрипция голосовых сообщений.

WhisperSingleton:
  Загружает модель faster-whisper при старте FastAPI (один раз).
  Используется через asyncio.to_thread для неблокирующей транскрипции.

transcribe_voice:
  Полный pipeline: скачать .ogg -> конвертировать в WAV -> транскрибировать -> удалить.
"""

import asyncio
import subprocess
import uuid
from pathlib import Path

from faster_whisper import WhisperModel

from chronos_agent.logging import get_logger

logger = get_logger(__name__)

_AUDIO_DIR = Path("/app/audio")
_WHISPER_TIMEOUT_SECONDS = 60
_MIN_WAV_SIZE_BYTES = 100
_MIN_AUDIO_DURATION_SECONDS = 0.5


class WhisperSingleton:
    _model: WhisperModel | None = None

    @classmethod
    def load(
        cls,
        model: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
    ) -> None:
        """
        Загружает модель. Вызывается в lifespan FastAPI.
        Повторный вызов — no-op.
        """
        if cls._model is not None:
            return

        logger.info(
            "whisper_model_loading",
            model=model,
            device=device,
            compute_type=compute_type,
        )
        cls._model = WhisperModel(model, device=device, compute_type=compute_type)
        logger.info("whisper_model_loaded", model=model)

    @classmethod
    def get(cls) -> WhisperModel:
        """Возвращает загруженную модель. Поднимает RuntimeError если не инициализирована."""
        if cls._model is None:
            raise RuntimeError(
                "WhisperSingleton not loaded — call WhisperSingleton.load() at startup"
            )
        return cls._model


async def transcribe_voice(file_id: str, bot) -> str | None:
    """
    Полный pipeline транскрипции голосового сообщения.

    1. Скачиваем .ogg по file_id из Telegram
    2. Конвертируем в WAV через ffmpeg (16kHz, mono)
    3. Транскрибируем с asyncio.wait_for timeout
    4. Удаляем временные файлы

    Возвращает текст транскрипции или None при ошибке/таймауте/тишине.
    """
    _AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    uid = uuid.uuid4().hex
    ogg_path = _AUDIO_DIR / f"{uid}.ogg"
    wav_path = _AUDIO_DIR / f"{uid}.wav"

    try:
        # Шаг 1: Скачиваем .ogg из Telegram
        await _download_voice(bot, file_id, ogg_path)

        # Шаг 2: Конвертируем .ogg -> WAV
        await asyncio.to_thread(_convert_to_wav, ogg_path, wav_path)

        # Шаг 3: Транскрибируем с timeout
        text = await asyncio.wait_for(
            asyncio.to_thread(_transcribe, wav_path),
            timeout=_WHISPER_TIMEOUT_SECONDS,
        )

        logger.info(
            "whisper_transcription_done",
            file_id=file_id,
            text_length=len(text) if text else 0,
        )
        return text

    except TimeoutError:
        logger.warning(
            "whisper_timeout",
            file_id=file_id,
            timeout=_WHISPER_TIMEOUT_SECONDS,
        )
        return None
    except Exception as exc:
        logger.error("whisper_error", file_id=file_id, error=str(exc))
        return None
    finally:
        # Шаг 4: Удаляем временные файлы
        _cleanup(ogg_path, wav_path)


async def _download_voice(bot, file_id: str, dest: Path) -> None:
    """Скачивает файл из Telegram по file_id в dest."""
    file = await bot.get_file(file_id)
    await bot.download_file(file.file_path, destination=str(dest))

    size_bytes = dest.stat().st_size if dest.exists() else 0
    logger.info("voice_downloaded", file_id=file_id, size_bytes=size_bytes)


def _convert_to_wav(ogg_path: Path, wav_path: Path) -> None:
    """
    Конвертирует .ogg (Opus) в WAV через ffmpeg.

    Параметры: 16kHz, mono — оптимально для Whisper.
    Проверяет результат: пустой/маленький WAV = ошибка конвертации.
    """
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(ogg_path),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-f",
            "wav",
            str(wav_path),
        ],
        capture_output=True,
        timeout=30,
    )

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"ffmpeg conversion failed (code {result.returncode}): {stderr[:300]}")

    if not wav_path.exists() or wav_path.stat().st_size < _MIN_WAV_SIZE_BYTES:
        size = wav_path.stat().st_size if wav_path.exists() else 0
        raise RuntimeError(f"ffmpeg produced empty or too small WAV: {size} bytes")

    logger.info(
        "voice_converted",
        wav_path=str(wav_path),
        size_bytes=wav_path.stat().st_size,
    )


def _transcribe(wav_path: Path) -> str | None:
    """
    Синхронно транскрибирует WAV файл через faster-whisper.
    Вызывается через asyncio.to_thread — не блокирует event loop.
    """
    model = WhisperSingleton.get()

    segments, info = model.transcribe(
        str(wav_path),
        language="ru",
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )

    if info.duration < _MIN_AUDIO_DURATION_SECONDS:
        logger.warning(
            "whisper_audio_too_short",
            duration=info.duration,
            wav_path=str(wav_path),
        )
        return None

    text = " ".join(segment.text.strip() for segment in segments).strip()
    return text if text else None


def _cleanup(*paths: Path) -> None:
    """Удаляет временные аудиофайлы. Non-fatal."""
    for p in paths:
        try:
            if p.exists():
                p.unlink()
        except Exception as exc:
            logger.warning("audio_cleanup_failed", path=str(p), error=str(exc))
