"""MCP-сервер dev (сборка обработок) по stdio: белый список скиллов и песочница."""
import asyncio
import json
import os
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from bridge.config import Settings
from bridge.mcp_config_builder import allowed_tools, build_mcp_config

REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / "tools" / "cc-1c-skills" / ".claude" / "skills"
pytestmark = pytest.mark.skipif(not (SKILLS / "epf-build").is_dir(),
                                reason="скиллы не установлены (scripts/install_1c_skills.ps1)")


async def _call(cfg: dict, cwd: Path, calls: list[tuple[str, dict]]):
    params = StdioServerParameters(command=cfg["command"], args=cfg["args"], env=cfg["env"], cwd=str(cwd))
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        return [await s.call_tool(name, args) for name, args in calls]


def _dev_server(tmp_path: Path) -> tuple[dict, Path]:
    settings = Settings.load(REPO / "config" / "settings.example.yaml")
    out = tmp_path / "work" / "outbox" / "job1"
    cfg = json.loads(build_mcp_config(settings, base_id=None, outbox=out, attachments=tmp_path / "att",
                                      target=tmp_path / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert "mcp__dev__build" in allowed_tools(None, dev=True)
    return cfg["dev"], tmp_path / "work" / "dev"


def test_guides_skills_and_sandbox(tmp_path):
    cfg, ws = _dev_server(tmp_path)
    (tmp_path / "cwd").mkdir()
    guides, guide, init, files, escape, flag, banned, write, read = asyncio.run(_call(cfg, tmp_path / "cwd", [
        ("list_guides", {}),
        ("read_guide", {"name": "composing-1c-queries"}),
        ("run_skill", {"name": "epf-init", "args": ["-Name", "ЗагрузкаExcel", "-Synonym", "Загрузка из Excel"]}),
        ("list_files", {}),
        ("write_file", {"path": "../../секрет.txt", "content": "x"}),
        ("run_skill", {"name": "epf-init", "args": ["-Name", "X", "-V8Path", "C:/1cv8"]}),
        ("run_skill", {"name": "db-load-cf", "args": []}),
        ("write_file", {"path": "src/ЗагрузкаExcel/Ext/ObjectModule.bsl", "content": "// модуль"}),
        ("read_file", {"path": "src/ЗагрузкаExcel/Ext/ObjectModule.bsl"}),
    ]))
    names = {g["name"] for g in json.loads(guides.content[0].text)} if guides.content[0].text.startswith("[") \
        else set(guides.structured_content["result"][i]["name"] for i in range(len(guides.structured_content["result"])))
    assert {"epf-init", "form-add", "composing-1c-queries"} <= names and "db-load-cf" not in names
    assert "ВЫБРАТЬ" in guide.content[0].text or "запрос" in guide.content[0].text.lower()
    assert not init.is_error, init.content[0].text
    assert any(f.endswith("ЗагрузкаExcel.xml") for f in json.loads(files.content[0].text)) \
        if files.content[0].text.startswith("[") else True
    assert (ws / "src" / "ЗагрузкаExcel.xml").is_file()
    assert escape.is_error and "вне рабочей папки" in escape.content[0].text
    assert flag.is_error and "запрещён" in flag.content[0].text
    assert banned.is_error and "недоступен" in banned.content[0].text
    assert not write.is_error and read.content[0].text == "// модуль"
    assert not (tmp_path / "work" / "секрет.txt").exists()


@pytest.mark.skipif(not os.environ.get("ONEC_TEST_V8_PATH"), reason="нужна платформа 1С: ONEC_TEST_V8_PATH")
def test_build_epf_with_real_platform(tmp_path, monkeypatch):
    """Настоящая сборка: ONEC_TEST_V8_PATH=…/bin, ONEC_TEST_FORMAT=2.16 для 8.3.23."""
    cfg, ws = _dev_server(tmp_path)
    cfg["env"]["V8_PATH"] = os.environ["ONEC_TEST_V8_PATH"]
    (tmp_path / "cwd").mkdir()
    fmt = os.environ.get("ONEC_TEST_FORMAT", "2.17")
    init, build = asyncio.run(_call(cfg, tmp_path / "cwd", [
        ("run_skill", {"name": "epf-init", "args": ["-Name", "ПроверкаСборки", "-FormatVersion", fmt]}),
        ("build", {"source_xml": "src/ПроверкаСборки.xml", "file_name": "Проверка сборки"}),
    ]))
    assert not init.is_error, init.content[0].text
    assert not build.is_error, build.content[0].text
    assert (tmp_path / "work" / "outbox" / "job1" / "Проверка сборки.epf").stat().st_size > 1000
