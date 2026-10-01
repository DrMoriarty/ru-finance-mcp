#!/usr/bin/env python3
"""Генератор синтетического датасета tool-calling для ru-finance MCP (по ролям).

Запуск из корня репо или из dataset/: python3 dataset/gen.py
JSONL-файлы пишутся рядом со скриптом (dataset/<role>.jsonl).

При добавлении нового @mcp.tool() в mcp_server.py:
1) добавьте схему в TOOLS (helper T(name, desc, required, **props));
2) добавьте примеры (вопрос + вызовы) в EXAMPLES нужной роли.
"""
import json
import os
import sys

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
os.makedirs(OUT, exist_ok=True)


def T(name, desc, required=(), **props):
    """Схема функции OpenAI-format. props: param=(json_type, description)."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": {
                    k: {"type": t, "description": d} for k, (t, d) in props.items()
                },
                "required": list(required),
            },
        },
    }


# ---------------------------------------------------------------- tool schemas
TOOLS = [
    T("current_datetime", "Текущая дата и время сервера (UTC, ISO 8601)."),
    T("moex_resolve", "Определить бумагу по тикеру/ISIN/номеру ОФЗ/названию.",
      ("query",), query=("string", "Тикер, ISIN, номер ОФЗ или название")),
    T("moex_search", "Поиск бумаг на MOEX (тикер/ISIN/название), до 200 совпадений.",
      ("query",), query=("string", "Поисковый запрос"),
       sec_type=("string", "Фильтр типа: bond, share, stock, fund, etf, index")),
    T("moex_quote", "Текущая котировка акции/фонда на MOEX (задержка ~15 мин).",
      ("query",), query=("string", "Тикер или название")),
    T("moex_bond", "Облигация с метриками: цена, YTM, дюрация, купон, погашение.",
      ("query",), query=("string", "Номер ОФЗ или ISIN")),
    T("moex_candles", "Свечи OHLCV за период.",
      ("query", "frm", "till"),
      query=("string", "Тикер/ISIN"), frm=("string", "Дата начала YYYY-MM-DD"),
      till=("string", "Дата конца YYYY-MM-DD"),
      interval=("string", "Интервал: 1,10,60,24,7,31,4 (по умолч. 24 — день)")),
    T("moex_history", "Дневная история торгов (компактная: дата, закрытие, объём).",
      ("query", "frm", "till"),
      query=("string", "Тикер/ISIN"), frm=("string", "YYYY-MM-DD"), till=("string", "YYYY-MM-DD")),
    T("moex_full_history", "Дневная история торгов MOEX, все поля ISS.",
      ("query", "frm", "till"),
      query=("string", "Тикер/ISIN"), frm=("string", "YYYY-MM-DD"), till=("string", "YYYY-MM-DD")),
    T("moex_correlations", "Коэффициенты корреляции и бета бумаги с другими бумагами и индексами.",
      ("secid",), secid=("string", "Тикер")),
    T("moex_splits", "Справочник дроблений и консолидаций бумаг.",
      (), secid=("string", "Тикер (опционально, без — все сплиты)")),
    T("moex_bond_market_aggregates", "Агрегированные показатели рынка облигаций по типам (ОФЗ, корп, муниципальные).",
      (), frm=("string", "YYYY-MM-DD (опц.)"), till=("string", "YYYY-MM-DD (опц.)")),
    T("moex_emitent_bonds", "Все облигации эмитента с фильтром по дюрации.",
      ("query",),
      query=("string", "Имя/тикер эмитента"),
      min_duration=("number", "Мин. дюрация, лет"), max_duration=("number", "Макс. дюрация, лет")),
    T("moex_bond_coupons", "Расписание купонов облигации (прошлые и будущие), из НРД/MOEX.",
      ("query",), query=("string", "ISIN или номер ОФЗ")),
    T("moex_zcyc_history", "История параметров КБД (кривой бескупонной доходности ОФЗ), НСС-модель по дням.",
      ("frm", "till"), frm=("string", "YYYY-MM-DD"), till=("string", "YYYY-MM-DD")),
    T("moex_turnovers", "Сводные обороты по рынкам MOEX за день."),
    T("moex_sitenews", "Новости Московской биржи.",
      (), limit=("integer", "Сколько строк (по умолч. 20)")),
    T("moex_aggregates", "Агрегированные итоги торгов за дату по бумаге.",
      ("query", "date"), query=("string", "Тикер"), date=("string", "YYYY-MM-DD")),
    T("moex_indicative_rates", "Индикативные курсы валют срочного рынка MOEX.",
      (), frm=("string", "YYYY-MM-DD (опц.)"), till=("string", "YYYY-MM-DD (опц.)")),
    T("moex_ir_calendar", "Календарь IR-мероприятий (даты отчётов публичных компаний).",
      (), limit=("integer", "Сколько строк (по умолч. 50)")),
    T("moex_market_capitalization", "Капитализация фондового рынка MOEX (₽)."),

    T("moex_futures_list", "Каталог фьючерсных контрактов FORTS с рыночными данными и спецификацией.",
      (), asset_code=("string", "Код базисного актива: Si, RTS, BR, GAZR... (без — все)")),
    T("moex_futures_open_interest", "Открытый интерес фьючерсов по юрлицам/физлицам.",
      ("asset",), asset=("string", "Код базисного актива: Si, RTS, SBRF...")),
    T("moex_futures_series", "Календарь экспираций фьючерсов (серии).",
      (), asset=("string", "Код базисного актива (опц.)")),
    T("moex_futures_basis", "Contango/backwardation: годовая ставка переноса фьючерса к споту.",
      ("asset_code",), asset_code=("string", "Код базисного актива: Si, GAZR, GAZP...")),
    T("moex_futures_promo", "Агрегированная статистика комиссий срочного рынка FORTS."),

    T("moex_options_assets", "Базисные активы опционов FORTS с рыночными данными.",
      (), asset_type=("string", "S/F/M/C (опц.)")),
    T("moex_options_board", "Опционная доска: страйки, IV, OI, теор. цены по активу.",
      ("asset",), asset=("string", "Код базисного актива: Si, GAZR, SBRF, BR...")),
    T("moex_option_quote", "Котировка конкретного опциона: премия, спред, ГО.",
      ("secid",), secid=("string", "Код опциона, напр. Si87000BI6A")),
    T("moex_option_orderbook", "Лучшие bid/offer и спред опциона.",
      ("secid",), secid=("string", "Код опциона")),
    T("moex_option_history", "История сделок по опциону.",
      ("secid",), secid=("string", "Код опциона"),
      frm=("string", "YYYY-MM-DD (опц.)"), till=("string", "YYYY-MM-DD (опц.)")),

    T("smartlab_dividends", "Календарь ближайших дивидендов (smart-lab.ru).",
      (), limit=("integer", "Сколько строк (по умолч. 50)")),
    T("smartlab_dividend_history", "История дивидендов по тикеру (все выплаты эмитента).",
      ("ticker",), ticker=("string", "Тикер")),
    T("smartlab_company_financials", "Финансовая отчётность компании за мульти-год.",
      ("ticker",), ticker=("string", "Тикер"),
      period=("string", "annual (по умолч.) или quarter"),
      standard=("string", "rsbu (по умолч.) или ifrs")),
    T("smartlab_company_financials_multi", "Финансовая отчётность нескольких компаний (batch).",
      ("tickers",), tickers=("array", "Список тикеров"),
      period=("string", "annual/quarter"), standard=("string", "rsbu/ifrs")),
    T("smartlab_stock_screener", "Фундаментальный скринер акций MOEX (LTM-множители).",
      (),
      market_cap_min=("number", "Мин. капитализация, ₽"), market_cap_max=("number", "Макс. капитализация, ₽"),
      pe_min=("number", "Мин. P/E"), pe_max=("number", "Макс. P/E"),
      ps_min=("number", "Мин. P/S"), ps_max=("number", "Макс. P/S"),
      pb_min=("number", "Мин. P/B"), pb_max=("number", "Макс. P/B"),
      ev_ebitda_min=("number", "Мин. EV/EBITDA"), ev_ebitda_max=("number", "Макс. EV/EBITDA"),
      roe_min=("number", "Мин. ROE, %"), roa_min=("number", "Мин. ROA, %"),
      ebitda_margin_min=("number", "Мин. рентабельность EBITDA, %"),
      div_yield_min=("number", "Мин. дивдоходность, %"), div_yield_max=("number", "Макс. дивдоходность, %"),
      sort_field=("string", "Поле сортировки"), sort_desc=("boolean", "По убыванию"),
      limit=("integer", "Макс. результатов (по умолч. 30, макс. 200)")),
    T("stock_f_score", "Piotroski F-Score (0–9) по финансовой отчётности.",
      ("ticker",), ticker=("string", "Тикер")),
    T("stock_z_score", "Altman Z-Score (модиф. для emerging markets) — риск банкротства.",
      ("ticker",), ticker=("string", "Тикер")),
    T("stock_peer_comparison", "Сравнение мультипликаторов акции с топ-N peers.",
      ("ticker",), ticker=("string", "Тикер"), top_n=("integer", "Сколько peers (по умолч. 15)")),
    T("dividend_analysis", "Дивидендный анализ: CAGR, средняя доходность, стабильность выплат.",
      ("ticker",), ticker=("string", "Тикер")),
    T("stock_growth_analysis", "Рост выручки/EBITDA/прибыли, тренды ROE/ROA/маржинальности.",
      ("ticker",), ticker=("string", "Тикер")),
    T("bank_benchmark", "Бенчмарк банков по ключевым банковским метрикам (NIM, NPL, LDR...).",
      ("tickers",), tickers=("array", "Список тикеров банков"),
      short_names=("array", "Отображаемые имена (опц.)")),
    T("bank_peer_comparison", "Ранжирование банка по метрикам относительно сектора (~15 крупнейших).",
      ("ticker",), ticker=("string", "Тикер банка")),
    T("company_fundamental_report", "Всё в одном: мультипликаторы, дивиденды, рост, Z/F-Score, пирс.",
      ("ticker",), ticker=("string", "Тикер")),

    T("raexpert_rating", "Кредитный рейтинг эмитента/облигации от Эксперт РА.",
      ("query",), query=("string", "Название эмитента или облигации")),
    T("raexpert_emitent_ratings", "Эмитенты облигаций с фильтром по рейтингу и отрасли.",
      (), rating_min=("string", "Мин. рейтинг: ruBBB-, ruA, ruA+..."),
      sector=("string", "Отрасль MOEX: Финансовый, Нефтегазовый, Металлургия и добыча...")),

    T("zpif_payments", "Календарь выплат ЗПИФ недвижимости (vsezpif.ru, оценочный).",
      (), fund_name=("string", "Название фонда или часть (опц.)"),
      isin=("string", "ISIN (опц.)"), limit=("integer", "Макс. записей")),
    T("zpif_funds_list", "Список всех ЗПИФ недвижимости с vsezpif.ru."),

    T("cbr_key_rate", "Ключевая ставка ЦБ РФ (история).",
      (), first_date=("string", "YYYY-MM-DD (опц.)"), last_date=("string", "YYYY-MM-DD (опц.)"),
      tail=("integer", "Сколько последних точек (по умолч. 30)")),
    T("cbr_inflation", "Инфляция (CPI, % г/г) и ключевая ставка ЦБ помесячно с 2013 г.",
      (), first_date=("string", "YYYY-MM или YYYY-MM-DD (опц.)"),
      last_date=("string", "YYYY-MM или YYYY-MM-DD (опц.)"),
      tail=("integer", "Последних записей (по умолч. 24)")),
    T("cbr_ruonia", "Ставка RUONIA overnight, % годовых.",
      (), first_date=("string", "опц."), last_date=("string", "опц."), tail=("integer", "опц.")),
    T("cbr_ruonia_index", "RUONIA-индекс и срочные средние RUONIA_AVG_1M/3M/6M.",
      (), first_date=("string", "опц."), last_date=("string", "опц."), tail=("integer", "опц.")),
    T("cbr_ibor", "MIACR — фактические ставки межбанка по срокам D1/D7/...",
      (), first_date=("string", "опц."), last_date=("string", "опц."), tail=("integer", "опц.")),
    T("cbr_currency", "Курс валюты ЦБ к рублю. symbol: USD, EUR, CNY...",
      ("symbol", "first_date", "last_date"),
      symbol=("string", "Код валюты"), first_date=("string", "YYYY-MM-DD"), last_date=("string", "YYYY-MM-DD"),
      tail=("integer", "опц.")),
    T("cbr_metals", "Учётные цены ЦБ на драгметаллы (золото, серебро, платина, палладий).",
      (), first_date=("string", "опц."), last_date=("string", "опц."), tail=("integer", "опц.")),
    T("cbr_reserves", "Международные (золотовалютные) резервы РФ.",
      (), first_date=("string", "опц."), last_date=("string", "опц."), tail=("integer", "опц.")),

    T("bond_report", "Глубокий разбор облигации: метрики, сценарии ставки, спред к G-кривой, конвексность, GRY, НКД, реальная доходность.",
      ("query",), query=("string", "Номер ОФЗ или ISIN")),
    T("bond_accrued_interest", "НКД облигации: сумма, дни, купонный период.",
      ("query",), query=("string", "Номер ОФЗ или ISIN")),
    T("bond_screener", "Скринер облигаций (корп TQCB + ОФЗ TQOB): фильтры по YTM, дюрации, цене, купону, офёрте, амортизации, валюте, рейтингу, сектору.",
      (),
      ytm_min=("number", "Мин. YTM, %"), ytm_max=("number", "Макс. YTM, %"),
      coupon_min=("number", "Мин. купон, %"), coupon_max=("number", "Макс. купон, %"),
      price_min=("number", "Мин. цена, % номинала"), price_max=("number", "Макс. цена, % номинала"),
      maturity_from=("string", "Погашение с YYYY-MM-DD"), maturity_to=("string", "Погашение по YYYY-MM-DD"),
      duration_min=("number", "Мин. дюрация, лет"), duration_max=("number", "Макс. дюрация, лет"),
      years_to_maturity_min=("number", "Мин. срок, лет"), years_to_maturity_max=("number", "Макс. срок, лет"),
      has_offer=("boolean", "True — только с офертой, False — без"),
      has_amortization=("boolean", "True — только амортизируемые"),
      coupon_type=("string", "fixed, float или amortization"),
      coupon_freq_min=("integer", "Мин. купонов в год"), coupon_freq_max=("integer", "Макс. купонов в год"),
      currency=("string", "SUR, USD, EUR, CNY"),
      issue_volume_min=("integer", "Мин. объём выпуска"), issue_volume_max=("integer", "Макс. объём выпуска"),
      accrued_int_min=("number", "Мин. НКД, ₽"), accrued_int_max=("number", "Макс. НКД, ₽"),
      rating_min=("string", "Мин. рейтинг Эксперт РА (напр. ruBBB-)"),
      sector=("string", "MOEX-сектор эмитента"),
      include_qualified=("boolean", "Добавить поле is_qualified"),
      qualified_only=("boolean", "Только для квалифицированных (нужен include_qualified=True)"),
      sort_by=("string", "ytm, duration, maturity, price, coupon, issue_volume"),
      sort_desc=("boolean", "По убыванию (по умолч.)"),
      limit=("integer", "Макс. результатов (1..500, по умолч. 15)")),
    T("bond_prescreener", "Компактный прескринер облигаций: только secname и isin, те же фильтры, что у bond_screener.",
      (),
      ytm_min=("number", "Мин. YTM, %"), ytm_max=("number", "Макс. YTM, %"),
      currency=("string", "SUR, USD, EUR, CNY"),
      coupon_type=("string", "fixed, float, amortization"),
      has_offer=("boolean", "С офертой / без"),
      limit=("integer", "Макс. результатов")),
    T("bond_synthetic_yield", "Синтетическая доходность с реинвестированием купонов на горизонте.",
      ("query", "horizon_years"),
      query=("string", "Тикер/ISIN облигации"), horizon_years=("number", "Горизонт, лет"),
      reinvest_rate_ytm_pct=("number", "Ставка реинвестирования, % (по умолч. = YTM)")),

    T("price_volatility", "Волатильность, Sharpe, max drawdown по дневным свечам.",
      ("query",), query=("string", "Тикер"), days=("integer", "Период, дней (по умолч. 90)"),
      rf_annual=("number", "Безрисковая ставка, % годовых (по умолч. 16)")),
    T("liquidity_assessment", "Оценка ликвидности: оборот, Amihud, спред, скор 0-10, grade A-E.",
      ("query",), query=("string", "Тикер"), days=("integer", "Период, дней (по умолч. 90)")),
    T("technical_indicators", "Полный набор технических индикаторов из дневных свечей (RSI, MACD, Bollinger, Ichimoku, ADX, MA...).",
      ("query",), query=("string", "Тикер/ISIN"), days=("integer", "Период, дней (по умолч. 90; для MA200 нужно ≥200)")),
    T("candlestick_patterns", "Распознавание классических свечных паттернов (14 шт.) из дневных свечей.",
      ("query",), query=("string", "Тикер/ISIN"), days=("integer", "Период, дней (по умолч. 90)")),

    T("etf_fund_info", "Информация о БПИФ: категория, бенчмарк, iNAV, премия/дисконт.",
      ("query",), query=("string", "Тикер фонда")),
    T("etf_premium_discount", "Текущая премия/дисконт БПИФ к iNAV.",
      ("query",), query=("string", "Тикер фонда")),
    T("etf_tracking_error", "Трекинг-ошибка БПИФ к индексу-бенчмарку за N дней.",
      ("query",), query=("string", "Тикер фонда"), days=("integer", "Период, дней (по умолч. 90)")),
    T("etf_screener", "Скринер БПИФ/ETF с фильтрами по классу активов, УК, бенчмарку, метрикам и TA-сигналам.",
      (),
      category=("string", "equity_russia, equity_foreign, equity_sector, equity_dividend, bond_gov, bond_corp, money_market, commodity, fx, mixed"),
      emitent=("string", "УК: Т-Капитал, Сбер, Альфа, ВТБ"),
      benchmark=("string", "Тикер бенчмарка: IMOEX, GOLD, RGBITR"),
      currency=("string", "SUR, USD, EUR, CNY, HKD"),
      price_min=("number", "Мин. цена, ₽"), price_max=("number", "Макс. цена, ₽"),
      volume_min=("number", "Мин. среднедневной объём, ₽"), volume_max=("number", "Макс. объём, ₽"),
      spread_max=("number", "Макс. bid-ask спред, %"),
      volatility_min=("number", "Мин. годовая волатильность, %"), volatility_max=("number", "Макс. волатильность, %"),
      sharpe_min=("number", "Мин. Sharpe"), sharpe_max=("number", "Макс. Sharpe"),
      beta_min=("number", "Мин. бета"), beta_max=("number", "Макс. бета"),
      performance_min=("number", "Мин. доходность, %"), performance_max=("number", "Макс. доходность, %"),
      performance_period=("string", "1m, 3m, 6m, 1y, ytd"),
      rsi_min=("number", "Мин. RSI(14)"), rsi_max=("number", "Макс. RSI(14)"),
      ma_signal=("string", "golden_cross или death_cross"),
      adx_min=("number", "Мин. ADX"), macd_signal=("string", "bullish или bearish"),
      stochastic_min=("number", "Мин. Stochastic %K"), stochastic_max=("number", "Макс. Stochastic %K"),
      cci_min=("number", "Мин. CCI(20)"), cci_max=("number", "Макс. CCI(20)"),
      williams_min=("number", "Мин. Williams %R"), williams_max=("number", "Макс. Williams %R"),
      ichimoku_signal=("string", "bullish, bearish, in_cloud"),
      psar_direction=("string", "long или short"),
      cmf_signal=("string", "buying_pressure, selling_pressure, neutral"),
      roc_min=("number", "Мин. ROC(10), %"), roc_max=("number", "Макс. ROC(10), %"),
      premium_discount_max=("number", "Макс. премия/дисконт к NAV, %"),
      tracking_error_max=("number", "Макс. трекинг-ошибка, %"),
      include_indicators=("boolean", "Рассчитывать TA (default true)"),
      sort_by=("string", "performance, volatility, sharpe, volume, spread, premium, rsi, adx, beta, ..."),
      sort_desc=("boolean", "По убыванию (default)"),
      limit=("integer", "Макс. результатов (1..200, default 15)")),

    T("rate_expectations", "Рыночные ожидания по ключевой ставке из КБД ОФЗ: спреды, форварды, метка read.",
      (), key_rate=("number", "Ключевая ставка, % (опц., иначе берётся из cbr_key_rate)")),
    T("curve_yield", "Доходность G-кривой ОФЗ (точная NSS-модель MOEX) на произвольном сроке, % годовых.",
      ("years",), years=("number", "Срок, лет (можно экстраполировать за 20)")),

    T("portfolio_snapshot", "Полный снимок портфеля: веса, P&L, дюрация, денежный поток, дивдоходность, спреды к кривой, реальная доходность.",
      ("assets",), assets=("string", "Портфель в markdown: '# Счёт', '## Класс', '- Название: N шт. (цена покупки -> текущая)'")),
    T("portfolio_rate_whatif", "Эффект сдвига доходностей облигаций портфеля на delta_pp п.п.",
      ("delta_pp", "assets"),
      delta_pp=("number", "Сдвиг доходности, п.п. (+2 рост, -2 снижение)"),
      assets=("string", "Портфель в markdown")),
    T("portfolio_income_calendar", "Ближайшие поступления: купоны по облигациям и дивиденды, отсортированы по дате.",
      ("assets",), assets=("string", "Портфель в markdown")),
    T("portfolio_movers", "Кто вырос/просел: топ дневных изменений и P&L против цены покупки.",
      ("assets",), assets=("string", "Портфель в markdown")),

    T("option_calc_assets", "Список базовых активов опционного калькулятора.",
      (), asset_type=("string", "commodity/currency/futures/index/share"),
      asset_subtype=("string", "commodity/currency/index/share (для фьючерсов)"),
      query=("string", "Фильтр по названию (макс. 8 симв.)")),
    T("option_calc_futures", "Фьючерсы базового актива для опционного калькулятора.",
      ("asset_code",), asset_code=("string", "Код актива: Si, RTS, GAZR..."),
      expiration_date=("string", "YYYY-MM-DD (опц.)")),
    T("option_calc_options", "Опционы на базовый актив с фильтрами по серии/страйку/типу.",
      ("asset_code", "expiration_date"),
      asset_code=("string", "Код актива"), expiration_date=("string", "YYYY-MM-DD (из option_calc_series)"),
      asset_type=("string", "опц."), series_type=("string", "W/M/Q"),
      strike=("number", "Страйк"), option_type=("string", "call или put"),
      limit=("integer", "По умолч. 50, макс. 200")),
    T("option_calc_option_brief", "Сводка по опциону: Greeks, теор. цена, IV. Поддерживает what-if (underlying_price, volatility).",
      ("asset_code", "secid"), asset_code=("string", "Код актива"), secid=("string", "Код опциона"),
      asset_type=("string", "опц."), days_until_expiring=("integer", "Override дней до экспирации"),
      underlying_price=("number", "Override цены БА"), volatility=("number", "Override IV, %")),
    T("option_calc_series", "Серии опционов (циклы экспираций) для базового актива.",
      ("asset_code",), asset_code=("string", "Код актива"), asset_type=("string", "опц.")),
    T("option_calc_series_detail", "Описание одной серии опционов.",
      ("asset_code", "optionseries_code"),
      asset_code=("string", "Код актива"), optionseries_code=("string", "Код серии"), asset_type=("string", "опц.")),
    T("option_calc_series_options", "Опционы в конкретной серии.",
      ("asset_code", "optionseries_code"),
      asset_code=("string", "Код актива"), optionseries_code=("string", "Код серии"),
      asset_type=("string", "опц."), strike=("number", "Фильтр страйка"), option_type=("string", "call/put")),
    T("option_calc_optionboard", "Доска опционов серии: страйки с Greeks, IV, bid/ask, теор. ценой.",
      ("asset_code", "optionseries_code"),
      asset_code=("string", "Код актива"), optionseries_code=("string", "Код серии"),
      asset_type=("string", "опц."), rows=("integer", "Кол-во страйков от центрального")),
    T("option_calc_volatility_graph", "Кривая implied volatility (smile) по страйкам серии.",
      ("asset_code", "optionseries_code"),
      asset_code=("string", "Код актива"), optionseries_code=("string", "Код серии"), asset_type=("string", "опц.")),
    T("option_calc_portfolio", "Расчёт опционного портфеля: агрегированные Greeks, P&L, ГО.",
      ("asset_code", "positions"),
      asset_code=("string", "Код актива"),
      positions=("array", "Позиции [{secid, type: option|futures, quantity, price?, ...}]; quantity>0 — покупка"),
      delta_sigma=("number", "Сдвиг IV для what-if, %"), date_of_calculation=("string", "YYYY-MM-DD для what-if")),
    T("option_calc_portfolio_graph", "График P&L или Greeks опционной стратегии по цене БА.",
      ("asset_code", "positions", "indicator"),
      asset_code=("string", "Код актива"),
      positions=("array", "Позиции [{secid, type: option|futures, quantity, ...}]"),
      indicator=("string", "profit_and_loss, delta, gamma, vega, theta, rho"),
      delta_sigma=("number", "Сдвиг IV, %"), date_of_calculation=("string", "YYYY-MM-DD")),
    T("option_calc_initial_margin", "Гарантийное обеспечение для набора позиций (кросс-БА).",
      ("positions",), positions=("array", "Позиции [{secid, type: option|futures, quantity, price?, netted_im?}]")),
]

TOOL_MAP = {t["function"]["name"]: t for t in TOOLS}


# ---------------------------------------------------------------- role prompts
ROLE_PROMPTS = {
    "screener": (
        "Ты — рыночный скринер. Найди инструменты, удовлетворяющие заданным критериям. "
        "Доступны: поиск и котировки MOEX, скринеры (smartlab_stock_screener, bond_screener, etf_screener, "
        "raexpert_emitent_ratings), фундаментал компаний, макро (ставки ЦБ, инфляция, кривая доходности). "
        "Цены с задержкой ~15 мин; ставки — % годовых, дюрация — в годах."
    ),
    "analyst": (
        "Ты — аналитик. Проводи глубокий анализ конкретного инструмента. "
        "Доступны: котировки и OHLCV-история, фундаментал (финотчётность, F/Z-Score, дивиденды, пир-компарисон), "
        "тех. анализ (price_volatility, technical_indicators, candlestick_patterns), "
        "облигации (bond_report, купоны, НКД, спред к кривой, рейтинги), ETF (fund_info, tracking error, премия/дисконт). "
    ),
    "constructor": (
        "Ты — конструктор портфеля. Собираешь оптимальный набор активов с учётом диверсификации и рисков. "
        "Доступны: котировки, скринеры (облигации, акции, ETF), облигации (bond_report, купоны, НКД, рейтинги), "
        "ETF (fund_info, tracking error), портфель (portfolio_snapshot, portfolio_rate_whatif, "
        "portfolio_income_calendar, portfolio_movers). "
        "Портфель всегда передаётся параметром assets в markdown. Проверяй концентрацию: эмитент ≤15%, сектор ≤30%."
    ),
    "timer": (
        "Ты — таймер. Определяешь оптимальную точку входа/выхода для позиции. "
        "Доступны: котировки, OHLCV-история, тех. анализ (technical_indicators, candlestick_patterns, price_volatility), "
        "деривативы (фьючерсы FORTS, опционные доски, contango/backwardation, опционный калькулятор). "
        "Сильный сигнал = паттерн strength ≥ 2 + подтверждение индикатором (RSI, MACD). "
        "Давай конкретную рекомендацию: цена входа, стоп, цель, горизонт."
    ),
    "macrotracker": (
        "Ты — трекер макроэкономических трендов РФ. "
        "Доступны: ключевая ставка, инфляция, RUONIA, MIACR, курсы валют, драгметаллы, ЗВР (ЦБ РФ), "
        "ожидания по ставке из КБД ОФЗ (rate_expectations), G-кривая (curve_yield), история КБД. "
        "Спреды *_gross включают срочную премию — это не чистое ожидание; для пути короткой ставки смотри форварды fwd_*."
    ),
    "risk_manager": (
        "Ты — риск-менеджер. Контролируешь риски портфеля и позиций. "
        "Доступны: портфель (portfolio_snapshot, portfolio_rate_whatif, portfolio_income_calendar, portfolio_movers), "
        "скринеры, макро (ставки, инфляция, rate_expectations), тех. анализ, облигации (bond_report), "
        "деривативы (открытый интерес, ГО опционов). "
        "Оценивай: чувствительность к ставке, волатильность, ликвидность, концентрацию, реальную доходность."
    ),
    "instrument_specialist": (
        "Ты — специалист по инструментам: ETF/БПИФ и производные. "
        "Доступны: ETF (fund_info, premium_discount, tracking_error, screener), фьючерсы FORTS (списки, серии, basis), "
        "опционы (доски, котировки, стакан, история, опционный калькулятор с Greeks), тех. анализ и OHLCV-история. "
        "iNAV доступен только в торговые часы; P&L опционных стратегий считай через option_calc_portfolio(_graph)."
    ),
    "portfolio_manager": (
        "Ты — портфельный менеджер. Обслуживаешь портфель клиента. "
        "Доступны: портфель (portfolio_snapshot, portfolio_rate_whatif, portfolio_income_calendar, portfolio_movers), "
        "скринеры, фундаментал, макро, облигации (bond_report, метрики). "
        "Портфель всегда передаётся параметром assets в markdown. P&L приблизительный: средняя цена покупки, "
        "без купонов/дивидендов и налогов — так и сообщай клиенту."
    ),
}

ASSETS = (
    "refresh date: 2026-09-30\n"
    "# ИИС\n"
    "## Облигации\n"
    "- ОФЗ 26249: 125 шт. (88,8 -> 86,4)\n"
    "- ГТЛК (RU000A10C6F7): 14 шт. (101,69 -> 100,80)\n"
    "## Акции и фонды\n"
    "- Сбербанк (SBER): 51 шт. (321,26 ₽ -> 301 ₽)\n"
    "- Т-Капитал (TMOS): 300 шт. (5,90 ₽ -> 5,65 ₽)"
)

# ------------------------------------------------------------ examples per role
# (user_question, [(tool_name, {args}), ...])
EXAMPLES = {
    "screener": [
        ("Найди корпоративные облигации с доходностью от 16 до 20%, фиксированным купоном, без оферты и номиналом в рублях. Отсортируй по доходности.",
         [("bond_screener", {"ytm_min": 16, "ytm_max": 20, "coupon_type": "fixed", "has_offer": False, "currency": "SUR", "sort_by": "ytm", "sort_desc": True})]),
        ("Какие рублёвые облигации доступны неквалифицированным инвесторам с YTM выше 18%?",
         [("bond_screener", {"ytm_min": 18, "currency": "SUR", "include_qualified": True, "qualified_only": False, "sort_by": "ytm"})]),
        ("Подбери российские акции с дивидендной доходностью не меньше 8% и капитализацией от 100 млрд рублей.",
         [("smartlab_stock_screener", {"div_yield_min": 8, "market_cap_min": 100000000000, "sort_field": "dividend_yield", "sort_desc": True})]),
        ("Покажи дешёвые по P/E акции с ROE выше 15% и рентабельностью EBITDA от 20%.",
         [("smartlab_stock_screener", {"pe_max": 5, "roe_min": 15, "ebitda_margin_min": 20, "sort_field": "pe", "sort_desc": False})]),
        ("Какие БПИФ на золото торгуются на МосБирже?",
         [("etf_screener", {"category": "commodity"})]),
        ("Нужны фонды с Sharpe выше 1 и годовой волатильностью до 20%, покажи лучшие по Шарпу.",
         [("etf_screener", {"sharpe_min": 1.0, "volatility_max": 20, "sort_by": "sharpe", "sort_desc": True})]),
        ("Нефтегазовые эмитенты облигаций с рейтингом Эксперт РА не ниже ruA — кто есть?",
         [("raexpert_emitent_ratings", {"rating_min": "ruA", "sector": "Нефтегазовый"})]),
        ("Какие эмитенты из финансового сектора имеют инвестиционный рейтинг от ruBBB-?",
         [("raexpert_emitent_ratings", {"rating_min": "ruBBB-", "sector": "Финансовый"})]),
        ("Выпускала ли ГТЛК короткие облигации дюрацией до 3 лет?",
         [("moex_emitent_bonds", {"query": "ГТЛК", "max_duration": 3})]),
        ("Найди все бумаги Сбербанка, торгующиеся на MOEX.",
         [("moex_search", {"query": "Сбербанк"})]),
        ("Сравни мультипликаторы GMKN с ближайшими пирами.",
         [("stock_peer_comparison", {"ticker": "GMKN"})]),
        ("На каком месте Сбербанк по NIM и NPL относительно других банков?",
         [("bank_peer_comparison", {"ticker": "SBER"})]),
        ("Какая сейчас ключевая ставка ЦБ? Нужна как ориентир для отбора облигаций.",
         [("cbr_key_rate", {"tail": 1})]),
        ("Инфляция г/г за последние полгода — хочу прикинуть реальную доходность бондов.",
         [("cbr_inflation", {"tail": 6})]),
        ("Сначала проверь, какая сейчас ключевая ставка, потом посмотри инфляцию за полгода.",
         [("cbr_key_rate", {"tail": 1}), ("cbr_inflation", {"tail": 6})]),
        ("Есть ли у Сбера облигационные выпуски, и какие краткосрочные?",
         [("moex_search", {"query": "Сбербанк", "sec_type": "bond"}),
          ("moex_emitent_bonds", {"query": "Сбербанк"})]),
    ],
    "analyst": [
        ("Сделай полный разбор ОФЗ 26253: сценарии при снижении ставки, спред к кривой, конвексность, реальная доходность с учётом инфляции.",
         [("bond_report", {"query": "26253"})]),
        ("Разбери облигацию РЖД с ISIN RU000A106FP4: что с купонами и сколько стоит владение год?",
         [("bond_report", {"query": "RU000A106FP4"})]),
        ("Какая волатильность, Sharpe и макс просадка у ЛУКОЙЛа за последние полгода?",
         [("price_volatility", {"query": "LKOH", "days": 180})]),
        ("Полный технический анализ Сбера с учётом MA200 и Ишимоку.",
         [("technical_indicators", {"query": "SBER", "days": 250})]),
        ("Есть ли на Газпроме медвежьи свечные паттерны за последний квартал?",
         [("candlestick_patterns", {"query": "GAZP", "days": 90})]),
        ("Дивидендная история МТС за все годы — как стабильны выплаты?",
         [("smartlab_dividend_history", {"ticker": "MTSS"})]),
        ("Оцени дивидендную надёжность Сбера: CAGR выплат, средняя доходность, консистентность.",
         [("dividend_analysis", {"ticker": "SBER"})]),
        ("Дай финансовую отчётность X5 по МСФО за все доступные годы.",
         [("smartlab_company_financials", {"ticker": "X5", "standard": "ifrs"})]),
        ("Сравни выручку и ROE Сбера, ВТБ и Т-Банка по годам.",
         [("smartlab_company_financials_multi", {"tickers": ["SBER", "VTBR", "TCSG"]})]),
        ("Сделай сводный фундаментальный отчёт по Роснефти.",
         [("company_fundamental_report", {"ticker": "ROSN"})]),
        ("Посчитай Piotroski F-Score и Altman Z-Score для Фармсинтеза.",
         [("stock_f_score", {"ticker": "PHOR"}), ("stock_z_score", {"ticker": "PHOR"})]),
        ("Что за фонд TMOS: бенчмарк, категория, есть ли премия к NAV?",
         [("etf_fund_info", {"query": "TMOS"})]),
        ("Насколько SBMX отстаёт от своего индекса за последние 3 месяца?",
         [("etf_tracking_error", {"query": "SBMX", "days": 90})]),
        ("Покажи дневные свечи Тинькофф за сентябрь.",
         [("moex_candles", {"query": "T", "frm": "2026-09-01", "till": "2026-09-30", "interval": "24"})]),
        ("С какими бумагами и индексами коррелирует Яндекс, и какая бета к рынку?",
         [("moex_correlations", {"secid": "YDEX"})]),
        ("Какой кредитный рейтинг у Атомэнергопрома по Эксперт РА?",
         [("raexpert_rating", {"query": "Атомэнергопром"})]),
        ("Сколько сейчас НКД по ОФЗ 26253?",
         [("bond_accrued_interest", {"query": "26253"})]),
        ("Если купить ОФЗ 26238 и держать 3 года с реинвестированием купонов, какая будет эффективная доходность?",
         [("bond_synthetic_yield", {"query": "26238", "horizon_years": 3})]),
        ("Как менялась кривая бескупонной доходности ОФЗ за лето?",
         [("moex_zcyc_history", {"frm": "2026-06-01", "till": "2026-08-31"})]),
        ("Быстрый разбор: какой купон у облигации ГТЛК RU000A10C6F7 и когда следующий платёж?",
         [("moex_bond_coupons", {"query": "RU000A10C6F7"})]),
        ("Дай текущую котировку и историю закрытий Северстали за последние 3 месяца.",
         [("moex_quote", {"query": "CHMF"}), ("moex_history", {"query": "CHMF", "frm": "2026-07-01", "till": "2026-09-30"})]),
        ("Был ли сплит у ВТБ и когда?",
         [("moex_splits", {"secid": "VTB"})]),
        ("Оцени ликвидность и волатильность Феесаллера — могу ли выйти из позиции без потерь?",
         [("liquidity_assessment", {"query": "FEES", "days": 90}),
          ("price_volatility", {"query": "FEES", "days": 90})]),
    ],
    "constructor": [
        ("Вот мой портфель:\n\n" + ASSETS + "\n\nСделай полный снимок: веса, P&L, дюрацию, денежный поток и реальную доходность.",
         [("portfolio_snapshot", {"assets": ASSETS})]),
        ("Для портфеля:\n\n" + ASSETS + "\n\nчто будет, если доходности облигаций упадут на 2 п.п.?",
         [("portfolio_rate_whatif", {"delta_pp": -2, "assets": ASSETS})]),
        ("Составь календарь поступлений на полгода вперёд для портфеля:\n\n" + ASSETS,
         [("portfolio_income_calendar", {"assets": ASSETS})]),
        ("Кто в портфеле\n\n" + ASSETS + "\n\nсегодня вырос и кто просел?",
         [("portfolio_movers", {"assets": ASSETS})]),
        ("Проверь кредитный рейтинг ГТЛК перед включением его облигаций в портфель.",
         [("raexpert_rating", {"query": "ГТЛК"})]),
        ("Подбери надёжные корпоративные облигации для консервативной части портфеля: YTM от 14 до 17%, рейтинг от ruAA, без офёрты.",
         [("bond_screener", {"ytm_min": 14, "ytm_max": 17, "rating_min": "ruAA", "has_offer": False, "currency": "SUR", "sort_by": "ytm"})]),
        ("Рассматриваю TMOS как акционную часть портфеля. Расскажи о фонде.",
         [("etf_fund_info", {"query": "TMOS"})]),
        ("Какая сейчас доходность кривой ОФЗ на срок 4.5 года? Хочу сравнить с корпоративной бумагой похожей дюрации.",
         [("curve_yield", {"years": 4.5})]),
        ("Дай котировки кандидатов: SBER, TMOS и ОФЗ 26249 — нужно для сборки портфеля.",
         [("moex_quote", {"query": "SBER"}), ("moex_quote", {"query": "TMOS"}), ("moex_bond", {"query": "26249"})]),
        ("Собираю портфель: сначала сделай снимок текущего, затем оцени чувствительность к росту ставки на 1 п.п.\n\n" + ASSETS,
         [("portfolio_snapshot", {"assets": ASSETS}),
          ("portfolio_rate_whatif", {"delta_pp": 1, "assets": ASSETS})]),
    ],
    "timer": [
        ("Стоит ли сейчас входить в Сбера? Дай полную техническую картину.",
         [("technical_indicators", {"query": "SBER", "days": 180})]),
        ("Проверь ЛУКОЙЛ на свечные паттерны за полгода — ищу точку входа.",
         [("candlestick_patterns", {"query": "LKOH", "days": 180})]),
        ("Какая волатильность и просадки у Газпрома за квартал? Оцениваю риск входа.",
         [("price_volatility", {"query": "GAZP", "days": 90})]),
        ("Что сейчас по базису фьючерса на Si — contango или backwardation?",
         [("moex_futures_basis", {"asset_code": "Si"})]),
        ("Какие фьючерсы на нефть Brent сейчас торгуются и у какого лучшие объёмы?",
         [("moex_futures_list", {"asset_code": "BR"})]),
        ("Покажи опционную доску по Сберу: где центральный страйк и какая IV?",
         [("moex_options_board", {"asset": "SBRF"})]),
        ("Найди серии опционов на Si и покажи доску для ближайшей месячной серии.",
         [("option_calc_series", {"asset_code": "Si"})]),
        ("Хочу видеть доску опционов Si серии SI-9.26M100926XA — по 5 страйков от центрального.",
         [("option_calc_optionboard", {"asset_code": "Si", "optionseries_code": "SI-9.26M100926XA", "rows": 5})]),
        ("Построй smile волатильности для серии опционов на Si SI-9.26M100926XA.",
         [("option_calc_volatility_graph", {"asset_code": "Si", "optionseries_code": "SI-9.26M100926XA"})]),
        ("Покажи дневные свечи ВТБ за последние 2 месяца — оцениваю пробой уровня.",
         [("moex_candles", {"query": "VTBR", "frm": "2026-08-01", "till": "2026-09-30", "interval": "24"})]),
        ("Что с котировкой опциона Si87000BI6A — премия, спред, открытый интерес?",
         [("moex_option_quote", {"secid": "Si87000BI6A"})]),
        ("Какие call-опционы на Si со страйком 84000 на экспирацию 17.09.2026, и какова их дельта?",
         [("option_calc_options", {"asset_code": "Si", "expiration_date": "2026-09-17", "option_type": "call", "strike": 84000})]),
        ("Оцени опцион Si84000BC6A: греки сейчас и what-if, если Si вырастет до 86000 при той же IV.",
         [("option_calc_option_brief", {"asset_code": "Si", "secid": "Si84000BC6A", "underlying_price": 86000})]),
        ("RSI по Яндексу не перекуплен? Проверь по полной истории индикаторов.",
         [("technical_indicators", {"query": "YDEX", "days": 180})]),
    ],
    "macrotracker": [
        ("Как менялась ключевая ставка ЦБ за последний год?",
         [("cbr_key_rate", {"tail": 12})]),
        ("Дай инфляцию г/г и ключевую ставку помесячно за год.",
         [("cbr_inflation", {"tail": 12})]),
        ("Что сейчас с RUONIA и как она соотносится с ключевой?",
         [("cbr_ruonia", {"tail": 5})]),
        ("Покажи RUONIA-индекс и срочные средние RUONIA за месяц/квартал.",
         [("cbr_ruonia_index", {"tail": 5})]),
        ("Какие сейчас ставки MIACR на межбанке по разным срокам?",
         [("cbr_ibor", {"tail": 5})]),
        ("Что рынок закладывает в ключевую ставку на ближайший год по кривой ОФЗ?",
         [("rate_expectations", {})]),
        ("Проверь ожидания по ставке, если ключевая сейчас 14.5%.",
         [("rate_expectations", {"key_rate": 14.5})]),
        ("Какая доходность на кривой ОФЗ для срока 5.5 года?",
         [("curve_yield", {"years": 5.5})]),
        ("Дай доходность G-кривой на 30 лет — за пределами стандартных узлов.",
         [("curve_yield", {"years": 30})]),
        ("Динамика юаня к рублю с начала года по данным ЦБ.",
         [("cbr_currency", {"symbol": "CNY", "first_date": "2026-01-01", "last_date": "2026-09-30"})]),
        ("Учётные цены ЦБ на золото за последнюю неделю.",
         [("cbr_metals", {"tail": 7})]),
        ("Как менялись международные резервы РФ в этом квартале?",
         [("cbr_reserves", {"tail": 3})]),
        ("История кривой КБД за сентябрь — хочу видеть тренд коротких и длинных доходностей.",
         [("moex_zcyc_history", {"frm": "2026-09-01", "till": "2026-09-30"})]),
        ("Какие сейчас индикативные курсы на валютном срочном рынке?",
         [("moex_indicative_rates", {})]),
        ("Оцени монетарный фон: ключевая ставка, инфляция и рыночные ожидания разом.",
         [("cbr_key_rate", {"tail": 1}), ("cbr_inflation", {"tail": 3}), ("rate_expectations", {})]),
    ],
    "risk_manager": [
        ("Оцени риск портфеля при росте доходностей облигаций на 2 п.п.:\n\n" + ASSETS,
         [("portfolio_rate_whatif", {"delta_pp": 2, "assets": ASSETS})]),
        ("Полная оценка рисков портфеля: дюрация, ставка, концентрация, денежный поток.\n\n" + ASSETS,
         [("portfolio_snapshot", {"assets": ASSETS})]),
        ("Волатильность Сбера за 90 дней с безрисковой ставкой 14% — оцени Var-риск позиции.",
         [("price_volatility", {"query": "SBER", "days": 90, "rf_annual": 14.0})]),
        ("Насколько ликвидна бумага FEES — смогу ли выйти из позиции при просадке?",
         [("liquidity_assessment", {"query": "FEES", "days": 90})]),
        ("Не перекуплен ли Яндекс по RSI? Оцениваю риск разворота.",
         [("technical_indicators", {"query": "YDEX", "days": 90})]),
        ("Какой процентный риск у ОФЗ 26238: дюрация, конвексность, безубыток?",
         [("bond_report", {"query": "26238"})]),
        ("Реальная доходность портфеля с учётом фактической инфляции — сильно ли её съедает?\n\n" + ASSETS,
         [("portfolio_snapshot", {"assets": ASSETS}), ("cbr_inflation", {"tail": 1})]),
        ("Сколько нужно ГО под позицию: 5 фьючерсов SiU6 и продажа 10 опционов Si84000BI6?",
         [("option_calc_initial_margin", {"positions": [{"secid": "SiU6", "type": "futures", "quantity": 5}, {"secid": "Si84000BI6", "type": "option", "quantity": -10}]})]),
        ("Что делают юрлица на фьючерсах Si — растут ли длинные позиции при падении цены?",
         [("moex_futures_open_interest", {"asset": "Si"})]),
        ("Рынок закладывает снижение ключевой ставки? Проверь по кривой ОФЗ.",
         [("rate_expectations", {})]),
        ("Насколько Т-Банк коррелирует с индексом — высок ли системный риск позиции?",
         [("moex_correlations", {"secid": "T"})]),
        ("Стресс-тест: снимок портфеля и эффект -1.5 п.п. по облигациям.\n\n" + ASSETS,
         [("portfolio_snapshot", {"assets": ASSETS}),
          ("portfolio_rate_whatif", {"delta_pp": -1.5, "assets": ASSETS})]),
    ],
    "instrument_specialist": [
        ("Есть ли сейчас премия или дисконт у TMOS к iNAV?",
         [("etf_premium_discount", {"query": "TMOS"})]),
        ("Как TGLD отслеживает золото? Трекинг-ошибка за полгода.",
         [("etf_tracking_error", {"query": "TGLD", "days": 180})]),
        ("Расскажи о фонде SBMX: бенчмарк, категория, премия/дисконт.",
         [("etf_fund_info", {"query": "SBMX"})]),
        ("Покажи фонды денежного рынка, отсортированные по объёму.",
         [("etf_screener", {"category": "money_market", "sort_by": "volume", "sort_desc": True})]),
        ("Какие фьючерсы на Brent есть и когда ближайшая экспирация?",
         [("moex_futures_series", {"asset": "BR"})]),
        ("Сравни фьючерсы RTS по открытом интересу и ГО.",
         [("moex_futures_list", {"asset_code": "RTS"})]),
        ("Какой carry у фьючерса на Газпром — contango или backwardation?",
         [("moex_futures_basis", {"asset_code": "GAZP"})]),
        ("Опционная доска по GAZR: страйки, IV, открытый интерес.",
         [("moex_options_board", {"asset": "GAZR"})]),
        ("Какой спред bid/offer у опциона GZ85CU6A?",
         [("moex_option_orderbook", {"secid": "GZ85CU6A"})]),
        ("История сделок по опциону GZ85CU6A за август-сентябрь.",
         [("moex_option_history", {"secid": "GZ85CU6A", "frm": "2026-08-01", "till": "2026-09-30"})]),
        ("Рассчитай Greeks опциона Si84000BC6A при условии, что Si стоит 86000, а IV 24%.",
         [("option_calc_option_brief", {"asset_code": "Si", "secid": "Si84000BC6A", "underlying_price": 86000, "volatility": 24.0})]),
        ("Построй график P&L стратегии: продажа 10 call Si84000BC6A и покупка 5 фьючерсов SIU6.",
         [("option_calc_portfolio_graph", {"asset_code": "Si", "positions": [{"secid": "Si84000BC6A", "type": "option", "quantity": -10}, {"secid": "SIU6", "type": "futures", "quantity": 5}], "indicator": "profit_and_loss"})]),
        ("Был ли сплит у ВТБ?",
         [("moex_splits", {"secid": "VTB"})]),
        ("Какие базовые активы доступны в опционном калькуляторе в секторе индексов?",
         [("option_calc_assets", {"asset_type": "index"})]),
        ("Покажи недельные свечи TMOS за квартал и трекинг-ошибку за 3 месяца.",
         [("moex_candles", {"query": "TMOS", "frm": "2026-07-01", "till": "2026-09-30", "interval": "7"}),
          ("etf_tracking_error", {"query": "TMOS", "days": 90})]),
    ],
    "portfolio_manager": [
        ("Подготовь обзор портфеля клиента для встречи: полный снимок.\n\n" + ASSETS,
         [("portfolio_snapshot", {"assets": ASSETS})]),
        ("Клиент спрашивает, что сегодня двигалось в его портфеле:\n\n" + ASSETS,
         [("portfolio_movers", {"assets": ASSETS})]),
        ("Какие купоны и дивиденды придут в портфель ближайшее время?\n\n" + ASSETS,
         [("portfolio_income_calendar", {"assets": ASSETS})]),
        ("Если ставка вырастет на 1 п.п. — насколько просадит портфель клиента?\n\n" + ASSETS,
         [("portfolio_rate_whatif", {"delta_pp": 1, "assets": ASSETS})]),
        ("Когда ближайшие отчёты публичных компаний? Хочу предупредить клиента о событиях.",
         [("moex_ir_calendar", {"limit": 10})]),
        ("Текущая цена Сбера для отчёта клиенту.",
         [("moex_quote", {"query": "SBER"})]),
        ("Клиент держит ОФЗ 26249 — обнови её метрики: YTM, дюрация, купон.",
         [("moex_bond", {"query": "26249"})]),
        ("Полный разбор ОФЗ 26249 для клиента: сценарии и спред к кривой.",
         [("bond_report", {"query": "26249"})]),
        ("Какие дивиденды объявлены на ближайший месяц?",
         [("smartlab_dividends", {"limit": 10})]),
        ("Сводный фундаментальный отчёт по ЛУКОЙЛу — клиент спрашивает про качество бумаг.",
         [("company_fundamental_report", {"ticker": "LKOH"})]),
        ("Когда у ЗПИФ Акцент ближайшая выплата и сколько за пай?",
         [("zpif_payments", {"fund_name": "Акцент"})]),
        ("Какие ЗПИФ недвижимости есть в календаре выплат?",
         [("zpif_funds_list", {})]),
        ("Ключевая ставка сейчас — для контекста отчёта клиенту.",
         [("cbr_key_rate", {"tail": 1})]),
        ("Обнови цены позиций и покажи, кто из облигаций торгуется дорого к кривой:\n\n" + ASSETS,
         [("portfolio_snapshot", {"assets": ASSETS})]),
    ],
}


def main():
    total = 0
    for role, examples in EXAMPLES.items():
        path = os.path.join(OUT, f"{role}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for i, (question, calls) in enumerate(examples):
                tool_defs = [TOOL_MAP[n] for n, _ in calls]
                tool_calls = [
                    {
                        "id": f"call_{i}_{j}",
                        "type": "function",
                        "function": {"name": n, "arguments": json.dumps(args, ensure_ascii=False)},
                    }
                    for j, (n, args) in enumerate(calls)
                ]
                row = {
                    "messages": [
                        {"role": "system", "content": ROLE_PROMPTS[role]},
                        {"role": "user", "content": question},
                        {"role": "assistant", "content": None, "tool_calls": tool_calls},
                    ],
                    "tools": tool_defs,
                }
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                total += 1
        print(f"{path}: {len(examples)} examples")
    print(f"Total: {total}")


if __name__ == "__main__":
    main()
