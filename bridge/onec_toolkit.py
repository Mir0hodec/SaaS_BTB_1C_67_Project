"""Связь с базой 1С через MCP Toolkit (github.com/ROCTUP/1c-mcp-toolkit).

Сотрудник открывает свою копию базы под «Администратор» с паролем «1» и
запускает в ней обработку MCP_Toolkit.epf в режиме «Встроенный сервер»:
http://<IP-ПК>:6003/mcp, доступ по токену (Authorization: Bearer). /health
отвечает без токена.

Здесь — то, что мост делает сам, до запуска Claude:
  * health() — открыта ли база с запущенным MCP Toolkit (быстро, без токена);
  * probe() — настоящий MCP-разговор: инструменты + проверка учётки.

Проверка учётки: сеанс должен быть открыт под «Администратор», а пароль —
«1». Пароль бот не знает и не спрашивает: 1С хранит отпечаток пароля
(СохраняемоеЗначениеПароля = Base64(SHA-1) …), мы сравниваем его начало с
отпечатком «1». Так рабочий пароль сотрудника не попадёт ни боту, ни в
настройки — если в копии базы стоит другой пароль, бот работать откажется.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import anyio
import requests

ADMIN_USER = "Администратор"
ADMIN_PASSWORD = "1"
PASSWORD_FINGERPRINT = base64.b64encode(hashlib.sha1(ADMIN_PASSWORD.encode()).digest()).decode()

# Инструменты MCP Toolkit, которые получает бот. restart/close_1c_session — нет:
# они перезапускают сеанс 1С сотрудника.
READ_TOOLS = ["get_metadata", "execute_query", "get_event_log", "get_object_by_link", "get_link_of_object",
              "find_references_to_object", "get_access_rights", "get_bsl_syntax_help",
              # Анонимизация MCP Toolkit: ФИО, ИНН, телефоны приходят метками [ORG-00001] и т.п.;
              # итоговый ответ модель прогоняет через этот инструмент — метки снова становятся значениями.
              "submit_for_deanonymization"]
WRITE_TOOLS = ["execute_code"]

# Сравнения — внутри 1С, наружу только «1»/«0»: анонимизация MCP Toolkit может заменить имя
# пользователя или строку, похожую на код, меткой ([PER-00001], [SWIFT-00001]).
_MARK = "ai_bot_check"
CHECK_CODE = (
    "Пользователь = ПользователиИнформационнойБазы.ТекущийПользователь();\n"
    f'Результат = "{_MARK}|" + ?(ИмяПользователя() = "{ADMIN_USER}", "1", "0") + "|"\n'
    f'    + ?(Лев(Пользователь.СохраняемоеЗначениеПароля, {len(PASSWORD_FINGERPRINT)}) = '
    f'"{PASSWORD_FINGERPRINT}", "1", "0") + "|"\n'
    '    + ИмяПользователя() + "|" + Метаданные.Имя + "|" + Метаданные.Синоним + "|" + Метаданные.Версия + "|";'
)
_RESULT_RE = re.compile(_MARK + r"\|([01])\|([01])\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|")

WRONG_PASSWORD = ("Пароль «1» не подходит для пользователя «Администратор». Проверьте, что в вашей копии базы "
                  "у пользователя Администратор поставлен пароль 1, и откройте базу заново.")
WRONG_USER = ("База открыта под пользователем «{user}». Откройте её под «Администратор» с паролем 1 "
              "(ярлыком «1С + MCP Toolkit») и запустите MCP Toolkit заново.")


class ToolkitError(Exception):
    pass


@dataclass
class ProbeResult:
    ok: bool
    message: str = ""
    user: str = ""
    password_ok: bool = False
    config: str = ""
    config_title: str = ""
    version: str = ""
    tools: list[str] = field(default_factory=list)
    checked_at: float = field(default_factory=time.time)


def health_url(mcp_url: str) -> str:
    parts = urlsplit(mcp_url)
    return f"{parts.scheme}://{parts.netloc}/health"


def health(mcp_url: str, timeout: float = 3.0) -> tuple[bool, str]:
    """Открыта ли база с запущенным сервером MCP Toolkit."""
    try:
        r = requests.get(health_url(mcp_url), timeout=timeout)
    except requests.RequestException as exc:
        return False, f"нет связи с {urlsplit(mcp_url).netloc}: {type(exc).__name__}"
    if r.status_code != 200:
        return False, f"MCP Toolkit ответил HTTP {r.status_code}"
    return True, ""


def _text(result) -> str:
    parts = []
    for c in getattr(result, "content", None) or []:
        if getattr(c, "text", None):
            parts.append(c.text)
    structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if structured:
        parts.append(json.dumps(structured, ensure_ascii=False))
    return "\n".join(parts)


async def _session_call(mcp_url: str, token: str, calls: list[tuple[str, dict]], timeout: float) -> tuple[list[str], list[str]]:
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
    import httpx2

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    client = create_mcp_http_client(headers=headers, timeout=httpx2.Timeout(timeout))
    async with client:
        async with streamable_http_client(mcp_url, http_client=client) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = [t.name for t in (await session.list_tools()).tools]
                out = []
                for name, args in calls:
                    res = await session.call_tool(name, args)
                    if getattr(res, "is_error", False) or getattr(res, "isError", False):
                        raise ToolkitError(f"{name}: {_text(res)[:300]}")
                    out.append(_text(res))
                return tools, out


def call(mcp_url: str, token: str, calls: list[tuple[str, dict]], timeout: float = 60.0) -> tuple[list[str], list[str]]:
    return anyio.run(_session_call, mcp_url, token, calls, timeout)


def parse_check(text: str) -> ProbeResult:
    m = _RESULT_RE.search(text.replace("\\n", "\n"))
    if not m:
        raise ToolkitError("MCP Toolkit не вернул результат проверки учётки")
    is_admin, pwd, user, name, title, version = m.groups()
    res = ProbeResult(ok=False, user=user, password_ok=pwd == "1", config=name, config_title=title, version=version)
    if is_admin != "1":
        res.message = WRONG_USER.format(user=user or "без имени")
    elif not res.password_ok:
        res.message = WRONG_PASSWORD
    else:
        res.ok = True
    return res


def probe(mcp_url: str, token: str, timeout: float = 60.0) -> ProbeResult:
    """Полная проверка базы: связь, токен, инструменты, учётка «Администратор»/«1»."""
    alive, why = health(mcp_url)
    if not alive:
        return ProbeResult(ok=False, message=f"База недоступна ({why}). Откройте базу и в MCP Toolkit нажмите "
                                             "«Запустить сервер».")
    try:
        tools, (check_text,) = call(mcp_url, token, [("execute_code", {"code": CHECK_CODE})], timeout)
    except ToolkitError as exc:
        return ProbeResult(ok=False, message=f"MCP Toolkit: {exc}")
    except Exception as exc:  # noqa: BLE001 — сетевые и протокольные ошибки клиента MCP
        text = str(exc) or type(exc).__name__
        if "401" in text or "403" in text:
            text = "токен не подошёл — сверьте токен в панели с токеном в форме MCP Toolkit"
        return ProbeResult(ok=False, message=f"MCP Toolkit не ответил: {text[:300]}")
    try:
        res = parse_check(check_text)
    except ToolkitError as exc:
        return ProbeResult(ok=False, message=str(exc), tools=tools)
    res.tools = tools
    return res
