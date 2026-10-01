import base64
import json
import logging

import pytest
import requests

import counterparty_check_client as cc

BIN = "750710450345"
URL = "http://1c-server.example/gke/hs/CounterpartyCheck/check"
LOGIN, PASSWORD = "svc_check", "S3cr3t-P@ss"
TOKEN = base64.b64encode(f"{LOGIN}:{PASSWORD}".encode()).decode()


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv(cc.ENV_URL, URL)
    monkeypatch.setenv(cc.ENV_LOGIN, LOGIN)
    monkeypatch.setenv(cc.ENV_PASSWORD, PASSWORD)
    monkeypatch.setenv(cc.ENV_TIMEOUT, "15")


@pytest.fixture
def post(monkeypatch):
    """Подменяет requests.post; answer — ответ или исключение для следующего вызова."""
    calls = []

    def fake(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if isinstance(fake.answer, Exception):
            raise fake.answer
        return fake.answer

    fake.answer = FakeResponse(200, {})
    fake.calls = calls
    monkeypatch.setattr(cc.requests, "post", fake)
    return fake


def test_success_clean_counterparty(env, post):
    post.answer = FakeResponse(200, {"counterpartyName": "ТОО Альфа", "bin": BIN, "debtAmount": 0,
                                     "unsignedEavrCount": 0, "unsignedEavrAmount": 0})
    r = cc.check_counterparty(BIN)
    assert (r.status, r.error, r.counterparty_name, r.debt_amount, r.unsigned_eavr_count) == \
        (cc.STATUS_OK, None, "ТОО Альфа", 0.0, 0)
    assert not r.needs_manager
    call = post.calls[0]
    assert call["url"] == URL and call["json"] == {"bin": BIN}
    assert call["auth"] == (LOGIN, PASSWORD) and call["timeout"] == 15.0


def test_debt_goes_to_manager(env, post):
    post.answer = FakeResponse(200, {"counterpartyName": "ИП Бета", "debtAmount": 15000.5, "unsignedEavrCount": 0})
    r = cc.check_counterparty(BIN)
    assert r.status == cc.STATUS_NEEDS_MANAGER and r.needs_manager and r.debt_amount == 15000.5


def test_unsigned_eavr_goes_to_manager(env, post):
    post.answer = FakeResponse(200, {"debtAmount": 0, "unsignedEavrCount": 2, "unsignedEavrAmount": 48000})
    r = cc.check_counterparty(BIN)
    assert r.needs_manager and r.unsigned_eavr_count == 2 and r.unsigned_eavr_amount == 48000.0


def test_snake_case_nested_and_string_numbers(env, post):
    post.answer = FakeResponse(200, {"result": {"counterparty_name": "ТОО Гамма", "debt_amount": "1 234,50",
                                                "unsigned_eavr_count": "0", "unsigned_eavr_amount": "0,00"}})
    r = cc.check_counterparty(f" {BIN[:6]} {BIN[6:]} ")
    assert (r.counterparty_name, r.bin, r.debt_amount, r.unsigned_eavr_count) == ("ТОО Гамма", BIN, 1234.5, 0)
    assert r.needs_manager
    assert r.raw_response["result"]["debt_amount"] == "1 234,50"


def test_negative_debt_is_normal_scenario(env, post):
    post.answer = FakeResponse(200, {"debtAmount": -500, "unsignedEavrCount": 0})   # переплата
    assert cc.check_counterparty(BIN).status == cc.STATUS_OK


def test_calculation_datetime_is_sent(env, post):
    post.answer = FakeResponse(200, {"debtAmount": 0, "unsignedEavrCount": 0})
    cc.check_counterparty(BIN, "2026-09-28T00:00:00")
    assert post.calls[0]["json"] == {"bin": BIN, "calculationDateTime": "2026-09-28T00:00:00"}


@pytest.mark.parametrize("code,kind", [(400, "bad_request"), (401, "unauthorized"),
                                       (403, "forbidden"), (500, "server_error"), (502, "server_error")])
def test_http_errors(env, post, code, kind):
    post.answer = FakeResponse(code, text="Ошибка")
    r = cc.check_counterparty(BIN)
    assert r.status == cc.STATUS_ERROR and r.error.startswith(f"{kind}: HTTP {code}")
    assert not r.needs_manager


def test_timeout(env, post):
    post.answer = requests.Timeout("read timed out")
    r = cc.check_counterparty(BIN)
    assert r.status == cc.STATUS_ERROR and r.error == "timeout: сервис не ответил за 15 с"


def test_network_error(env, post):
    post.answer = requests.ConnectionError("Connection refused")
    r = cc.check_counterparty(BIN)
    assert r.status == cc.STATUS_ERROR and r.error.startswith("network_error: ConnectionError")


@pytest.mark.parametrize("missing", [cc.ENV_URL, cc.ENV_LOGIN, cc.ENV_PASSWORD])
def test_missing_env_no_request(env, post, monkeypatch, missing):
    monkeypatch.delenv(missing)
    r = cc.check_counterparty(BIN)
    assert r.status == cc.STATUS_ERROR and r.error.startswith("config_error") and missing in r.error
    assert post.calls == []


def test_bad_timeout_env_falls_back_to_default(env, post, monkeypatch):
    monkeypatch.setenv(cc.ENV_TIMEOUT, "abc")
    post.answer = FakeResponse(200, {"debtAmount": 0, "unsignedEavrCount": 0})
    cc.check_counterparty(BIN)
    assert post.calls[0]["timeout"] == 15.0


@pytest.mark.parametrize("bad_bin", ["", "12345", "75071045034X", "7507104503451"])
def test_invalid_bin_no_request(env, post, bad_bin):
    r = cc.check_counterparty(bad_bin)
    assert r.error.startswith("invalid_bin") and post.calls == []


def test_response_without_debt_fields_is_error_not_ok(env, post):
    post.answer = FakeResponse(200, {"counterpartyName": "ТОО Альфа", "message": "Контрагент не найден"})
    r = cc.check_counterparty(BIN)
    assert r.status == cc.STATUS_ERROR and "Контрагент не найден" in r.error


def test_non_json_response(env, post):
    post.answer = FakeResponse(200, None, text="<html>Service unavailable</html>")
    assert cc.check_counterparty(BIN).error.startswith("invalid_response")


def test_password_and_authorization_never_logged(env, post, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(cc.ENV_URL, f"http://{LOGIN}:{PASSWORD}@1c-server.example/gke/hs/CounterpartyCheck/check")
    results = []
    # Сервер отдаёт в теле ошибки заголовок и пароль, сетевая ошибка содержит пароль в тексте.
    post.answer = FakeResponse(401, text=f"Authorization: Basic {TOKEN} password={PASSWORD}")
    results.append(cc.check_counterparty(BIN))
    post.answer = requests.ConnectionError(f"failed for {PASSWORD}")
    results.append(cc.check_counterparty(BIN))
    post.answer = FakeResponse(200, {"debtAmount": 0, "unsignedEavrCount": 0})
    results.append(cc.check_counterparty(BIN))

    logged = caplog.text
    assert logged, "лог должен быть"
    for secret in (PASSWORD, TOKEN, f"Basic {TOKEN}"):
        assert secret not in logged
        assert all(secret not in (r.error or "") for r in results)
    assert "Authorization: Basic ***" in results[0].error


def test_cli_prints_result_without_password(env, post, monkeypatch, capsys):
    monkeypatch.setattr(cc, "_load_dotenv", lambda path: None)
    post.answer = FakeResponse(200, {"counterpartyName": "ТОО Альфа", "debtAmount": 100, "unsignedEavrCount": 0})
    assert cc.main([BIN, "--at", "2026-09-28T00:00:00", "-v"]) == 2
    out = capsys.readouterr()
    assert '"status": "needs_manager"' in out.out and "клиент-менеджеру" in out.out
    assert PASSWORD not in out.out + out.err and TOKEN not in out.out + out.err

    post.answer = FakeResponse(200, {"debtAmount": 0, "unsignedEavrCount": 0})
    assert cc.main([BIN]) == 0
    monkeypatch.delenv(cc.ENV_PASSWORD)
    assert cc.main([BIN]) == 1
