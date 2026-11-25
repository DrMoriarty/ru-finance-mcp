"""Классификация ошибок ретраев (ru_finance/session.py).

Транзиентные (таймауты, ConnectionError, HTTP 5xx) — ретраятся с нарастающим
sleep; детерминированные (4xx, SSL, битый JSON, ошибки шаблонов aioboy) —
падают с первой попытки, обёрнутые в NonRetryableError / SSLVerifyFailed.
Число попыток проверяется по счётчику вызовов.
"""
import ssl

import niquests
import pytest
import requests
import responses

from ru_finance import session

URL = f"{session.ISS}/securities/SBER.json"

_OK = {"securities": {"columns": ["SECID"], "data": [["SBER"]]}}


def _http_error(status: int, family=requests.exceptions):
    """HTTPError с прикреплённым response — как из raise_for_status()."""
    resp = requests.Response()
    resp.status_code = status
    return family.HTTPError(f"{status} error", response=resp)


def _ssl_verify_error(family=requests.exceptions):
    inner = ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
    )
    e = family.SSLError(inner)
    e.__cause__ = inner
    return e


def _fake_moex(execute_results, render_error=None):
    """Стенд-ин Moex: execute выдаёт заготовленные результаты/исключения."""
    class _Fake:
        calls = 0

        def render_url(self, template_id, **kw):
            if render_error is not None:
                raise render_error
            return "https://iss.moex.com/fake.json"

        def execute(self, url, **kw):
            result = execute_results[min(_Fake.calls, len(execute_results) - 1)]
            _Fake.calls += 1
            if isinstance(result, BaseException):
                raise result
            return result

    return _Fake()


# ─────────────────────────── is_transient ───────────────────────────

@pytest.mark.parametrize("exc", [
    requests.exceptions.ConnectTimeout("t"),
    requests.exceptions.ReadTimeout("t"),
    niquests.exceptions.ConnectTimeout("t"),
    niquests.exceptions.ReadTimeout("t"),
    requests.exceptions.ConnectionError("conn"),
    niquests.exceptions.ConnectionError("conn"),
    _http_error(500),
    _http_error(503),
    _http_error(500, niquests),
])
def test_is_transient_true(exc):
    assert session.is_transient(exc)


@pytest.mark.parametrize("exc", [
    _http_error(400),
    _http_error(403),
    _http_error(404),
    _http_error(404, niquests),
    _ssl_verify_error(),
    _ssl_verify_error(niquests.exceptions),
    requests.exceptions.SSLError("handshake failure"),
    niquests.exceptions.SSLError("certificate unknown"),
    requests.exceptions.JSONDecodeError("bad json", "<html>", 0),
    ValueError("не HTTP-ошибка вообще"),
])
def test_is_transient_false(exc):
    assert not session.is_transient(exc)


def test_is_transient_ssl_hidden_in_chain():
    # urllib3-стиль: ConnectionError, внутри которого спрятан ssl.SSLError
    inner = ssl.SSLError("certificate verify failed")
    outer = requests.exceptions.ConnectionError(inner)
    outer.__cause__ = inner
    assert not session.is_transient(outer)


# ─────────────────────────── raw_get ───────────────────────────

@responses.activate
def test_raw_get_retries_5xx_then_succeeds(fast_retries):
    responses.get(URL, body=_http_error(502))
    responses.get(URL, body=_http_error(500))
    responses.get(URL, json=_OK)

    assert session.raw_get("securities/SBER") == _OK
    assert len(responses.calls) == 3


@responses.activate
def test_raw_get_no_retry_on_4xx(fast_retries):
    responses.get(URL, body=_http_error(404))

    with pytest.raises(session.NonRetryableError, match="404"):
        session.raw_get("securities/SBER", retries=4)

    assert len(responses.calls) == 1  # детерминированная — с первой попытки


@responses.activate
def test_raw_get_no_retry_on_plain_ssl_error(fast_retries):
    responses.get(URL, body=requests.exceptions.SSLError("handshake failure"))

    with pytest.raises(session.NonRetryableError):
        session.raw_get("securities/SBER", retries=4)

    assert len(responses.calls) == 1


@responses.activate
def test_raw_get_no_retry_on_bad_json(fast_retries):
    responses.get(URL, body="<html>not json</html>",
                  content_type="application/json")

    with pytest.raises(session.NonRetryableError, match="Детерминированная"):
        session.raw_get("securities/SBER", retries=4)

    assert len(responses.calls) == 1


@responses.activate
def test_raw_get_deterministic_error_keeps_cause(fast_retries):
    err = _http_error(403)
    responses.get(URL, body=err)

    with pytest.raises(session.NonRetryableError) as info:
        session.raw_get("securities/SBER")

    assert info.value.__cause__ is err


@responses.activate
def test_raw_get_transient_exhausted_raises_original(fast_retries):
    for _ in range(4):
        responses.get(URL, body=requests.exceptions.ReadTimeout("t"))

    with pytest.raises(requests.exceptions.ReadTimeout):
        session.raw_get("securities/SBER", retries=4)

    assert len(responses.calls) == 4


@responses.activate
def test_raw_get_retry_sleeps_growing_on_transient(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("ru_finance.session.time.sleep", sleeps.append)
    responses.get(URL, body=requests.exceptions.ConnectionError("c"))
    responses.get(URL, json=_OK)

    session.raw_get("securities/SBER")

    assert sleeps == [0.5]


# ─────────────────────────── exec_template (aioboy) ───────────────────────────

def test_exec_template_retries_niquests_timeout(monkeypatch, fast_retries):
    ok = type("Data", (), {"raw": _OK})()
    fake = _fake_moex([niquests.exceptions.ReadTimeout("t1"), ok])
    monkeypatch.setattr(session, "get_moex", lambda: fake)

    assert session.exec_template(205) == _OK
    assert fake.calls == 2


def test_exec_template_no_retry_on_4xx(monkeypatch):
    fake = _fake_moex([_http_error(404, niquests)])
    monkeypatch.setattr(session, "get_moex", lambda: fake)

    with pytest.raises(session.NonRetryableError):
        session.exec_template(205, retries=4)

    assert fake.calls == 1


def test_exec_template_retries_on_5xx(monkeypatch, fast_retries):
    ok = type("Data", (), {"raw": _OK})()
    fake = _fake_moex([_http_error(503, niquests), ok])
    monkeypatch.setattr(session, "get_moex", lambda: fake)

    assert session.exec_template(205) == _OK
    assert fake.calls == 2


def test_exec_template_no_retry_on_template_render_error(monkeypatch):
    from moex.exceptions import MoexTemplateRenderError
    fake = _fake_moex([], render_error=MoexTemplateRenderError(["sec"], "/path"))
    monkeypatch.setattr(session, "get_moex", lambda: fake)

    with pytest.raises(session.NonRetryableError, match="MoexTemplateRenderError"):
        session.exec_template(205, retries=4)

    assert fake.calls == 0  # упало на render_url, до execute не дошло


def test_exec_template_retries_on_empty_response(monkeypatch, fast_retries):
    # aioboy возвращает None на не-200/не-JSON (status съеден внутри moex):
    # считаем транзиентным — ISS «капризен» именно так
    ok = type("Data", (), {"raw": _OK})()
    fake = _fake_moex([None, ok])
    monkeypatch.setattr(session, "get_moex", lambda: fake)

    assert session.exec_template(205) == _OK
    assert fake.calls == 2


def test_exec_template_no_retry_on_ssl_verify_error(monkeypatch):
    fake = _fake_moex([_ssl_verify_error(niquests.exceptions)])
    monkeypatch.setattr(session, "get_moex", lambda: fake)

    with pytest.raises(session.SSLVerifyFailed):
        session.exec_template(205, retries=4)

    assert fake.calls == 1
