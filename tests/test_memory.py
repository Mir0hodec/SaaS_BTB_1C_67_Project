"""Память разговора: что ломало контекст в журнале 14–16.10 и как это починено.

Заглушка CLI (scripts/mock_claude.py) помнит разговоры, как настоящий Claude Code:
--resume неизвестного разговора — ошибка, а в ответе — номер вопроса в разговоре.
"""
import platform
import re
import shutil

import pytest
from fastapi.testclient import TestClient

from bridge.app import create_app
from bridge.assistant import Assistant, Job
from bridge.config import Settings
from bridge.store import Store
from common.yaml_edit import set_value

windows_only = pytest.mark.skipif(platform.system() != "Windows", reason="заглушка CLI — .cmd-шим Windows")
LINE, BOT, USER = "line-1", "bot-spec", "00000000-0000-0000-0000-000000000001"
DEMO = "http://demo.local/Demo"


@pytest.fixture
def settings(repo_root, tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "users.yaml").write_text(f'allow_all: false\nusers:\n  "{USER}": {{name: "Иванов И.", bases: "*"}}\n',
                                    encoding="utf-8")
    shutil.copy(repo_root / "config" / "bases.example.yaml", cfg / "bases.yaml")
    set_value(cfg / "bases.yaml", ("demo_base", "url"), DEMO)
    s = Settings.load(repo_root / "config" / "settings.example.yaml")
    s.users_path, s.bases_path, s.data_dir = cfg / "users.yaml", cfg / "bases.yaml", tmp_path / "data"
    s.knowledge_base.root, s.knowledge_base.index_path = tmp_path / "kb", tmp_path / "kb.sqlite3"
    s.claude.binary = str(repo_root / "scripts" / "mock_claude.cmd")
    s.claude.config_dir = ""
    s.connect.line_id, s.connect.bot_specialist_id = LINE, BOT
    monkeypatch.setenv("CONNECT_WEBHOOK_TOKEN", "hook-secret")
    return s


@pytest.fixture
def bot(settings):
    a = Assistant(settings, Store(settings.data_dir / "bridge.sqlite3"))
    user = a.users()[USER]
    return a, lambda text: a.handle(Job("connect", user, text))


def _session(reply) -> tuple[str, int]:
    found = re.findall(r"Сессия: ([0-9a-f-]+), вопрос №(\d+)", reply.text)
    assert found, reply.text
    return found[-1][0], int(found[-1][1])          # последнее: в напоминании истории есть и прошлые


@windows_only
def test_link_alone_then_question(bot):
    """Одна ссылка без вопроса: раньше следующий вопрос продолжал разговор, которого у Claude не было."""
    _, ask = bot
    assert ask(DEMO).status == "command"
    reply = ask("остатки по кассе")
    assert reply.status == "ok" and "Демо-база" in reply.text
    assert _session(reply)[1] == 1
    assert "Работаю с базой" in reply.text                 # заметка: Claude знает, что мост подключил базу


@windows_only
def test_errors_keep_the_conversation(bot):
    """Лимит ходов и разовая ошибка больше не стирают разговор; две ошибки подряд — новый, с напоминанием."""
    a, ask = bot
    first = ask(f"{DEMO} найди документ поступления")
    sid, n = _session(first)
    long_task = ask("исправь все документы [mock:max-turns]")
    assert long_task.status == "error" and "продолжай" in long_task.text
    again = ask("продолжай")
    assert _session(again) == (sid, n + 2)                  # тот же разговор: Claude помнит задачу
    assert ask("ещё раз [mock:error]").status == "error"
    assert _session(ask("а теперь?"))[0] == sid             # одна ошибка — разговор тот же
    ask("сбой [mock:error]")
    ask("и снова [mock:error]")                              # две подряд — разговор сломан
    fresh = ask("что там с документом?")
    new_sid, n2 = _session(fresh)
    assert new_sid != sid and n2 == 1
    assert "Предыдущий разговор прервался" in fresh.text and "найди документ поступления" in fresh.text
    assert "Демо-база" in fresh.text                         # база осталась


@windows_only
def test_resume_of_missing_conversation_restarts_with_recap(bot, settings):
    _, ask = bot
    sid, _ = _session(ask(f"{DEMO} покажи остатки"))
    for f in settings.data_dir.rglob(sid):                   # транскрипт Claude пропал
        f.unlink()
    reply = ask("а по складу?")
    assert reply.status == "ok" and _session(reply)[0] != sid
    assert "Предыдущий разговор прервался" in reply.text and "покажи остатки" in reply.text


@windows_only
def test_bridge_replies_reach_claude_as_notes(bot):
    a, ask = bot
    a.store.set_state("paused", "x")
    assert ask("вопрос, пока бот на паузе").status == "paused"
    a.store.set_state("paused", None)
    reply = ask("ну что?")
    assert "Перед этим в чате" in reply.text and "вопрос, пока бот на паузе" in reply.text
    assert "Перед этим в чате" not in ask("спасибо").text   # заметка ушла один раз


@windows_only
def test_continue_returns_yesterdays_conversation(bot):
    a, ask = bot
    sid, n = _session(ask(f"{DEMO} задача: сверить остатки"))
    a.store.reset_session(USER)                              # /новый (или 8 часов тишины)
    other, _ = _session(ask("как начислить пени?"))
    listing = ask("/разговоры").text
    assert "сверить остатки" in listing and "пени" not in listing      # текущий разговор не в списке
    back = ask("/продолжить")
    assert back.status == "command" and "Вернулся к разговору" in back.text and "Демо-база" in back.text
    reply = ask("продолжаем")
    assert _session(reply) == (sid, n + 1) and "Демо-база" in reply.text
    assert "вернулся к этому разговору" in reply.text
    assert ask("/продолжить 7").text.startswith("Нет разговора")


@windows_only
def test_continue_without_transcript_retells_history(bot, monkeypatch):
    a, ask = bot
    sid, _ = _session(ask(f"{DEMO} задача: найти ошибку в поступлении"))
    a.store.reset_session(USER)
    monkeypatch.setattr(a.runner, "session_exists", lambda s: False if s == sid else None)
    ask("/продолжить")
    reply = ask("продолжаем")
    assert _session(reply)[0] != sid and "Так он шёл" in reply.text and "найти ошибку в поступлении" in reply.text
    assert "Демо-база" in reply.text


class FakeConnect:
    def __init__(self):
        self.messages = []

    def send_message(self, *, line_id, user_id, author_id, text):
        self.messages.append(text)

    def send_file(self, **kw):
        pass

    def download(self, url, dest, *, max_bytes, auth_hosts):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("Платёжка: получатель ИП Сидоров, 178 000 тенге", encoding="utf-8")
        return dest


def _event(text=None, *, msg_id, mtype=1, file=None):
    e = {"event_type": "line", "message_type": mtype, "line_id": LINE, "user_id": USER, "author_id": USER,
         "message_id": msg_id}
    if text is not None:
        e["text"] = text
    if file:
        e["file"] = file
    return e


@windows_only
def test_screenshot_and_text_become_one_question(settings):
    """Текст пришёл раньше картинки: раньше бот отвечал «ничего не приложено»."""
    settings.connect.batch_seconds = 30
    fake = FakeConnect()
    app = create_app(settings, connect=fake)
    hdr = {"Authorization": "Bearer hook-secret"}
    with TestClient(app) as client:
        client.post("/connect/hook", json=_event("клиент скинул платёжку, что не так?", msg_id="1"), headers=hdr)
        client.post("/connect/hook", headers=hdr, json=_event(msg_id="2", mtype=70, file={
            "file_id": "f1", "file_path": "https://filetransfer.buhphone.com/x", "file_name": "платёжка.txt"}))
        client.post("/connect/hook", json=_event(msg_id="3", mtype=81), headers=hdr)    # служебное — молча
        client.app.state.batcher.flush()
        client.app.state.worker.queue.join()
    answers = [t for t in fake.messages if t.startswith("[mock]")]
    assert len(answers) == 1 and "что не так?" in answers[0] and "ИП Сидоров" in answers[0]
    assert not any("получил. Напишите вопрос" in t for t in fake.messages)


@windows_only
def test_late_screenshot_goes_to_previous_question(settings):
    settings.connect.batch_seconds = 0
    fake = FakeConnect()
    app = create_app(settings, connect=fake)
    hdr = {"Authorization": "Bearer hook-secret"}
    with TestClient(app) as client:
        client.post("/connect/hook", json=_event("что не так с платёжкой?", msg_id="1"), headers=hdr)
        client.app.state.worker.queue.join()
        client.post("/connect/hook", headers=hdr, json=_event(msg_id="2", mtype=70, file={
            "file_id": "f1", "file_path": "https://filetransfer.buhphone.com/x", "file_name": "платёжка.txt"}))
        client.app.state.worker.queue.join()
    answers = [t for t in fake.messages if t.startswith("[mock]")]
    assert len(answers) == 2 and "дослал файл" in answers[1] and "ИП Сидоров" in answers[1]
    assert "вопрос №2" in answers[1]                        # в том же разговоре, что и вопрос
