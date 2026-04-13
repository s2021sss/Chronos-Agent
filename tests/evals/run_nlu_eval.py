#!/usr/bin/env python3
"""
NLU Eval Runner — Chronos Agent.

Тестирует ReAct-агент на эталонном датасете (nlu_dataset.yaml).
Каждый case: запуск LLM с REACT_SYSTEM_PROMPT + REACT_TOOL_DEFINITIONS ->
сравнение выбранного tool (action) и аргументов с ожидаемым -> pass/fail.

Запуск (из корня проекта):
    python tests/evals/run_nlu_eval.py
    python tests/evals/run_nlu_eval.py --out results.json   # JSON-отчёт
    python tests/evals/run_nlu_eval.py --filter-category out_of_scope
    python tests/evals/run_nlu_eval.py --delay 2.0          # пауза между кейсами

Переменные окружения:
    MISTRAL_API_KEY    — ключ API (читается из .env автоматически)
    LLM_PROXY_ENABLED  — если False, идёт напрямую к Mistral (default: False)
    EVAL_TIMEZONE      — часовой пояс для тестов (default: Europe/Moscow)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import openai
import yaml

os.environ.setdefault("LLM_PROXY_ENABLED", "false")

_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ROOT))

_EVAL_TIMEZONE = os.getenv("EVAL_TIMEZONE", "Europe/Moscow")
_CASE_TIMEOUT = 60.0
_MAX_REACT_ITERATIONS = 5
_DURATION_TOLERANCE = 15


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


@dataclass
class FieldCheck:
    name: str
    passed: bool
    actual: str
    expected: str
    note: str = ""


@dataclass
class EvalResult:
    case_id: str
    category: str
    input_text: str
    # Action
    expected_action: str
    actual_action: str | None
    actual_tool: str | None
    action_passed: bool
    # Field checks
    field_checks: list[FieldCheck]
    all_fields_passed: bool
    # Overall verdict
    passed: bool
    # Performance
    latency_ms: float
    iterations: int
    # Error
    error: str | None = None


def _mock_title(input_text: str, target: str) -> str:
    """
    Строит реалистичный заголовок для mock события/задачи.

    Если перед целевым словом стоит прилагательное (русское окончание -ний/-ный/-ой/-ий),
    включает его в заголовок: "утренний стендап" → "Утренний стендап".
    Иначе возвращает просто target.capitalize().
    """
    lower = input_text.lower()
    idx = lower.find(target.lower())
    if idx != -1:
        before = lower[:idx].split()
        if before:
            prev = before[-1]
            adj_endings = ("ний", "ной", "ный", "ой", "ий", "ая", "яя", "ое", "ее")
            if any(prev.endswith(sfx) for sfx in adj_endings):
                return (prev + " " + target).capitalize()
    return target.capitalize()


def _build_mock_responses(case: dict) -> dict[str, str]:
    """
    Строит контекстные mock-ответы для read-only tools.

    Для кейсов move_event / complete_task возвращает непустые данные,
    чтобы LLM мог найти целевое событие/задачу и вызвать нужный tool.
    Для остальных — пустые ответы (стандартное поведение).
    """
    from datetime import datetime, timedelta

    expected = case.get("expected", {})
    action = expected.get("action", "")
    target = expected.get("target_title_contains") or expected.get("title_contains", "")
    input_text = case.get("input", "")

    responses: dict[str, str] = {}

    if action == "move_event" and target:
        title = _mock_title(input_text, target)
        event_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%dT10:00:00+03:00")
        event_end = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%dT11:00:00+03:00")
        fake_events = [
            {
                "id": "evt_eval_mock",
                "title": title,
                "start": event_date,
                "end": event_end,
                "description": "",
            }
        ]
        responses["get_calendar_events"] = json.dumps(fake_events, ensure_ascii=False)

    elif action == "complete_task" and target:
        title = _mock_title(input_text, target)
        fake_tasks = [
            {
                "id": "task_eval_mock",
                "title": title,
                "due": None,
                "completed": False,
                "notes": "",
            }
        ]
        responses["get_pending_tasks"] = json.dumps(fake_tasks, ensure_ascii=False)

    return responses


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


def _build_system_prompt(today: date, tz_name: str) -> str:
    """Строит REACT_SYSTEM_PROMPT с текущей датой/временем — логика из react_reasoner.py."""
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

    mock_tool_responses — переопределяет ответы для конкретных tool (для modify_existing).
    Возвращает (tool_name, tool_args, iterations_used).
    tool_name = "unknown" если LLM не вызвал ни одного SIDE_EFFECT/TERMINAL tool.
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

        # Side-effect или terminal tool -> это наш ответ
        if tool_name not in _READ_ONLY_TOOLS:
            return tool_name, args, iteration

        # Read-only tool -> симулируем mock-результат и продолжаем
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

    # Исчерпали итерации без SIDE_EFFECT/TERMINAL tool
    return "unknown", {}, _MAX_REACT_ITERATIONS


# ── Проверка полей ────────────────────────────────────────────────────────────


def _extract_dt(tool_name: str, args: dict) -> datetime | None:
    """Извлекает datetime из аргументов tool call в зависимости от типа tool."""
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
    """Извлекает title из аргументов."""
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

    # ── title_contains ────────────────────────────────────────────────────────
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

    # ── target_title_contains ─────────────────────────────────────────────────
    if "target_title_contains" in expected:
        pattern = expected["target_title_contains"].lower()
        # move_event: event_title; complete_task: task_title
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

    # ── start_date ────────────────────────────────────────────────────────────
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

    # ── start_weekday ─────────────────────────────────────────────────────────
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

    # ── start_hour ────────────────────────────────────────────────────────────
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

    # ── start_hour_min ────────────────────────────────────────────────────────
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

    # ── start_hour_max ────────────────────────────────────────────────────────
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

    # ── duration_minutes ──────────────────────────────────────────────────────
    if "duration_minutes" in expected:
        exp_dur = expected["duration_minutes"]
        actual_dur: int | None = None

        if tool_name == "create_event":
            start_str = args.get("start")
            end_str = args.get("end")
            if start_str and end_str:
                try:
                    start_dt = datetime.fromisoformat(start_str)
                    end_dt = datetime.fromisoformat(end_str)
                    actual_dur = int((end_dt - start_dt).total_seconds() / 60)
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

    # ── has_clarification ─────────────────────────────────────────────────────
    if "has_clarification" in expected:
        exp_val = expected["has_clarification"]
        # В ReAct агенте ask_user означает уточнение
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

    # ReAct агент не производит confidence score; пропускаем эти проверки.

    return checks


# ── Core eval ─────────────────────────────────────────────────────────────────


async def eval_case(
    case: dict,
    today: date,
    client: openai.AsyncOpenAI,
    model: str,
    system_prompt: str,
    tool_definitions: list,
) -> EvalResult:
    """Выполняет один eval case: ReAct LLM loop + проверка результатов."""
    case_id = case["id"]
    category = case.get("category", "unknown")
    input_text = case["input"]
    expected = case.get("expected", {})
    accepted_actions: list[str] = expected.get("action_accepts", [])
    primary_action: str = expected.get("action", "unknown")

    t0 = time.perf_counter()
    mock_responses = _build_mock_responses(case)

    try:
        tool_name, tool_args, iterations = await asyncio.wait_for(
            _run_react_loop(
                input_text=input_text,
                client=client,
                model=model,
                system_prompt=system_prompt,
                tool_definitions=tool_definitions,
                mock_tool_responses=mock_responses or None,
            ),
            timeout=_CASE_TIMEOUT,
        )
    except TimeoutError:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return EvalResult(
            case_id=case_id,
            category=category,
            input_text=input_text,
            expected_action=primary_action,
            actual_action=None,
            actual_tool=None,
            action_passed=False,
            field_checks=[],
            all_fields_passed=False,
            passed=False,
            latency_ms=elapsed_ms,
            iterations=0,
            error="Timeout",
        )
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return EvalResult(
            case_id=case_id,
            category=category,
            input_text=input_text,
            expected_action=primary_action,
            actual_action=None,
            actual_tool=None,
            action_passed=False,
            field_checks=[],
            all_fields_passed=False,
            passed=False,
            latency_ms=elapsed_ms,
            iterations=0,
            error=str(exc)[:200],
        )

    elapsed_ms = (time.perf_counter() - t0) * 1000

    # Маппинг tool_name -> action
    actual_action = _TOOL_TO_ACTION.get(tool_name, tool_name)

    # Проверка action
    all_accepted = [primary_action] + accepted_actions
    action_passed = actual_action in all_accepted

    # Проверка полей
    field_checks = _check_fields(tool_name, tool_args, expected, today)
    all_fields_passed = all(fc.passed for fc in field_checks)

    passed = action_passed and all_fields_passed

    return EvalResult(
        case_id=case_id,
        category=category,
        input_text=input_text,
        expected_action=primary_action,
        actual_action=actual_action,
        actual_tool=tool_name,
        action_passed=action_passed,
        field_checks=field_checks,
        all_fields_passed=all_fields_passed,
        passed=passed,
        latency_ms=elapsed_ms,
        iterations=iterations,
    )


# ── Reporting ─────────────────────────────────────────────────────────────────


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = min(int(len(sorted_vals) * p / 100), len(sorted_vals) - 1)
    return sorted_vals[idx]


def print_report(results: list[EvalResult]) -> None:
    SEP = "─" * 72
    total = len(results)
    if total == 0:
        print("No results.")
        return

    passed_count = sum(1 for r in results if r.passed)
    action_correct = sum(1 for r in results if r.action_passed)
    errors = sum(1 for r in results if r.error)

    latencies = [r.latency_ms for r in results if r.error is None]
    p50 = _percentile(latencies, 50)
    p95 = _percentile(latencies, 95)
    p99 = _percentile(latencies, 99)

    nlu_pct = passed_count / total * 100
    action_pct = action_correct / total * 100

    print(f"\n{'=' * 72}")
    print("REACT EVAL REPORT — Chronos Agent")
    print(f"{'=' * 72}")
    print(f"Датасет        : {total} cases")
    print(f"NLU Success    : {passed_count}/{total} ({nlu_pct:.1f}%)")
    print(f"Action Accuracy: {action_correct}/{total} ({action_pct:.1f}%)")
    print(f"Errors         : {errors}")
    print(f"Latency p50    : {p50:.0f} ms")
    print(f"Latency p95    : {p95:.0f} ms    {'✅ < 7s' if p95 < 7000 else '❌ > 7s target'}")
    print(f"Latency p99    : {p99:.0f} ms")
    print(SEP)

    categories: dict[str, list[EvalResult]] = {}
    for r in results:
        categories.setdefault(r.category, []).append(r)

    print("По категориям:")
    for cat, cat_results in sorted(categories.items()):
        cat_passed = sum(1 for r in cat_results if r.passed)
        cat_total = len(cat_results)
        icon = "✅" if cat_passed == cat_total else ("⚠️ " if cat_passed > 0 else "❌")
        print(f"  {icon} {cat:<25} {cat_passed}/{cat_total}")

    print(SEP)
    print("Детали по кейсам:")

    for r in results:
        status = "✅" if r.passed else "❌"
        lat = f"{r.latency_ms:>6.0f}ms"
        iter_note = f" iter={r.iterations}"
        action_note = ""
        if not r.action_passed:
            action_note = (
                f" [action: ожидался={r.expected_action},"
                f" получен={r.actual_action} (tool={r.actual_tool})]"
            )

        print(f"  {status} {r.case_id:<10} [{r.category:<20}] {lat}{iter_note}{action_note}")

        if r.error:
            print(f"       ERROR: {r.error}")
        elif not r.passed:
            for fc in r.field_checks:
                if not fc.passed:
                    note = f" ({fc.note})" if fc.note else ""
                    print(
                        f"       FAIL {fc.name}: got '{fc.actual}', expected '{fc.expected}'{note}"
                    )

    print(f"{'=' * 72}\n")


# ── Main ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    parser = argparse.ArgumentParser(description="Chronos ReAct Eval Runner")
    parser.add_argument(
        "--dataset",
        default=str(Path(__file__).parent / "dataset" / "nlu_dataset.yaml"),
        help="Путь к YAML датасету (default: dataset/nlu_dataset.yaml)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Сохранить JSON-результаты в файл",
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
        help="Задержка в секундах между кейсами (default: 2.0 для избежания rate limit).",
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

    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        print(f"Dataset not found: {dataset_path}", file=sys.stderr)
        sys.exit(1)

    with dataset_path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    cases: list[dict] = raw if isinstance(raw, list) else raw.get("cases", [])

    if args.filter_category:
        cases = [c for c in cases if c.get("category") == args.filter_category]

    today = date.today()

    system_prompt = _build_system_prompt(today, _EVAL_TIMEZONE)

    client = openai.AsyncOpenAI(
        api_key=settings.mistral_api_key,
        base_url=settings.mistral_base_url,
    )

    print(f"Chronos ReAct Eval — {len(cases)} cases, tz={_EVAL_TIMEZONE}, date={today}")
    print(f"Model: {settings.mistral_model}")
    print(f"Delay between cases: {args.delay}s")
    print(f"Max ReAct iterations per case: {_MAX_REACT_ITERATIONS}")
    print()

    results: list[EvalResult] = []

    for i, case in enumerate(cases, 1):
        case_id = case.get("id", f"case_{i}")
        print(f"  [{i:02d}/{len(cases)}] {case_id:<10} ...", end="", flush=True)
        result = await eval_case(
            case=case,
            today=today,
            client=client,
            model=settings.mistral_model,
            system_prompt=system_prompt,
            tool_definitions=REACT_TOOL_DEFINITIONS,
        )
        results.append(result)

        status = "✅" if result.passed else "❌"
        err_note = f" ERROR: {result.error}" if result.error else ""
        tool_note = f" [{result.actual_tool}]" if result.actual_tool else ""
        print(f" {status} {result.latency_ms:.0f}ms{tool_note}{err_note}")

        if args.delay > 0 and i < len(cases):
            await asyncio.sleep(args.delay)

    print_report(results)

    total = len(results)
    passed_count = sum(1 for r in results if r.passed)
    action_correct = sum(1 for r in results if r.action_passed)
    latencies = [r.latency_ms for r in results if r.error is None]

    summary = {
        "date": str(today),
        "timezone": _EVAL_TIMEZONE,
        "model": "",  # set below
        "total_cases": total,
        "passed": passed_count,
        "nlu_success_rate": round(passed_count / total, 3) if total else 0.0,
        "action_accuracy": round(action_correct / total, 3) if total else 0.0,
        "latency_p50_ms": round(_percentile(latencies, 50)),
        "latency_p95_ms": round(_percentile(latencies, 95)),
        "latency_p99_ms": round(_percentile(latencies, 99)),
        "errors": sum(1 for r in results if r.error),
        "results": [asdict(r) for r in results],
    }

    from chronos_agent.config import settings as s

    summary["model"] = s.mistral_model

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(
            json.dumps(summary, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"Results saved to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
