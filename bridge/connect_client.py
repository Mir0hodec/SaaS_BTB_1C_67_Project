"""Клиент API 1С-Коннект (push.1c-connect.com) по публичной документации:
https://1c-connect.atlassian.net/wiki/spaces/PUBLIC/pages/1349877768/4.2.+API

Используемые методы:
  POST /v1/colleague/send/message/  SendMessagecolleague — личное сообщение сотруднику
  POST /v1/colleague/send/file/     SendFileСollegue — файл в личный чат (multipart: meta + file)
  POST /v1/hook/               SetHook — адрес вебхука (только режим линии)
  POST /v1/line/send/message/  SendMessageLine — текст в чат линии
  POST /v1/line/send/file/     SendFileLine — файл в чат (multipart: meta + file)
Авторизация — HTTP Basic. Ограничения: 20 запросов/с, 600/мин (иначе 429).
"""
from __future__ import annotations

import json
import mimetypes
import time
from pathlib import Path
from urllib.parse import urlparse

import requests


class ConnectError(Exception):
    pass


class ConnectClient:
    def __init__(self, base_url: str, login: str, password: str, timeout: float = 60,
                 retries: int = 3, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.auth = (login, password)
        self.timeout = timeout
        self.retries = retries
        self.http = session or requests.Session()

    def _post(self, path: str, **kwargs) -> requests.Response:
        if not all(self.auth):
            raise ConnectError("Не заданы логин/пароль API 1С-Коннект (переменные окружения)")
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = self.http.post(f"{self.base_url}{path}", auth=self.auth, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:   # сеть/таймаут — повторяем
                last = exc
            else:
                if r.status_code == 429 or r.status_code >= 500:
                    last = ConnectError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
                elif r.status_code >= 400:
                    raise ConnectError(f"{path}: HTTP {r.status_code} {r.text[:300]}")
                else:
                    return r
            if attempt < self.retries:
                time.sleep(1 + attempt * 2)
        raise ConnectError(f"{path}: не удалось после {self.retries + 1} попыток: {last}")

    def send_message(self, *, line_id: str, user_id: str, author_id: str, text: str) -> None:
        self._post("/v1/line/send/message/", json={
            "line_id": line_id, "user_id": user_id, "author_id": author_id,
            "text": text, "bot_as_spec": True,
        })

    def send_file(self, *, line_id: str, user_id: str, author_id: str, path: Path,
                  comment: str | None = None) -> None:
        meta = {"line_id": line_id, "user_id": user_id, "author_id": author_id,
                "file_name": path.name, "bot_as_spec": True}
        if comment:
            meta["comment"] = comment
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            self._post("/v1/line/send/file/", files={
                "meta": (None, json.dumps(meta, ensure_ascii=False), "application/json"),
                "file": (path.name, f, mime),
            })

    def send_colleague_message(self, *, recipient_id: str, author_id: str, text: str) -> None:
        """SendMessagecolleague — личное сообщение сотруднику (поле в API: recepient_id)."""
        self._post("/v1/colleague/send/message/", json={
            "recepient_id": recipient_id, "author_id": author_id, "text": text,
        })

    def send_colleague_file(self, *, recipient_id: str, author_id: str, path: Path,
                            comment: str | None = None) -> None:
        """SendFileСollegue — файл в личный чат сотруднику."""
        meta = {"recepient_id": recipient_id, "author_id": author_id, "file_name": path.name}
        if comment:
            meta["comment"] = comment
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            self._post("/v1/colleague/send/file/", files={
                "meta": (None, json.dumps(meta, ensure_ascii=False), "application/json"),
                "file": (path.name, f, mime),
            })

    def set_hook(self, *, line_id: str, url: str, token: str) -> None:
        self._post("/v1/hook/", json={
            "type": "bot", "id": line_id, "url": url,
            "back_auth": {"type": "bearer", "token": token},
        })

    def download(self, url: str, dest: Path, *, max_bytes: int, auth_hosts: list[str]) -> Path:
        """Скачать вложение. Basic-авторизацию отправляем только на хосты
        1С-Коннект из списка — чужому серверу пароль не уйдёт."""
        host = urlparse(url).hostname or ""
        trusted = any(host == h or host.endswith("." + h) for h in auth_hosts)
        r = self.http.get(url, timeout=self.timeout, stream=True)
        if r.status_code in (401, 403) and trusted and all(self.auth):
            r.close()
            r = self.http.get(url, timeout=self.timeout, stream=True, auth=self.auth)
        if r.status_code >= 400:
            raise ConnectError(f"скачивание вложения: HTTP {r.status_code}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(65536):
                size += len(chunk)
                if size > max_bytes:
                    f.close()
                    dest.unlink(missing_ok=True)
                    raise ConnectError(f"вложение больше {max_bytes // 1_000_000} МБ")
                f.write(chunk)
        return dest
