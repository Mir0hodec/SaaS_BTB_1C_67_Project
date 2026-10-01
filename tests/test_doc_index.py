from pathlib import Path

import pytest

from common.doc_index import (
    MAX_READ_CHUNKS,
    DocIndex,
    ExcludeRule,
    IndexNotBuiltError,
    title_from_filename,
)
from conftest import make_docx


@pytest.fixture
def kb(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "kb"
    root.mkdir()
    make_docx(root / "12. Ошибки проведения документов.docx", [
        "Ошибки проведения документов",
        "Документы не проводятся при отрицательных остатках на складе.",
    ])
    make_docx(root / "Как добавлять пользователя Кассир в конфигурацию Обменный пункт.docx", [
        "Для того, чтобы добавить нового пользователя, откройте Администрирование.",
    ])
    (root / "Загрузка курса валют.txt").write_text("Курсы валют загружаются с сайта Нацбанка.", encoding="utf-8")
    (root / "Загрузка курса валют — копия.txt").write_text("Курсы валют загружаются с сайта Нацбанка.", encoding="utf-8")
    (root / "клиенты.xlsx").write_bytes(b"not really xlsx")
    (root / "пустой.txt").write_text("   \n  ", encoding="utf-8")
    (root / "картинка.png").write_bytes(b"\x89PNG")
    make_docx(root / "Длинное руководство.docx", [f"Абзац номер {i} про настройку табло и кассы. " * 5 for i in range(80)])
    return root, tmp_path / "index.sqlite3"


def build(kb):
    root, db = kb
    idx = DocIndex(db)
    report = idx.rebuild_from_dir(root, [ExcludeRule("клиенты.xlsx", "список клиентов")])
    idx.close()
    return report, DocIndex(db, readonly=True)


def test_build_report(kb):
    report, _ = build(kb)
    assert ("Загрузка курса валют — копия.txt", "Загрузка курса валют.txt") in report.duplicates
    assert report.excluded == [("клиенты.xlsx", "список клиентов")]
    assert report.empty == ["пустой.txt"]
    assert report.unsupported == ["картинка.png"]
    assert not report.failed


def test_search_handles_word_forms(kb):
    _, idx = build(kb)
    hits = idx.search("проведение документа")
    assert hits and hits[0].title == "Ошибки проведения документов"
    assert hits[0].matched_terms == 2


def test_search_matches_shorter_stem_in_index(kb):
    # «обменнике» → основа «обменник», а в индексе «Обменный» → «обмен».
    _, idx = build(kb)
    titles = [h.title for h in idx.search("кассир в обменнике")]
    assert "Как добавлять пользователя Кассир в конфигурацию Обменный пункт" in titles


def test_stopword_only_and_hostile_queries_do_not_crash(kb):
    _, idx = build(kb)
    assert idx.search("как это") == []
    assert idx.search('NEAR(" OR * AND ") -- ;') == []
    idx.search('курс" OR title_stem:*')  # спецсимволы FTS5 не должны ломать запрос


def test_read_document_is_clamped_and_rejects_unknown_path(kb):
    _, idx = build(kb)
    doc = idx.read_document("Длинное руководство.docx", start_chunk=0, max_chunks=500)
    assert doc["total_chunks"] > MAX_READ_CHUNKS
    assert len(doc["chunks"]) == MAX_READ_CHUNKS
    with pytest.raises(KeyError):
        idx.read_document("..\\..\\config\\settings.yaml")


def test_readonly_without_index_fails_clearly(tmp_path):
    with pytest.raises(IndexNotBuiltError, match="build_kb_index"):
        DocIndex(tmp_path / "missing.sqlite3", readonly=True)


def test_rebuild_is_repeatable(kb):
    root, db = kb
    first = DocIndex(db)
    n1 = first.rebuild_from_dir(root).chunks
    n2 = first.rebuild_from_dir(root).chunks
    first.close()
    assert n1 == n2


@pytest.mark.parametrize("name,title", [
    ("!!! Руководство ПО Обменный пункт. 2025 — копия.docx", "Руководство ПО Обменный пункт. 2025"),
    ("+3 .Справочники.docx", "Справочники"),
    ("12. Как отразить оплату.docx", "Как отразить оплату"),
    ("+2. Загрузка шаблона (1).docx", "Загрузка шаблона"),
])
def test_title_from_filename(name, title):
    assert title_from_filename(name) == title
