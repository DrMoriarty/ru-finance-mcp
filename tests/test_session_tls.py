"""TLS-конфигурация единой сессии (ru_finance/session.py).

Проверяются env-переменные REQUESTS_CA_BUNDLE / REQUESTS_CERT / SSL_VERIFY /
SSL_TRUSTSTORE, fail-fast на CERTIFICATE_VERIFY_FAILED (без ретраев) и
прокидывание настроенного niquests-коннектора в aioboy/moex.
"""
import ssl
import sys

import niquests
import pytest
import requests
import responses

from ru_finance import session

TLS_ENV = ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "REQUESTS_CERT",
           "SSL_VERIFY", "SSL_TRUSTSTORE")


def _ssl_verify_error() -> requests.exceptions.SSLError:
    """requests-обёртка над ssl.SSLCertVerificationError — как из urllib3."""
    inner = ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
           "self-signed certificate in certificate chain (_ssl.c:1000)"
    )
    e = requests.exceptions.SSLError(inner)
    e.__cause__ = inner
    return e


@pytest.fixture
def fresh_session(monkeypatch):
    """Сбросить singleton сессии и TLS-env, чтобы тест шёл с чистого листа."""
    monkeypatch.setattr(session, "_session", None)
    monkeypatch.setattr(session, "_truststore_injected", False)
    for name in TLS_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# ─────────────────────────── verify / CA-bundle ───────────────────────────

def test_default_verify_is_true(fresh_session):
    # по умолчанию безопасность не понижается
    assert session.get_session().verify is True


def test_session_is_singleton(fresh_session):
    assert session.get_session() is session.get_session()


def test_requests_ca_bundle_used(fresh_session, tmp_path):
    bundle = tmp_path / "proxy-ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
    fresh_session.setenv("REQUESTS_CA_BUNDLE", str(bundle))

    assert session.get_session().verify == str(bundle)


def test_curl_ca_bundle_fallback(fresh_session, tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("x")
    fresh_session.setenv("CURL_CA_BUNDLE", str(bundle))

    assert session.get_session().verify == str(bundle)


def test_missing_ca_bundle_warns_but_is_used(fresh_session, tmp_path):
    # путь невалиден — предупреждаем, но не молча подменяем на True:
    # requests сам даст понятную ошибку про invalid path
    fresh_session.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "nope.pem"))

    with pytest.warns(UserWarning, match="REQUESTS_CA_BUNDLE"):
        s = session.get_session()
    assert s.verify == str(tmp_path / "nope.pem")


def test_ssl_verify_off_disables_verification(fresh_session):
    fresh_session.setenv("SSL_VERIFY", "0")

    with pytest.warns(UserWarning, match="SSL_VERIFY"):
        s = session.get_session()
    assert s.verify is False


def test_ssl_verify_garbage_value_keeps_verification(fresh_session):
    # любое значение кроме 0/false/no/off — verify=True (fail closed)
    fresh_session.setenv("SSL_VERIFY", "maybe")

    assert session.get_session().verify is True


# ─────────────────────────── клиентский сертификат ───────────────────────────

def test_requests_cert_single_file(fresh_session, tmp_path):
    cert = tmp_path / "client.pem"
    cert.write_text("x")
    fresh_session.setenv("REQUESTS_CERT", str(cert))

    assert session.get_session().cert == str(cert)


def test_requests_cert_pair(fresh_session):
    fresh_session.setenv("REQUESTS_CERT", "/p/client.pem, /p/client.key")

    assert session.get_session().cert == ("/p/client.pem", "/p/client.key")


def test_requests_cert_unset(fresh_session):
    assert session.get_session().cert is None


# ─────────────────────────── truststore ───────────────────────────

def test_truststore_injection_called(fresh_session, monkeypatch):
    calls: list[bool] = []

    class _FakeTruststore:
        @staticmethod
        def inject_into_ssl():
            calls.append(True)

    monkeypatch.setitem(sys.modules, "truststore", _FakeTruststore())
    fresh_session.setenv("SSL_TRUSTSTORE", "1")

    session.get_session()
    assert calls == [True]


def test_truststore_off_by_default(fresh_session, monkeypatch):
    calls: list[bool] = []

    class _FakeTruststore:
        @staticmethod
        def inject_into_ssl():
            calls.append(True)

    monkeypatch.setitem(sys.modules, "truststore", _FakeTruststore())

    session.get_session()
    assert calls == []


def test_truststore_missing_package_warns(fresh_session, monkeypatch):
    monkeypatch.setitem(sys.modules, "truststore", None)  # import вернёт ImportError
    fresh_session.setenv("SSL_TRUSTSTORE", "1")

    with pytest.warns(UserWarning, match="truststore"):
        session.get_session()


# ─────────────────────────── диагностика SSL ───────────────────────────

def test_raise_if_ssl_verify_error_raises_friendly():
    with pytest.raises(session.SSLVerifyFailed, match="REQUESTS_CA_BUNDLE") as info:
        session.raise_if_ssl_verify_error(_ssl_verify_error())
    # оригинал сохранён как причина (requests-обёртка -> ssl-исключение)
    assert isinstance(info.value.__cause__.__cause__, ssl.SSLCertVerificationError)


def test_raise_if_ssl_verify_error_passes_other_errors():
    session.raise_if_ssl_verify_error(requests.exceptions.ReadTimeout("t"))
    session.raise_if_ssl_verify_error(ValueError("x"))


def test_detects_error_deep_in_chain():
    # urllib3: ConnectionError -> SSLError -> SSLCertVerificationError
    inner = ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
    )
    mid = requests.exceptions.SSLError(inner)
    mid.__cause__ = inner
    outer = requests.exceptions.ConnectionError(mid)
    outer.__cause__ = mid
    with pytest.raises(session.SSLVerifyFailed):
        session.raise_if_ssl_verify_error(outer)


def test_plain_ssl_error_without_verify_marker_passes():
    e = requests.exceptions.SSLError("handshake timeout")
    session.raise_if_ssl_verify_error(e)  # не должен поднять


# ─────────────────────────── fail-fast вместо ретраев ───────────────────────────

@responses.activate
def test_raw_get_fails_fast_on_ssl_verify_error(fresh_session, monkeypatch):
    # SSL verify не чинится повторами — ретраев быть не должно
    monkeypatch.setattr("ru_finance.session.time.sleep", lambda _s: None)
    responses.get(f"{session.ISS}/securities/SBER.json", body=_ssl_verify_error())

    with pytest.raises(session.SSLVerifyFailed, match="корпоративный"):
        session.raw_get("securities/SBER", retries=4)

    assert len(responses.calls) == 1


@responses.activate
def test_raw_get_still_retries_other_errors(fresh_session, monkeypatch):
    monkeypatch.setattr("ru_finance.session.time.sleep", lambda _s: None)
    url = f"{session.ISS}/securities/SBER.json"
    responses.get(url, body=requests.exceptions.ReadTimeout("t1"))
    responses.get(url, json={"securities": {"columns": ["SECID"], "data": [["SBER"]]}})

    assert session.raw_get("securities/SBER")["securities"]["data"] == [["SBER"]]
    assert len(responses.calls) == 2


def test_exec_template_fails_fast_on_ssl_verify_error(monkeypatch):
    class _FakeMoexSSL:
        def __enter__(self):
            return self

        def render_url(self, template_id, **kw):
            return "https://iss.moex.com/fake.json"

        def execute(self, url, **kw):
            raise _ssl_verify_error()

    monkeypatch.setattr(session, "get_moex", lambda: _FakeMoexSSL())
    monkeypatch.setattr("ru_finance.session.time.sleep", lambda _s: None)

    with pytest.raises(session.SSLVerifyFailed):
        session.exec_template(205)


# ─────────────────────────── коннектор aioboy/moex ───────────────────────────

class _FakeMoex:
    """Ловит kwargs конструктора Moex (нас интересует connector)."""

    created: list[dict] = []

    def __init__(self, *args, **kwargs):
        _FakeMoex.created.append(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_get_moex_passes_configured_niquests_connector(fresh_session, monkeypatch, tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("x")
    fresh_session.setenv("REQUESTS_CA_BUNDLE", str(bundle))

    monkeypatch.setattr(session, "Moex", _FakeMoex)
    monkeypatch.setattr(session, "_client", None)
    _FakeMoex.created = []

    session.get_moex()

    kwargs = _FakeMoex.created[0]
    connector = kwargs["connector"]
    assert isinstance(connector, niquests.Session)
    # TLS-конфигурация окружения прокинута в коннектор aioboy
    assert connector.verify == str(bundle)
