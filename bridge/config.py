"""Настройки моста: config/settings.yaml. Секреты — только в переменных окружения."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from common.doc_index import ExcludeRule
from common.dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent


def repo_path(value: str | Path) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (REPO_ROOT / p).resolve()


@dataclass
class ClaudeSettings:
    binary: str = "claude"
    model: str = "claude-opus-5-5"   # только полное имя: короткое «opus» ведёт на старую версию
    effort: str = "high"
    config_dir: str = ""             # отдельный профиль Claude Code для бота (см. README)
    timeout_seconds: int = 900
    max_turns: int = 60
    mcp_timeout_ms: int = 30000      # ждать MCP-серверы до первого хода

    def __post_init__(self) -> None:
        # Процесс Claude стартует в пустой папке сессии, поэтому путь вида
        # «scripts/mock_claude.cmd» резолвим здесь. Голое «claude» — поиск по PATH.
        p = Path(self.binary)
        if not p.is_absolute() and p.name != self.binary:
            self.binary = str(repo_path(p))
        if self.config_dir:
            self.config_dir = str(repo_path(self.config_dir))


@dataclass
class ConnectSettings:
    # colleague — личные сообщения сотрудников учётной записи бота (приём через
    # «API приложений», ответ через REST); line — линия поддержки и вебхук.
    channel: str = "colleague"
    agent_login_env: str = "CONNECT_AGENT_LOGIN"   # логин учётки «ГК Эксперт | ИИ-помощник»
    api_base_url: str = "https://push.1c-connect.com"
    api_login_env: str = "CONNECT_API_LOGIN"
    api_password_env: str = "CONNECT_API_PASSWORD"
    webhook_token_env: str = "CONNECT_WEBHOOK_TOKEN"
    line_id: str = ""
    bot_specialist_id: str = ""
    ack_text: str = "Принял, работаю над ответом…"
    max_message_chars: int = 4000
    request_timeout_seconds: int = 60
    file_download_hosts: list[str] = field(default_factory=lambda: ["buhphone.com", "1c-connect.com"])
    max_attachment_mb: int = 20
    # Приём личных сообщений: history — опрос SOAP-истории (работает с новым
    # «1C-Connect Desktop»), pipe — «API приложений» старого клиента.
    receive: str = "history"
    history_colleagues: list[str] = field(default_factory=list)   # пусто — вся переписка бота одним запросом
    history_interval_seconds: float = 75.0   # лимит API — 100 запросов в час на всех
    history_hours: float = 6.0
    webhook_mode: str = "tunnel"     # tunnel — HTTPS через Cloudflare без домена; url — свой адрес
    public_url: str = ""             # для webhook_mode: url, например https://bot.example.kz

    def __post_init__(self) -> None:
        # Те же имена переменных, что были у старого бота на сервере.
        self.line_id = self.line_id or os.environ.get("CONNECT_LINE_ID", "")
        self.bot_specialist_id = self.bot_specialist_id or os.environ.get("CONNECT_BOT_SPECIALIST_ID", "")

    @property
    def agent_login(self) -> str:
        return os.environ.get(self.agent_login_env, "")

    @property
    def login(self) -> str:
        return os.environ.get(self.api_login_env, "")

    @property
    def password(self) -> str:
        return os.environ.get(self.api_password_env, "")

    @property
    def webhook_token(self) -> str:
        return os.environ.get(self.webhook_token_env, "")


@dataclass
class ApiClient:
    name: str
    token_env: str
    bases: list[str] | str = "*"

    @property
    def token(self) -> str:
        return os.environ.get(self.token_env, "")


@dataclass
class KnowledgeBaseSettings:
    root: Path
    index_path: Path
    exclude: list[ExcludeRule] = field(default_factory=list)


@dataclass
class OnecCodeSettings:
    """Поиск по коду 1С (onec_rag): индекс строит Планировщик задач, см. onec_rag/README.md."""
    config_path: Path
    python: str = ""                 # пусто — onec_rag/.venv, если он есть, иначе Python моста

    @property
    def enabled(self) -> bool:
        return self.config_path.is_file()


@dataclass
class Settings:
    claude: ClaudeSettings
    connect: ConnectSettings
    api_clients: list[ApiClient]
    knowledge_base: KnowledgeBaseSettings
    users_path: Path
    bases_path: Path
    data_dir: Path
    idle_timeout_minutes: int = 60
    python: str = sys.executable
    onec_code: OnecCodeSettings = field(
        default_factory=lambda: OnecCodeSettings(REPO_ROOT / "onec_rag" / "config.json"))

    @property
    def counterparty_check_enabled(self) -> bool:
        return bool(os.environ.get("COUNTERPARTY_CHECK_URL", "").strip())

    def bridge_only_env_names(self) -> set[str]:
        """Переменные, которые нужны только мосту: в процесс агента не передаются."""
        names = {self.connect.api_login_env, self.connect.api_password_env, self.connect.webhook_token_env}
        names |= {c.token_env for c in self.api_clients}
        return names

    @staticmethod
    def load(path: Path | None = None) -> "Settings":
        if path is None:
            # Боевой запуск: секреты из окружения, для разработки — из .env.
            load_dotenv(REPO_ROOT / ".env")
        path = path or REPO_ROOT / "config" / "settings.yaml"
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        kb = raw.get("knowledge_base", {})
        session = raw.get("session", {})
        code = raw.get("onec_code", {}) or {}
        return Settings(
            claude=ClaudeSettings(**raw.get("claude", {})),
            connect=ConnectSettings(**raw.get("connect", {})),
            api_clients=[ApiClient(**c) for c in (raw.get("api", {}) or {}).get("clients", []) or []],
            knowledge_base=KnowledgeBaseSettings(
                root=repo_path(kb.get("root", "База знаний")),
                index_path=repo_path(kb.get("index_path", ".index/kb.sqlite3")),
                exclude=[ExcludeRule(e["pattern"], e.get("reason", "")) for e in kb.get("exclude", []) or []],
            ),
            users_path=repo_path(raw.get("users_path", "config/users.yaml")),
            bases_path=repo_path(raw.get("bases_path", "config/bases.yaml")),
            data_dir=repo_path(session.get("data_dir", ".data")),
            idle_timeout_minutes=session.get("idle_timeout_minutes", 60),
            python=raw.get("python") or sys.executable,
            onec_code=OnecCodeSettings(repo_path(code.get("config_path", "onec_rag/config.json")),
                                       code.get("python", "")),
        )
