"""Проверка качества поиска по коду 1С: попадание нужной процедуры в top-1/5/8.

    python onec_rag/eval.py --generate 30     # набрать вопросы из реального кода → eval_questions.json
    python onec_rag/eval.py                   # прогнать вопросы через тот же поиск, что у search_1c

Вопросы генерируются из индекса трёх видов: по комментарию над процедурой
(«какая процедура …»), по имени процедуры, разбитому на слова («где …»), и
«что делает Модуль.Процедура». Сгенерированный файл — заготовка: вопросы
из комментариев заведомо «лёгкие» (слова вопроса есть в коде), поэтому его
стоит дополнить настоящими вопросами сотрудников в том же формате:

    {"q": "где считается пеня", "expect": {"path": "CommonModules/РасчетПени", "proc": "РассчитатьПеню"}}

Попадание — результат, у которого путь содержит expect.path и (если задано)
имя процедуры равно expect.proc.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import onec_index as ix  # noqa: E402
import onec_mcp as mcp_search  # noqa: E402

DEFAULT_QUESTIONS = Path(__file__).resolve().parent / "eval_questions.json"
_CAMEL_RE = re.compile(r"[А-ЯЁA-Z]+(?![а-яёa-z])|[А-ЯЁA-Z]?[а-яёa-z]+|\d+")


def camel_words(name: str) -> list[str]:
    return [w.lower() for w in _CAMEL_RE.findall(name)]


def _first_comment(body: str) -> str:
    """Первая фраза комментария над процедурой."""
    words: list[str] = []
    for line in body.split("\n"):
        s = line.strip()
        if not s.startswith("//"):
            break
        s = s.lstrip("/").strip()
        if not s:
            if words:
                break
            continue
        if s.rstrip(":").lower() in ("параметры", "возвращаемое значение", "parameters", "returns"):
            break
        words.append(s)
    text = " ".join(words).split(". ")[0].strip().rstrip(".")
    return text[0].lower() + text[1:] if text else ""


def generate(conn, count: int, seed: int = 1) -> list[dict]:
    rows = conn.execute(
        "SELECT f.path, c.object_type, c.object_name, c.proc_name, c.is_export, chunks_fts.body "
        "FROM chunks c JOIN files f ON f.id=c.file_id JOIN chunks_fts ON chunks_fts.rowid=c.id "
        "WHERE c.kind IN ('proc','func') AND c.proc_lower IN "
        "(SELECT proc_lower FROM chunks WHERE kind IN ('proc','func') GROUP BY proc_lower HAVING count(*)=1)"
    ).fetchall()          # только уникальные имена: у ожидаемого ответа не должно быть двойников
    rnd = random.Random(seed)
    rnd.shuffle(rows)
    by_comment, by_name, by_call = [], [], []
    for path, otype, oname, proc, export, body in rows:
        expect = {"path": path, "proc": proc}
        comment = _first_comment(body)
        if 4 <= len(comment.split()) <= 25:
            by_comment.append({"q": f"какая процедура {comment}?", "expect": expect, "kind": "комментарий"})
        words = camel_words(proc)
        if len(words) >= 3:
            owner = " ".join(camel_words(oname))
            by_name.append({"q": f"где {' '.join(words)} ({owner})", "expect": expect, "kind": "имя"})
        if export and otype == "ОбщийМодуль":
            by_call.append({"q": f"что делает {oname}.{proc}", "expect": expect, "kind": "вызов"})
    questions: list[dict] = []
    used: set[tuple] = set()
    pools = [by_comment, by_name, by_call]
    while len(questions) < count and any(pools):
        for pool in pools:
            while pool:
                item = pool.pop()
                key = (item["expect"]["path"], item["expect"]["proc"])
                if key not in used:
                    used.add(key)
                    questions.append(item)
                    break
            if len(questions) >= count:
                break
    return questions


def is_hit(hit: mcp_search.Hit, expect: dict | list) -> bool:
    """expect — один вариант или список допустимых (любой засчитывается)."""
    for item in expect if isinstance(expect, list) else [expect]:
        path, proc = item.get("path", ""), item.get("proc", "")
        if (not path or path in f"{hit.repo}/{hit.path}") and (not proc or hit.proc_name.lower() == proc.lower()):
            return True
    return False


def _expect_text(expect: dict | list) -> str:
    items = expect if isinstance(expect, list) else [expect]
    return " | ".join(f"{i.get('path', '').strip('/')}:{i.get('proc', '')}".strip(":") for i in items)


def evaluate(conn, questions: list[dict], search_cfg: dict, depth: int = 8) -> tuple[list[dict], dict]:
    rows = []
    for item in questions:
        hits, _ = mcp_search.search_chunks(conn, item["q"], depth, search_cfg)
        rank = next((i for i, h in enumerate(hits, 1) if is_hit(h, item["expect"])), None)
        rows.append({**item, "rank": rank, "top1": f"{hits[0].path}:{hits[0].proc_name}" if hits else "—"})
    n = len(rows) or 1
    summary = {
        "questions": len(rows),
        "top1": sum(1 for r in rows if r["rank"] == 1) / n,
        "top5": sum(1 for r in rows if r["rank"] and r["rank"] <= 5) / n,
        "top8": sum(1 for r in rows if r["rank"] and r["rank"] <= 8) / n,
        "mrr": sum(1 / r["rank"] for r in rows if r["rank"]) / n,
    }
    return rows, summary


def print_report(rows: list[dict], summary: dict) -> None:
    print(f"{'№':>3} {'место':>5}  top5 top8  вопрос → ожидалось")
    for i, r in enumerate(rows, 1):
        rank = r["rank"]
        mark = lambda k: " да " if rank and rank <= k else " НЕТ"  # noqa: E731
        print(f"{i:>3} {rank or '—':>5}  {mark(5)} {mark(8)}  {r['q'][:90]} → {_expect_text(r['expect'])[:110]}")
        if not rank:
            print(f"{'':>21}первым найдено: {r['top1']}")
    print(f"\nВопросов: {summary['questions']}   top-1: {summary['top1']:.0%}   top-5: {summary['top5']:.0%}   "
          f"top-8: {summary['top8']:.0%}   MRR: {summary['mrr']:.2f}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Оценка поиска по коду 1С")
    ap.add_argument("--config", help="путь к config.json")
    ap.add_argument("--questions", default=str(DEFAULT_QUESTIONS), help="файл с вопросами (JSON)")
    ap.add_argument("--generate", type=int, metavar="N", help="сгенерировать N вопросов из индекса и выйти")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--json", action="store_true", help="вывести итог в JSON")
    ap.add_argument("--min-top8", type=float, default=0.0, help="код возврата 1, если top-8 ниже порога (0..1)")
    args = ap.parse_args(argv)

    cfg = ix.load_config(ix.config_path(args.config))
    try:
        conn = mcp_search.open_ro(cfg.index_path)
    except mcp_search.NotFound as exc:
        print(exc, file=sys.stderr)
        return 2
    questions_path = Path(args.questions)
    try:
        if args.generate:
            questions = generate(conn, args.generate, args.seed)
            questions_path.write_text(json.dumps(questions, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"Записано вопросов: {len(questions)} → {questions_path}")
            return 0
        if not questions_path.exists():
            print(f"Нет файла вопросов {questions_path}. Сначала: eval.py --generate 30", file=sys.stderr)
            return 2
        questions = json.loads(questions_path.read_text(encoding="utf-8-sig"))
        rows, summary = evaluate(conn, questions, cfg.search)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print_report(rows, summary)
    return 1 if summary["top8"] < args.min_top8 else 0


if __name__ == "__main__":
    sys.exit(main())
