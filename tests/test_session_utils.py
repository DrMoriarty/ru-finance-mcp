"""Тесты ru_finance/session.py.

Часть 1 — чистые функции records/first (без сети).
Часть 2 — сетевые помощники: raw_get (ретраи на responses) и
singleton get_moex (Moex подменяется фейком, реальной загрузки шаблонов нет).
"""
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


def test_get_moex_singleton(monkeypatch):
    monkeypatch.setattr(session, "Moex", _FakeMoex)
    monkeypatch.setattr(session, "_client", None)  # изолируем глобальный синглтон
    _FakeMoex.created = []

    c1 = session.get_moex()
    c2 = session.get_moex()

    assert c1 is c2
    # клиент создаётся ровно один раз — шаблоны aioboy не перезагружаются
    assert len(_FakeMoex.created) == 1
