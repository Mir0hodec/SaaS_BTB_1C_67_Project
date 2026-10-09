"""Проверка интеграции с 1С-Коннект по шагам — с фактическими запросами и HTTP-кодами.

    python scripts/connect_check.py auth                     # логин/пароль API + UUID учётки бота
    python scripts/connect_check.py send <UUID_сотрудника> [текст]   # личное сообщение от бота
    python scripts/connect_check.py inbound <UUID_сотрудника> [текст] # входящее → локальный мост
    python scripts/connect_check.py pipe [секунд]            # что приходит из 1С-Коннект (приложение)

Пароль и заголовок Authorization не печатаются.
"""
from __future__ import annotations

import json
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.win_utf8 import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

from bridge import connect_pipe  # noqa: E402
from bridge.config import Settings  # noqa: E402


def show(method: str, url: str, body: dict | None, r: requests.Response) -> None:
    print(f"> {method} {url}")
    if body is not None:
        print("> " + json.dumps(body, ensure_ascii=False))
    print(f"< HTTP {r.status_code}")
    print("< " + (r.text[:1500] if r.text else "(пустое тело)"))


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
    print(f"< сотрудников: {len(people)}")
    if cs.agent_login:
        bot = next((p for p in people if str(p.get("login")) == cs.agent_login), None)
        if bot:
            print(f"Учётка бота «{bot.get('surname', '')} {bot.get('name', '')}»: user_id={bot['user_id']}")
            if bot["user_id"] != cs.bot_specialist_id:
                print(f"→ впишите в .env: CONNECT_BOT_SPECIALIST_ID={bot['user_id']}")
        else:
            print(f"Логин {cs.agent_login} не найден среди специалистов линий — UUID бота указать вручную")
    return 0


def send(s: Settings, recipient: str, text: str) -> int:
    cs = s.connect
    body = {"author_id": cs.bot_specialist_id, "recepient_id": recipient, "text": text}
    url = f"{cs.api_base_url}/v1/colleague/send/message/"
    r = requests.post(url, json=body, auth=(cs.login, cs.password), headers={"Accept": "application/json"},
                      timeout=30)
    show("POST", url, body, r)
    return 0 if r.ok else 1


def inbound(s: Settings, sender: str, text: str) -> int:
    body = {"platform": "1c-connect", "event": "message_received", "message_id": str(uuid.uuid4()),
            "sender_id": sender, "recipient_id": s.connect.bot_specialist_id, "text": text,
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds")}
    url = f"http://127.0.0.1:{s.port}/connect/colleague/message"
    r = requests.post(url, json=body, headers={"Authorization": f"Bearer {s.connect.webhook_token}"}, timeout=30)
    show("POST", url, body, r)
    print("Ответ бота придёт сотруднику в личный чат 1С-Коннект (см. logs/bridge.log).")
    return 0 if r.status_code == 202 else 1


def pipe(s: Settings, seconds: int) -> int:
    login = s.connect.agent_login
    if not login:
        print(f"Не задан {s.connect.agent_login_env} в .env")
        return 1
    stop = threading.Event()
    got = []

    def on_message(m):
        got.append(m)
        print(f"< входящее {m.message_id} от {m.colleague_id} ({m.sent_at}): {m.text!r}")

    print(f"Слушаю {connect_pipe.PIPE_PREFIX}{login} {seconds} с — напишите боту в 1С-Коннект…")
    t = threading.Thread(target=connect_pipe.listen, args=(login, on_message, stop),
                         kwargs={"retry_min": 2, "retry_max": 5}, daemon=True)
    t.start()
    t.join(seconds)
    stop.set()
    print(f"Получено сообщений: {len(got)}. Мост при этом лучше остановить: канал рассчитан на одного клиента.")
    return 0 if got else 1


def main() -> int:
    s = Settings.load()
    args = sys.argv[1:]
    if args[:1] == ["auth"]:
        return auth(s)
    if args[:1] == ["send"] and len(args) >= 2:
        return send(s, args[1], " ".join(args[2:]) or "Проверка ИИ-помощника ГК «Эксперт».")
    if args[:1] == ["inbound"] and len(args) >= 2:
        return inbound(s, args[1], " ".join(args[2:]) or "Привет, бот!")
    if args[:1] == ["pipe"]:
        return pipe(s, int(args[1]) if len(args) > 1 else 60)
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
