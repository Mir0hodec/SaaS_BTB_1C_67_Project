"""Сборка расширения MCP_HTTPService из исходников (extensions/MCP_HTTPService/src) в .cfe.

    python scripts/build_extension.py                    # → extensions/MCP_HTTPService.cfe
    python scripts/build_extension.py --out D:/tmp/x.cfe

Платформа — как у сборки обработок (settings.dev.v8_path или последняя 8.3 с конфигуратором).
Сборка идёт во временной пустой базе: загрузка XML как расширения, проверка модулей
(тонкий клиент и сервер), выгрузка .cfe. Базы сотрудников и клиентов не трогаются.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.win_utf8 import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

from bridge.config import REPO_ROOT, Settings  # noqa: E402

NAME = "MCP_HTTPService"
SRC = REPO_ROOT / "extensions" / NAME / "src"


def designer(v8: Path, args: list[str], log: Path, timeout: int = 900) -> str:
    """1cv8 DESIGNER с /Out в файл: код возврата и текст лога конфигуратора."""
    res = subprocess.run([str(v8), *args, "/Out", str(log), "/DisableStartupDialogs", "/DisableStartupMessages"],
                         timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    text = log.read_text(encoding="utf-8-sig", errors="replace") if log.exists() else ""
    if not text.strip() and log.exists():
        text = log.read_bytes().decode("cp1251", errors="replace")
    if res.returncode != 0:
        raise SystemExit(f"конфигуратор вернул {res.returncode}:\n{text.strip()}")
    return text.strip()


def build(out: Path, platform: str) -> Path:
    v8 = Path(platform) / "1cv8.exe" if Path(platform).is_dir() else Path(platform)
    if not v8.is_file():
        raise SystemExit(f"нет конфигуратора: {v8}")
    work = Path(tempfile.mkdtemp(prefix="cfe-build-"))
    try:
        ib = work / "ib"
        designer(v8, ["CREATEINFOBASE", f"File={ib}"], work / "create.log")
        print(designer(v8, ["DESIGNER", "/F", str(ib), "/LoadConfigFromFiles", str(SRC), "-Extension", NAME],
                       work / "load.log") or "исходники загружены")
        print(designer(v8, ["DESIGNER", "/F", str(ib), "/CheckModules", "-ThinClient", "-Server",
                            "-Extension", NAME], work / "check.log") or "модули без ошибок")
        tmp = work / f"{NAME}.cfe"
        designer(v8, ["DESIGNER", "/F", str(ib), "/DumpCfg", str(tmp), "-Extension", NAME], work / "dump.log")
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tmp, out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "extensions" / f"{NAME}.cfe")
    args = parser.parse_args()
    platform = Settings.load().dev.platform()
    if not platform:
        print("не найдена платформа 1С с конфигуратором (dev.v8_path в settings.yaml)")
        return 1
    out = build(args.out, platform)
    print(f"Готово: {out} ({out.stat().st_size // 1024} КБ), платформа {platform}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
