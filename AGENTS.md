# AGENTS.md — для ИИ-агента, подключившего `ru-finance`

Краткая ориентировка для ассистента (Claude и др.), у которого появился этот MCP.
Описания каждой ручки приходят по протоколу автоматически; здесь — **что это за
сервер и как им правильно пользоваться**. Полный справочник: [docs/TOOLS.md](docs/TOOLS.md).

## Что это и чего тут нет

`ru-finance` — поставщик **данных и расчётов** по российскому рынку: котировки и
облигации MOEX, ставки Банка России, ожидания по ставке из G-кривой ОФЗ, готовые
отчёты по портфелю.

Сервер **не** собирает новости, **не** хранит портфели и **не** даёт инвестсоветов.
Сбор новостей/нарратива, привязка к целям конкретного пользователя и финальные выводы —
задача клиента поверх сервера, **вне этого репозитория**.

## Принципы

- **Числа — на сервере, смысл — на тебе.** Объясняй механику стандартной финтеории,
  не выдавай прогнозы как гарантии.
- **Generic.** Ручки `portfolio_*` принимают портфель параметром `assets` (markdown,
  формат — в [docs/TOOLS.md](docs/TOOLS.md)). Сервер ничего о пользователе не хранит —
  данные портфеля берёшь из своего контекста и передаёшь в вызов.

## Единицы и подводные камни

- **Цены — задержка ~15 мин**; в выходные — последняя сессия (поле `price_field`
  показывает, откуда взята цена). Годится для тенденций, не для копеечной точности.
- **Дюрация — в годах**; ставки ЦБ и RUONIA — **в процентах годовых**.
- **`rate_expectations`:** спреды `*_gross` к ставке включают **срочную премию** — это
  не чистое ожидание. Для пути короткой ставки смотри форварды `fwd_*` и якорь
  `short_vs_ruonia_1y`. Метка `read` грубая (деадбенд ±0.25 пп), не финальный вывод.
- **`curve_yield(years)`** — точная NSS-модель MOEX: можно привязывать к дюрации
  конкретной бумаги и экстраполировать за 20 лет.
- **P&L в `portfolio_*` приблизительный** (средняя цена покупки, без купонов/
  дивидендов и налогов) — так и сообщай пользователю.
- **Выплаты ЗПИФ** с vsezpif.ru — б**о**льшая часть данных **оценочная**: даты
  и суммы взяты из последних фактических выплат и периодичности фонда; это не
  гарантированная информация, а прогноз на основе истории. Всегда предупреждай
  пользователя об этом.
- **Кредитные рейтинги** `raexpert_rating` — скрейпинг raexpert.ru (20 последних
  рейтинговых действий по 10 категориям). Покрывает крупных эмитентов, но не
  гарантирует наличие рейтинга для любого тикера. Рейтинг недоступен для ОФЗ
  (они не нуждаются в кредитном рейтинге — суверенные). Кэш 4 ч.
- **`raexpert_emitent_ratings(rating_min, sector)`** — эмитенты облигаций с
  фильтрацией по рейтингу и отрасли. `rating_min` — минимальный рейтинг (напр.
  `"ruBBB-"` для investment grade). `sector` — отрасль из отраслевых индексов
  МосБиржи (10 секторов, ~100 эмитентов). Кэш 4 ч.
- **`futures_basis(asset_code)`** — contango/backwardation: сравнивает цену
  фьючерса (settle) со спот-ценой базового актива и вычисляет ставку переноса
  годовых (`annualized_return_pct`). Положительная → contango (futures дороже
  spot — можно «продать фьючерс + купить spot», дождаться конвергенции).
  Отрицательная → backwardation (futures дешевле spot — можно «купить фьючерс +
  продать spot»). Поле `basis_pct` — процентная разница до экспирации. Спот
  резолвится автоматически: FX → курс ЦБ (USD, EUR, CNY...), акции → MOEX quote,
  индексы → MOEX index, товары → CBR/MOEX. Если спот не найден, возвращает `error`.

## Типовая цепочка

1. `portfolio_snapshot(assets)` — веса, P&L, дюрация, денежный поток, дивидендная
   доходность акций, спред облигаций к G-кривой, реальная доходность портфеля.
2. `bond_screener(...)` — предварительный отбор облигаций по YTM, дюрации, цене,
   купону, типу, офёрте, амортизации, валюте, объёму, кредитному рейтингу, сектору;
   `rate_expectations()` + `bond_report(<секьюрити>)` — режим ставки, сценарии по
   ключевым облигациям (параллельные + twist), спред к кривой, конвексность, GRY,
   НКД; `portfolio_rate_whatif(±Δ, assets)` — эффект на портфель.
3. `price_volatility(<тикер>, days=90)` — волатильность, Sharpe, max drawdown для
   отдельных позиций (акции/фонды).
4. `liquidity_assessment(<тикер>, days=90)` — единная ликвидность: оборот, Amihud
   illiquidity ratio, спред (OHLC + bid/ask), скор 0-10 grade A–E.
5. **Технический анализ:** `technical_indicators(<тикер>, days)` — полный набор TA
   из дневных OHLCV-свечей: RSI(14), Stochastic %K/%D, ADX+DI, MACD(12,26,9),
   Bollinger Bands(20,2), ATR(14), OBV, VWAP, CCI(20), Williams %R(14),
   Ichimoku(9,26,52), Parabolic SAR, Pivot Points, Fibonacci, Momentum/ROC(10),
   Chaikin Money Flow(20), EMA(12/26), SMA(50/200), MA golden/death cross.
6. **Свечной анализ:** `candlestick_patterns(<тикер>, days)` — 14 классических
   паттернов: doji (4 варианта: standard, dragonfly, gravestone, long_legged),
   hammer/hanging_man, shooting_star, bullish/bearish_marubozu, engulfing,
   harami, piercing_line, dark_cloud_cover, tweezer_top/bottom, morning/evening_star,
   three_white_soldiers, three_black_crows. Каждый паттерн: signal, strength (1-3),
   bars_back, позиция в периоде (high/low context). Сводный signal.
6. **ETF/БПИФ:** `etf_fund_info(<тикер>)` — тип фонда, бенчмарк, iNAV, премия/дисконт;
   `etf_tracking_error(<тикер>, days)` — трекинг-ошибка к индексу;
   `etf_screener(...)` — скринер фондов: фильтрация по классу активов, эмитенту,
   бенчмарку, доходности, волатильности, Sharpe, RSI, MA/MACD/ADX, Beta,
   Stochastic, CCI, Williams %R, Ichimoku, PSAR, CMF, ROC.
7. Свяжи числа с механикой (дюрация × сдвиг доходности → эффект на тело, конвексность
   при больших сдвигах) и с тем контекстом/целями пользователя, что есть у тебя на
   стороне клиента.

## Группы и роли (tool groups & roles)

95 инструментов разбиты на **14 групп** (без дубликатов). **Роль** = набор
групп, подключаемых агенту. В HTTP-режиме доступны три уровня:

| Тип | URL | Пример |
|-----|-----|--------|
| Все | `/mcp` | все инструменты |
| Группа | `/g/{group}/mcp` | `/g/screening/mcp`, `/g/fixed_income/mcp` |
| Роль | `/{role}/mcp` | `/screener/mcp`, `/analyst/mcp` |

### Группы инструментов

| Группа | Инстр. | Состав |
|--------|-------:|--------|
| core_lookup | 5 | `current_datetime`, `moex_resolve`, `moex_search`, `moex_quote`, `moex_bond` |
| price_history | 4 | `moex_candles`, `moex_history`, `moex_full_history`, `moex_aggregates` |
| market_data | 2 | `moex_turnovers`, `moex_indicative_rates` |
| fundamental | 17 | `moex_company_info`, `moex_company_info_by_id`, `moex_market_capitalization`, `moex_ir_calendar`, `moex_sitenews`, `smartlab_dividends`, `smartlab_dividend_history`, `smartlab_company_financials`, `smartlab_company_financials_multi`, `stock_f_score`, `stock_z_score`, `stock_peer_comparison`, `stock_growth_analysis`, `dividend_analysis`, `bank_benchmark`, `bank_peer_comparison`, `company_fundamental_report` |
| technical | 5 | `price_volatility`, `liquidity_assessment`, `technical_indicators`, `candlestick_patterns`, `moex_splits`, `cointegration_test` |
| screening | 6 | `smartlab_stock_screener`, `bond_screener`, `bond_prescreener`, `etf_screener`, `raexpert_emitent_ratings`, `moex_correlations`, `cointegration_scan`, `cointegration_matrix` |
| discover | 2 | `moex_search_endpoints`, `moex_query` |
| macro | 7 | `cbr_key_rate`, `cbr_inflation`, `cbr_ruonia`, `cbr_ruonia_index`, `cbr_ibor`, `rate_expectations`, `curve_yield` |
| fx_metals | 3 | `cbr_currency`, `cbr_metals`, `cbr_reserves` |
| fixed_income | 10 | `moex_emitent_bonds`, `moex_bond_coupons`, `moex_bond_market_aggregates`, `moex_zcyc_history`, `bond_report`, `bond_accrued_interest`, `bond_synthetic_yield`, `raexpert_rating`, `zpif_payments`, `zpif_funds_list` |
| etf | 3 | `etf_fund_info`, `etf_premium_discount`, `etf_tracking_error` |
| derivatives | 10 | `moex_futures_list`, `moex_futures_open_interest`, `moex_futures_series`, `moex_futures_promo`, `moex_futures_basis`, `moex_options_assets`, `moex_options_board`, `moex_option_quote`, `moex_option_orderbook`, `moex_option_history` |
| risk | 5 | `portfolio_snapshot`, `portfolio_rate_whatif`, `portfolio_income_calendar`, `portfolio_movers`, `portfolio_alpha_beta` |
| option_calc | 13 | `option_calc_assets`, `option_calc_asset_detail`, `option_calc_futures`, `option_calc_options`, `option_calc_option_brief`, `option_calc_series`, `option_calc_series_detail`, `option_calc_series_options`, `option_calc_optionboard`, `option_calc_volatility_graph`, `option_calc_portfolio`, `option_calc_portfolio_graph`, `option_calc_initial_margin` |

### Роли агентов (наборы групп)

| Роль | URL | Задача | Группы |
|------|-----|--------|--------|
| **screener** | `/screener/mcp` | Обнаружить кандидатов из широкой вселенной | core_lookup, discover, screening, fundamental, macro |
| **analyst** | `/analyst/mcp` | Глубокий анализ конкретного инструмента | core_lookup, price_history, fundamental, technical, fixed_income, etf |
| **constructor** | `/constructor/mcp` | Собрать оптимальный портфель | core_lookup, screening, risk, fixed_income, etf |
| **timer** | `/timer/mcp` | Определить точку входа/выхода | core_lookup, price_history, technical, derivatives, option_calc |
| **macrotracker** | `/macrotracker/mcp` | Мониторинг макроэкономических трендов | core_lookup, macro, fx_metals, fixed_income |
| **risk_manager** | `/risk_manager/mcp` | Контроль рисков портфеля и позиций | core_lookup, risk, screening, macro, technical, fixed_income, derivatives, option_calc |
| **instrument_specialist** | `/instrument_specialist/mcp` | ETF/БПИФ, производные | core_lookup, etf, derivatives, technical, price_history, option_calc |
| **portfolio_manager** | `/portfolio_manager/mcp` | Обслуживание портфеля | core_lookup, risk, screening, fundamental, macro, fixed_income |

Stdio-режим отдаёт все 95 инструментов (группы и роли не разделены).

## Работа с репозиторием

- **Нет тестов, линтера, типчекера и CI.** Их нет в `pyproject.toml` (`[tool.*]`
  только для setuptools) — не ищи `pytest`/`ruff`/`mypy` и не предлага их без
  просьбы. Проверка — ручной запуск сервера (ниже).
- **Сетевой доступ обязателен.** Сервер живёт за счёт `iss.moex.com` (moex) и
  `cbr.ru` (cbrapi); без интернета все инструменты падают. `session.py` держит
  singleton-коннектор и делает до 4 ретраев — ISS капризен (случайные таймауты).
- **Запуск для проверки:**
  ```bash
  python3 -m venv .venv && source .venv/bin/activate
  pip install -e .
  python -m ru_finance.mcp_server        # stdio (по умолчанию)
  MCP_TRANSPORT=streamable-http MCP_PORT=8000 python -m ru_finance.mcp_server  # http://127.0.0.1:8000/mcp
  ```
  `start.sh` — это уже remote-режим на `0.0.0.0:8000`.
- **Модели в `ru_finance/`:** `session.py` (клиент aioboy/moex + ретраи на сырой requests/niquests),
  `moex.py` (резолв/котировки/облигации/история + CCI/НРД-эндпоинты: компании (через securities search),
  IR-календарь, капитализация, корреляции, сплиты, агрегаты облигаций, история КБД,
  обороты, новости, индикативные курсы, технические индикаторы),
  `cbr.py` (ставка, RUONIA, MIACR, валюта,
  металлы, ЗВР), `bonds.py` (дюрация и сценарии по ставке), `rate.py`
  (G-кривая ОФЗ, ожидания по ставке, NSS-модель КБД), `portfolio.py`
  (парсинг `assets` + отчёты),   `smartlab.py` (календарь + история дивидендов со
  smart-lab.ru), `vsezpif.py` (календарь выплат ЗПИФ недвижимости с vsezpif.ru),
  `raexpert.py` (кредитные рейтинги Эксперт РА: эмитенты + облигации,
  скрейпинг raexpert.ru), `option_calc.py` (опционный калькулятор MOEX: базовые
  активы, фьючерсы, опционы, серии, доски с Greeks, волатильные кривые, расчёт
  опционных стратегий и гарантийного обеспечения).
  Наружу всё пробрасывается как инструменты в `mcp_server.py` (95 `@mcp.tool()`).

## Streaming и progress notifications

Сервер поддерживает **progress notifications** через `ctx.report_progress()` —
клиент видит промежуточные обновления во время работы тяжёлых инструментов
(portfolio_snapshot, bond_report, rate_expectations и др.; 15 async tools).
Клиенты без progress token просто не увидят нотификаций — обратная совместимость
сохраняется.

**SSE resumability:** для HTTP-транспорта работает `InMemoryEventStore` — при обрыве
соединения клиент переподключается по `Last-Event-ID` и получает пропущенные
нотификации. `retry_interval=5` с.

- **Личные данные — вне репо.** `assets.md`/`context.md` (портфель и контекст
  пользователя) находятся в родительской папке и в `.gitignore` — не создавай их
  здесь и не храни в коде.
