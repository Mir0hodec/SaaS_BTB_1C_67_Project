"""Проверка доступа к API 1С-Коннект — с фактическими запросами и HTTP-кодами.

    python scripts/connect_check.py auth     # логин/пароль API и учётка бота среди специалистов

Состояние линии «ИИ-помощник» и вебхука — scripts/connect_line_setup.py status.
Пароль и заголовок Authorization не печатаются.
"""
from __future__ import annotations

import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.win_utf8 import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

from bridge.config import Settings  # noqa: E402


def auth(s: Settings) -> int:
    cs = s.connect
    if not (cs.login and cs.password):
        print(f"Не заданы {cs.api_login_env}/{cs.api_password_env} в .env")
        return 1
    url = f"{cs.api_base_url}/v1/line/specialists/"
    r = requests.get(url, auth=(cs.login, cs.password), headers={"Accept": "application/json"}, timeout=30)
    print(f"> GET {url}\n< HTTP {r.status_code}")
    if r.status_code != 200:
        print("< " + r.text[:500])
        return 1
    people = r.json()
    print(f"< специалистов линий: {len(people)}")
    bot = next((p for p in people if str(p.get("user_id")) == cs.bot_specialist_id), None)
    if bot:
        print(f"Учётка бота: «{bot.get('surname', '')} {bot.get('name', '')}» ({cs.bot_specialist_id})")
    else:
        print(f"Учётки бота {cs.bot_specialist_id or '(CONNECT_BOT_SPECIALIST_ID не задан)'} среди специалистов "
              "линий нет — проверьте компетенцию на линии «ИИ-помощник» (scripts/connect_line_setup.py)")
    return 0


def main() -> int:
    s = Settings.load()
    if sys.argv[1:2] == ["auth"]:
        return auth(s)
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
