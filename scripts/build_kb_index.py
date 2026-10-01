"""Сборка поискового индекса базы знаний. Запускать после изменения папки.

    python scripts/build_kb_index.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.win_utf8 import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

from bridge.config import Settings  # noqa: E402
from common.doc_index import DocIndex  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", type=Path, default=None)
    kb = Settings.load(parser.parse_args().settings).knowledge_base
    print(f"База знаний: {kb.root}")
    if not kb.root.is_dir():
        print("ОШИБКА: папка не найдена")
        return 1
    idx = DocIndex(kb.index_path)
    try:
        r = idx.rebuild_from_dir(kb.root, kb.exclude)
    finally:
        idx.close()
    print(f"  документов: {len(r.indexed)}, фрагментов: {r.chunks} → {kb.index_path}")
    for dup, kept in r.duplicates:
        print(f"  дубль пропущен: {dup}  (= {kept})")
    for name, reason in r.excluded:
        print(f"  исключён: {name} — {reason}")
    for name in r.empty:
        print(f"  без текста (скан или только картинки?): {name}")
    for name in r.unsupported:
        print(f"  формат не поддерживается: {name}")
    for name, err in r.failed:
        print(f"  ОШИБКА чтения: {name}: {err}")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
