#!/usr/bin/env python3
"""
Rate Limit Stress Test — Phase 12, Chronos Agent.

Тестирует SlidingWindowRateLimiter напрямую (без запуска всего приложения).
Проверяет:
  1. Первые N запросов разрешены (N = max_calls)
  2. N+1-й и последующие заблокированы в пределах окна
  3. Изоляция пользователей (лимит одного не влияет на другого)
  4. После истечения окна лимит сбрасывается
  5. reset() сбрасывает счётчик досрочно
  6. remaining() возвращает корректное оставшееся число запросов

Запуск (из корня проекта):
    python tests/evals/run_ratelimit_stress.py
    python tests/evals/run_ratelimit_stress.py --max-calls 5 --window 2
    python tests/evals/run_ratelimit_stress.py --verbose
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from chronos_agent.bot.rate_limiter import SlidingWindowRateLimiter

_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ROOT))


class TestResult:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[str] = []

    def ok(self, name: str, note: str = "") -> None:
        msg = f"✅ {name}" + (f" — {note}" if note else "")
        self.passed.append(msg)
        print(f"  {msg}")

    def fail(self, name: str, reason: str) -> None:
        msg = f"❌ {name} — {reason}"
        self.failed.append(msg)
        print(f"  {msg}")

    def assert_true(self, condition: bool, name: str, reason: str = "", note: str = "") -> bool:
        if condition:
            self.ok(name, note)
        else:
            self.fail(name, reason or "assertion failed")
        return condition

    def summary(self) -> int:
        """Печатает итог. Возвращает exit code (0=success, 1=failures)."""
        total = len(self.passed) + len(self.failed)
        print(f"\n{'=' * 60}")
        print("Rate Limit Stress Test — итог")
        print(f"{'=' * 60}")
        print(f"Пройдено : {len(self.passed)}/{total}")
        print(f"Провалено: {len(self.failed)}/{total}")
        if self.failed:
            print("\nПровалившиеся тесты:")
            for f in self.failed:
                print(f"  {f}")
        print(f"{'=' * 60}\n")
        return 1 if self.failed else 0


def run_tests(max_calls: int, window_seconds: int, verbose: bool) -> int:
    res = TestResult()
    print(f"\nSlidingWindowRateLimiter(max_calls={max_calls}, window_seconds={window_seconds})")
    print("─" * 60)

    # ── Тест 1: Первые max_calls запросов разрешены ───────────────────────────
    print(f"\n[1] Первые {max_calls} запросов должны быть разрешены")
    limiter = SlidingWindowRateLimiter(max_calls=max_calls, window_seconds=window_seconds)
    user_a = "user_a"

    for i in range(1, max_calls + 1):
        allowed = limiter.is_allowed(user_a)
        res.assert_true(
            allowed,
            f"  Запрос {i}/{max_calls} разрешён",
            reason=f"Запрос {i} должен быть разрешён (ещё {max_calls - i + 1} осталось)",
        )

    # ── Тест 2: max_calls+1 и далее блокируются ───────────────────────────────
    print(f"\n[2] Запрос {max_calls + 1}+ блокируется в текущем окне")
    for extra in range(1, 4):
        blocked = not limiter.is_allowed(user_a)
        res.assert_true(
            blocked,
            f"  Лишний запрос {extra} заблокирован (429)",
            reason=f"Запрос {max_calls + extra} должен быть заблокирован",
        )

    # ── Тест 3: remaining() ───────────────────────────────────────────────────
    print("\n[3] remaining() после исчерпания лимита = 0")
    remaining = limiter.remaining(user_a)
    res.assert_true(
        remaining == 0,
        "  remaining() == 0",
        reason=f"Ожидалось 0, получено {remaining}",
        note=f"got={remaining}",
    )

    # ── Тест 4: Изоляция пользователей ───────────────────────────────────────
    print("\n[4] Лимит user_a не влияет на user_b")
    limiter2 = SlidingWindowRateLimiter(max_calls=max_calls, window_seconds=window_seconds)
    limiter2.is_allowed("user_a")  # исчерпываем лимит user_a
    for _ in range(max_calls - 1):
        limiter2.is_allowed("user_a")

    for i in range(1, max_calls + 1):
        allowed = limiter2.is_allowed("user_b")
        res.assert_true(
            allowed,
            f"  user_b запрос {i}/{max_calls} разрешён",
            reason="user_b должен быть независим от user_a",
        )

    # ── Тест 5: reset() сбрасывает счётчик ───────────────────────────────────
    print("\n[5] reset() позволяет снова отправлять запросы")
    limiter3 = SlidingWindowRateLimiter(max_calls=max_calls, window_seconds=window_seconds)
    user_r = "user_reset"
    for _ in range(max_calls):
        limiter3.is_allowed(user_r)
    # Лимит исчерпан
    blocked_before = not limiter3.is_allowed(user_r)
    res.assert_true(
        blocked_before,
        "  Заблокирован до reset()",
        reason="Лимит должен быть исчерпан",
    )
    limiter3.reset(user_r)
    allowed_after = limiter3.is_allowed(user_r)
    res.assert_true(
        allowed_after,
        "  Разрешён после reset()",
        reason="После reset() должны быть доступны запросы",
    )
    remaining_after = limiter3.remaining(user_r)
    res.assert_true(
        remaining_after == max_calls - 1,
        f"  remaining() после reset() и 1 запроса = {max_calls - 1}",
        reason=f"Ожидалось {max_calls - 1}, получено {remaining_after}",
        note=f"got={remaining_after}",
    )

    # ── Тест 6: После истечения окна лимит сбрасывается ──────────────────────
    print(f"\n[6] После истечения окна ({window_seconds}с) лимит сбрасывается")
    limiter4 = SlidingWindowRateLimiter(max_calls=max_calls, window_seconds=window_seconds)
    user_exp = "user_expire"
    for _ in range(max_calls):
        limiter4.is_allowed(user_exp)
    blocked = not limiter4.is_allowed(user_exp)
    res.assert_true(
        blocked,
        "  Заблокирован до истечения окна",
        reason="Лимит должен быть исчерпан",
    )

    print(f"  Ожидаем {window_seconds + 0.1:.1f}с...", end="", flush=True)
    time.sleep(window_seconds + 0.1)
    print(" готово.")

    allowed_exp = limiter4.is_allowed(user_exp)
    res.assert_true(
        allowed_exp,
        "  Разрешён после истечения окна",
        reason="После window_seconds запросы должны снова проходить",
    )
    remaining_exp = limiter4.remaining(user_exp)
    res.assert_true(
        remaining_exp == max_calls - 1,
        f"  remaining() после сброса окна = {max_calls - 1}",
        reason=f"Ожидалось {max_calls - 1}, получено {remaining_exp}",
        note=f"got={remaining_exp}",
    )

    # ── Тест 7: Скользящее окно (не фиксированное) ───────────────────────────
    print("\n[7] Скользящее окно: ранние запросы вытекают первыми")
    # Используем короткое окно независимо от аргумента
    slide_window = max(window_seconds, 1)
    limiter5 = SlidingWindowRateLimiter(max_calls=max_calls, window_seconds=slide_window)
    user_s = "user_slide"

    # Делаем max_calls/2 запросов, ждём чуть больше половины окна, делаем ещё max_calls/2
    half = max(max_calls // 2, 1)
    for _ in range(half):
        limiter5.is_allowed(user_s)

    half_window = slide_window / 2 + 0.1
    print(f"  Делаем {half} запросов, ждём {half_window:.1f}с...", end="", flush=True)
    time.sleep(half_window)
    print(" готово.")

    # Теперь добиваем лимит
    for _ in range(max_calls - half):
        limiter5.is_allowed(user_s)

    # Ждём ещё чуть-чуть — первые запросы должны вытечь из окна
    wait_more = slide_window / 2 + 0.1
    print(f"  Ждём ещё {wait_more:.1f}с (первые {half} запросов вытекут)...", end="", flush=True)
    time.sleep(wait_more)
    print(" готово.")

    # Теперь оставшееся = half (первые вытекли)
    remaining_slide = limiter5.remaining(user_s)
    res.assert_true(
        remaining_slide >= half,
        f"  remaining() >= {half} после вытекания ранних записей",
        reason=(
            f"Скользящее окно должно освободить {half} слотов, получено remaining={remaining_slide}"
        ),
        note=f"remaining={remaining_slide}, expected >= {half}",
    )

    return res.summary()


def main() -> None:
    parser = argparse.ArgumentParser(description="Rate Limit Stress Test")
    parser.add_argument(
        "--max-calls",
        type=int,
        default=5,
        help="Максимум вызовов за окно (default: 5 — как в production)",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=2,
        help="Размер окна в секундах (default: 2 для быстрого теста; prod=60)",
    )
    parser.add_argument("--verbose", action="store_true", help="Подробный вывод")
    args = parser.parse_args()

    print("=" * 60)
    print("Chronos Agent — Rate Limit Stress Test")
    print("=" * 60)
    print("Тестируемый класс: SlidingWindowRateLimiter")
    print(f"Конфигурация: max_calls={args.max_calls}, window={args.window}s")
    print("Production параметры: max_calls=5, window=60s (RATE_LIMIT_MSG_PER_MINUTE)")

    exit_code = run_tests(args.max_calls, args.window, args.verbose)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
