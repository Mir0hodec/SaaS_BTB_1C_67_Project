"""Опубликованные базы с расширением MCP_HTTPService (HTTP-сервис /hs/mcp-1c).

База опубликована на веб-сервере, в ней установлено расширение — помощник читает
метаданные, данные запросами, журнал регистрации и формы. Только чтение: записи в
расширении нет. Вход — всегда «Администратор» / «1» (common/onec_login.py).

Сотрудник подключает базу сам: присылает боту ссылку на неё. Бот находит в
сообщении адрес публикации, проверяет расширение и вносит базу в bases.yaml —
разговор сразу продолжается в ней. Ссылкой же сотрудник выбирает базу и потом.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

import requests

from bridge.onec_toolkit import ProbeResult
from common.onec_login import ADMIN_PASSWORD, ADMIN_USER
from mcp_1c.onec_client import OneCError, OneCExtensionClient

_URL_RE = re.compile(r"https?://[^\s<>\"'«»]+", re.I)
_TOOLKIT_RE = re.compile(r"^https?://[^/\s]+:\d{1,5}/mcp/?$", re.I)
# Части адреса после имени публикации: веб-клиент (/ru_RU/, #e1cib/...), сервисы (/hs/, /odata/, /ws/)
_SERVICE_PARTS = {"hs", "odata", "ws", "ru_ru", "en_us", "ru", "en", "e1cib", "e1csys"}
_TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
                      "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def probe(url: str, timeout: float = 30.0) -> ProbeResult:
    client = OneCExtensionClient(url, auth=(ADMIN_USER, ADMIN_PASSWORD), timeout=timeout)
    try:
        version = client.version()
        cfg = client.configuration()
    except OneCError as exc:
        return ProbeResult(ok=False, message=str(exc))
    name = str(cfg.get("name") or "")
    detail = f"расширение {version}, платформа {cfg.get('platform_version') or '?'}"
    return ProbeResult(ok=True, user=ADMIN_USER, password_ok=True, config=name, config_title=name,
                       version=str(cfg.get("version") or ""), detail=detail)


def looks_like_1c(url: str, timeout: float = 10.0) -> bool:
    """Это публикация 1С? Стартовая страница веб-клиента (e1csys) или вход 1С по Basic."""
    try:
        r = requests.get(url.rstrip("/") + "/", timeout=timeout, allow_redirects=True)
    except requests.RequestException:
        return False
    return "1C:Enterprise" in (r.headers.get("WWW-Authenticate") or "") or "e1csys" in r.text[:20000]


def publication_url(raw: str) -> str | None:
    """Ссылка из чата → адрес публикации: «http://сервер/База/ru_RU/#e1cib/…» → «http://сервер/База»."""
    parts = urlsplit(raw.rstrip(".,;:!?)"))
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return None
    segments = []
    for seg in parts.path.split("/"):
        if not seg:
            continue
        if seg.lower() in _SERVICE_PARTS:
            break
        segments.append(seg)
    if not segments:
        return None                     # у публикации всегда есть имя базы в пути
    return f"{parts.scheme.lower()}://{parts.netloc}/" + "/".join(segments)


def _rest(text: str, m: re.Match) -> str:
    return re.sub(r"[ \t]{2,}", " ", text[:m.start()] + text[m.end():]).strip()


def find_links(text: str) -> list[tuple[str, str]]:
    """Все ссылки в сообщении: (ссылка, сообщение без неё)."""
    return [(m.group(0).rstrip(".,;:!?)"), _rest(text, m)) for m in _URL_RE.finditer(text)]


def _norm(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}".lower()


def same_link(base_link: str, raw: str) -> bool:
    """Ссылка из чата ведёт на эту базу? Для публикации годится и адрес веб-клиента (…/ru_RU/#e1cib/…)."""
    return _norm(base_link) in {_norm(raw), _norm(publication_url(raw) or "")}


def is_toolkit_url(raw: str) -> bool:
    """Адрес MCP Toolkit на ПК сотрудника: http://192.168.0.51:6003/mcp."""
    return bool(_TOOLKIT_RE.match(raw))


def find_publication(text: str) -> tuple[str, str] | None:
    """Первая ссылка на опубликованную базу в сообщении → (адрес публикации, сообщение без ссылки)."""
    for m in _URL_RE.finditer(text):
        url = publication_url(m.group(0))
        if url and not is_toolkit_url(m.group(0).rstrip(".,;:!?)")):
            return url, _rest(text, m)
    return None


def base_id_for(url: str, taken: set[str]) -> str:
    """Код базы из имени публикации: латиница, цифры и _ (кириллицу Claude Code вычищает из имён)."""
    name = urlsplit(url).path.rstrip("/").split("/")[-1].lower()
    slug = re.sub(r"[^a-z0-9]+", "_", "".join(_TRANSLIT.get(ch, ch) for ch in name)).strip("_")[:30] or "base"
    if not slug[0].isalpha():
        slug = "b_" + slug
    candidate, n = slug, 2
    while candidate in taken:
        candidate, n = f"{slug}_{n}", n + 1
    return candidate
