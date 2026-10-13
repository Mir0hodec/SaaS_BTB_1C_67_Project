"""Тестовая база 1С — проверить помощника целиком без сотрудника и его ПК.

    python scripts/test_base.py create            создать базу, внести её в панель как #тест
    python scripts/test_base.py open              открыть базу с MCP Toolkit (порт 6013)
    python scripts/test_base.py status            цены и проведение поступлений — что исправил бот
    python scripts/test_base.py remove            убрать из панели и удалить базу

Конфигурация «ТестПомощника»: номенклатура, контрагенты, поступления товаров,
регистр остатков. Третье поступление намеренно не проводится — у «Сахар» нет
цены, ошибка записана в журнал регистрации. Есть что найти и что исправить.

В базе пользователь «Администратор» с паролем 1 (--password 2 — проверить отказ
помощника), токен MCP Toolkit уже сохранён в настройках обработки. После
«open» в окне MCP Toolkit остаётся только включить автоподтверждение «Записать».
Нужны платформа 1С и скиллы (scripts/install_1c_skills.ps1).
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from common.win_utf8 import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

from bridge import toolkit_secrets  # noqa: E402
from bridge.config import Settings  # noqa: E402
from common.yaml_edit import YamlEditError, remove_section, set_value  # noqa: E402

BASE_ID = "test_base"
PORT = 6013
DEFAULT_PATH = REPO / ".data" / "test_base"
FORMAT_VERSIONS = {"8.3.23": ("2.16", "Version8_3_23"), "8.3.24": ("2.17", "Version8_3_24"),
                   "8.3.25": ("2.18", "Version8_3_25"), "8.3.26": ("2.19", "Version8_3_26"),
                   "8.3.27": ("2.20", "Version8_3_27")}

OBJECTS = [
    {"type": "Catalog", "name": "Номенклатура", "descriptionLength": 100,
     "attributes": ["Артикул: String(25)", "Цена: Number(15,2)"]},
    {"type": "Catalog", "name": "Контрагенты", "descriptionLength": 100, "attributes": ["ИНН: String(12)"]},
    {"type": "AccumulationRegister", "name": "ОстаткиТоваров", "registerType": "Balance",
     "dimensions": ["Номенклатура: CatalogRef.Номенклатура"],
     "resources": ["Количество: Number(15,3)", "Сумма: Number(15,2)"]},
    {"type": "Document", "name": "ПоступлениеТоваров", "registerRecords": ["AccumulationRegister.ОстаткиТоваров"],
     "attributes": ["Контрагент: CatalogRef.Контрагенты"],
     "tabularSections": {"Товары": ["Номенклатура: CatalogRef.Номенклатура", "Количество: Number(15,3)",
                                    "Цена: Number(15,2)", "Сумма: Number(15,2)"]}},
    {"type": "CommonModule", "name": "ТестПомощникаСервер", "context": "server"},
]
ROLE = {"name": "ПолныеПрава", "synonym": "Полные права", "setForNewObjects": True, "objects": [
    "Configuration.ТестПомощника: Administration, DataAdministration, UpdateDataBaseConfiguration, "
    "ConfigurationExtensionsAdministration, ActiveUsers, EventLog, ExclusiveMode, ThinClient, ThickClient, "
    "WebClient, ExternalConnection, Automation, Output, SaveUserData, InteractiveOpenExtDataProcessors, "
    "InteractiveOpenExtReports, MainWindowModeNormal",
    "Catalog.Номенклатура: @edit", "Catalog.Контрагенты: @edit", "Document.ПоступлениеТоваров: @edit",
    "AccumulationRegister.ОстаткиТоваров: Read, Update, View, Edit"]}

POSTING = """Процедура ОбработкаПроведения(Отказ, РежимПроведения)
	Движения.ОстаткиТоваров.Записывать = Истина;
	Для Каждого Строка Из Товары Цикл
		Если Строка.Цена = 0 Тогда
			ТекстОшибки = СтрШаблон("Поступление %1: у номенклатуры «%2» не указана цена (строка %3)",
				Номер, Строка.Номенклатура, Строка.НомерСтроки);
			ЗаписьЖурналаРегистрации("Проведение.ПоступлениеТоваров", УровеньЖурналаРегистрации.Ошибка,
				Метаданные(), Ссылка, ТекстОшибки);
			ВызватьИсключение ТекстОшибки;
		КонецЕсли;
		Движение = Движения.ОстаткиТоваров.ДобавитьПриход();
		Движение.Период = Дата;
		Движение.Номенклатура = Строка.Номенклатура;
		Движение.Количество = Строка.Количество;
		Движение.Сумма = Строка.Сумма;
	КонецЦикла;
КонецПроцедуры
"""

FILL = """Функция Заполнить(Токен, Пароль) Экспорт
	Если ПользователиИнформационнойБазы.ПолучитьПользователей().Количество() > 0 Тогда
		Возврат "в базе уже есть пользователи — создайте базу заново";
	КонецЕсли;

	Товары = Новый Соответствие;
	Для Каждого Описание Из СтрРазделить("Молоко 2,5%|МЛК-01|450;Хлеб белый|ХЛБ-01|220;Сахар|СХР-01|0;"
			+ "Масло сливочное|МСЛ-01|1650;Чай чёрный|ЧАЙ-01|980", ";") Цикл
		Части = СтрРазделить(Описание, "|");
		Элемент = Справочники.Номенклатура.СоздатьЭлемент();
		Элемент.Наименование = Части[0];
		Элемент.Артикул = Части[1];
		Элемент.Цена = Число(Части[2]);
		Элемент.Записать();
		Товары.Вставить(Части[0], Элемент.Ссылка);
	КонецЦикла;

	Контрагенты = Новый Массив;
	Для Каждого Описание Из СтрРазделить("ТОО «Альфа Трейд»|120340005678;ИП Сергеев|870512300123", ";") Цикл
		Части = СтрРазделить(Описание, "|");
		Элемент = Справочники.Контрагенты.СоздатьЭлемент();
		Элемент.Наименование = Части[0];
		Элемент.ИНН = Части[1];
		Элемент.Записать();
		Контрагенты.Добавить(Элемент.Ссылка);
	КонецЦикла;

	Поступления = Новый Массив;
	Поступления.Добавить(Новый Структура("Контрагент, Товары", Контрагенты[0], "Молоко 2,5%:40;Хлеб белый:60"));
	Поступления.Добавить(Новый Структура("Контрагент, Товары", Контрагенты[1], "Масло сливочное:10;Чай чёрный:12"));
	Поступления.Добавить(Новый Структура("Контрагент, Товары", Контрагенты[0], "Молоко 2,5%:20;Сахар:50"));
	ДнейНазад = 3;
	Для Каждого Описание Из Поступления Цикл
		Документ = Документы.ПоступлениеТоваров.СоздатьДокумент();
		Документ.Дата = НачалоДня(ТекущаяДатаСеанса()) - ДнейНазад * 86400 + 36000;
		ДнейНазад = ДнейНазад - 1;
		Документ.Контрагент = Описание.Контрагент;
		Для Каждого Позиция Из СтрРазделить(Описание.Товары, ";") Цикл
			Части = СтрРазделить(Позиция, ":");
			Строка = Документ.Товары.Добавить();
			Строка.Номенклатура = Товары[Части[0]];
			Строка.Количество = Число(Части[1]);
			Строка.Цена = Строка.Номенклатура.Цена;
			Строка.Сумма = Строка.Количество * Строка.Цена;
		КонецЦикла;
		Документ.Записать(РежимЗаписиДокумента.Запись);
		Попытка
			Документ.Записать(РежимЗаписиДокумента.Проведение);
		Исключение
			// Третье поступление не проводится намеренно: у «Сахар» нет цены.
		КонецПопытки;
	КонецЦикла;

	Пользователь = ПользователиИнформационнойБазы.СоздатьПользователя();
	Пользователь.Имя = "Администратор";
	Пользователь.ПолноеИмя = "Администратор";
	Пользователь.АутентификацияСтандартная = Истина;
	Пользователь.ПоказыватьВСпискеВыбора = Истина;
	Защита = Новый ОписаниеЗащитыОтОпасныхДействий;
	Защита.ПредупреждатьОбОпасныхДействиях = Ложь;
	Пользователь.ЗащитаОтОпасныхДействий = Защита;
	Пользователь.Пароль = Пароль;
	Пользователь.Роли.Добавить(Метаданные.Роли.ПолныеПрава);
	Пользователь.Записать();
	ХранилищеОбщихНастроек.Сохранить("MCPToolkit", "ТокенВстроенногоСервера", Токен, , "Администратор");
	Возврат "ok";
КонецФункции
"""

APP_MODULE = """Процедура ПриНачалеРаботыСистемы()
	Если Не СтрНачинаетсяС(ПараметрЗапуска, "ai_test_setup;") Тогда
		Возврат;
	КонецЕсли;
	Параметры = Новый Соответствие;
	Для Каждого Пара Из СтрРазделить(Сред(ПараметрЗапуска, 15), ";", Ложь) Цикл
		Позиция = СтрНайти(Пара, "=");
		Если Позиция > 0 Тогда
			Параметры.Вставить(Лев(Пара, Позиция - 1), Сред(Пара, Позиция + 1));
		КонецЕсли;
	КонецЦикла;
	Попытка
		Результат = ТестПомощникаСервер.Заполнить(Параметры["token"], Параметры["pwd"]);
	Исключение
		Результат = ОписаниеОшибки();
	КонецПопытки;
	Запись = Новый ЗаписьТекста(Параметры["out"], КодировкаТекста.UTF8);
	Запись.ЗаписатьСтроку(Результат);
	Запись.Закрыть();
	ЗавершитьРаботуСистемы(Ложь);
КонецПроцедуры
"""


def _platform(settings: Settings) -> tuple[Path, str]:
    bin_dir = Path(settings.dev.platform())
    if not (bin_dir / "1cv8.exe").is_file():
        sys.exit("Не найдена платформа 1С (Program Files\\1cv8\\8.3.*). Укажите dev.v8_path в settings.yaml.")
    version = ".".join(bin_dir.parent.name.split(".")[:3])
    if version not in FORMAT_VERSIONS:
        sys.exit(f"Платформа {bin_dir.parent.name}: поддерживаются 8.3.23–8.3.27")
    return bin_dir, version


def _skill(settings: Settings, name: str, *args: str) -> None:
    script = settings.dev.skills_dir / name / "scripts" / f"{name}.py"
    if not script.is_file():
        sys.exit(f"Нет скилла {name} — выполните scripts/install_1c_skills.ps1")
    res = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0:
        sys.exit(f"{name}: {(res.stdout + res.stderr).strip()[-2000:]}")


def create(settings: Settings, path: Path, password: str) -> None:
    if path.exists():
        sys.exit(f"База уже есть: {path}. Сначала: python scripts/test_base.py remove")
    bin_dir, version = _platform(settings)
    fmt, compat = FORMAT_VERSIONS[version]
    work = Path(tempfile.mkdtemp(prefix="ai-test-base-"))
    try:
        src = work / "src"
        print(f"== Конфигурация «ТестПомощника» (платформа {version}, формат {fmt})")
        _skill(settings, "cf-init", "-Name", "ТестПомощника", "-Synonym", "Тест помощника", "-OutputDir", str(src),
               "-Version", "1.0.0.1", "-FormatVersion", fmt, "-CompatibilityMode", compat)
        if fmt == "2.16":    # 8.3.23 не знает часть мобильных функциональностей из шаблона cf-init
            cfg = src / "Configuration.xml"
            cfg.write_text(re.sub(r"<UsedMobileApplicationFunctionalities>.*?</UsedMobileApplicationFunctionalities>",
                                  "<UsedMobileApplicationFunctionalities/>", cfg.read_text(encoding="utf-8-sig"),
                                  flags=re.S), encoding="utf-8-sig")
        objects = work / "objects.json"
        objects.write_text(json.dumps(OBJECTS, ensure_ascii=False), encoding="utf-8")
        _skill(settings, "meta-compile", "-JsonPath", str(objects), "-OutputDir", str(src))
        role = work / "role.json"
        role.write_text(json.dumps(ROLE, ensure_ascii=False), encoding="utf-8")
        _skill(settings, "role-compile", "-JsonPath", str(role), "-OutputDir", str(src))
        for rel, text in (("Documents/ПоступлениеТоваров/Ext/ObjectModule.bsl", POSTING),
                          ("CommonModules/ТестПомощникаСервер/Ext/Module.bsl", FILL),
                          ("Ext/ManagedApplicationModule.bsl", APP_MODULE)):
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).write_text(text, encoding="utf-8-sig")

        print(f"== База {path}")
        _skill(settings, "db-create", "-V8Path", str(bin_dir), "-InfoBasePath", str(path))
        _skill(settings, "db-load-xml", "-V8Path", str(bin_dir), "-InfoBasePath", str(path), "-ConfigDir", str(src),
               "-UpdateDB")

        print("== Данные, пользователь «Администратор», токен MCP Toolkit")
        token = secrets.token_hex(16)
        out = work / "setup.txt"
        subprocess.run([str(bin_dir / "1cv8.exe"), "ENTERPRISE", "/F", str(path), "/DisableStartupDialogs",
                        "/DisableStartupMessages", "/C", f"ai_test_setup;token={token};pwd={password};out={out}"],
                       timeout=300)
        result = out.read_text(encoding="utf-8-sig").strip() if out.exists() else "1С не записала результат"
        if result != "ok":
            sys.exit(f"Заполнение базы: {result}")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    try:
        for key, value in (("title", "Тестовая база помощника"), ("tags", ["тест"]),
                           ("toolkit_url", f"http://127.0.0.1:{PORT}/mcp"), ("owners", []), ("write", True)):
            set_value(settings.bases_path, (BASE_ID, key), value)
    except YamlEditError as exc:
        sys.exit(f"bases.yaml: {exc}")
    toolkit_secrets.set_token(BASE_ID, token)
    print(f"Готово. База внесена в панель как #тест ({BASE_ID}).\nДальше: python scripts/test_base.py open")


def open_base(settings: Settings, path: Path, password: str, port: int = PORT) -> None:
    if not (path / "1Cv8.1CD").is_file():
        sys.exit(f"Нет базы {path}. Сначала: python scripts/test_base.py create")
    bin_dir, _ = _platform(settings)
    epf = REPO / "tools" / ("MCP_Toolkit_x86.epf" if "(x86)" in str(bin_dir) else "MCP_Toolkit.epf")
    if not epf.is_file():
        sys.exit(f"Нет tools/{epf.name} — выполните scripts/install_1c_skills.ps1")
    subprocess.Popen([str(bin_dir / "1cv8.exe"), "ENTERPRISE", "/F", str(path), "/N", "Администратор", "/P", password,
                      "/DisableStartupDialogs", "/Execute", str(epf),
                      "/C", f"startup;mode=embedded;port={port};auth=password;pwd={password}"])
    for _ in range(90):
        time.sleep(2)
        try:
            from bridge.onec_toolkit import health
            alive, _why = health(f"http://127.0.0.1:{port}/mcp")
        except Exception:  # noqa: BLE001
            alive = False
        if alive:
            print(f"MCP Toolkit запущен: http://127.0.0.1:{port}/mcp\n"
                  "Для записи в окне MCP Toolkit включите автоподтверждение «Записать».\n"
                  "Проверка: панель → Базы 1С → «Тестовая база помощника» → «Проверить».")
            return
    sys.exit("MCP Toolkit не ответил за 3 минуты — посмотрите окно 1С.")


STATUS_QUERY = (
    "ВЫБРАТЬ \"Номенклатура\" КАК Вид, Н.Наименование КАК Имя, Н.Цена КАК Значение ИЗ Справочник.Номенклатура КАК Н "
    "ОБЪЕДИНИТЬ ВСЕ ВЫБРАТЬ \"Поступление\", Д.Номер, ВЫБОР КОГДА Д.Проведен ТОГДА 1 ИНАЧЕ 0 КОНЕЦ "
    "ИЗ Документ.ПоступлениеТоваров КАК Д")


def status(port: int = PORT) -> None:
    from bridge import onec_toolkit
    url = f"http://127.0.0.1:{port}/mcp"
    alive, why = onec_toolkit.health(url)
    if not alive:
        sys.exit(f"База не открыта ({why}). Сначала: python scripts/test_base.py open")
    _tools, (text,) = onec_toolkit.call(url, toolkit_secrets.get_token(BASE_ID),
                                        [("execute_query", {"query": STATUS_QUERY})])
    data = json.loads(text).get("data", text)
    print(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=1))
    print("Поступление: 1 — проведено, 0 — нет. У «Сахар» исходно цена 0, третье поступление не проведено.")


def remove(settings: Settings, path: Path) -> None:
    try:
        remove_section(settings.bases_path, BASE_ID)
    except (YamlEditError, FileNotFoundError):
        pass
    toolkit_secrets.set_token(BASE_ID, None)
    if path.exists():
        shutil.rmtree(path)
    print("Тестовая база удалена.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["create", "open", "status", "remove"])
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--password", default="1", help="пароль Администратора в тестовой базе (по умолчанию 1)")
    parser.add_argument("--port", type=int, default=PORT, help="порт MCP Toolkit для open")
    args = parser.parse_args()
    settings = Settings.load()
    {"create": lambda: create(settings, args.path, args.password),
     "open": lambda: open_base(settings, args.path, args.password, args.port),
     "status": lambda: status(args.port),
     "remove": lambda: remove(settings, args.path)}[args.action]()


if __name__ == "__main__":
    main()
