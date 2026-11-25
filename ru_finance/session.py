"""Общий клиент aioboy/moex + надёжные HTTP-помощники.

aioboy/moex кэширует свои ~252 ISS-шаблона на уровне класса, поэтому они
загружаются один раз глобально. Держим один открытый коннектор на всё время
жизни MCP-сервера и добавляем свой цикл ретраев — iss.moex.com доступен, но
капризен (случайные таймауты).

TLS / корпоративный MITM-прокси
-------------------------------
Все прямые HTTP-вызовы идут через единую сессию :func:`get_session` (requests)
или через коннектор, отданный в aioboy/moex. Конфигурация TLS — только через
окружение, в коде verify нигде не отключается «молча»:

``REQUESTS_CA_BUNDLE`` (или ``CURL_CA_BUNDLE``)
    путь к CA-bundle; сюда кладут корневой сертификат корпоративного прокси,
    который подменяет сертификаты iss.moex.com / cbr.ru / smart-lab.ru и т.д.
``REQUESTS_CERT``
    клиентский сертификат для mTLS: путь к PEM или ``"cert.pem,key.pem"``.
``SSL_VERIFY``
    ``0/false/no/off`` — отключить проверку сертификата целиком
    (небезопасно, логирует предупреждение; по умолчанию verify=True).
``SSL_TRUSTSTORE``
    ``1/true/yes/on`` — перед созданием сессий вызвать
    ``truststore.inject_into_ssl()``: библиотека подменяет stdlib-SSL-контекст
    на системное хранилище доверия (macOS Keychain, Windows store, /etc/ssl).
    Это самый простой способ пройти корпоративный MITM-прокси, если корневой
    сертификат уже установлен в системе, — отдельный CA-bundle не нужен.

При ошибке верификации сертификата вместо сырого трейсбека поднимается
:exc:`SSLVerifyFailed` с подсказкой про перечисленные переменные.
"""
from __future__ import annotations

import os
import ssl
import time
import warnings

import niquests
import requests
from moex import Moex

ISS = "https://iss.moex.com/iss"

_client: Moex | None = None
_session: requests.Session | None = None
_truststore_injected = False

_FALSE = {"0", "false", "no", "off"}
_TRUE = {"1", "true", "yes", "on"}


class SSLVerifyFailed(RuntimeError):
    """Проверка TLS-сертификата не прошла (обычно — корпоративный MITM-прокси)."""


# ─────────────────────────── конфигурация TLS ───────────────────────────

def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


def _resolve_verify() -> bool | str:
    """verify-параметр для сессий: True | False | путь к CA-bundle.

    Порядок: явный SSL_VERIFY=0 выключает проверку (с предупреждением),
    иначе REQUESTS_CA_BUNDLE/CURL_CA_BUNDLE, иначе значение по умолчанию True.
    """
    if os.environ.get("SSL_VERIFY", "").strip().lower() in _FALSE:
        return False
    ca = (os.environ.get("REQUESTS_CA_BUNDLE")
          or os.environ.get("CURL_CA_BUNDLE") or "").strip()
    if ca:
        if not os.path.exists(ca):
            warnings.warn(
                f"REQUESTS_CA_BUNDLE указывает на несуществующий файл: {ca!r} — "
                "все TLS-запросы будут падать; проверьте путь"
            )
        return ca
    return True


def _resolve_cert() -> str | tuple[str, str] | None:
    """Клиентский сертификат из REQUESTS_CERT: путь или "cert,key"."""
    cert = os.environ.get("REQUESTS_CERT", "").strip()
    if not cert:
        return None
    if "," in cert:
        c, k = cert.split(",", 1)
        return (c.strip(), k.strip())
    return cert


def _maybe_inject_truststore() -> None:
    """SSL_TRUSTSTORE=1 → системное хранилище доверия вместо certifi (один раз).

    Необратимо на весь процесс, поэтому вызывается лениво — перед первым
    созданием сессии. niquests (коннектор aioboy) собственное хранилище
    (wassima) и так читает системные корни, а requests/urllib3 — нет;
    инъекция выравнивает их поведение.
    """
    global _truststore_injected
    if _truststore_injected or not _env_flag("SSL_TRUSTSTORE"):
        return
    try:
        import truststore
    except ImportError:
        warnings.warn(
            "SSL_TRUSTSTORE=1, но пакет truststore не установлен — "
            "игнорирую (pip install truststore)"
        )
        return
    truststore.inject_into_ssl()
    _truststore_injected = True


def get_session() -> requests.Session:
    """Единая requests.Session с TLS-конфигурацией из окружения (lazy init)."""
    global _session
    if _session is None:
        _maybe_inject_truststore()
        verify = _resolve_verify()
        cert = _resolve_cert()
        s = requests.Session()
        s.verify = verify
        if cert is not None:
            s.cert = cert
        if verify is False:
            warnings.warn(
                "SSL_VERIFY=0: проверка TLS-сертификатов отключена "
                "(небезопасно, трафик может перехватываться)"
            )
        _session = s
    return _session


# ─────────────────────────── диагностика SSL ───────────────────────────

def _is_ssl_verify_error(e: BaseException) -> bool:
    """Ищет по цепочке исключений CERTIFICATE_VERIFY_FAILED (ssl/requests/urllib3)."""
    seen: set[int] = set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(e):
            return True
        e = e.__cause__ or e.__context__
    return False


def raise_if_ssl_verify_error(e: BaseException) -> None:
    """Перевести ошибку верификации сертификата в понятную диагностику.

    Вызывается из catch-блоков ретраев: SSL verify не «рассосётся» от
    повторов, поэтому вместо ретраев и сырого трейсбека сразу поднимаем
    SSLVerifyFailed с подсказкой про корпоративный прокси и env-переменные.
    """
    if _is_ssl_verify_error(e):
        raise SSLVerifyFailed(
            "Проверка TLS-сертификата не прошла — скорее всего, трафик "
            "перехватывает корпоративный MITM-прокси. Варианты: "
            "(1) положите корневой сертификат прокси в файл и задайте "
            "REQUESTS_CA_BUNDLE=/path/to/proxy-ca.pem; "
            "(2) SSL_TRUSTSTORE=1 — использовать системное хранилище доверия "
            "(если корневой сертификат уже установлен в системе); "
            "(3) SSL_VERIFY=0 — отключить проверку (не рекомендуется)."
        ) from e


# ─────────────────────────── aioboy/moex ───────────────────────────

def _niquests_connector() -> niquests.Session:
    """Настроенный niquests.Session — коннектор для aioboy/moex.

    API aioboy позволяет передать свой коннектор (параметр connector у
    Moex.__init__), иначе его NiquestsSession создавался бы без нашей
    TLS-конфигурации.
    """
    s = niquests.Session()
    s.verify = _resolve_verify()
    cert = _resolve_cert()
    if cert is not None:
        s.cert = cert
    return s


def get_moex() -> Moex:
    """Вернуть singleton-клиент aioboy/moex (шаблоны грузятся один раз)."""
    global _client
    if _client is None:
        c = Moex(output_format=".json", connector=_niquests_connector())
        c.__enter__()  # загрузка шаблонов (сеть, один раз) + открытие коннектора
        _client = c
    return _client


def exec_template(template_id: int, vars: dict | None = None,
                  params: dict | None = None, retries: int = 4) -> dict:
    """render_url(template_id, **vars) -> execute(**params), с ретраями.

    Возвращает сырой dict ISS вида {block: {columns, data, ...}}.
    """
    vars, params = vars or {}, params or {}
    c = get_moex()
    last: Exception | None = None
    for i in range(retries):
        try:
            url = c.render_url(template_id, **vars)
            return c.execute(url, **params).raw
        except Exception as e:
            raise_if_ssl_verify_error(e)
            last = e
            time.sleep(0.5 * (i + 1))
    raise last  # type: ignore[misc]


def raw_get(path: str, params: dict | None = None, retries: int = 4) -> dict:
    """Прямой GET к ISS для эндпоинтов вне шаблонов aioboy (напр. dividends)."""
    params = dict(params or {})
    params.setdefault("iss.meta", "off")
    url = f"{ISS}/{path.lstrip('/')}.json"
    last: Exception | None = None
    for i in range(retries):
        try:
            r = get_session().get(url, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            raise_if_ssl_verify_error(e)
            last = e
            time.sleep(0.5 * (i + 1))
    raise last  # type: ignore[misc]


def records(raw: dict, block: str) -> list[dict]:
    """ISS-блок {columns, data} -> список словарей-строк."""
    b = raw.get(block) or {}
    cols = b.get("columns") or []
    return [dict(zip(cols, row, strict=False)) for row in (b.get("data") or [])]


def first(*vals):
    """Первое не-None/не-пустое значение (для фоллбэков по полям цены)."""
    for v in vals:
        if v is not None and v != "":
            return v
    return None
