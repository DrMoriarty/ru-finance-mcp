"""Общий клиент aioboy/moex + надёжные HTTP-помощники.

aioboy/moex кэширует свои ~252 ISS-шаблона на уровне класса, поэтому они
загружаются один раз глобально. Держим один открытый коннектор на всё время
жизни MCP-сервера и добавляем свой цикл ретраев — iss.moex.com доступен, но
капризен (случайные таймауты).
"""
from __future__ import annotations

import time

import requests
from moex import Moex

ISS = "https://iss.moex.com/iss"

_client: Moex | None = None


def get_moex() -> Moex:
    """Вернуть singleton-клиент aioboy/moex (шаблоны грузятся один раз)."""
    global _client
    if _client is None:
        c = Moex(output_format=".json")
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
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
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
