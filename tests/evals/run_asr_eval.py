#!/usr/bin/env python3
"""
ASR + Voice E2E Eval — Chronos Agent.

Тестирует голосовой pipeline без Telegram:
  1. ASR accuracy  — Whisper транскрибирует .ogg файл, транскрипт содержит ожидаемую подстроку
  2. NLU accuracy  — ReAct агент на транскрипте вызывает правильный tool (action/поля)

Требования:
  - faster-whisper установлен (pip install faster-whisper)
  - ffmpeg доступен в PATH
  - .ogg файлы лежат в tests/evals/dataset/voice/
  - MISTRAL_API_KEY задан в .env или окружении

Запуск (из корня проекта):
    python tests/evals/run_asr_eval.py
    python tests/evals/run_asr_eval.py --whisper-model base
    python tests/evals/run_asr_eval.py --filter-category create_event --delay 2.0
    python tests/evals/run_asr_eval.py --asr-only   # только транскрипция, без NLU
    python tests/evals/run_asr_eval.py --out tests/evals/results/results_asr.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import perf_counter

import openai
import yaml

os.environ.setdefault("LLM_PROXY_ENABLED", "false")

_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ROOT))

_EVAL_TIMEZONE = "Europe/Moscow"
_CASE_TIMEOUT = 60.0
_WHISPER_TIMEOUT = 30.0
_MAX_REACT_ITERATIONS = 5
_DURATION_TOLERANCE = 15

_VOICE_DIR = Path(__file__).parent / "dataset" / "voice"
_TMP_DIR = Path(__file__).parent / "_tmp_wav"

_READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "get_conversation_history",
        "get_calendar_events",
        "find_free_slots",
        "get_pending_tasks",
    }
)

_DUMMY_TOOL_RESPONSES: dict[str, str] = {
    "get_conversation_history": "[]",
    "get_calendar_events": "[]",
    "find_free_slots": "[]",
    "get_pending_tasks": "[]",
}

_TOOL_TO_ACTION: dict[str, str] = {
    "create_event": "create_event",
    "create_task": "create_task",
    "move_event": "move_event",
    "complete_task": "complete_task",
    "ask_user": "unknown",
}

_WEEKDAY_MAP: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
    "понедельник": 0,
    "вторник": 1,
    "среда": 2,
    "четверг": 3,
    "пятница": 4,
    "суббота": 5,
    "воскресенье": 6,
}
_WEEKDAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class FieldCheck:
    name: str
    passed: bool
    actual: str
    expected: str
    note: str = ""


@dataclass
class ASREvalResult:
    case_id: str
    category: str
    difficulty: str
    file: str
    say_exactly: str

    # ASR
    transcript: str | None
    asr_passed: bool
    asr_latency_ms: float

    # NLU
    expected_action: str | None
    actual_action: str | None
    actual_tool: str | None
    action_passed: bool | None
    field_checks: list[FieldCheck] = field(default_factory=list)
    nlu_latency_ms: float = 0.0
    nlu_iterations: int = 0

    passed: bool = False
    error: str | None = None


def _convert_to_wav(ogg_path: Path, wav_path: Path) -> None:
    """Конвертирует .ogg -> WAV (16kHz, mono) через ffmpeg."""
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
        raise RuntimeError(f"ffmpeg failed (code {result.returncode}): {stderr[:300]}")
    if not wav_path.exists() or wav_path.stat().st_size < 100:
        raise RuntimeError("ffmpeg produced empty WAV")


def _transcribe_sync(wav_path: Path, model) -> str | None:
    """Синхронная транскрипция через faster-whisper."""
    segments, info = model.transcribe(
        str(wav_path),
        language="ru",
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )
    if info.duration < 0.5:
        return None
    text = " ".join(seg.text.strip() for seg in segments).strip()
    return text if text else None


async def transcribe_ogg(ogg_path: Path, model) -> tuple[str | None, float]:
    """
    Конвертирует .ogg -> WAV и транскрибирует.
    Возвращает (текст, latency_ms).
    """
    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    wav_path = _TMP_DIR / f"{uuid.uuid4().hex}.wav"

    t0 = perf_counter()
    try:
        await asyncio.to_thread(_convert_to_wav, ogg_path, wav_path)
        text = await asyncio.wait_for(
            asyncio.to_thread(_transcribe_sync, wav_path, model),
            timeout=_WHISPER_TIMEOUT,
        )
        latency_ms = (perf_counter() - t0) * 1000
        return text, latency_ms
    finally:
        if wav_path.exists():
            wav_path.unlink()


def _build_mock_responses(expected_nlu: dict) -> dict[str, str]:
    """Строит контекстные mock-ответы для read-only tools."""
    from datetime import datetime, timedelta

    action = expected_nlu.get("action", "")
    target = expected_nlu.get("target_title_contains") or expected_nlu.get("title_contains", "")

    responses: dict[str, str] = {}

    if action == "move_event" and target:
        event_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%dT10:00:00+03:00")
        event_end = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%dT11:00:00+03:00")
        fake_events = [
            {
                "id": "evt_eval_mock",
                "title": target.capitalize(),
                "start": event_date,
                "end": event_end,
                "description": "",
            }
        ]
        responses["get_calendar_events"] = json.dumps(fake_events, ensure_ascii=False)

    elif action == "complete_task" and target:
        fake_tasks = [
            {
                "id": "task_eval_mock",
                "title": target.capitalize(),
                "due": None,
                "completed": False,
                "notes": "",
            }
        ]
        responses["get_pending_tasks"] = json.dumps(fake_tasks, ensure_ascii=False)

    return responses


# ── ReAct eval loop ───────────────────────────────────────────────────────────


async def _run_react_loop(
    input_text: str,
    client: openai.AsyncOpenAI,
    model: str,
    system_prompt: str,
    tool_definitions: list,
    mock_tool_responses: dict[str, str] | None = None,
) -> tuple[str, dict, int]:
    """
    Симулирует ReAct loop: LLM вызывает tools, read-only tools возвращают mock-результат.
    Возвращает (tool_name, tool_args, iterations_used).
    """
    effective_responses = {**_DUMMY_TOOL_RESPONSES}
    if mock_tool_responses:
        effective_responses.update(mock_tool_responses)

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": input_text},
    ]

    for iteration in range(1, _MAX_REACT_ITERATIONS + 1):
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tool_definitions,
            tool_choice="auto",
            parallel_tool_calls=False,
            temperature=0.1,
            max_tokens=512,
        )

        choice = response.choices[0]

        if choice.finish_reason != "tool_calls" or not choice.message.tool_calls:
            return "unknown", {}, iteration

        tool_call = choice.message.tool_calls[0]
        tool_name = tool_call.function.name

        try:
            args = json.loads(tool_call.function.arguments)
        except (json.JSONDecodeError, TypeError):
            args = {}

        if tool_name not in _READ_ONLY_TOOLS:
            return tool_name, args, iteration

        assistant_msg: dict = {
            "role": "assistant",
            "content": choice.message.content,
            "tool_calls": [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": tool_call.function.arguments,
                    },
                }
            ],
        }
        tool_result_msg: dict = {
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": effective_responses.get(tool_name, "[]"),
        }
        messages = messages + [assistant_msg, tool_result_msg]

    return "unknown", {}, _MAX_REACT_ITERATIONS


# ── System prompt builder ─────────────────────────────────────────────────────


def _build_system_prompt(today: date, tz_name: str) -> str:
    """Строит REACT_SYSTEM_PROMPT — логика из react_reasoner.py."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from chronos_agent.config import settings as _s
    from chronos_agent.llm.prompts import REACT_SYSTEM_PROMPT

    _WEEKDAY_RU = [
        "Понедельник",
        "Вторник",
        "Среда",
        "Четверг",
        "Пятница",
        "Суббота",
        "Воскресенье",
    ]

    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        tz = ZoneInfo("UTC")

    now = datetime.now(tz)
    today_d = now.date()
    tomorrow = today_d + timedelta(days=1)

    dt_str = now.strftime("%Y-%m-%dT%H:%M:%S%z")
    if len(dt_str) >= 5 and dt_str[-5] in ("+", "-") and ":" not in dt_str[-5:]:
        dt_str = dt_str[:-2] + ":" + dt_str[-2:]

    next_weekdays_lines = []
    for delta in range(1, 8):
        day = today_d + timedelta(days=delta)
        weekday = _WEEKDAY_RU[day.weekday()]
        next_weekdays_lines.append(f"  {weekday}: {day.isoformat()}")
    next_weekdays = "\n".join(next_weekdays_lines)

    return REACT_SYSTEM_PROMPT.format(
        current_datetime=dt_str,
        user_timezone=tz_name,
        today_date=today_d.isoformat(),
        tomorrow_date=tomorrow.isoformat(),
        weekday_name=_WEEKDAY_RU[today_d.weekday()],
        next_weekdays=next_weekdays,
        max_iterations=_s.max_tool_calls_per_iteration,
    )


# ── NLU field checks ──────────────────────────────────────────────────────────


def _resolve_relative_date(spec: str, today: date) -> date | None:
    spec = spec.lower().strip()
    if spec == "today":
        return today
    if spec == "tomorrow":
        return today + timedelta(days=1)
    if spec == "day_after_tomorrow":
        return today + timedelta(days=2)
    prefix = "next_" if spec.startswith("next_") else ""
    weekday_name = spec[len(prefix) :]
    weekday_num = _WEEKDAY_MAP.get(weekday_name)
    if weekday_num is None:
        return None
    days_ahead = weekday_num - today.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return today + timedelta(days=days_ahead)


def _extract_dt(tool_name: str, args: dict) -> datetime | None:
    dt_str: str | None = None
    if tool_name == "create_event":
        dt_str = args.get("start")
    elif tool_name == "create_task":
        dt_str = args.get("due_date")
    elif tool_name == "move_event":
        dt_str = args.get("new_start")

    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


def _extract_title(tool_name: str, args: dict) -> str:
    if tool_name in ("create_event", "create_task"):
        return args.get("title", "")
    if tool_name == "move_event":
        return args.get("event_title", "")
    if tool_name == "complete_task":
        return args.get("task_title", "")
    return ""


def _check_fields(
    tool_name: str,
    args: dict,
    expected: dict,
    today: date,
) -> list[FieldCheck]:
    """Проверяет ожидаемые поля из датасета против аргументов ReAct tool call."""
    checks: list[FieldCheck] = []

    title = _extract_title(tool_name, args)
    dt = _extract_dt(tool_name, args)

    if "title_contains" in expected:
        pattern = expected["title_contains"].lower()
        passed = pattern in title.lower()
        checks.append(
            FieldCheck(
                name="title_contains",
                passed=passed,
                actual=title,
                expected=f"contains '{expected['title_contains']}'",
                note="" if passed else f"'{pattern}' not in '{title.lower()}'",
            )
        )

    if "target_title_contains" in expected:
        pattern = expected["target_title_contains"].lower()
        target = args.get("event_title", args.get("task_title", "")).lower()
        actual_title_lower = title.lower()
        passed = pattern in target or pattern in actual_title_lower
        checks.append(
            FieldCheck(
                name="target_title_contains",
                passed=passed,
                actual=target or actual_title_lower,
                expected=f"contains '{expected['target_title_contains']}'",
            )
        )

    if "start_date" in expected:
        expected_date = _resolve_relative_date(expected["start_date"], today)
        if expected_date is None:
            checks.append(
                FieldCheck(
                    name="start_date",
                    passed=False,
                    actual="",
                    expected=expected["start_date"],
                    note=f"Unknown date spec: {expected['start_date']}",
                )
            )
        elif dt is None:
            checks.append(
                FieldCheck(
                    name="start_date",
                    passed=False,
                    actual="None",
                    expected=str(expected_date),
                )
            )
        else:
            actual_date = dt.date()
            passed = actual_date == expected_date
            checks.append(
                FieldCheck(
                    name="start_date",
                    passed=passed,
                    actual=str(actual_date),
                    expected=str(expected_date),
                )
            )

    if "start_weekday" in expected:
        expected_wd = _WEEKDAY_MAP.get(expected["start_weekday"].lower())
        if expected_wd is None:
            checks.append(
                FieldCheck(
                    "start_weekday",
                    False,
                    "",
                    expected["start_weekday"],
                    note="Unknown weekday name",
                )
            )
        elif dt is None:
            checks.append(FieldCheck("start_weekday", False, "None", expected["start_weekday"]))
        else:
            actual_wd = dt.weekday()
            passed = actual_wd == expected_wd
            checks.append(
                FieldCheck(
                    name="start_weekday",
                    passed=passed,
                    actual=_WEEKDAY_SHORT[actual_wd],
                    expected=_WEEKDAY_SHORT[expected_wd],
                )
            )

    if "start_hour" in expected:
        if dt is None:
            checks.append(FieldCheck("start_hour", False, "None", str(expected["start_hour"])))
        else:
            passed = dt.hour == expected["start_hour"]
            checks.append(
                FieldCheck(
                    name="start_hour",
                    passed=passed,
                    actual=str(dt.hour),
                    expected=str(expected["start_hour"]),
                )
            )

    if "start_hour_min" in expected:
        if dt is None:
            checks.append(
                FieldCheck(
                    "start_hour_min",
                    False,
                    "None",
                    f">= {expected['start_hour_min']}",
                )
            )
        else:
            passed = dt.hour >= expected["start_hour_min"]
            checks.append(
                FieldCheck(
                    "start_hour_min",
                    passed,
                    str(dt.hour),
                    f">= {expected['start_hour_min']}",
                )
            )

    if "start_hour_max" in expected:
        if dt is None:
            checks.append(
                FieldCheck(
                    "start_hour_max",
                    False,
                    "None",
                    f"<= {expected['start_hour_max']}",
                )
            )
        else:
            passed = dt.hour <= expected["start_hour_max"]
            checks.append(
                FieldCheck(
                    "start_hour_max",
                    passed,
                    str(dt.hour),
                    f"<= {expected['start_hour_max']}",
                )
            )

    if "duration_minutes" in expected:
        exp_dur = expected["duration_minutes"]
        actual_dur: int | None = None

        if tool_name == "create_event":
            start_str = args.get("start")
            end_str = args.get("end")
            if start_str and end_str:
                try:
                    s = datetime.fromisoformat(start_str)
                    e = datetime.fromisoformat(end_str)
                    actual_dur = int((e - s).total_seconds() / 60)
                except (ValueError, TypeError):
                    pass
        elif tool_name == "move_event":
            new_start = args.get("new_start")
            new_end = args.get("new_end")
            if new_start and new_end:
                try:
                    s = datetime.fromisoformat(new_start)
                    e = datetime.fromisoformat(new_end)
                    actual_dur = int((e - s).total_seconds() / 60)
                except (ValueError, TypeError):
                    pass

        if actual_dur is None:
            checks.append(
                FieldCheck(
                    "duration_minutes",
                    False,
                    "None",
                    f"{exp_dur} ±{_DURATION_TOLERANCE}",
                    note="Could not derive duration from args",
                )
            )
        else:
            passed = abs(actual_dur - exp_dur) <= _DURATION_TOLERANCE
            checks.append(
                FieldCheck(
                    name="duration_minutes",
                    passed=passed,
                    actual=str(actual_dur),
                    expected=f"{exp_dur} ±{_DURATION_TOLERANCE}",
                )
            )

    if "has_clarification" in expected:
        exp_val = expected["has_clarification"]
        actual_has = tool_name == "ask_user"
        passed = actual_has == exp_val
        checks.append(
            FieldCheck(
                name="has_clarification",
                passed=passed,
                actual=str(actual_has),
                expected=str(exp_val),
                note=args.get("question", "") if tool_name == "ask_user" else "",
            )
        )

    return checks


# ── Main eval case ────────────────────────────────────────────────────────────


async def eval_case(
    case: dict,
    today: date,
    whisper_model,
    asr_only: bool,
    client: openai.AsyncOpenAI,
    model: str,
    system_prompt: str,
    tool_definitions: list,
) -> ASREvalResult:
    case_id = case.get("id", "??")
    category = case.get("category", "")
    difficulty = case.get("difficulty", "")
    filename = case.get("file", "")
    say_exactly = case.get("say_exactly", "")
    expected_nlu = case.get("expected_nlu", {})

    ogg_path = _VOICE_DIR / filename

    result = ASREvalResult(
        case_id=case_id,
        category=category,
        difficulty=difficulty,
        file=filename,
        say_exactly=say_exactly,
        transcript=None,
        asr_passed=False,
        asr_latency_ms=0.0,
        expected_action=expected_nlu.get("action"),
        actual_action=None,
        actual_tool=None,
        action_passed=None,
    )

    if not ogg_path.exists():
        result.error = f"Файл не найден: {ogg_path}"
        return result

    try:
        # ── ASR ───────────────────────────────────────────────────────────────
        transcript, asr_ms = await transcribe_ogg(ogg_path, whisper_model)
        result.transcript = transcript
        result.asr_latency_ms = asr_ms

        expected_contains = case.get("expected_transcript_contains", "").lower()
        if transcript and expected_contains:
            result.asr_passed = expected_contains in transcript.lower()
        elif transcript:
            result.asr_passed = True
        else:
            result.asr_passed = False
            result.error = "Whisper вернул пустой транскрипт"
            return result

        if asr_only:
            result.passed = result.asr_passed
            return result

        if not transcript:
            result.passed = False
            return result

        # ── NLU (ReAct) ───────────────────────────────────────────────────────
        t0 = perf_counter()
        mock_responses = _build_mock_responses(expected_nlu)

        tool_name, tool_args, iterations = await asyncio.wait_for(
            _run_react_loop(
                input_text=transcript,
                client=client,
                model=model,
                system_prompt=system_prompt,
                tool_definitions=tool_definitions,
                mock_tool_responses=mock_responses or None,
            ),
            timeout=_CASE_TIMEOUT,
        )
        result.nlu_latency_ms = (perf_counter() - t0) * 1000
        result.nlu_iterations = iterations

        result.actual_tool = tool_name
        result.actual_action = _TOOL_TO_ACTION.get(tool_name, tool_name)

        expected_action = expected_nlu.get("action")
        accepted = expected_nlu.get("action_accepts", [])
        result.action_passed = (
            result.actual_action == expected_action or result.actual_action in accepted
        )

        result.field_checks = _check_fields(tool_name, tool_args, expected_nlu, today)
        all_fields_ok = all(fc.passed for fc in result.field_checks)
        result.passed = result.asr_passed and bool(result.action_passed) and all_fields_ok

    except TimeoutError:
        result.error = f"Timeout ({_CASE_TIMEOUT}s)"
    except Exception as exc:
        result.error = str(exc)[:300]

    return result


def _percentile(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, int(len(s) * p / 100) - 1)
    return s[idx]


def print_report(results: list[ASREvalResult], asr_only: bool) -> None:
    total = len(results)
    asr_ok = sum(1 for r in results if r.asr_passed)
    nlu_ok = sum(1 for r in results if r.action_passed) if not asr_only else None
    passed = sum(1 for r in results if r.passed)
    errors = sum(1 for r in results if r.error)

    asr_latencies = [r.asr_latency_ms for r in results if r.asr_latency_ms > 0]
    nlu_latencies = [r.nlu_latency_ms for r in results if r.nlu_latency_ms > 0]

    print()
    print("=" * 72)
    print("ASR + NLU E2E EVAL REPORT — Chronos Agent (ReAct)")
    print("=" * 72)
    print(f"Всего кейсов   : {total}")
    print(f"ASR accuracy   : {asr_ok}/{total} ({asr_ok / total * 100:.1f}%)")
    if not asr_only and nlu_ok is not None:
        print(f"NLU accuracy   : {nlu_ok}/{total} ({nlu_ok / total * 100:.1f}%)")
        print(f"E2E passed     : {passed}/{total} ({passed / total * 100:.1f}%)")
    print(f"Errors         : {errors}")
    if asr_latencies:
        print(f"ASR latency p50: {_percentile(asr_latencies, 50):.0f} ms")
        print(f"ASR latency p95: {_percentile(asr_latencies, 95):.0f} ms")
    if nlu_latencies:
        print(f"NLU latency p50: {_percentile(nlu_latencies, 50):.0f} ms")
        print(f"NLU latency p95: {_percentile(nlu_latencies, 95):.0f} ms")

    categories: dict[str, list[ASREvalResult]] = {}
    for r in results:
        categories.setdefault(r.category, []).append(r)

    print("─" * 72)
    print("По категориям:")
    for cat, cat_results in sorted(categories.items()):
        cat_passed = sum(1 for r in cat_results if r.passed)
        icon = "✅" if cat_passed == len(cat_results) else "⚠️ "
        print(f"  {icon} {cat:<20} {cat_passed}/{len(cat_results)}")

    print("─" * 72)
    print("Детали:")
    for r in results:
        status = "✅" if r.passed else "❌"
        asr_icon = "✅" if r.asr_passed else "❌"
        transcript_preview = (r.transcript or "")[:50].replace("\n", " ")
        if r.error:
            print(f"  {status} {r.case_id:<10} [{r.difficulty:<6}] ASR:{asr_icon} ERROR: {r.error}")
        else:
            nlu_part = f" NLU:{r.actual_action}" if not asr_only else ""
            iter_part = f" iter={r.nlu_iterations}" if r.nlu_iterations else ""
            print(
                f"  {status} {r.case_id:<10} [{r.difficulty:<6}] "
                f"ASR:{asr_icon} {r.asr_latency_ms:.0f}ms{nlu_part}{iter_part}"
            )
            print(f"       transcript: «{transcript_preview}»")
            for fc in r.field_checks:
                if not fc.passed:
                    print(f"       ❌ {fc.name}: got={fc.actual!r}, want={fc.expected}")
    print("=" * 72)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Chronos ASR + Voice E2E Eval (ReAct)")
    parser.add_argument(
        "--dataset",
        default=str(_VOICE_DIR / "asr_dataset.yaml"),
        help="Путь к YAML датасету (default: dataset/voice/asr_dataset.yaml)",
    )
    parser.add_argument(
        "--whisper-model",
        default="base",
        help="Модель faster-whisper: tiny/base/small/medium (default: base)",
    )
    parser.add_argument(
        "--asr-only",
        action="store_true",
        help="Тестировать только транскрипцию, без NLU",
    )
    parser.add_argument(
        "--filter-category",
        default=None,
        help="Запустить только кейсы указанной категории",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Задержка в секундах между кейсами (default: 2.0 для избежания rate limit)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Сохранить JSON-результаты в файл",
    )
    args = parser.parse_args()

    import importlib.util as _ilu

    from chronos_agent.config import settings

    _spec = _ilu.spec_from_file_location(
        "chronos_agent_react_tools_eval",
        _ROOT / "chronos_agent" / "agent" / "react_tools.py",
    )
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    REACT_TOOL_DEFINITIONS = _mod.REACT_TOOL_DEFINITIONS

    from faster_whisper import WhisperModel

    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        print(f"Dataset not found: {dataset_path}", file=sys.stderr)
        sys.exit(1)

    with dataset_path.open(encoding="utf-8") as f:
        cases: list[dict] = yaml.safe_load(f)

    if args.filter_category:
        cases = [c for c in cases if c.get("category") == args.filter_category]

    today = date.today()
    print(f"Chronos ASR+NLU Eval — {len(cases)} cases, whisper={args.whisper_model}, date={today}")
    print(f"Режим: {'ASR only' if args.asr_only else 'ASR + NLU/ReAct (E2E)'}")
    print(f"Model: {settings.mistral_model}")
    print(f"Задержка между кейсами: {args.delay}s")
    print()

    print(f"Загружаю Whisper модель '{args.whisper_model}'...", end=" ", flush=True)
    whisper_model = WhisperModel(args.whisper_model, device="cpu", compute_type="int8")
    print("готово.")

    client = openai.AsyncOpenAI(
        api_key=settings.mistral_api_key,
        base_url=settings.mistral_base_url,
    )

    system_prompt = _build_system_prompt(today, _EVAL_TIMEZONE)
    print()

    results: list[ASREvalResult] = []

    for i, case in enumerate(cases, 1):
        case_id = case.get("id", f"case_{i}")
        print(f"  [{i:02d}/{len(cases)}] {case_id:<10} ...", end="", flush=True)
        result = await eval_case(
            case=case,
            today=today,
            whisper_model=whisper_model,
            asr_only=args.asr_only,
            client=client,
            model=settings.mistral_model,
            system_prompt=system_prompt,
            tool_definitions=REACT_TOOL_DEFINITIONS,
        )
        results.append(result)

        if result.error and not result.transcript:
            print(f" ❌ ERROR: {result.error}")
        else:
            status = "✅" if result.passed else "❌"
            asr_ms = f"{result.asr_latency_ms:.0f}ms"
            transcript_short = (result.transcript or "")[:40]
            nlu_note = f" [{result.actual_tool}]" if result.actual_tool else ""
            print(f" {status} ASR:{asr_ms} «{transcript_short}»{nlu_note}")

        if args.delay > 0 and i < len(cases):
            await asyncio.sleep(args.delay)

    print_report(results, args.asr_only)

    if args.out:
        total = len(results)
        asr_ok = sum(1 for r in results if r.asr_passed)
        passed = sum(1 for r in results if r.passed)
        asr_latencies = [r.asr_latency_ms for r in results if r.asr_latency_ms > 0]
        nlu_latencies = [r.nlu_latency_ms for r in results if r.nlu_latency_ms > 0]

        summary = {
            "date": str(today),
            "whisper_model": args.whisper_model,
            "mistral_model": settings.mistral_model,
            "mode": "asr_only" if args.asr_only else "e2e",
            "total_cases": total,
            "asr_passed": asr_ok,
            "asr_accuracy": round(asr_ok / total, 3) if total else 0.0,
            "e2e_passed": passed,
            "e2e_accuracy": round(passed / total, 3) if total else 0.0,
            "asr_latency_p50_ms": round(_percentile(asr_latencies, 50)),
            "asr_latency_p95_ms": round(_percentile(asr_latencies, 95)),
            "nlu_latency_p50_ms": round(_percentile(nlu_latencies, 50)),
            "nlu_latency_p95_ms": round(_percentile(nlu_latencies, 95)),
            "results": [asdict(r) for r in results],
        }
        out_path = Path(args.out)
        out_path.write_text(
            json.dumps(summary, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"Results saved to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
