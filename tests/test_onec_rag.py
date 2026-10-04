"""Поиск по коду 1С (onec_rag): чанкинг, инкрементальный индекс, поиск, MCP по stdio."""
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from bridge.config import OnecCodeSettings, Settings
from bridge.mcp_config_builder import allowed_tools, build_mcp_config

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "onec_rag"))

import onec_index as ix  # noqa: E402
import onec_mcp as om  # noqa: E402

COMMON_MODULE = """\
#Область ПрограммныйИнтерфейс

// Рассчитывает пеню по просроченной задолженности контрагента.
//
// Параметры:
//  Контрагент - СправочникСсылка.Контрагенты
//
&НаСервере
Функция РассчитатьПеню(Контрагент, Знач Дата = Неопределено) Экспорт
\tЗапрос = Новый Запрос(
\t"ВЫБРАТЬ
\t|\tДолги.Сумма КАК Сумма
\t|ИЗ
\t|\tРегистрНакопления.Взаиморасчеты.Остатки КАК Долги");
\tВозврат Запрос.Выполнить().Выгрузить().Итог("Сумма") * СтавкаПени();
КонецФункции

Function СтавкаПени()
\tReturn 0.01;
EndFunction

#КонецОбласти
"""

OBJECT_MODULE = """\
Перем КэшСтавок;

Процедура ОбработкаПроведения(Отказ, РежимПроведения)
\tДвижения.Взаиморасчеты.Записывать = Истина;
\tЗаполнитьТабличнуюЧастьТовары();
КонецПроцедуры

// Заполняет табличную часть товарами по остаткам склада.
Процедура ЗаполнитьТабличнуюЧастьТовары() Экспорт
\tТовары.Очистить();
КонецПроцедуры

КэшСтавок = Новый Соответствие;
"""

DOCUMENT_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core"
  xmlns:xr="http://v8.1c.ru/8.3/xcf/readable" xmlns:xs="http://www.w3.org/2001/XMLSchema"
  xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config">
 <Document uuid="1">
  <Properties>
   <Name>РеализацияТоваров</Name>
   <Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>Реализация товаров и услуг</v8:content></v8:item></Synonym>
   <RegisterRecords><xr:Item>AccumulationRegister.Взаиморасчеты</xr:Item></RegisterRecords>
  </Properties>
  <ChildObjects>
   <Attribute uuid="2"><Properties><Name>Контрагент</Name>
     <Type><v8:Type>cfg:CatalogRef.Контрагенты</v8:Type></Type></Properties></Attribute>
   <TabularSection uuid="3"><Properties><Name>Товары</Name></Properties><ChildObjects>
     <Attribute uuid="4"><Properties><Name>Количество</Name><Type><v8:Type>xs:decimal</v8:Type></Type></Properties></Attribute>
   </ChildObjects></TabularSection>
   <Form>ФормаДокумента</Form>
  </ChildObjects>
 </Document>
</MetaDataObject>
"""

CATALOG_MDO = """\
<?xml version="1.0" encoding="UTF-8"?>
<mdclass:Catalog xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass" uuid="5">
  <name>Склады</name>
  <synonym><key>ru</key><value>Склады и магазины</value></synonym>
  <attributes uuid="6"><name>Ответственный</name><type><types>CatalogRef.Сотрудники</types></type></attributes>
  <forms uuid="7"><name>ФормаЭлемента</name></forms>
</mdclass:Catalog>
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8-sig")       # выгрузка 1С — UTF-8 с BOM


@pytest.fixture
def dump(tmp_path):
    """Выгрузка в формате Конфигуратора (cfg) и проект EDT (edt) + config.json."""
    cfg_root, edt_root = tmp_path / "выгрузка cfg", tmp_path / "edt"
    _write(cfg_root / "CommonModules/РасчетПени/Ext/Module.bsl", COMMON_MODULE)
    _write(cfg_root / "Documents/РеализацияТоваров/Ext/ObjectModule.bsl", OBJECT_MODULE)
    _write(cfg_root / "Documents/РеализацияТоваров.xml", DOCUMENT_XML)
    _write(cfg_root / "Documents/РеализацияТоваров/Forms/ФормаДокумента/Ext/Form.xml", "<Form/>")
    _write(cfg_root / "Documents/РеализацияТоваров/Forms/ФормаДокумента/Ext/Form/Module.bsl",
           "&НаКлиенте\nПроцедура ПриОткрытии(Отказ)\nКонецПроцедуры\n")
    _write(edt_root / "src/Catalogs/Склады/Склады.mdo", CATALOG_MDO)
    _write(edt_root / "src/Catalogs/Склады/ManagerModule.bsl", "// только комментарий\nПерем А;\n")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "index_path": "index/onec.sqlite3",
        "repos": [{"name": "cfg", "path": str(cfg_root), "git_pull": False},
                  {"name": "edt", "path": str(edt_root), "git_pull": False}],
    }, ensure_ascii=False), encoding="utf-8-sig")
    return config


@pytest.fixture
def index(dump):
    cfg = ix.load_config(dump)
    ix.run(cfg, pull=False)
    conn = om.open_ro(cfg.index_path)
    yield conn, cfg
    conn.close()


def test_chunk_bsl():
    chunks = ix.chunk_bsl(COMMON_MODULE)
    assert [(c.kind, c.name, c.is_export) for c in chunks] == [
        ("func", "РассчитатьПеню", True), ("func", "СтавкаПени", False)]
    first = chunks[0]
    # Комментарий и директива над процедурой — в чанке; «Процедура» внутри текста запроса чанк не рвёт.
    assert first.body.startswith("// Рассчитывает пеню") and "&НаСервере" in first.body
    assert first.body.rstrip().endswith("КонецФункции") and (first.line_start, first.line_end) == (3, 16)

    chunks = ix.chunk_bsl(OBJECT_MODULE)
    assert [(c.kind, c.name, c.line_start, c.line_end) for c in chunks] == [
        ("module", "", 1, 1), ("proc", "ОбработкаПроведения", 3, 6),
        ("proc", "ЗаполнитьТабличнуюЧастьТовары", 8, 11), ("module", "", 13, 13)]
    # Модуль без процедур — один чанк; пустой — ни одного.
    assert [(c.kind, c.line_start, c.line_end) for c in ix.chunk_bsl("Перем А;\nА = 1;\n")] == [("module", 1, 2)]
    assert ix.chunk_bsl("\n\n") == []


def test_path_meta_both_formats():
    m = ix.path_meta("Documents/РеализацияТоваров/Forms/ФормаДокумента/Ext/Form/Module.bsl")
    assert (m.object_type, m.object_name, m.module_kind, m.sub_name) == (
        "Документ", "РеализацияТоваров", "МодульФормы", "ФормаДокумента")
    m = ix.path_meta("src/Catalogs/Склады/ManagerModule.bsl")
    assert (m.object_type, m.object_name, m.module_kind) == ("Справочник", "Склады", "МодульМенеджера")
    assert ix.path_meta("CommonModules/РасчетПени/Ext/Module.bsl").module_kind == "ОбщийМодуль"
    assert ix.path_meta("Ext/SessionModule.bsl").label == "Конфигурация МодульСеанса"
    assert ix.is_object_file("Documents/РеализацияТоваров.xml")
    assert ix.is_object_file("src/Catalogs/Склады/Склады.mdo")
    assert not ix.is_object_file("Documents/РеализацияТоваров/Forms/ФормаДокумента/Ext/Form.xml")


def test_query_processing():
    terms = om.parse_query("Где считается пеня по документам реализации?")
    assert [t.variants[0] for t in terms] == ["счит", "пен", "документ", "реализац"]
    assert "расчет" in terms[0].variants                     # синонимы «считается» ↔ «расчёт»
    ident = om.parse_query("что делает ОбщегоНазначения.ЗначениеРеквизитаОбъекта")
    assert [(t.variants, t.exact) for t in ident] == [
        (["общегоназначения"], True), (["значениереквизитаобъекта"], True)]
    assert om.parse_query("как и в на") == []                # стоп-слова и слова короче трёх букв


def test_search_ranks_procedure_and_links(index):
    conn, cfg = index
    hits, terms = om.search_chunks(conn, "где считается пеня по задолженности", 5)
    assert hits[0].proc_name == "РассчитатьПеню"
    assert hits[0].link == "cfg/CommonModules/РасчетПени/Ext/Module.bsl:3-16"
    text = om.format_hits(hits, terms, 12000)
    assert "ОбщийМодуль.РасчетПени" in text and "Функция РассчитатьПеню (Экспорт)" in text

    hits, _ = om.search_chunks(conn, "какая процедура заполняет табличную часть товаров", 5)
    assert hits[0].proc_name == "ЗаполнитьТабличнуюЧастьТовары"
    hits, _ = om.search_chunks(conn, "РасчетПени.СтавкаПени", 5)
    assert hits[0].proc_name == "СтавкаПени"
    hits, terms = om.search_chunks(conn, "квантовая телепортация", 5)
    assert hits == [] and "Ничего не найдено" in om.format_hits(hits, terms, 12000)
    # Ограничение размера вывода.
    hits, terms = om.search_chunks(conn, "товары документ пеня", 8)
    assert len(om.format_hits(hits, terms, 400)) < 700 and "обрезан" in om.format_hits(hits, terms, 400)


def test_get_module_and_find_object(index):
    conn, cfg = index
    text = om.read_module(conn, cfg, "РасчетПени", "СтавкаПени")
    assert text.splitlines()[0] == "cfg/CommonModules/РасчетПени/Ext/Module.bsl:18-20"
    assert "19: \tReturn 0.01;" in text
    whole = om.read_module(conn, cfg, "cfg/Documents/РеализацияТоваров/Ext/ObjectModule.bsl:3-6")
    assert whole.splitlines()[1].startswith("3: Процедура ОбработкаПроведения") and "8:" not in whole
    big = om.read_module(conn, cfg, "cfg/CommonModules/РасчетПени/Ext/Module.bsl", max_chars=200)
    assert "модуль большой" in big and "3-16: Функция РассчитатьПеню Экспорт" in big
    assert "нет процедуры" in om.read_module(conn, cfg, "РасчетПени", "НетТакой")
    with pytest.raises(om.NotFound):
        om.read_module(conn, cfg, "../config.json")          # только файлы из индекса

    obj = om.describe_object(conn, "Документ.РеализацияТоваров", 12000)
    assert "«Реализация товаров и услуг»" in obj and "Контрагент (СправочникСсылка.Контрагенты)" in obj
    assert "Табличная часть Товары: Количество (Число)" in obj and "Формы: ФормаДокумента" in obj
    assert "Движения по регистрам: РегистрНакопления.Взаиморасчеты" in obj
    assert "cfg/Documents/РеализацияТоваров/Ext/ObjectModule.bsl" in obj     # список модулей объекта
    assert obj.startswith("cfg/Documents/РеализацияТоваров.xml:1-")
    by_synonym = om.describe_object(conn, "склады и магазины", 12000)        # EDT + поиск по синониму
    assert "Справочник.Склады" in by_synonym and "Ответственный (СправочникСсылка.Сотрудники)" in by_synonym
    with pytest.raises(om.NotFound):
        om.describe_object(conn, "НетТакогоОбъекта", 12000)


def test_incremental_and_removed_files(dump):
    cfg = ix.load_config(dump)
    first = ix.run(cfg, pull=False)
    assert first["files_bsl"] == 4 and first["files_xml"] == 2 and first["changed"] == 6
    assert ix.run(cfg, pull=False)["changed"] == 0           # ничего не менялось — ничего не трогаем

    module = cfg.repos[0].path / "CommonModules/РасчетПени/Ext/Module.bsl"
    module.write_text(COMMON_MODULE.replace("СтавкаПени", "СтавкаШтрафа"), encoding="utf-8-sig")
    (cfg.repos[0].path / "Documents/РеализацияТоваров/Ext/ObjectModule.bsl").unlink()
    result = ix.run(cfg, pull=False)
    assert result["repos"][0]["indexed"] == 1 and result["repos"][0]["removed"] == 1
    conn = om.open_ro(cfg.index_path)
    try:
        names = {r[0] for r in conn.execute("SELECT proc_name FROM chunks WHERE kind IN ('proc','func')")}
        assert "СтавкаШтрафа" in names and not names & {"СтавкаПени", "ОбработкаПроведения"}
        assert om.search_chunks(conn, "ЗаполнитьТабличнуюЧастьТовары", 5)[0] == []
        with pytest.raises(sqlite3.OperationalError):        # MCP открывает индекс только на чтение
            conn.execute("DELETE FROM chunks")
    finally:
        conn.close()

    # Пропавший каталог выгрузки не стирает индекс.
    cfg.repos[1].path = cfg.repos[1].path / "нет такого"
    assert "error" in ix.run(cfg, pull=False)["repos"][1]
    conn = om.open_ro(cfg.index_path)
    try:
        assert conn.execute("SELECT count(*) FROM files WHERE repo='edt'").fetchone()[0] == 2
    finally:
        conn.close()


def test_bot_gets_tools_only_when_configured(repo_root, tmp_path, dump):
    settings = Settings.load(repo_root / "config" / "settings.example.yaml")
    settings.onec_code = OnecCodeSettings(tmp_path / "нет.json")
    servers = json.loads(build_mcp_config(settings, base_id=None, outbox=tmp_path, attachments=tmp_path,
                                          target=tmp_path / "m.json").read_text(encoding="utf-8"))["mcpServers"]
    assert "onec_code" not in servers
    assert not [t for t in allowed_tools(None, False, settings.onec_code.enabled) if "onec_code" in t]
    assert allowed_tools(None, False, True)[-3:] == [
        "mcp__onec_code__search_1c", "mcp__onec_code__get_module", "mcp__onec_code__find_object"]


def test_mcp_server_over_stdio(repo_root, tmp_path, dump):
    """Тот же путь, что у бота: конфиг моста, отдельный процесс, пустая рабочая папка."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    ix.run(ix.load_config(dump), pull=False)
    settings = Settings.load(repo_root / "config" / "settings.example.yaml")
    settings.onec_code = OnecCodeSettings(dump, python=sys.executable)
    server = json.loads(build_mcp_config(settings, base_id=None, outbox=tmp_path / "out", attachments=tmp_path / "att",
                                         target=tmp_path / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["onec_code"]
    work = tmp_path / "work"
    work.mkdir()

    async def session():
        params = StdioServerParameters(command=server["command"], args=server["args"],
                                       env={**os.environ, **server["env"]}, cwd=str(work))
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            tools = sorted(t.name for t in (await s.list_tools()).tools)
            calls = [await s.call_tool(name, args) for name, args in [
                ("search_1c", {"query": "где считается пеня", "limit": 3}),
                ("get_module", {"path": "РасчетПени", "procedure": "РассчитатьПеню"}),
                ("find_object", {"name": "РеализацияТоваров"}),
                ("get_module", {"path": "НетТакогоМодуля"}),
            ]]
            return tools, calls

    tools, (found, module, obj, missing) = asyncio.run(session())
    assert tools == ["find_object", "get_module", "search_1c"]
    assert "cfg/CommonModules/РасчетПени/Ext/Module.bsl:3-16" in found.content[0].text
    assert "9: Функция РассчитатьПеню(Контрагент" in module.content[0].text
    assert "Табличная часть Товары" in obj.content[0].text
    assert missing.is_error and "не найден" in missing.content[0].text


def test_word_boundary_and_description(index):
    # «рол» — начало слова в «РолиДоступны» и «ролей», но середина слова в «Контроль».
    assert om._match_quality("РолиДоступны", "ролидоступны", ["рол"]) == 1.0
    assert om._match_quality("одной из ролей", "одной из ролей", ["рол"]) == 1.0
    assert om._match_quality("КонтрольОстатков", "контрольостатков", ["рол"]) == 0.4
    assert om._match_quality("Остатки", "остатки", ["рол"]) == 0.0
    # Комментарий над процедурой лежит в отдельной колонке doc: слова вопроса находятся по описанию.
    conn, _ = index
    doc = conn.execute("SELECT chunks_fts.doc FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid "
                       "WHERE c.proc_name='РассчитатьПеню'").fetchone()[0]
    assert doc.startswith("Рассчитывает пеню по просроченной задолженности") and "//" not in doc
    hits, _ = om.search_chunks(conn, "просроченная задолженность контрагента", 3)
    assert hits[0].proc_name == "РассчитатьПеню"
