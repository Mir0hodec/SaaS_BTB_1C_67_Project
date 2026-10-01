"""Статистика по журналу бота (как слайд «Два месяца в работе»).

    python scripts/stats.py            # сводка
    python scripts/stats.py --unknown  # user_id тех, кого нет в белом списке
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.win_utf8 import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

from bridge.config import Settings  # noqa: E402
from bridge.store import Store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unknown", action="store_true")
    args = parser.parse_args()
    settings = Settings.load()
    rows = Store(settings.data_dir / "bridge.sqlite3").journal()

    if args.unknown:
        seen = Counter(r["user_key"] for r in rows if r["status"] == "unknown_user")
        for user_id, n in seen.most_common():
            print(f"{user_id}  обращений: {n}")
        return 0

    answered = [r for r in rows if r["status"] in ("ok", "error")]
    if not answered:
        print("В журнале пока нет вопросов.")
        return 0
    ok = [r for r in answered if r["status"] == "ok"]
    durations = [r["duration_ms"] / 1000 for r in ok if r["duration_ms"]]
    costs = [r["cost_usd"] for r in ok if r["cost_usd"] is not None]
    print(f"Период: {rows[0]['created_at'][:10]} — {rows[-1]['created_at'][:10]}")
    print(f"Вопросов: {len(answered)}, без ошибок: {100 * len(ok) / len(answered):.0f}%")
    if durations:
        print(f"Время ответа: медиана {statistics.median(durations):.0f} с, максимум {max(durations):.0f} с")
    if costs:
        print(f"Цена по оценке CLI (как если бы по API): ~${sum(costs) / len(costs):.2f} за вопрос, "
              f"всего ${sum(costs):.2f}")
    print(f"Лимит подписки: {sum(1 for r in rows if r['status'] == 'limit')} раз")
    print("Базы:", ", ".join(f"{b or 'только база знаний'}: {n}"
                             for b, n in Counter(r["base_id"] for r in answered).most_common()))
    print("Сотрудники:", ", ".join(f"{u}: {n}" for u, n in Counter(r["user_name"] for r in answered).most_common(10)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
