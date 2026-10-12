import pytest
import yaml

from common.yaml_edit import YamlEditError, set_value

SETTINGS = """# Настройки бота
claude:
  binary: "claude"              # путь к CLI
  model: "claude-opus-5-5"      # всегда полное имя модели
  effort: "high"

connect:
  receive: "history"            # history — опрос SOAP-истории
  history_colleagues: ["a-1"]   # Бубнов Иван
  history_interval_seconds: 75  # лимит API 100 в час
  ack_text: "Принял, работаю над ответом…"   # пусто — не отправлять

users_path: "config/users.yaml"
"""


def test_replaces_values_and_keeps_comments(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text(SETTINGS, encoding="utf-8")
    set_value(p, ("claude", "model"), "claude-sonnet-5-5")
    set_value(p, ("connect", "history_colleagues"), ["a-1", "b-2"])
    set_value(p, ("connect", "history_interval_seconds"), 90)
    set_value(p, ("connect", "ack_text"), "Секунду, # думаю")
    text = p.read_text(encoding="utf-8")
    assert '  model: "claude-sonnet-5-5"      # всегда полное имя модели' in text
    assert '  history_colleagues: ["a-1", "b-2"]   # Бубнов Иван' in text
    assert "# Настройки бота" in text and "# пусто — не отправлять" in text
    data = yaml.safe_load(text)
    assert data["connect"]["ack_text"] == "Секунду, # думаю"
    assert data["connect"]["history_interval_seconds"] == 90
    assert data["claude"]["effort"] == "high" and data["users_path"] == "config/users.yaml"
    assert (tmp_path / "settings.yaml.bak").exists()


def test_block_list_top_level_key_and_missing_keys(tmp_path):
    p = tmp_path / "users.yaml"
    p.write_text("allow_all: true\ndeny:\n  - x-1\n  - x-2\n\n# Особые права\nusers: {}\n", encoding="utf-8")
    set_value(p, ("deny",), ["x-2"])
    text = p.read_text(encoding="utf-8")
    assert 'deny: ["x-2"]\n\n# Особые права' in text
    set_value(p, ("default_bases",), "*")
    set_value(p, ("connect", "history_hours"), 6)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data == {"allow_all": True, "deny": ["x-2"], "users": {}, "default_bases": "*",
                    "connect": {"history_hours": 6}}


def test_missing_key_inside_existing_section(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text(SETTINGS, encoding="utf-8")
    set_value(p, ("claude", "config_dir"), "")
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["claude"] == {"binary": "claude", "model": "claude-opus-5-5", "effort": "high", "config_dir": ""}
    assert data["connect"]["receive"] == "history"


def test_unparsable_result_leaves_file_untouched(tmp_path):
    p = tmp_path / "settings.yaml"
    original = 'connect:\n  history_colleagues: ["a",\n    "b"]\n'
    p.write_text(original, encoding="utf-8")
    with pytest.raises(YamlEditError):
        set_value(p, ("connect", "history_colleagues"), ["c"])
    assert p.read_text(encoding="utf-8") == original
