"""Консоль: тот же помощник без 1С-Коннект — для проверки и отладки.

    python -m bridge.cli                  # от имени первого владельца из users.yaml
    python -m bridge.cli --user <user_id> --attach скриншот.png
    python -m bridge.cli "#тест почему не проводится поступление?" "исправь"   # вопросы по очереди и выход
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.win_utf8 import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

from bridge.access import User, resolve_user  # noqa: E402
from bridge.assistant import Assistant, Job  # noqa: E402
from bridge.config import Settings  # noqa: E402
from bridge.store import Store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--user", help="user_id из users.yaml")
    parser.add_argument("--attach", type=Path, action="append", default=[], help="вложение к первому вопросу")
    parser.add_argument("questions", nargs="*", help="вопросы по очереди; без них — диалог в консоли")
    args = parser.parse_args()

    settings = Settings.load(args.settings)
    assistant = Assistant(settings, Store(settings.data_dir / "bridge.sqlite3"))
    users = assistant.users()
    if args.user:
        user = users.get(args.user) or resolve_user(settings.users_path, args.user)
    else:
        user = next((u for u in users.values() if u.owner), None) or resolve_user(settings.users_path, "cli-check")
    if user is None:
        print("Пользователь не найден в users.yaml")
        return 1
    if user.key == "cli-check":
        user = User(key=user.key, name="Проверка из консоли", bases=user.bases)
    out_dir = settings.data_dir / "cli_files"
    print(f"Вы: {user.name}. Пустая строка или Ctrl+C — выход. /помощь — подсказка.")

    attachments = []
    for a in args.attach:
        copy = settings.data_dir / "incoming" / "cli" / a.name
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(a, copy)
        attachments.append(copy)

    queue = list(args.questions)
    while True:
        if args.questions:
            if not queue:
                return 0
            text = queue.pop(0).strip()
            print(f"> {text}")
        else:
            try:
                text = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                return 0
        if not text:
            return 0
        reply = assistant.handle(Job(channel="cli", user=user, text=text, attachments=attachments,
                                     on_start=lambda: print("…работаю")))
        attachments = []
        print(reply.text)
        for f in reply.files:
            out_dir.mkdir(parents=True, exist_ok=True)
            target = out_dir / f.name
            shutil.copy(f, target)
            print(f"[файл] {target}")


if __name__ == "__main__":
    sys.exit(main())
