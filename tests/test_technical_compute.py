"""Тесты чистых вычислений тех. анализа в ru_finance/moex.py:
_compute_technical_indicators и _compute_candlestick_analysis.
Работают на синтетических свечах, сеть не трогают.
"""
import math

import pytest

from ru_finance.moex import (
    _compute_candlestick_analysis,
    _compute_technical_indicators,
)


# ═══════════════════════════ фикстуры-ряды ═══════════════════════════


def rising_series(n=250, base=100.0, step=1.0):
    closes = [base + step * i for i in range(n)]
    return closes, [c + 1 for c in closes], [c - 1 for c in closes], [1000] * n


def falling_series(n=250, base=400.0, step=1.0):
    closes = [base - step * i for i in range(n)]
    return closes, [c + 1 for c in closes], [c - 1 for c in closes], [1000] * n


def sideways_series(n=250):
    closes = [100.5 if i % 2 == 0 else 99.5 for i in range(n)]
    return closes, [c + 0.5 for c in closes], [c - 0.5 for c in closes], [1000] * n


def candles_from(closes, highs, lows, volumes, prefix_days=0):
    """Свечной rows в формате _compute_candlestick_analysis."""
    rows = []
    for i, c in enumerate(closes):
        rows.append({
            "open": c - 0.25, "high": highs[i], "low": lows[i],
            "close": c, "volume": volumes[i], "begin": f"d{i}",
        })
    return rows


# ═══════════════════════════ _compute_technical_indicators ═══════════════════════════


class TestRsi:
    def test_monotonic_up_is_100(self):
        closes, highs, lows, vols = rising_series()
        r = _compute_technical_indicators(closes, highs, lows, vols, "UP", 90, "24")
        assert r["rsi_14"] == 100.0

    def test_monotonic_down_is_0(self):
        closes, highs, lows, vols = falling_series()
        r = _compute_technical_indicators(closes, highs, lows, vols, "DOWN", 90, "24")
        assert r["rsi_14"] == 0.0

    def test_sideways_near_50(self):
        closes, highs, lows, vols = sideways_series()
        r = _compute_technical_indicators(closes, highs, lows, vols, "FLAT", 90, "24")
        assert 45.0 < r["rsi_14"] < 55.0

    def test_absent_on_short_series(self):
        closes, highs, lows, vols = rising_series(n=10)
        r = _compute_technical_indicators(closes, highs, lows, vols, "SHORT", 90, "24")
        assert "rsi_14" not in r


class TestMovingAverages:
    def test_sma_50_200_exact_on_rising(self):
        closes, highs, lows, vols = rising_series()
        r = _compute_technical_indicators(closes, highs, lows, vols, "UP", 90, "24")
        # последние 50 значений: 300..349 (base=100, step=1)
        assert r["ma_50"] == pytest.approx(324.5)
        # последние 200: 150..349
        assert r["ma_200"] == pytest.approx(249.5)

    def test_golden_cross_after_uptrend(self):
        closes, highs, lows, vols = rising_series()
        r = _compute_technical_indicators(closes, highs, lows, vols, "UP", 90, "24")
        assert r["ma_signal"] == "golden_cross"
        assert r["ma_50"] > r["ma_200"]

    def test_death_cross_after_downtrend(self):
        closes, highs, lows, vols = falling_series()
        r = _compute_technical_indicators(closes, highs, lows, vols, "DOWN", 90, "24")
        assert r["ma_signal"] == "death_cross"
        assert r["ma_50"] < r["ma_200"]

    def test_insufficient_data_on_short_series(self):
        closes, highs, lows, vols = rising_series(n=10)
        r = _compute_technical_indicators(closes, highs, lows, vols, "SHORT", 90, "24")
        assert r["ma_50"] is None
        assert r["ma_200"] is None
        assert r["ma_signal"] == "insufficient_data"

    def test_ema_values_on_linear_series(self):
        closes, highs, lows, vols = rising_series()
        r = _compute_technical_indicators(closes, highs, lows, vols, "UP", 90, "24")
        # на линейном ряде EMA(n) отстаёт от цены на (n-1)/2 шага
        assert r["ema_12"] == pytest.approx(349 - 5.5, abs=0.5)
        assert r["ema_26"] == pytest.approx(349 - 12.5, abs=0.5)
        assert r["ema_12"] > r["ema_26"]

    def test_ema_absent_on_short_series(self):
        closes, highs, lows, vols = rising_series(n=10)
        r = _compute_technical_indicators(closes, highs, lows, vols, "S", 90, "24")
        assert "ema_12" not in r and "ema_26" not in r

    def test_ema_12_present_on_medium_series(self):
        # EMA(n) требует лишь n точек, а не 2n
        closes, highs, lows, vols = rising_series(n=20)
        r = _compute_technical_indicators(closes, highs, lows, vols, "M", 90, "24")
        assert "ema_12" in r
        assert "ema_26" not in r


class TestGoldenDeathCrossOnReversal:
    @staticmethod
    def _reversal_up():
        # затяжной даунтренд -> разворот вверх
        down = [400.0 - j for j in range(200)]
        up = [200.0 + j for j in range(150)]
        closes = down + up
        return closes, [c + 1 for c in closes], [c - 1 for c in closes], [1000] * len(closes)

    @staticmethod
    def _reversal_down():
        up = [200.0 + j for j in range(200)]
        down = [399.0 - j for j in range(150)]
        closes = up + down
        return closes, [c + 1 for c in closes], [c - 1 for c in closes], [1000] * len(closes)

    def test_golden_cross_after_bottom(self):
        closes, highs, lows, vols = self._reversal_up()
        r = _compute_technical_indicators(closes, highs, lows, vols, "REV", 90, "24")
        assert r["ma_50"] == pytest.approx(324.5)   # 300..349
        # окно 200: 50 дней даунтренда (201..250, ср. 225.5) + 150 аптренда (200..349, ср. 274.5)
        assert r["ma_200"] == pytest.approx(262.25)
        assert r["ma_signal"] == "golden_cross"

    def test_death_cross_after_top(self):
        closes, highs, lows, vols = self._reversal_down()
        r = _compute_technical_indicators(closes, highs, lows, vols, "REV", 90, "24")
        assert r["ma_50"] < r["ma_200"]
        assert r["ma_signal"] == "death_cross"


class TestMacd:
    def test_positive_on_uptrend(self):
        closes, highs, lows, vols = rising_series()
        r = _compute_technical_indicators(closes, highs, lows, vols, "UP", 90, "24")
        # на линейном ряде MACD = (lag26 - lag12) * step = 7 * 1
        assert r["macd"] == pytest.approx(7.0, abs=1.0)
        assert r["macd"] > 0
        assert r["macd_signal_line"] == pytest.approx(7.0, abs=1.0)

    def test_negative_on_downtrend(self):
        closes, highs, lows, vols = falling_series()
        r = _compute_technical_indicators(closes, highs, lows, vols, "DOWN", 90, "24")
        assert r["macd"] == pytest.approx(-7.0, abs=1.0)

    def test_bullish_on_accelerating_uptrend(self):
        # ускоряющийся рост: гистограмма положительная
        closes = [100 + 0.05 * i * i for i in range(120)]
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        vols = [1000] * len(closes)
        r = _compute_technical_indicators(closes, highs, lows, vols, "ACC", 90, "24")
        assert r["macd"] > 0
        assert r["macd_histogram"] > 0
        assert r["macd_signal"] == "bullish"

    def test_bearish_on_fading_uptrend(self):
        # рост затухает (выйти на плато): гистограмма отрицательная
        closes = [400 - 200 * math.exp(-i / 25.0) for i in range(120)]
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        vols = [1000] * len(closes)
        r = _compute_technical_indicators(closes, highs, lows, vols, "FADE", 90, "24")
        assert r["macd"] > 0          # тренд всё ещё вверх
        assert r["macd_histogram"] < 0
        assert r["macd_signal"] == "bearish"

    def test_absent_on_short_series(self):
        closes, highs, lows, vols = rising_series(n=30)
        r = _compute_technical_indicators(closes, highs, lows, vols, "S", 90, "24")
        assert "macd" not in r


class TestAtr:
    def test_constant_range_gives_exact_atr(self):
        # high-low = 2 на каждой свече, гэпов нет -> ATR ровно 2
        closes, highs, lows, vols = rising_series()
        r = _compute_technical_indicators(closes, highs, lows, vols, "UP", 90, "24")
        assert r["atr_14"] == pytest.approx(2.0, abs=0.01)
        assert r["atr_14_pct"] == pytest.approx(2.0 / 349 * 100, abs=0.01)

    def test_wilder_smoothing_converges(self):
        # после всплеска волатильности ATR выше, чем до него
        closes = [100.0 + i for i in range(60)]
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        lows[30:] = [c - 4 for c in closes[30:]]  # расширили диапазон
        vols = [1000] * 60
        r = _compute_technical_indicators(closes, highs, lows, vols, "VOL", 90, "24")
        assert r["atr_14"] > 2.5

    def test_absent_on_short_series(self):
        closes, highs, lows, vols = rising_series(n=10)
        r = _compute_technical_indicators(closes, highs, lows, vols, "S", 90, "24")
        assert "atr_14" not in r


class TestBollinger:
    def test_bands_on_linear_series(self):
        closes, highs, lows, vols = rising_series()
        r = _compute_technical_indicators(closes, highs, lows, vols, "UP", 90, "24")
        # окно 330..349, шаг 1: mean=339.5, sd=sqrt(399/12)
        sd = math.sqrt((20 ** 2 - 1) / 12)
        assert r["bollinger_middle"] == pytest.approx(339.5, abs=0.01)
        assert r["bollinger_upper"] == pytest.approx(339.5 + 2 * sd, abs=0.01)
        assert r["bollinger_lower"] == pytest.approx(339.5 - 2 * sd, abs=0.01)
        assert r["bollinger_upper"] > r["bollinger_middle"] > r["bollinger_lower"]

    def test_width_and_pct_position(self):
        closes, highs, lows, vols = rising_series()
        r = _compute_technical_indicators(closes, highs, lows, vols, "UP", 90, "24")
        sd = math.sqrt((20 ** 2 - 1) / 12)
        assert r["bollinger_width"] == pytest.approx(4 * sd / 339.5 * 100, abs=0.05)
        # цена на максимуме окна, но за пределы полосы почти не выходит
        assert r["bollinger_pct"] == pytest.approx(0.9119, abs=0.01)
        assert 0.0 < r["bollinger_pct"] <= 1.0

    def test_flat_series_zero_width_no_pct(self):
        closes = [100.0] * 30
        r = _compute_technical_indicators(closes, closes, closes, [0] * 30, "FLAT", 90, "24")
        assert r["bollinger_upper"] == 100.0
        assert r["bollinger_middle"] == 100.0
        assert r["bollinger_lower"] == 100.0
        assert r["bollinger_width"] == 0.0
        assert "bollinger_pct" not in r  # rng == 0 -> позиция не определена

    def test_absent_on_short_series(self):
        closes, highs, lows, vols = rising_series(n=15)
        r = _compute_technical_indicators(closes, highs, lows, vols, "S", 90, "24")
        assert "bollinger_upper" not in r


class TestShapeAndMeta:
    def test_meta_fields(self):
        closes, highs, lows, vols = rising_series(n=60)
        r = _compute_technical_indicators(closes, highs, lows, vols, "TICK", 42, "60")
        assert r["secid"] == "TICK"
        assert r["interval"] == "60"
        assert r["period"] == 42
        assert r["trading_days"] == 60

    def test_obv_accumulates_on_uptrend(self):
        closes, highs, lows, vols = rising_series(n=60)
        r = _compute_technical_indicators(closes, highs, lows, vols, "UP", 90, "24")
        # все свечи растут -> OBV = сумма объёмов, кроме первой (i=0 не входит)
        assert r["obv"] == 1000 * 59

    def test_vwap_between_low_and_high(self):
        closes, highs, lows, vols = rising_series(n=60)
        r = _compute_technical_indicators(closes, highs, lows, vols, "UP", 90, "24")
        assert min(lows) < r["vwap"] < max(highs)


# ═══════════════════════════ _compute_candlestick_analysis ═══════════════════════════


def mk(o, h, l, c, v=100, i=0):
    return {"open": o, "high": h, "low": l, "close": c, "volume": v, "begin": f"d{i}"}


def flat_fillers(n=5, o=110.5, h=113.0, l=110.0):
    """Ровные «фоновые» свечи, не образующие паттернов (кроме мелких doji)."""
    return [mk(o, h, l, o, i=i) for i in range(n)]


def patterns_by_name(result, name):
    return [p for p in result["patterns"] if p["pattern"] == name]


class TestCandlestickMeta:
    def test_insufficient_data(self):
        rows = [mk(10, 11, 9, 10.5), mk(10.5, 11.5, 10, 11)]
        r = _compute_candlestick_analysis(rows, "T", 90, "24")
        assert r["error"] == "недостаточно данных"
        assert r["trading_days"] == 2

    def test_meta_fields(self):
        rows = flat_fillers(5)
        r = _compute_candlestick_analysis(rows, "TICK", 42, "7")
        assert r["secid"] == "TICK"
        assert r["interval"] == "7"
        assert r["period"] == 42
        assert r["trading_days"] == 5
        assert r["candles_analyzed"] == 5
        assert isinstance(r["patterns"], list)
        assert "signal" in r

    def test_rows_with_missing_ohlc_are_skipped(self):
        rows = flat_fillers(3) + [{"open": None, "high": None, "low": None,
                                   "close": None, "volume": 0, "begin": "bad"}]
        r = _compute_candlestick_analysis(rows, "T", 90, "24")
        assert r["trading_days"] == 3


def last_bar(hits):
    """Целевая свеча — последняя в ряду (bars_back == 0)."""
    target = [p for p in hits if p["bars_back"] == 0]
    assert target, "нет паттерна на последней свече"
    return target[0]


class TestDoji:
    def test_long_legged_doji(self):
        rows = flat_fillers(5) + [mk(100, 104, 96, 100.2, i=5)]  # open≈close, тени в обе стороны
        r = _compute_candlestick_analysis(rows, "T", 90, "24")
        hits = patterns_by_name(r, "doji")
        assert hits, "doji должен быть обнаружен"
        last = last_bar(hits)
        assert last["signal"] == "neutral"
        assert last["bars_back"] == 0
        assert "long_legged" in last["signal_detail"]

    def test_dragonfly_doji(self):
        # тело сверху, длинная нижняя тень, верхней почти нет
        rows = flat_fillers(5) + [mk(100, 100.2, 92, 100.1, i=5)]
        r = _compute_candlestick_analysis(rows, "T", 90, "24")
        hits = patterns_by_name(r, "doji")
        assert hits
        last = last_bar(hits)
        assert "dragonfly" in last["signal_detail"]
        assert last["signal"] == "neutral"
        # самый широкий диапазон в периоде -> сила повышена
        assert last["strength"] == 3

    def test_gravestone_doji(self):
        # тело снизу, длинная верхняя тень
        rows = flat_fillers(5) + [mk(100, 108, 99.9, 100.1, i=5)]
        r = _compute_candlestick_analysis(rows, "T", 90, "24")
        hits = patterns_by_name(r, "doji")
        assert hits
        last = last_bar(hits)
        assert "gravestone" in last["signal_detail"]
        assert last["signal"] == "neutral"

    def test_doji_bars_back_counts_from_end(self):
        rows = [mk(100, 104, 96, 100.2, i=0)] + flat_fillers(3)
        r = _compute_candlestick_analysis(rows, "T", 90, "24")
        hits = patterns_by_name(r, "doji")
        assert hits
        # doji — 4-я с конца (3 фоновых после неё)
        assert [p for p in hits if p["bars_back"] == 3]


class TestHammer:
    def test_hammer_bullish(self):
        # маленькое тело в верхней части, длинная нижняя тень (x2 от тела),
        # верхняя тень < 10% диапазона; в нижней половине периода -> hammer
        rows = flat_fillers(5, h=114.0) + [mk(101.5, 101.7, 90, 100, i=5)]
        r = _compute_candlestick_analysis(rows, "T", 90, "24")
        hits = patterns_by_name(r, "hammer")
        assert hits, f"hammer не найден: {[p['pattern'] for p in r['patterns']]}"
        p = hits[-1]
        assert p["signal"] == "bullish"
        assert p["bar_type"] == "single"
        assert 1 <= p["strength"] <= 3
        assert p["bars_back"] == 0

    def test_hanging_man_in_upper_half(self):
        # та же форма, но свеча у максимума периода -> hanging_man (bearish)
        rows = [mk(110.5, 112, 110, 110.5, i=i) for i in range(3)]
        rows += [mk(101.5, 101.7, 90, 100, i=3)]  # низ периода, высокий ctx
        rows += [mk(105, 107, 104, 105.5, i=4), mk(108, 110, 107, 108.5, i=5)]
        r = _compute_candlestick_analysis(rows, "T", 90, "24")
        hits = patterns_by_name(r, "hanging_man")
        assert hits, f"hanging_man не найден: {[p['pattern'] for p in r['patterns']]}"
        assert hits[-1]["signal"] == "bearish"


class TestEngulfingAndHarami:
    def test_bullish_harami(self):
        # большое медвежье тело, затем малое бычье тело внутри него
        rows = flat_fillers(3) + [
            mk(105, 106, 99.5, 100, i=3),
            mk(102, 103, 101, 102.5, i=4),
        ]
        r = _compute_candlestick_analysis(rows, "T", 90, "24")
        hits = patterns_by_name(r, "bullish_harami")
        assert hits, f"harami не найден: {[p['pattern'] for p in r['patterns']]}"
        p = hits[-1]
        assert p["signal"] == "bullish"
        assert p["bar_type"] == "double"
        assert p["strength"] == 1
        assert p["bars_back"] == 0

    def test_bearish_harami(self):
        # большое бычье тело, затем малое медвежье тело внутри него
        rows = flat_fillers(3) + [
            mk(100, 106, 99.5, 105, i=3),
            mk(104, 104.5, 101.5, 102, i=4),
        ]
        r = _compute_candlestick_analysis(rows, "T", 90, "24")
        hits = patterns_by_name(r, "bearish_harami")
        assert hits
        assert hits[-1]["signal"] == "bearish"
        assert hits[-1]["bars_back"] == 0

    @pytest.mark.xfail(reason="баг в moex.py: условия bullish_engulfing "
                              "(p.close >= q.close и p.open <= q.open при q bullish) "
                              "геометрически противоречивы — паттерн не может сработать никогда",
                       strict=True)
    def test_bullish_engulfing(self):
        # классическое бычье поглощение: q перекрывает тело p с обеих сторон
        rows = flat_fillers(3) + [
            mk(105, 106, 99.5, 100, i=3),    # bearish
            mk(99.5, 106.5, 99, 106, i=4),   # bullish engulfing
        ]
        r = _compute_candlestick_analysis(rows, "T", 90, "24")
        hits = patterns_by_name(r, "bullish_engulfing")
        assert hits
        assert hits[-1]["signal"] == "bullish"
        assert hits[-1]["bars_back"] == 0
        assert 1 <= hits[-1]["strength"] <= 3

    @pytest.mark.xfail(reason="баг в moex.py: условия bearish_engulfing "
                              "(p.close <= q.close и p.open >= q.open при q bearish) "
                              "геометрически противоречивы — паттерн не может сработать никогда",
                       strict=True)
    def test_bearish_engulfing(self):
        rows = flat_fillers(3) + [
            mk(100, 106, 99.5, 105, i=3),    # bullish
            mk(106, 106.5, 99, 99.5, i=4),   # bearish engulfing
        ]
        r = _compute_candlestick_analysis(rows, "T", 90, "24")
        hits = patterns_by_name(r, "bearish_engulfing")
        assert hits
        assert hits[-1]["signal"] == "bearish"
        assert hits[-1]["bars_back"] == 0


class TestCandlestickSummary:
    def test_summary_counts(self):
        rows = flat_fillers(3) + [
            mk(105, 106, 99.5, 100, i=3),
            mk(102, 103, 101, 102.5, i=4),  # bullish_harami
        ]
        r = _compute_candlestick_analysis(rows, "T", 90, "24")
        assert r["total_bullish"] >= 1
        assert r["signal_score_bullish"] >= 1
        assert r["signal"] in ("bullish", "slightly_bullish", "neutral")

    def test_patterns_sorted_by_strength_then_recency(self):
        rows = flat_fillers(5) + [mk(100, 104, 96, 100.2, i=5)]
        r = _compute_candlestick_analysis(rows, "T", 90, "24")
        keys = [(-p["strength"], p["bars_back"]) for p in r["patterns"]]
        assert keys == sorted(keys)
