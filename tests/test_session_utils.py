"""Тесты ru_finance/session.py.

Часть 1 — чистые функции records/first (без сети).
Часть 2 — сетевые помощники: raw_get (ретраи на responses) и
singleton get_moex (Moex подменяется фейком, реальной загрузки шаблонов нет).
"""
import threading
import time

import pytest
import requests
import responses

from ru_finance import session


# ─────────────────────────── records ───────────────────────────

def test_records_several_blocks():
    raw = {
        "securities": {
            "columns": ["SECID", "PREVPRICE", "SECTYPE"],
            "data": [
                ["SBER", 300.5, "1"],
                ["GAZP", 130.1, "2"],
            ],
        },
        "marketdata": {
            "columns": ["SECID", "LAST"],
            "data": [["SBER", 301.0]],
        },
    }
    assert session.records(raw, "securities") == [
        {"SECID": "SBER", "PREVPRICE": 300.5, "SECTYPE": "1"},
        {"SECID": "GAZP", "PREVPRICE": 130.1, "SECTYPE": "2"},
    ]
    assert session.records(raw, "marketdata") == [
        {"SECID": "SBER", "LAST": 301.0},
    ]


def test_records_missing_block():
    assert session.records({}, "securities") == []


def test_records_empty_block():
    assert session.records({"securities": {"columns": ["A"], "data": []}}, "securities") == []


def test_records_block_without_data():
    # columns есть, data отсутствует — тоже пусто
    assert session.records({"securities": {"columns": ["A"]}}, "securities") == []


def test_records_null_block():
    # ISS иногда отдаёт явный null вместо блока
    assert session.records({"securities": None}, "securities") == []


def test_records_ragged_row():
    # strict=False: короткая строка не падает — zip просто обрезается по ней,
    # недостающие колонки не попадают в dict
    raw = {"securities": {"columns": ["A", "B", "C"], "data": [["x", 1]]}}
    assert session.records(raw, "securities") == [{"A": "x", "B": 1}]


# ─────────────────────────── first ───────────────────────────

def test_first_takes_first_value():
    assert session.first(3, 7, 9) == 3


def test_first_skips_none():
    assert session.first(None, None, 5) == 5


def test_first_skips_empty_string():
    assert session.first(None, "", 5.5) == 5.5


def test_first_all_none():
    assert session.first(None, "", None) is None
    assert session.first() is None


def test_first_zero_is_a_value():
    # 0 — валидное значение (не None и не "")
    assert session.first(None, 0, 2) == 0


# ─────────────────────────── raw_get ───────────────────────────

URL = f"{session.ISS}/securities/SBER.json"


@responses.activate
def test_raw_get_two_timeouts_then_success(fast_retries):
    responses.get(URL, body=requests.exceptions.ConnectTimeout("t1"))
    responses.get(URL, body=requests.exceptions.ConnectTimeout("t2"))
    responses.get(URL, json={"securities": {"columns": ["SECID"], "data": [["SBER"]]}})

    result = session.raw_get("securities/SBER")

    assert result == {"securities": {"columns": ["SECID"], "data": [["SBER"]]}}
    assert len(responses.calls) == 3  # два таймаута + один успех


@responses.activate
def test_raw_get_all_retries_exhausted(fast_retries):
    for _ in range(4):
        responses.get(URL, body=requests.exceptions.ReadTimeout("iss is moody"))

    with pytest.raises(requests.exceptions.ReadTimeout):
        session.raw_get("securities/SBER", retries=4)

    # ровно 4 фактических HTTP-вызова, ни одного лишнего
    assert len(responses.calls) == 4


@responses.activate
def test_raw_get_passes_params_and_iss_meta_default(fast_retries):
    responses.get(URL, json={"ok": True})

    result = session.raw_get("securities/SBER", params={"iss.only": "securities"})

    assert result == {"ok": True}
    req = responses.calls[0].request
    assert "iss.only=securities" in req.url
    # iss.meta добавляется по умолчанию
    assert "iss.meta=off" in req.url


@responses.activate
def test_raw_get_custom_params_not_overridden(fast_retries):
    responses.get(URL, json={})
    session.raw_get("securities/SBER", params={"iss.meta": "full"})
    assert "iss.meta=full" in responses.calls[0].request.url


# ─────────────────────────── get_moex ───────────────────────────

class _FakeMoex:
    """Стенд-ин для Moex: без сети, __enter__ как у настоящего context manager."""

    created: list["_FakeMoex"] = []

    def __init__(self, *args, **kwargs):
        _FakeMoex.created.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _SlowMoex:
    """__enter__ с задержкой — имитация сетевой загрузки шаблонов aioboy."""

    created: list["_SlowMoex"] = []

    def __init__(self, *args, **kwargs):
        _SlowMoex.created.append(self)

    def __enter__(self):
        time.sleep(0.1)
        return self

    def __exit__(self, *args):
        return False


class _FailingMoex:
    """__enter__ падает (сеть недоступна) — шаблоны не загружаются."""

    created: list["_FailingMoex"] = []
    entered = 0

    def __init__(self, *args, **kwargs):
        _FailingMoex.created.append(self)

    def __enter__(self):
        _FailingMoex.entered += 1
        raise ConnectionError("iss.moex.com недоступен")

    def __exit__(self, *args):
        return False


def test_get_moex_singleton(monkeypatch):
    monkeypatch.setattr(session, "Moex", _FakeMoex)
    monkeypatch.setattr(session, "_client", None)  # изолируем глобальный синглтон
    _FakeMoex.created = []

    c1 = session.get_moex()
    c2 = session.get_moex()

    assert c1 is c2
    # клиент создаётся ровно один раз — шаблоны aioboy не перезагружаются
    assert len(_FakeMoex.created) == 1


def test_get_moex_concurrent_init(monkeypatch):
    """N потоков одновременно на пустом singleton — ровно одна инициализация.

    Воспроизводит гонку из crash.log: без lock второй поток входил бы в
    __enter__ параллельно с первым и падал на MappingProxyType-шаблонах.
    """
    monkeypatch.setattr(session, "Moex", _SlowMoex)
    monkeypatch.setattr(session, "_client", None)
    _SlowMoex.created = []

    n = 8
    barrier = threading.Barrier(n)
    results: list = []
    errors: list[Exception] = []

    def worker():
        try:
            barrier.wait()  # все потоки стартуют одновременно
            results.append(session.get_moex())
        except Exception as e:  # pragma: no cover — падение означает регрессию
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == n
    # все потоки получили один и тот же объект
    assert all(c is results[0] for c in results)
    # __enter__ выполнен ровно один раз (не соревновательно)
    assert len(_SlowMoex.created) == 1


def test_get_moex_exception_not_cached(monkeypatch):
    """Упавший __enter__ не оставляет _client в полуинициализированном виде:
    singleton остаётся None, следующий вызов повторяет попытку с нуля."""
    monkeypatch.setattr(session, "Moex", _FailingMoex)
    monkeypatch.setattr(session, "_client", None)
    _FailingMoex.created = []
    _FailingMoex.entered = 0

    with pytest.raises(ConnectionError):
        session.get_moex()

    assert session._client is None
    assert len(_FailingMoex.created) == 1

    # после восстановления сети клиент успешно создаётся при новом вызове
    monkeypatch.setattr(session, "Moex", _FakeMoex)
    _FakeMoex.created = []
    client = session.get_moex()
    assert client is session._client
    assert len(_FakeMoex.created) == 1


# ─────────────────────────── warmup_moex ───────────────────────────

def test_warmup_moex_ok(monkeypatch):
    monkeypatch.setattr(session, "Moex", _FakeMoex)
    monkeypatch.setattr(session, "_client", None)
    _FakeMoex.created = []

    client = session.warmup_moex()

    assert client is session._client
    assert len(_FakeMoex.created) == 1


def test_warmup_moex_failure_does_not_raise(monkeypatch, caplog):
    """Прогрев при недоступной сети только логирует warning — старт сервера
    не валидся, клиент будет создан лениво при первом вызове."""
    monkeypatch.setattr(session, "Moex", _FailingMoex)
    monkeypatch.setattr(session, "_client", None)
    _FailingMoex.created = []
    _FailingMoex.entered = 0

    with caplog.at_level("WARNING", logger="ru_finance.session"):
        result = session.warmup_moex()

    assert result is None
    assert session._client is None
    assert any("Прогрев" in r.message for r in caplog.records)
