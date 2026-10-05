#!/usr/bin/env python3
"""Генератор синтетического датасета tool-calling для ru-finance MCP (по ролям).

Запуск из корня репо или из dataset/: python3 dataset/gen.py
JSONL-файлы пишутся рядом со скриптом (dataset/<role>.jsonl).

Каждый пример несёт 3 парафраза вопроса (разный текст, одинаковый вызов) —
на каждый инструмент в датасете минимум 3 разных пользовательских запроса.
main() проверяет покрытие и падает, если условие нарушено.

При добавлении нового @mcp.tool() в mcp_server.py:
1) добавьте схему в TOOLS (helper T(name, desc, required, **props));
2) добавьте пример (3 парафраза + вызовы) в EXAMPLES нужной роли.
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
      sec_type=("string", "Фильтр типа: bond, share, stock, fund, etf, index"),
      limit=("integer", "Макс. результатов (по умолч. 15)")),
    T("moex_quote", "Текущая котировка акции/фонда на MOEX (задержка ~15 мин).",
      ("query",), query=("string", "Тикер или название")),
    T("moex_bond", "Облигация с метриками: цена, YTM, дюрация, купон, погашение.",
      ("query",), query=("string", "Номер ОФЗ или ISIN")),
    T("moex_candles", "Свечи OHLCV за период.",
      ("query", "frm", "till"),
      query=("string", "Тикер/ISIN"), frm=("string", "Дата начала YYYY-MM-DD"),
      till=("string", "Дата конца YYYY-MM-DD"),
      interval=("string", "Интервал: 10,60,24,7,31,4 (по умолч. 24 — день)"),
      allow_big_output=("boolean", "Разрешить большой ответ (по умолч. False)")),
    T("moex_history", "Дневная история торгов (компактная: дата, закрытие, объём).",
      ("query", "frm", "till"),
      query=("string", "Тикер/ISIN"), frm=("string", "YYYY-MM-DD"), till=("string", "YYYY-MM-DD")),
    T("moex_full_history", "Дневная история торгов MOEX, все поля ISS.",
      ("query", "frm", "till"),
      query=("string", "Тикер/ISIN"), frm=("string", "YYYY-MM-DD"), till=("string", "YYYY-MM-DD")),
    T("moex_search_endpoints", "Найти ISS-эндпоинты по подстроке пути (для доступа к сырым данным).",
      ("pattern",), pattern=("string", "Подстрока пути: '/candles', '/dividends', 'turnovers'")),
    T("moex_query", "Универсальный доступ к любому ISS-эндпоинту по template_id (fallback).",
      ("template_id",),
      template_id=("integer", "ID шаблона из moex_search_endpoints"),
      path_vars=("object", "engine/market/board/security (опц.)"),
      query_params=("object", "from/till и др. (опц.)")),
    T("moex_company_info", "Поиск компании по ИНН/ОГРН/названию (CCI/NSD), до 20 результатов.",
      ("query",), query=("string", "ИНН, ОГРН, тикер или фрагмент названия")),
    T("moex_company_info_by_id", "Компания по внутреннему ID MOEX (basis_company_id из moex_company_info).",
      ("company_id",), company_id=("integer", "Числовой ID компании")),
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
      ("asset_code",), asset_code=("string", "Код базисного актива: Si, RTS, BR, GAZR...")),
    T("moex_futures_open_interest", "Открытый интерес фьючерсов по юрлицам/физлицам.",
      ("asset",), asset=("string", "Код базисного актива: Si, RTS, SBRF...")),
    T("moex_futures_series", "Календарь экспираций фьючерсов (серии).",
      (), asset=("string", "Код базисного актива (опц.)")),
    T("moex_futures_basis", "Contango/backwardation: годовая ставка переноса фьючерса к споту.",
      ("asset_code",), asset_code=("string", "Код базисного актива: Si, GAZR, GAZP...")),
    T("moex_futures_promo", "Агрегированная статистика комиссий срочного рынка FORTS."),

    T("moex_options_assets", "Базисные активы опционов FORTS с рыночными данными.",
      ()),
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
      period=("string", "y (по умолч.), q или LTM"),
      standard=("string", "MSFO (по умолч.) или RAS"),
      fields=("array", "Список полей отчётности (опц.)")),
    T("smartlab_company_financials_multi", "Финансовая отчётность нескольких компаний (batch).",
      ("tickers",), tickers=("array", "Список тикеров"),
      period=("string", "y (по умолч.), q или LTM"), standard=("string", "MSFO (по умолч.) или RAS"),
      fields=("array", "Список полей отчётности (опц.)")),
    T("smartlab_stock_screener", "Фундаментальный скринер акций MOEX (smart-lab.ru).",
      (),
      period=("string", "Период отчётности: LTM (по умолч.), y, q"),
      report_type=("string", "Код типа отчёта (-1 по умолч.)"),
      sector_id=("integer", "ID сектора"),
      capitalization_min=("integer", "Мин. капитализация, ₽"),
      capitalization_max=("integer", "Макс. капитализация, ₽"),
      volume_min=("integer", "Мин. среднедневной оборот, ₽"),
      volume_max=("integer", "Макс. среднедневной оборот, ₽"),
      company_type=("string", "Тип компании (опц.)"),
      is_state_owned=("integer", "Гос-компания: -1 все, 0 нет, 1 да"),
      is_exporter=("integer", "Экспортёр: -1 все, 0 нет, 1 да"),
      is_raw_stuff=("integer", "Сырьевая: -1 все, 0 нет, 1 да"),
      emitent=("string", "Эмитент (опц.)"),
      order_by=("string", "Поле сортировки (по умолч. market_cap)"),
      order_dir=("string", "asc/desc (по умолч. desc)"),
      limit=("integer", "Макс. результатов (по умолч. 15)")),
    T("stock_f_score", "Piotroski F-Score (0–9) по финансовой отчётности.",
      ("ticker",), ticker=("string", "Тикер"), standard=("string", "MSFO (по умолч.) или RAS")),
    T("stock_z_score", "Altman Z-Score (модиф. для emerging markets) — риск банкротства.",
      ("ticker",), ticker=("string", "Тикер"), standard=("string", "MSFO (по умолч.) или RAS")),
    T("stock_peer_comparison", "Сравнение мультипликаторов акции с топ-N peers.",
      ("ticker",), ticker=("string", "Тикер"), limit=("integer", "Сколько peers (по умолч. 20)")),
    T("dividend_analysis", "Дивидендный анализ: CAGR, средняя доходность, стабильность выплат.",
      ("ticker",), ticker=("string", "Тикер")),
    T("stock_growth_analysis", "Рост выручки/EBITDA/прибыли, тренды ROE/ROA/маржинальности.",
      ("ticker",), ticker=("string", "Тикер"), standard=("string", "MSFO (по умолч.) или RAS")),
    T("bank_benchmark", "Бенчмарк банков по ключевым банковским метрикам (NIM, NPL, LDR...).",
      ("tickers",), tickers=("array", "Список тикеров банков"),
      standard=("string", "MSFO (по умолч.) или RAS")),
    T("bank_peer_comparison", "Ранжирование банка по метрикам относительно сектора (~15 крупнейших).",
      ("ticker",), ticker=("string", "Тикер банка")),
    T("company_fundamental_report", "Всё в одном: мультипликаторы, дивиденды, рост, Z/F-Score, пирс.",
      ("ticker",), ticker=("string", "Тикер"), standard=("string", "MSFO (по умолч.) или RAS")),

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
      emitent=("string", "Эмитент (опц.)"),
      sort_by=("string", "ytm, duration, maturity, price, coupon, issue_volume"),
      sort_desc=("boolean", "По убыванию (по умолч.)"),
      limit=("integer", "Макс. результатов (1..500, по умолч. 15)")),
    T("bond_prescreener", "Компактный прескринер облигаций: только secname и isin, те же фильтры, что у bond_screener.",
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
      emitent=("string", "Эмитент (опц.)"),
      sort_by=("string", "ytm, duration, maturity, price, coupon, issue_volume"),
      sort_desc=("boolean", "По убыванию (по умолч.)"),
      limit=("integer", "Макс. результатов (1..500, по умолч. 500)")),
    T("bond_synthetic_yield", "Синтетическая доходность с реинвестированием купонов на горизонте.",
      ("query", "horizon_years"),
      query=("string", "Тикер/ISIN облигации"), horizon_years=("number", "Горизонт, лет"),
      reinvest_rate=("number", "Ставка реинвестирования, % (по умолч. = YTM)")),

    T("price_volatility", "Волатильность, Sharpe, max drawdown по дневным свечам.",
      ("query",), query=("string", "Тикер"), days=("integer", "Период, дней (по умолч. 90)"),
      rf_annual=("number", "Безрисковая ставка, % годовых (по умолч. 16)")),
    T("liquidity_assessment", "Оценка ликвидности: оборот, Amihud, спред, скор 0-10, grade A-E.",
      ("query",), query=("string", "Тикер"), days=("integer", "Период, дней (по умолч. 90)")),
    T("technical_indicators", "Полный набор технических индикаторов из дневных свечей (RSI, MACD, Bollinger, Ichimoku, ADX, MA...).",
      ("query",), query=("string", "Тикер/ISIN"), days=("integer", "Период, дней (по умолч. 90; для MA200 нужно ≥200)")),
    T("candlestick_patterns", "Распознавание классических свечных паттернов (14 шт.) из дневных свечей.",
      ("query",), query=("string", "Тикер/ISIN"), days=("integer", "Период, дней (по умолч. 90)")),

    T("cointegration_test", "Тест коинтеграции двух бумаг MOEX (Engle-Granger / Йохансен): hedge ratio, half-life.",
      ("ticker1", "ticker2"),
      ticker1=("string", "Тикер 1"), ticker2=("string", "Тикер 2"),
      days=("integer", "Период, дней (по умолч. 252)"),
      method=("string", "engle_granger, johansen или both (по умолч.)")),
    T("cointegration_scan", "Поиск коинтегрированных пар для тикера среди списка кандидатов.",
      ("ticker", "candidates"),
      ticker=("string", "Референсный тикер"), candidates=("string", "Кандидаты через запятую"),
      days=("integer", "Период, дней (по умолч. 252)"),
      method=("string", "engle_granger, johansen или both (по умолч.)")),
    T("cointegration_matrix", "Попарный тест коинтеграции для списка тикеров.",
      ("tickers",),
      tickers=("string", "Тикеры через запятую"),
      days=("integer", "Период, дней (по умолч. 252)"),
      method=("string", "engle_granger, johansen или both (по умолч.)")),

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
    T("portfolio_alpha_beta", "Альфа, бета и корреляция портфеля к бенчмарку (по умолч. IMOEX).",
      ("assets",),
      assets=("string", "Портфель в markdown (как в portfolio_snapshot)"),
      benchmark=("string", "Тикер бенчмарка: IMOEX, RGBI, RGBITR, MCFTR, RTSI (по умолч. IMOEX)"),
      days=("integer", "Горизонт в торговых днях (по умолч. 252)")),
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
    T("option_calc_asset_detail", "Детали одного базового актива опционного калькулятора.",
      ("asset_code",), asset_code=("string", "Код актива: Si, GAZR, SBRF, RTS"),
      asset_type=("string", "опц.")),
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
      asset_type=("string", "опц."),
      delta_sigma=("number", "Сдвиг IV для what-if, %"), date_of_calculation=("string", "YYYY-MM-DD для what-if")),
    T("option_calc_portfolio_graph", "График P&L или Greeks опционной стратегии по цене БА.",
      ("asset_code", "positions", "indicator"),
      asset_code=("string", "Код актива"),
      positions=("array", "Позиции [{secid, type: option|futures, quantity, ...}]"),
      indicator=("string", "profit_and_loss, delta, gamma, vega, theta, rho"),
      asset_type=("string", "опц."),
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
# (3 парафраза вопроса, [(tool_name, {args}), ...]) — по строке на парафраз
EXAMPLES = {
    "screener": [
        ((
            "Найди корпоративные облигации с доходностью от 16 до 20%, фиксированным купоном, без оферты и номиналом в рублях. Отсортируй по доходности.",
            "Подбери рублёвые корпоративные бонды: YTM в диапазоне 16–20%, купон фиксированный, офёрты нет. Отсортируй по убыванию доходности.",
            "Покажи корпоративные облигации в рублях с доходностью к погашению 16–20% годовых, постоянным купоном и без встроенного колл-опциона.",
         ),
         [("bond_screener", {"ytm_min": 16, "ytm_max": 20, "coupon_type": "fixed", "has_offer": False, "currency": "SUR", "sort_by": "ytm", "sort_desc": True})]),
        ((
            "Какие рублёвые облигации доступны неквалифицированным инвесторам с YTM выше 18%?",
            "Мне нужен список бондов для неквала: доходность от 18% и выше, валюта рубли.",
            "Отбери облигации, которые может купить неквалифицированный инвестор, с доходностью к погашению более 18% годовых.",
         ),
         [("bond_screener", {"ytm_min": 18, "currency": "SUR", "include_qualified": True, "qualified_only": False, "sort_by": "ytm"})]),
        ((
            "Подбери российские акции с капитализацией не меньше 100 млрд рублей, отсортируй по капитализации.",
            "Нужны акции МосБиржи компаний стоимостью от 100 млрд ₽, крупные сверху.",
            "Отбери бумаги с капитализацией от 100 миллиардов рублей и отсортируй по убыванию капитализации.",
         ),
         [("smartlab_stock_screener", {"capitalization_min": 100000000000, "order_by": "market_cap", "order_dir": "desc"})]),
        ((
            "Покажи государственные компании с капитализацией от 100 млрд рублей.",
            "Отбери гос-компании стоимостью от 100 миллиардов — sorted по капитализации.",
            "Ищу бумаги гос-owned эмитентов с капитализацией не ниже 100 млрд ₽.",
         ),
         [("smartlab_stock_screener", {"is_state_owned": 1, "capitalization_min": 100000000000, "order_by": "market_cap"})]),
        ((
            "Какие БПИФ на золото торгуются на МосБирже?",
            "Дай список биржевых фондов на драгоценный металл — золото.",
            "Есть на MOEX фонды с бенчмарком по золоту? Покажи все.",
         ),
         [("etf_screener", {"category": "commodity"})]),
        ((
            "Нужны фонды с Sharpe выше 1 и годовой волатильностью до 20%, покажи лучшие по Шарпу.",
            "Отбери БПИФ: коэффициент Шарпа больше 1, волатильность не выше 20% в год, отсортируй по Шарпу.",
            "Покажи фонды с соотношением риск/доходность (Sharpe) от 1 и волатильностью до 20%, лучшие сверху.",
         ),
         [("etf_screener", {"sharpe_min": 1.0, "volatility_max": 20, "sort_by": "sharpe", "sort_desc": True})]),
        ((
            "Нефтегазовые эмитенты облигаций с рейтингом Эксперт РА не ниже ruA — кто есть?",
            "Дай эмитентов бондов из нефтегазового сектора с рейтингом от ruA по Эксперт РА.",
            "Кто в отрасли нефти и газа имеет кредитный рейтинг ruA и выше?",
         ),
         [("raexpert_emitent_ratings", {"rating_min": "ruA", "sector": "Нефтегазовый"})]),
        ((
            "Какие эмитенты из финансового сектора имеют инвестиционный рейтинг от ruBBB-?",
            "Покажи банки и финансовые компании с рейтингом не ниже ruBBB-.",
            "Отбери эмитентов облигаций финансовой отрасли с инвестрейтингом ruBBB- и выше.",
         ),
         [("raexpert_emitent_ratings", {"rating_min": "ruBBB-", "sector": "Финансовый"})]),
        ((
            "Выпускала ли ГТЛК короткие облигации дюрацией до 3 лет?",
            "Есть у ГТЛК бонды с дюрацией не больше трёх лет?",
            "Покажи выпуски ГТЛК со сроком до 3 лет.",
         ),
         [("moex_emitent_bonds", {"query": "ГТЛК", "max_duration": 3})]),
        ((
            "Найди все бумаги Сбербанка, торгующиеся на MOEX.",
            "Что из бумаг Сбера обращается на МосБирже?",
            "Покажи все инструменты Сбербанка на бирже.",
         ),
         [("moex_search", {"query": "Сбербанк"})]),
        ((
            "Сравни мультипликаторы GMKN с ближайшими пирами.",
            "Как Норникель выглядит на фоне компаний-аналогов по мультипликаторам?",
            "Дай пир-компарисон для GMKN.",
         ),
         [("stock_peer_comparison", {"ticker": "GMKN"})]),
        ((
            "На каком месте Сбербанк по NIM и NPL относительно других банков?",
            "Оцени положение Сбера по ключевым банковским метрикам среди сектора.",
            "Где Сбер в рейтинге банков по марже и качеству кредитного портфеля?",
         ),
         [("bank_peer_comparison", {"ticker": "SBER"})]),
        ((
            "Какая сейчас ключевая ставка ЦБ? Нужна как ориентир для отбора облигаций.",
            "Скажи текущее значение ключевой ставки — подбираю бонды.",
            "Какая ключевая ставка действует сейчас?",
         ),
         [("cbr_key_rate", {"tail": 1})]),
        ((
            "Инфляция г/г за последние полгода — хочу прикинуть реальную доходность бондов.",
            "Дай CPI год к году за 6 месяцев.",
            "Сколько составляет годовая инфляция в последние полгода?",
         ),
         [("cbr_inflation", {"tail": 6})]),
        ((
            "Сначала проверь, какая сейчас ключевая ставка, потом посмотри инфляцию за полгода.",
            "Мне нужны два показателя: текущая ключевая ставка и инфляция г/г за 6 месяцев.",
            "Покажи ключевую ставку ЦБ и следом полугодовую инфляцию.",
         ),
         [("cbr_key_rate", {"tail": 1}), ("cbr_inflation", {"tail": 6})]),
        ((
            "Есть ли у Сбера облигационные выпуски, и какие краткосрочные?",
            "Найди бонды Сбербанка на бирже и отдельно — короткие выпуски.",
            "Сначала поищи облигации Сбера, потом дай весь список его выпусков.",
         ),
         [("moex_search", {"query": "Сбербанк", "sec_type": "bond"}),
          ("moex_emitent_bonds", {"query": "Сбербанк"})]),
        ((
            "Что за бумага с ISIN RU000A10C6F7?",
            "Определи по ISIN RU000A10C6F7, что это за выпуск.",
            "Расшифруй идентификатор RU000A10C6F7 — какой это инструмент?",
         ),
         [("moex_resolve", {"query": "RU000A10C6F7"})]),
        ((
            "Какие обороты по рынкам МосБиржи за последний торговый день?",
            "Дай сводные объёмы торгов по рынкам MOEX за сегодня.",
            "Сколько наторговали на каждом рынке биржи за сутки?",
         ),
         [("moex_turnovers", {})]),
        ((
            "Дай компактный список рублёвых облигаций с YTM от 16% — только названия и ISIN.",
            "Нужен короткий перечень бондов в рублях с доходностью от 16%, без лишних полей.",
            "Прогони прескринер: рублёвые облигации, YTM минимум 16, выведи только secname и ISIN.",
         ),
         [("bond_prescreener", {"ytm_min": 16, "currency": "SUR"})]),
        ((
            "Какой ISS-эндпоинт отвечает за дивиденды? Найди по подстроке пути.",
            "Нужен template_id эндпоинта dividends в ISS — поищи.",
            "Найди в справочнике ISS эндпоинты, где путь содержит '/dividends'.",
         ),
         [("moex_search_endpoints", {"pattern": "/dividends"})]),
        ((
            "Выполни сырой запрос к ISS: шаблон 30, бумага SU26249RMFS4 в TQOB, за сентябрь.",
            "Мне нужны сырые данные через moex_query: template_id 30, path_vars для ОФЗ 26249 (stock/bonds/TQOB), период 1–30 сентября.",
            "Сделай fallback-запрос к ISS: template 30, engine stock, market bonds, board TQOB, security SU26249RMFS4, from 2026-09-01, till 2026-09-30.",
         ),
         [("moex_query", {"template_id": 30,
                          "path_vars": {"engine": "stock", "market": "bonds", "board": "TQOB", "security": "SU26249RMFS4"},
                          "query_params": {"from": "2026-09-01", "till": "2026-09-30"}})]),
        ((
            "Найди компанию по ИНН 7707083893.",
            "Определи организацию с ОГРН 1027700132197.",
            "Поищи в реестре компаний Сбербанк (по названию).",
         ),
         [("moex_company_info", {"query": "7707083893"})]),
        ((
            "Дай подробности по компании с ID 10171.",
            "Покажи карточку компании по basis_company_id = 10171.",
            "Расшифруй внутренний MOEX-ID 10171 — что за компания?",
         ),
         [("moex_company_info_by_id", {"company_id": 10171})]),
    ],
    "analyst": [
        ((
            "Сделай полный разбор ОФЗ 26253: сценарии при снижении ставки, спред к кривой, конвексность, реальная доходность с учётом инфляции.",
            "Разбери выпуск 26253 по полной: что будет при снижении ключевой, спред к G-кривой, конвексность и доходность минус инфляция.",
            "Оцени ОФЗ 26253 глубоко — сценарии ставок, спред к бескупонной кривой, конвексность, реальная доходность.",
         ),
         [("bond_report", {"query": "26253"})]),
        ((
            "Разбери облигацию РЖД с ISIN RU000A106FP4: что с купонами и сколько стоит владение год?",
            "Дай обзор бонда РЖД RU000A106FP4: купоны и доходность на годовом горизонте.",
            "Проанализируй выпуск RU000A106FP4 (РЖД) — купонная политика и годовое владение.",
         ),
         [("bond_report", {"query": "RU000A106FP4"})]),
        ((
            "Какая волатильность, Sharpe и макс просадка у ЛУКОЙЛа за последние полгода?",
            "Оцени риск-профиль LKOH за 180 дней: волатильность, коэффициент Шарпа, максимальная просадка.",
            "Посчитай volatility, Sharpe и drawdown по ЛУКОЙЛу за полгода.",
         ),
         [("price_volatility", {"query": "LKOH", "days": 180})]),
        ((
            "Полный технический анализ Сбера с учётом MA200 и Ишимоку.",
            "Прогони все индикаторы по SBER — нужны RSI, MACD, Боллинджер, Ишимоку и длинные средние.",
            "Дай полную техкартину Сбербанка, включая MA200.",
         ),
         [("technical_indicators", {"query": "SBER", "days": 250})]),
        ((
            "Есть ли на Газпроме медвежьи свечные паттерны за последний квартал?",
            "Проверь GAZP на разворотные медвежьи комбинации свечей за 90 дней.",
            "Найди сигналы продаж в свечных паттернах Газпрома за квартал.",
         ),
         [("candlestick_patterns", {"query": "GAZP", "days": 90})]),
        ((
            "Дивидендная история МТС за все годы — как стабильны выплаты?",
            "Покажи все дивиденды МТС по годам и оцени их постоянство.",
            "Как МТС платила дивиденды за всю историю?",
         ),
         [("smartlab_dividend_history", {"ticker": "MTSS"})]),
        ((
            "Оцени дивидендную надёжность Сбера: CAGR выплат, средняя доходность, консистентность.",
            "Насколько Сбер надёжен как дивидендная бумага — темп роста выплат, средняя доходность, стабильность.",
            "Проведи дивидендный анализ SBER.",
         ),
         [("dividend_analysis", {"ticker": "SBER"})]),
        ((
            "Дай финансовую отчётность X5 по МСФО за все доступные годы.",
            "Покажи годовую отчётность X5 в стандарте IFRS за всю историю.",
            "Нужны финансовые показатели X5 по МСФО за много лет.",
         ),
         [("smartlab_company_financials", {"ticker": "X5", "standard": "ifrs"})]),
        ((
            "Сравни выручку и ROE Сбера, ВТБ и Т-Банка по годам.",
            "Дай финотчётность трёх банков сразу: SBER, VTBR, TCSG — интересует выручка и рентабельность капитала.",
            "Сделай батч-выгрузку отчётности SBER, VTBR и TCSG для сравнения динамики.",
         ),
         [("smartlab_company_financials_multi", {"tickers": ["SBER", "VTBR", "TCSG"]})]),
        ((
            "Сделай сводный фундаментальный отчёт по Роснефти.",
            "Дай всё по фундаменталу ROSN: мультипликаторы, дивиденды, рост, скоринги, пиры.",
            "Нужен комплексный разбор Роснефти — фундаментал одним отчётом.",
         ),
         [("company_fundamental_report", {"ticker": "ROSN"})]),
        ((
            "Посчитай Piotroski F-Score и Altman Z-Score для Фармсинтеза.",
            "Оцени PHOR по скорингам: качество фундаментала (F-Score) и риск банкротства (Z-Score).",
            "Прогони Фармсинтез через Piotroski и Altman — оба скоринга.",
         ),
         [("stock_f_score", {"ticker": "PHOR"}), ("stock_z_score", {"ticker": "PHOR"})]),
        ((
            "Что за фонд TMOS: бенчмарк, категория, есть ли премия к NAV?",
            "Расскажи про БПИФ TMOS — на что ориентирован, к какому классу относится, торгуется ли дороже расчётной цены.",
            "Дай карточку фонда TMOS.",
         ),
         [("etf_fund_info", {"query": "TMOS"})]),
        ((
            "Насколько SBMX отстаёт от своего индекса за последние 3 месяца?",
            "Посчитай трекинг-ошибку SBMX к бенчмарку за квартал.",
            "Хорошо ли SBMX повторяет индекс за 90 дней?",
         ),
         [("etf_tracking_error", {"query": "SBMX", "days": 90})]),
        ((
            "Покажи дневные свечи Тинькофф за сентябрь.",
            "Дай OHLCV по бумаге T за сентябрь, дневной таймфрейм.",
            "Мне нужна история свечей Т-Банка за последний месяц, дневка.",
         ),
         [("moex_candles", {"query": "T", "frm": "2026-09-01", "till": "2026-09-30", "interval": "24"})]),
        ((
            "С какими бумагами и индексами коррелирует Яндекс, и какая бета к рынку?",
            "Оцени взаимосвязи YDEX с другими активами и чувствительность к индексу.",
            "Дай корреляции и бету для Яндекса.",
         ),
         [("moex_correlations", {"secid": "YDEX"})]),
        ((
            "Какой кредитный рейтинг у Атомэнергопрома по Эксперт РА?",
            "Проверь рейтинг Атомэнергопрома в Эксперт РА.",
            "На что Атомэнергопром оценён агентством Эксперт РА?",
         ),
         [("raexpert_rating", {"query": "Атомэнергопром"})]),
        ((
            "Сколько сейчас НКД по ОФЗ 26253?",
            "Какой накопленный купонный доход у выпуска 26253 на сегодня?",
            "Посчитай текущий НКД для ОФЗ 26253.",
         ),
         [("bond_accrued_interest", {"query": "26253"})]),
        ((
            "Если купить ОФЗ 26238 и держать 3 года с реинвестированием купонов, какая будет эффективная доходность?",
            "Посчитай синтетическую доходность 26238 на горизонте 3 лет с реинвестом по YTM.",
            "Сколько даст выпуск 26238, если три года реинвестировать купоны?",
         ),
         [("bond_synthetic_yield", {"query": "26238", "horizon_years": 3})]),
        ((
            "Как менялась кривая бескупонной доходности ОФЗ за лето?",
            "Дай историю КБД с июня по август.",
            "Покажи динамику G-кривой за летние месяцы.",
         ),
         [("moex_zcyc_history", {"frm": "2026-06-01", "till": "2026-08-31"})]),
        ((
            "Быстрый разбор: какой купон у облигации ГТЛК RU000A10C6F7 и когда следующий платёж?",
            "Покажи расписание купонов по RU000A10C6F7.",
            "Когда ГТЛК по бонду RU000A10C6F7 платит следующий купон и в каком размере?",
         ),
         [("moex_bond_coupons", {"query": "RU000A10C6F7"})]),
        ((
            "Дай текущую котировку и историю закрытий Северстали за последние 3 месяца.",
            "Сколько стоит CHMF сейчас и как закрывалась за квартал?",
            "Сначала котировка Северстали, потом дневные закрытия за 90 дней.",
         ),
         [("moex_quote", {"query": "CHMF"}), ("moex_history", {"query": "CHMF", "frm": "2026-07-01", "till": "2026-09-30"})]),
        ((
            "Был ли сплит у ВТБ и когда?",
            "Проверь историю дроблений ВТБ.",
            "Делили ли акции VTB, и если да — в какую дату?",
         ),
         [("moex_splits", {"secid": "VTB"})]),
        ((
            "Оцени ликвидность и волатильность Феесаллера — могу ли выйти из позиции без потерь?",
            "Прогони FEES по ликвидности и риску: обороты, спред, волатильность.",
            "Насколько FEES торгуема и насколько рискованна?",
         ),
         [("liquidity_assessment", {"query": "FEES", "days": 90}),
          ("price_volatility", {"query": "FEES", "days": 90})]),
        ((
            "Дай полную историю торгов Северстали за сентябрь со всеми полями ISS.",
            "Нужен полный дневной архив сделок CHMF за месяц — все доступные колонки.",
            "Выгрузи расширенную историю CHMF за сентябрь (full history).",
         ),
         [("moex_full_history", {"query": "CHMF", "frm": "2026-09-01", "till": "2026-09-30"})]),
        ((
            "Какие были агрегированные итоги торгов по Сберу 15 сентября?",
            "Дай сводку по бумаге SBER за дату 2026-09-15: объёмы, диапазон.",
            "Покажи агрегаты дня по Сбербанку на 15.09.2026.",
         ),
         [("moex_aggregates", {"query": "SBER", "date": "2026-09-15"})]),
        ((
            "Как росли выручка и прибыль Роснефти за последние годы?",
            "Оцени динамику ROSN: рост выручки, EBITDA, прибыли, тренды ROE и маржинальности.",
            "Проведи анализ роста для Роснефти.",
         ),
         [("stock_growth_analysis", {"ticker": "ROSN"})]),
        ((
            "Коинтегрированы ли Сбербанк и Газпром? Посчитай тест на годе истории.",
            "Проверь пару SBER–GAZP на коинтеграцию: hedge ratio и half-life.",
            "Есть ли долгосрочная связь между SBER и GAZP — прогони коинтеграционный тест за 252 дня.",
         ),
         [("cointegration_test", {"ticker1": "SBER", "ticker2": "GAZP", "days": 252})]),
        ((
            "Найди коинтегрированные пары для Сбера среди GAZP, LKOH и MOEX.",
            "Просканируй кандидатов GAZP, LKOH, MOEX на коинтеграцию с SBER.",
            "Кто из GAZP/LKOH/MOEX коинтегрирован со Сбером? Прогони скан.",
         ),
         [("cointegration_scan", {"ticker": "SBER", "candidates": "GAZP,LKOH,MOEX", "days": 252})]),
        ((
            "Построй матрицу коинтеграции для SBER, GAZP, LKOH и MOEX.",
            "Попарно протестируй SBER, GAZP, LKOH, MOEX на коинтеграцию.",
            "Мне нужна сводка по всем парам четвёрки SBER/GAZP/LKOH/MOEX — кто с кем коинтегрирован.",
         ),
         [("cointegration_matrix", {"tickers": "SBER,GAZP,LKOH,MOEX", "days": 252})]),
    ],
    "constructor": [
        ((
            "Вот мой портфель:\n\n" + ASSETS + "\n\nСделай полный снимок: веса, P&L, дюрацию, денежный поток и реальную доходность.",
            "Разбери мой портфель по полной:\n\n" + ASSETS + "\n\nНужны веса, прибыль/убыток, дюрация, поток денег и доходность за вычетом инфляции.",
            "Посчитай всё по составу:\n\n" + ASSETS + "\n\n— доли позиций, финрезультат, дюрацию, будущие поступления, реальную доходность.",
         ),
         [("portfolio_snapshot", {"assets": ASSETS})]),
        ((
            "Для портфеля:\n\n" + ASSETS + "\n\nчто будет, если доходности облигаций упадут на 2 п.п.?",
            "Оцени, как изменится стоимость облигационной части при снижении доходностей на 2 процентных пункта:\n\n" + ASSETS,
            "Смоделируй сдвиг доходностей бондов на -2 п.п. для портфеля:\n\n" + ASSETS,
         ),
         [("portfolio_rate_whatif", {"delta_pp": -2, "assets": ASSETS})]),
        ((
            "Составь календарь поступлений на полгода вперёд для портфеля:\n\n" + ASSETS,
            "Когда и сколько денег придёт в портфель:\n\n" + ASSETS + "\n\n— купоны и дивиденды по датам.",
            "Построй график входящих платежей (купоны/дивиденды) для:\n\n" + ASSETS,
         ),
         [("portfolio_income_calendar", {"assets": ASSETS})]),
        ((
            "Кто в портфеле\n\n" + ASSETS + "\n\nсегодня вырос и кто просел?",
            "Покажи дневных лидеров и аутсайдеров в:\n\n" + ASSETS,
            "Какие позиции в\n\n" + ASSETS + "\n\nсегодня двинулись вверх, а какие вниз?",
         ),
         [("portfolio_movers", {"assets": ASSETS})]),
        ((
            "Проверь кредитный рейтинг ГТЛК перед включением его облигаций в портфель.",
            "Смотрю на бонды ГТЛК для портфеля — какой у них рейтинг Эксперт РА?",
            "Насколько надёжен эмитент ГТЛК по оценке Эксперт РА?",
         ),
         [("raexpert_rating", {"query": "ГТЛК"})]),
        ((
            "Подбери надёжные корпоративные облигации для консервативной части портфеля: YTM от 14 до 17%, рейтинг от ruAA, без офёрты.",
            "Нужны бумаги под консервативную подушку: корпораты с доходностью 14–17%, кредитным рейтингом ruAA и выше, без оферты.",
            "Отбери качественные корпоративные бонды: YTM 14–17%, рейтинг минимум ruAA, колл-опциона у эмитента нет.",
         ),
         [("bond_screener", {"ytm_min": 14, "ytm_max": 17, "rating_min": "ruAA", "has_offer": False, "currency": "SUR", "sort_by": "ytm"})]),
        ((
            "Рассматриваю TMOS как акционную часть портфеля. Расскажи о фонде.",
            "Думаю взять TMOS под долю акций в портфеле — что за фонд?",
            "Собираю портфель и присматриваюсь к TMOS: дай информацию о нём.",
         ),
         [("etf_fund_info", {"query": "TMOS"})]),
        ((
            "Какая сейчас доходность кривой ОФЗ на срок 4.5 года? Хочу сравнить с корпоративной бумагой похожей дюрации.",
            "Дай точку G-кривой на 4.5 года.",
            "Сколько даёт бескупонная кривая на горизонте 4.5 года?",
         ),
         [("curve_yield", {"years": 4.5})]),
        ((
            "Дай котировки кандидатов: SBER, TMOS и ОФЗ 26249 — нужно для сборки портфеля.",
            "Посмотри цены по трём кандидатам в портфель: Сбербанк, фонд TMOS и выпуск 26249.",
            "Сколько стоят SBER, TMOS и ОФЗ 26249 — выбираю, что включить.",
         ),
         [("moex_quote", {"query": "SBER"}), ("moex_quote", {"query": "TMOS"}), ("moex_bond", {"query": "26249"})]),
        ((
            "Собираю портфель: сначала сделай снимок текущего, затем оцени чувствительность к росту ставки на 1 п.п.\n\n" + ASSETS,
            "Двухшаговая задача: снимок портфеля, потом what-if при сдвиге доходностей +1 п.п.\n\n" + ASSETS,
            "Обнови картину по портфелю и сразу посчитай эффект роста доходностей на 1 процентный пункт:\n\n" + ASSETS,
         ),
         [("portfolio_snapshot", {"assets": ASSETS}),
          ("portfolio_rate_whatif", {"delta_pp": 1, "assets": ASSETS})]),
    ],
    "timer": [
        ((
            "Стоит ли сейчас входить в Сбера? Дай полную техническую картину.",
            "Ищу точку входа по SBER — покажи все индикаторы.",
            "Оцени Сбербанк технически: могу ли покупать?",
         ),
         [("technical_indicators", {"query": "SBER", "days": 180})]),
        ((
            "Проверь ЛУКОЙЛ на свечные паттерны за полгода — ищу точку входа.",
            "Есть ли по LKOH разворотные или продолжающие свечные сигналы за 180 дней?",
            "Просканируй свечи ЛУКОЙЛа за полгода на предмет паттернов.",
         ),
         [("candlestick_patterns", {"query": "LKOH", "days": 180})]),
        ((
            "Какая волатильность и просадки у Газпрома за квартал? Оцениваю риск входа.",
            "Покажи рисковость GAZP за 90 дней: вола, Шарп, макс drawdown.",
            "Насколько качает Газпром за последние три месяца?",
         ),
         [("price_volatility", {"query": "GAZP", "days": 90})]),
        ((
            "Что сейчас по базису фьючерса на Si — contango или backwardation?",
            "В какую сторону искривлён календарь по Si: контанго или бэквордация?",
            "Какая ставка переноса у фьючерса на доллар Si?",
         ),
         [("moex_futures_basis", {"asset_code": "Si"})]),
        ((
            "Какие фьючерсы на нефть Brent сейчас торгуются и у какого лучшие объёмы?",
            "Покажи действующие контракты по Brent с рыночными данными.",
            "Дай каталог фьючерсов BR.",
         ),
         [("moex_futures_list", {"asset_code": "BR"})]),
        ((
            "Покажи опционную доску по Сберу: где центральный страйк и какая IV?",
            "Дай доску опционов на фьючерс SBRF — страйки, волатильность, открытый интерес.",
            "Мне нужна опционная таблица по Сберу.",
         ),
         [("moex_options_board", {"asset": "SBRF"})]),
        ((
            "Найди серии опционов на Si и покажи доску для ближайшей месячной серии.",
            "Какие экспирации есть по опционам Si?",
            "Дай календарь серий опционов на доллар Si.",
         ),
         [("option_calc_series", {"asset_code": "Si"})]),
        ((
            "Хочу видеть доску опционов Si серии SI-9.26M100926XA — по 5 страйков от центрального.",
            "Дай 5 страйков вокруг центра для серии SI-9.26M100926XA по Si.",
            "Опционная доска серии SI-9.26M100926XA (Si), окно 5 страйков.",
         ),
         [("option_calc_optionboard", {"asset_code": "Si", "optionseries_code": "SI-9.26M100926XA", "rows": 5})]),
        ((
            "Построй smile волатильности для серии опционов на Si SI-9.26M100926XA.",
            "Нужна кривая IV по страйкам серии SI-9.26M100926XA (Si).",
            "Покажи улыбку волатильности опционов Si серии SI-9.26M100926XA.",
         ),
         [("option_calc_volatility_graph", {"asset_code": "Si", "optionseries_code": "SI-9.26M100926XA"})]),
        ((
            "Покажи дневные свечи ВТБ за последние 2 месяца — оцениваю пробой уровня.",
            "Дай свечи VTBR за август-сентябрь, дневка.",
            "Мне нужна OHLCV-история ВТБ за два месяца.",
         ),
         [("moex_candles", {"query": "VTBR", "frm": "2026-08-01", "till": "2026-09-30", "interval": "24"})]),
        ((
            "Что с котировкой опциона Si87000BI6A — премия, спред, открытый интерес?",
            "Сколько стоит опцион Si87000BI6A и каков его спред?",
            "Дай котировку Si87000BI6A.",
         ),
         [("moex_option_quote", {"secid": "Si87000BI6A"})]),
        ((
            "Какие call-опционы на Si со страйком 84000 на экспирацию 17.09.2026, и какова их дельта?",
            "Покажи коллы Si со страйком 84000 с исполнением 2026-09-17.",
            "Отбери опционы call на Si, страйк 84000, экспирация 17.09.2026.",
         ),
         [("option_calc_options", {"asset_code": "Si", "expiration_date": "2026-09-17", "option_type": "call", "strike": 84000})]),
        ((
            "Оцени опцион Si84000BC6A: греки сейчас и what-if, если Si вырастет до 86000 при той же IV.",
            "Посчитай сводку по Si84000BC6A, а затем сценарий с базовым активом 86000.",
            "Дай греки Si84000BC6A и пересчитай при цене Si = 86000.",
         ),
         [("option_calc_option_brief", {"asset_code": "Si", "secid": "Si84000BC6A", "underlying_price": 86000})]),
        ((
            "RSI по Яндексу не перекуплен? Проверь по полной истории индикаторов.",
            "Дай индикаторы YDEX — интересно значение RSI.",
            "Проверь YDEX на перегретость по RSI.",
         ),
         [("technical_indicators", {"query": "YDEX", "days": 180})]),
        ((
            "Какое сегодня число? Хочу правильно задать даты для анализа.",
            "Скажи текущую дату — буду от неё считать периоды.",
            "Уточни сегодняшнюю дату.",
         ),
         [("current_datetime", {})]),
        ((
            "Какие фьючерсы доступны в калькуляторе по активу Si?",
            "Дай список фьючерсных контрактов Si для опционного калькулятора.",
            "Покажи фьючерсы базового актива Si.",
         ),
         [("option_calc_futures", {"asset_code": "Si"})]),
        ((
            "Какие опционы входят в серию SI-9.26M100926XA?",
            "Дай список опционов серии SI-9.26M100926XA по Si.",
            "Покажи содержимое опционной серии SI-9.26M100926XA.",
         ),
         [("option_calc_series_options", {"asset_code": "Si", "optionseries_code": "SI-9.26M100926XA"})]),
    ],
    "macrotracker": [
        ((
            "Как менялась ключевая ставка ЦБ за последний год?",
            "Дай динамику ключевой ставки за 12 месяцев.",
            "Покажи историю ключевой ставки ЦБ за год.",
         ),
         [("cbr_key_rate", {"tail": 12})]),
        ((
            "Дай инфляцию г/г и ключевую ставку помесячно за год.",
            "Покажи CPI и ставку ЦБ по месяцам за 12 месяцев.",
            "Мне нужна помесячная динамика инфляции и ключевой ставки за год.",
         ),
         [("cbr_inflation", {"tail": 12})]),
        ((
            "Что сейчас с RUONIA и как она соотносится с ключевой?",
            "Дай текущую ставку RUONIA.",
            "Покажи последние значения RUONIA overnight.",
         ),
         [("cbr_ruonia", {"tail": 5})]),
        ((
            "Покажи RUONIA-индекс и срочные средние RUONIA за месяц/квартал.",
            "Дай индекс RUONIA и средние RUONIA_AVG_1M/3M/6M.",
            "Мне нужны срочные средние по RUONIA и сам индекс.",
         ),
         [("cbr_ruonia_index", {"tail": 5})]),
        ((
            "Какие сейчас ставки MIACR на межбанке по разным срокам?",
            "Покажи фактические ставки межбанковского рынка MIACR.",
            "Дай текущие MIACR по срокам D1, D7 и дальше.",
         ),
         [("cbr_ibor", {"tail": 5})]),
        ((
            "Что рынок закладывает в ключевую ставку на ближайший год по кривой ОФЗ?",
            "Оцени рыночные ожидания по ставке из КБД.",
            "Какие спреды и форварды показывает кривая ОФЗ относительно ключевой?",
         ),
         [("rate_expectations", {})]),
        ((
            "Проверь ожидания по ставке, если ключевая сейчас 14.5%.",
            "Посчитай рыночные ожидания при ключевой ставке 14.5%.",
            "Прогони rate_expectations с фиксированной ключевой 14.5%.",
         ),
         [("rate_expectations", {"key_rate": 14.5})]),
        ((
            "Какая доходность на кривой ОФЗ для срока 5.5 года?",
            "Дай значение G-кривой на 5.5 года.",
            "Сколько даёт бескупонная кривая на сроке 5.5 лет?",
         ),
         [("curve_yield", {"years": 5.5})]),
        ((
            "Дай доходность G-кривой на 30 лет — за пределами стандартных узлов.",
            "Какая ставка на кривой ОФЗ для 30-летнего срока?",
            "Экстраполируй кривую ОФЗ на 30 лет.",
         ),
         [("curve_yield", {"years": 30})]),
        ((
            "Динамика юаня к рублю с начала года по данным ЦБ.",
            "Покажи курс CNY с 1 января по данным ЦБ РФ.",
            "Как менялся официальный курс юаня в этом году?",
         ),
         [("cbr_currency", {"symbol": "CNY", "first_date": "2026-01-01", "last_date": "2026-09-30"})]),
        ((
            "Учётные цены ЦБ на золото за последнюю неделю.",
            "Покажи официальные цены на драгметаллы за 7 дней.",
            "Сколько стоило золото по учётным ценам ЦБ на прошлой неделе?",
         ),
         [("cbr_metals", {"tail": 7})]),
        ((
            "Как менялись международные резервы РФ в этом квартале?",
            "Дай динамику золотовалютных резервов за последние месяцы.",
            "Покажи ЗВР России за квартал.",
         ),
         [("cbr_reserves", {"tail": 3})]),
        ((
            "История кривой КБД за сентябрь — хочу видеть тренд коротких и длинных доходностей.",
            "Покажи параметры КБД по дням за сентябрь.",
            "Дай дневную историю бескупонной кривой за текущий месяц.",
         ),
         [("moex_zcyc_history", {"frm": "2026-09-01", "till": "2026-09-30"})]),
        ((
            "Какие сейчас индикативные курсы на валютном срочном рынке?",
            "Покажи indicative rates срочной секции MOEX.",
            "Дай индикативные курсы FORTS.",
         ),
         [("moex_indicative_rates", {})]),
        ((
            "Какая капитализация всего фондового рынка MOEX сейчас?",
            "Сколько стоит российский фондовый рынок в совокупности?",
            "Дай совокупную капитализацию по данным МосБиржи.",
         ),
         [("moex_market_capitalization", {})]),
        ((
            "Как выглядит рынок облигаций в целом: доходности по сегментам ОФЗ, корпораты, муниципалы?",
            "Дай агрегированные показатели рынка бондов по типам выпусков.",
            "Покажи сводные метрики по сегментам облигационного рынка.",
         ),
         [("moex_bond_market_aggregates", {})]),
        ((
            "Оцени монетарный фон: ключевая ставка, инфляция и рыночные ожидания разом.",
            "Собери макрокартину: ставка ЦБ, CPI и что заложено в кривой.",
            "Мне нужен срез монетарной политики: ключевая, инфляция, ожидания рынка.",
         ),
         [("cbr_key_rate", {"tail": 1}), ("cbr_inflation", {"tail": 3}), ("rate_expectations", {})]),
    ],
    "risk_manager": [
        ((
            "Оцени риск портфеля при росте доходностей облигаций на 2 п.п.:\n\n" + ASSETS,
            "Что будет с моим портфелем, если доходности бондов подскочат на 2 процентных пункта:\n\n" + ASSETS,
            "Стресс-сценарий: сдвиг доходностей +2 п.п. по портфелю ниже:\n\n" + ASSETS,
         ),
         [("portfolio_rate_whatif", {"delta_pp": 2, "assets": ASSETS})]),
        ((
            "Полная оценка рисков портфеля: дюрация, ставка, концентрация, денежный поток.\n\n" + ASSETS,
            "Прогони риск-аудит портфеля:\n\n" + ASSETS + "\n\n— все ключевые метрики.",
            "Дай целостную картину по составу:\n\n" + ASSETS + "\n\n— с точки зрения рисков.",
         ),
         [("portfolio_snapshot", {"assets": ASSETS})]),
        ((
            "Волатильность Сбера за 90 дней с безрисковой ставкой 14% — оцени VaR-риск позиции.",
            "Посчитай риск-метрики SBER за квартал при безриске 14%.",
            "Насколько рискован Сбер за последние 90 дней (rf = 14%)?",
         ),
         [("price_volatility", {"query": "SBER", "days": 90, "rf_annual": 14.0})]),
        ((
            "Насколько ликвидна бумага FEES — смогу ли выйти из позиции при просадке?",
            "Оцени торгуемость FEES: обороты, спреды, скор ликвидности.",
            "Проверь FEES на ликвидность.",
         ),
         [("liquidity_assessment", {"query": "FEES", "days": 90})]),
        ((
            "Не перекуплен ли Яндекс по RSI? Оцениваю риск разворота.",
            "Есть ли у YDEX признаки перегрева по индикаторам?",
            "Проверь риск-индикаторы Яндекса за квартал.",
         ),
         [("technical_indicators", {"query": "YDEX", "days": 90})]),
        ((
            "Какой процентный риск у ОФЗ 26238: дюрация, конвексность, безубыток?",
            "Оцени чувствительность выпуска 26238 к движению ставок.",
            "Разбери 26238 с точки зрения процентного риска.",
         ),
         [("bond_report", {"query": "26238"})]),
        ((
            "Реальная доходность портфеля с учётом фактической инфляции — сильно ли её съедает?\n\n" + ASSETS,
            "Сравни доходность портфеля ниже с текущей инфляцией:\n\n" + ASSETS,
            "Насколько инфляция обесценивает мой портфель:\n\n" + ASSETS,
         ),
         [("portfolio_snapshot", {"assets": ASSETS}), ("cbr_inflation", {"tail": 1})]),
        ((
            "Сколько нужно ГО под позицию: 5 фьючерсов SiU6 и продажа 10 опционов Si84000BI6?",
            "Посчитай гарантийное обеспечение для комбинации: лонг 5 SiU6, шорт 10 Si84000BI6.",
            "Какой маржинальный буфер нужен под позицию из 5 фьючерсов SiU6 и -10 опционов Si84000BI6?",
         ),
         [("option_calc_initial_margin", {"positions": [{"secid": "SiU6", "type": "futures", "quantity": 5}, {"secid": "Si84000BI6", "type": "option", "quantity": -10}]})]),
        ((
            "Что делают юрлица на фьючерсах Si — растут ли длинные позиции при падении цены?",
            "Покажи открытый интерес по Si в разрезе юр/физлиц.",
            "Кто набирает позиции на фьючерсах Si — банки или физики?",
         ),
         [("moex_futures_open_interest", {"asset": "Si"})]),
        ((
            "Рынок закладывает снижение ключевой ставки? Проверь по кривой ОФЗ.",
            "Есть ли в КБД сигналы о будущих снижениях ставки?",
            "Что кривая ОФЗ говорит о направлении денежно-кредитной политики?",
         ),
         [("rate_expectations", {})]),
        ((
            "Насколько Т-Банк коррелирует с индексом — высок ли системный риск позиции?",
            "Оцени системную составляющую в Т-Банке: корреляции и бета.",
            "Проверь связь T с рынком.",
         ),
         [("moex_correlations", {"secid": "T"})]),
        ((
            "Сравни крупные банки по устойчивости: NIM, NPL, ликвидность — SBER, VTBR, TCSG.",
            "Дай бенчмарк по трём банкам: маржа, просрочки, фондирование.",
            "Оцени качество SBER, VTBR и TCSG по банковским метрикам.",
         ),
         [("bank_benchmark", {"tickers": ["SBER", "VTBR", "TCSG"]})]),
        ((
            "Посчитай альфу и бету моего портфеля к индексу МосБиржи за год.\n\n" + ASSETS,
            "Сколько доходности портфель получает от рынка (бета), а сколько — от управления (альфа)? Бенчмарк IMOEX, горизонт 252 дня:\n\n" + ASSETS,
            "Оцени системную и «навыковую» составляющую доходности портфеля к IMOEX:\n\n" + ASSETS,
         ),
         [("portfolio_alpha_beta", {"assets": ASSETS, "benchmark": "IMOEX", "days": 252})]),
        ((
            "Стресс-тест: снимок портфеля и эффект -1.5 п.п. по облигациям.\n\n" + ASSETS,
            "Сначала полный снимок, потом сценарий роста доходностей на 1.5 п.п.:\n\n" + ASSETS,
            "Двухшаговый стресс: обнови портфель и посчитай реакцию на -1.5 п.п.:\n\n" + ASSETS,
         ),
         [("portfolio_snapshot", {"assets": ASSETS}),
          ("portfolio_rate_whatif", {"delta_pp": -1.5, "assets": ASSETS})]),
    ],
    "instrument_specialist": [
        ((
            "Есть ли сейчас премия или дисконт у TMOS к iNAV?",
            "Насколько TMOS отклоняется от расчётной цены?",
            "Покажи premium/discount фонда TMOS.",
         ),
         [("etf_premium_discount", {"query": "TMOS"})]),
        ((
            "Как TGLD отслеживает золото? Трекинг-ошибка за полгода.",
            "Насколько точно TGLD повторяет цену золота за 180 дней?",
            "Посчитай отклонение TGLD от бенчмарка за полгода.",
         ),
         [("etf_tracking_error", {"query": "TGLD", "days": 180})]),
        ((
            "Расскажи о фонде SBMX: бенчмарк, категория, премия/дисконт.",
            "Дай карточку БПИФ SBMX.",
            "Что за фонд SBMX — на что смотрит и как торгуется к NAV?",
         ),
         [("etf_fund_info", {"query": "SBMX"})]),
        ((
            "Покажи фонды денежного рынка, отсортированные по объёму.",
            "Дай БПИФы money market с сортировкой по среднедневному обороту.",
            "Отбери фонды на денежный рынок, лучшие по ликвидности.",
         ),
         [("etf_screener", {"category": "money_market", "sort_by": "volume", "sort_desc": True})]),
        ((
            "Какие фьючерсы на Brent есть и когда ближайшая экспирация?",
            "Дай календарь серий по нефти Brent.",
            "Покажи серии контрактов BR с датами исполнения.",
         ),
         [("moex_futures_series", {"asset": "BR"})]),
        ((
            "Сравни фьючерсы RTS по открытому интересу и ГО.",
            "Дай список контрактов на индекс РТС со спеками.",
            "Покажи фьючерсы RTS с рыночными данными и спецификацией.",
         ),
         [("moex_futures_list", {"asset_code": "RTS"})]),
        ((
            "Какой carry у фьючерса на Газпром — contango или backwardation?",
            "Посчитай ставку переноса по GAZP.",
            "В какой фазе базис фьючерса GAZP?",
         ),
         [("moex_futures_basis", {"asset_code": "GAZP"})]),
        ((
            "Опционная доска по GAZR: страйки, IV, открытый интерес.",
            "Дай таблицу опционов на фьючерс Газпрома.",
            "Покажи опционы по активу GAZR.",
         ),
         [("moex_options_board", {"asset": "GAZR"})]),
        ((
            "Какой спред bid/offer у опциона GZ85CU6A?",
            "Покажи стакан опциона GZ85CU6A — лучшие цены покупки и продажи.",
            "Дай лучшие bid и ask по GZ85CU6A.",
         ),
         [("moex_option_orderbook", {"secid": "GZ85CU6A"})]),
        ((
            "История сделок по опциону GZ85CU6A за август-сентябрь.",
            "Покажи все сделки по GZ85CU6A с начала августа.",
            "Дай трейды опциона GZ85CU6A за два месяца.",
         ),
         [("moex_option_history", {"secid": "GZ85CU6A", "frm": "2026-08-01", "till": "2026-09-30"})]),
        ((
            "Рассчитай Greeks опциона Si84000BC6A при условии, что Si стоит 86000, а IV 24%.",
            "Дай сводку по Si84000BC6A с кастомной ценой базового 86000 и волатильностью 24%.",
            "Пересчитай греки Si84000BC6A для сценария: Si = 86000, IV = 24%.",
         ),
         [("option_calc_option_brief", {"asset_code": "Si", "secid": "Si84000BC6A", "underlying_price": 86000, "volatility": 24.0})]),
        ((
            "Построй график P&L стратегии: продажа 10 call Si84000BC6A и покупка 5 фьючерсов SIU6.",
            "Покажи профиль прибыли/убытка комбинации -10 Si84000BC6A и +5 SIU6 по цене БА.",
            "Начерти кривую P&L для: шорт 10 опционов Si84000BC6A, лонг 5 фьючерсов SIU6.",
         ),
         [("option_calc_portfolio_graph", {"asset_code": "Si", "positions": [{"secid": "Si84000BC6A", "type": "option", "quantity": -10}, {"secid": "SIU6", "type": "futures", "quantity": 5}], "indicator": "profit_and_loss"})]),
        ((
            "Был ли сплит у ВТБ?",
            "Проверь историю дробления акций VTB.",
            "Делил ли ВТБ свои акции когда-нибудь?",
         ),
         [("moex_splits", {"secid": "VTB"})]),
        ((
            "Какие базовые активы доступны в опционном калькуляторе в секторе индексов?",
            "Покажи перечень БА опционов типа index.",
            "Дай список индексных активов калькулятора.",
         ),
         [("option_calc_assets", {"asset_type": "index"})]),
        ((
            "Покажи недельные свечи TMOS за квартал и трекинг-ошибку за 3 месяца.",
            "Дай недельную OHLCV по TMOS и параллельно отклонение от бенчмарка за 90 дней.",
            "Свечи TMOS в недельном масштабе плюс трекинг-ошибка за квартал.",
         ),
         [("moex_candles", {"query": "TMOS", "frm": "2026-07-01", "till": "2026-09-30", "interval": "7"}),
          ("etf_tracking_error", {"query": "TMOS", "days": 90})]),
        ((
            "Какие базисные активы опционов сейчас есть на FORTS?",
            "Покажи список активов опционной секции FORTS.",
            "Дай перечень underlying опционов срочного рынка.",
         ),
         [("moex_options_assets", {})]),
        ((
            "Какая статистика комиссий срочного рынка FORTS в агрегате?",
            "Покажи сборы FORTS суммарно.",
            "Дай агрегированную статистику комиссий FORTS.",
         ),
         [("moex_futures_promo", {})]),
        ((
            "Что за новости на сайте Московской биржи? Покажи последние.",
            "Дай свежие объявления MOEX.",
            "Покажи ленту новостей МосБиржи.",
         ),
         [("moex_sitenews", {"limit": 10})]),
        ((
            "Опиши серию опционов SI-9.26M100926XA — что это за цикл?",
            "Дай детали серии SI-9.26M100926XA по Si.",
            "Расскажи о серии опционов SI-9.26M100926XA.",
         ),
         [("option_calc_series_detail", {"asset_code": "Si", "optionseries_code": "SI-9.26M100926XA"})]),
        ((
            "Посчитай агрегированные греки и ГО портфеля: лонг 3 опциона Si84000BC6A и шорт 2 фьючерса SIU6.",
            "Собери расчёт по комбинации: +3 Si84000BC6A, -2 SIU6 — греки, P&L, маржа.",
            "Оцени опционную позицию из 3 купленных коллов Si84000BC6A и 2 проданных фьючерсов SIU6.",
         ),
         [("option_calc_portfolio", {"asset_code": "Si", "positions": [{"secid": "Si84000BC6A", "type": "option", "quantity": 3}, {"secid": "SIU6", "type": "futures", "quantity": -2}]})]),
        ((
            "Дай детали базового актива Si из опционного калькулятора.",
            "Что за актив Si в калькуляторе опционов — тип и описание?",
            "Покажи карточку underlying Si.",
         ),
         [("option_calc_asset_detail", {"asset_code": "Si"})]),
    ],
    "portfolio_manager": [
        ((
            "Подготовь обзор портфеля клиента для встречи: полный снимок.\n\n" + ASSETS,
            "Мне нужен полный отчёт по составу перед встречей с клиентом:\n\n" + ASSETS,
            "Собери обзорную сводку портфеля:\n\n" + ASSETS + "\n\n— все метрики.",
         ),
         [("portfolio_snapshot", {"assets": ASSETS})]),
        ((
            "Клиент спрашивает, что сегодня двигалось в его портфеле:\n\n" + ASSETS,
            "Покажи клиенту дневных лидеров и отстающих в портфеле:\n\n" + ASSETS,
            "Что изменилось в портфеле за сегодня:\n\n" + ASSETS,
         ),
         [("portfolio_movers", {"assets": ASSETS})]),
        ((
            "Какие купоны и дивиденды придут в портфель ближайшее время?\n\n" + ASSETS,
            "Составь график будущих поступлений клиента:\n\n" + ASSETS,
            "Когда клиент получит деньги по купонам и дивидендам:\n\n" + ASSETS,
         ),
         [("portfolio_income_calendar", {"assets": ASSETS})]),
        ((
            "Если ставка вырастет на 1 п.п. — насколько просадит портфель клиента?\n\n" + ASSETS,
            "Оцени эффект подъёма доходностей на 1 п.п. для портфеля:\n\n" + ASSETS,
            "Смоделируй +1 п.п. по доходностям бондов:\n\n" + ASSETS,
         ),
         [("portfolio_rate_whatif", {"delta_pp": 1, "assets": ASSETS})]),
        ((
            "Когда ближайшие отчёты публичных компаний? Хочу предупредить клиента о событиях.",
            "Покажи календарь отчётностей эмитентов.",
            "Какие IR-даты запланированы у публичных компаний?",
         ),
         [("moex_ir_calendar", {"limit": 10})]),
        ((
            "Текущая цена Сбера для отчёта клиенту.",
            "Сколько стоит SBER прямо сейчас?",
            "Дай свежую котировку Сбербанка.",
         ),
         [("moex_quote", {"query": "SBER"})]),
        ((
            "Клиент держит ОФЗ 26249 — обнови её метрики: YTM, дюрация, купон.",
            "Дай свежие параметры выпуска 26249.",
            "Что сейчас с доходностью и дюрацией ОФЗ 26249?",
         ),
         [("moex_bond", {"query": "26249"})]),
        ((
            "Полный разбор ОФЗ 26249 для клиента: сценарии и спред к кривой.",
            "Подготовь детальный отчёт по 26249.",
            "Клиент просит анализ 26249 — сделай глубокий разбор.",
         ),
         [("bond_report", {"query": "26249"})]),
        ((
            "Какие дивиденды объявлены на ближайший месяц?",
            "Покажи календарь дивидендов на месяц вперёд.",
            "Кто из компаний скоро заплатит дивиденды?",
         ),
         [("smartlab_dividends", {"limit": 10})]),
        ((
            "Сводный фундаментальный отчёт по ЛУКОЙЛу — клиент спрашивает про качество бумаг.",
            "Дай полный фундаментал LKOH.",
            "Оцени ЛУКОЙЛ комплексно: мультипликаторы, дивиденды, скоринги, пиры.",
         ),
         [("company_fundamental_report", {"ticker": "LKOH"})]),
        ((
            "Когда у ЗПИФ Акцент ближайшая выплата и сколько за пай?",
            "Покажи платежи ЗПИФ Акцент.",
            "Есть ли у фонда Акцент предстоящие выплаты?",
         ),
         [("zpif_payments", {"fund_name": "Акцент"})]),
        ((
            "Какие ЗПИФ недвижимости есть в календаре выплат?",
            "Дай список фондов недвижимости с vsezpif.",
            "Покажи все ЗПИФ недвижимости.",
         ),
         [("zpif_funds_list", {})]),
        ((
            "Ключевая ставка сейчас — для контекста отчёта клиенту.",
            "Какая ключевая ставка ЦБ действует сегодня?",
            "Уточни текущую ключевую ставку.",
         ),
         [("cbr_key_rate", {"tail": 1})]),
        ((
            "Обнови цены позиций и покажи, кто из облигаций торгуется дорого к кривой:\n\n" + ASSETS,
            "Сделай свежий снимок портфеля с оценкой спредов к кривой:\n\n" + ASSETS,
            "Актуализируй портфель и выдели бумаги с богатыми ценами:\n\n" + ASSETS,
         ),
         [("portfolio_snapshot", {"assets": ASSETS})]),
    ],
}


def main():
    from collections import defaultdict
    total = 0
    for role, examples in EXAMPLES.items():
        path = os.path.join(OUT, f"{role}.jsonl")
        n = 0
        with open(path, "w", encoding="utf-8") as f:
            for variants, calls in examples:
                tool_defs = [TOOL_MAP[nm] for nm, _ in calls]
                for i, question in enumerate(variants):
                    tool_calls = [
                        {
                            "id": f"call_{n}_{j}",
                            "type": "function",
                            "function": {"name": nm, "arguments": json.dumps(args, ensure_ascii=False)},
                        }
                        for j, (nm, args) in enumerate(calls)
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
                    n += 1
        total += n
        print(f"{path}: {n} examples")

    # coverage check: каждый инструмент — минимум 3 разных запроса пользователя
    per_tool = defaultdict(set)
    for examples in EXAMPLES.values():
        for variants, calls in examples:
            for q in variants:
                for nm, _ in calls:
                    per_tool[nm].add(q)
    bad = sorted(nm for nm, qs in per_tool.items() if len(qs) < 3)
    if bad:
        raise SystemExit(f"ERROR: tools with <3 distinct user queries: {bad}")
    unused = sorted(set(TOOL_MAP) - set(per_tool))
    if unused:
        raise SystemExit(f"ERROR: tools not covered by any example: {unused}")
    print(f"Coverage OK: {len(per_tool)} tools x >=3 distinct queries")
    print(f"Total: {total}")


if __name__ == "__main__":
    main()
