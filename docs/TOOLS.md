# Справочник инструментов `ru-finance`

94 ручки. Полное описание сигнатур, входов/выходов и примеров. Краткий обзор — в
[README](../README.md). Принципы использования для ИИ-агента — в [AGENTS.md](../AGENTS.md).

Все ручки **generic**: конкретные бумаги и портфель передаются параметрами, в коде
сервера нет ничьих данных. Цены — задержка ~15 мин (бесплатный ISS); в выходные
отдаётся цена последней сессии.

Легенда: 🟢 факт-данные · 🧮 расчёт · 🛠 утилиты

---

## Утилиты

### 🛠 `current_datetime()`
Текущая дата и время сервера (UTC+0, ISO 8601).
- **Принимает:** ничего.
- **Возвращает:** `{datetime, date, time, timestamp}`.
- **Пример:** `current_datetime()` → `{"datetime":"2026-09-02T14:30:00.000000","date":"2026-09-02","time":"14:30:00.000000","timestamp":1693655400.0}`
- **Использование:** для определения текущего дня (напр. в `moex_candles`) или проверки доступности сервера.

---

## MOEX — Московская биржа

### 🟢 `moex_resolve(query)`
Определить одну бумагу по тикеру/ISIN/номеру ОФЗ/названию.
- **Принимает:** `query` — тикер/ISIN/номер ОФЗ/название (`"SBER"`, `"26253"`, `"RU000A10C6F7"`).
- **Возвращает:** `{secid, engine, market, board, type, shortname, isin, group, is_traded}`.
- **Пример:** `moex_resolve("26253")` → `{"secid":"SU26253RMFS3","market":"bonds",...}`

### 🟢 `moex_search(query, sec_type=None)`
Поиск бумаг на MOEX — возвращает массив совпадений (до 200).
- **Принимает:** `query` — тикер/ISIN/название (`"Сбербанк"`, `"Тинькофф"`).
  `sec_type` — фильтр: `"bond"`, `"share"`, `"stock"`, `"fund"`, `"etf"`, `"index"`.
- **Возвращает:** `[{secid, shortname, isin, type, group, is_traded, engine, market, board}, ...]`.
- **Примеры:** `moex_search("Сбербанк")` → все бумаги; `moex_search("Сбербанк", sec_type="bond")` → только облигации.

### 🟢 `moex_quote(query)`
Текущая котировка акции/фонда (нормализованная, с фоллбэком цены).
- **Принимает:** `query` — тикер/название.
- **Возвращает:** `{secid, price, change_pct, bid, ask, open, low, high, value_today, vol_today, updatetime, price_field}`. `price_field` = какое поле дало цену (в выходные `MARKETPRICE`/`LCLOSEPRICE` вместо `LAST`).
- **Пример:** `moex_quote("SBER")` → `{"price":299.69,"change_pct":0.03,"price_field":"LAST",...}`

### 🟢 `moex_bond(query)`
Облигация со всеми метриками для анализа под ставку.
- **Принимает:** `query` — номер ОФЗ или ISIN.
- **Возвращает:** `{price_pct, change_pct, ytm, duration_years, mod_duration_years, coupon_pct, coupon_value, annual_coupon_per_bond, next_coupon, coupon_period_days, maturity, accrued_int, face_value}`.
- **Пример:** `moex_bond("26253")` → `{"price_pct":87.18,"ytm":15.93,"duration_years":5.9,"mod_duration_years":5.46,"annual_coupon_per_bond":130.0,"next_coupon":"2026-10-21","maturity":"2038-10-06",...}`
- **⚠️ Ступенчатые купоны:** для облигаций с уменьшающимся/переменным купоном `ytm`, `duration_years` и `mod_duration_years` от MOEX ISS рассчитаны по фиксированной `coupon_pct` и **некорректны**. Используйте `bond_report` — он подтягивает реальное расписание купонов.

### 🟢 `moex_candles(query, frm, till, interval="24")`
Свечи OHLCV за период.
- **Принимает:** `query`; `frm`/`till` (`"YYYY-MM-DD"`); `interval` — `1,10,60`(час)`,24`(день)`,7`(нед)`,31`(мес)`,4`(кв).
- **Возвращает:** список `{begin, open, high, low, close, value, volume}`.
- **Пример:** `moex_candles("SBER","2026-06-22","2026-06-26","24")`

### 🟢 `moex_history(query, frm, till)`
Дневная история торгов (компактная версия).
- **Принимает:** `query`; `frm`/`till` (`"YYYY-MM-DD"`).
- **Возвращает:** `[{TRADEDATE, CLOSE, VOLUME}]` — минимальный набор полей.
- **Пример:** `moex_history("26253","2025-12-01","2026-06-27")`

### 🟢 `moex_full_history(query, frm, till)`
Дневная история торгов (полная версия, все поля).
- **Принимает:** `query`; `frm`/`till` (`"YYYY-MM-DD"`).
- **Возвращает:** `[{TRADEDATE, CLOSE, VOLUME, VALUE, ...}]` — все поля ISS.
- **Пример:** `moex_full_history("26253","2025-12-01","2026-06-27")`

### 🟢 `moex_search_endpoints(pattern)`
Найти ISS-эндпоинт (шаблон) по подстроке пути — для доступа к данным без готовой ручки.
- **Принимает:** `pattern` — `"/candles"`, `"turnovers"`, `"/dividends"`.
- **Возвращает:** `[{id, path, variables}]`. `id` → в `moex_query`.
- **Пример:** `moex_search_endpoints("turnovers")`

### 🟢 `moex_query(template_id, path_vars=None, query_params=None)`
Запасной доступ к ЛЮБОМУ из ~252 эндпоинтов ISS.
- **Принимает:** `template_id` (из `moex_search_endpoints`); `path_vars` — переменные пути (`{engine, market, board, security}`); `query_params` — query-параметры (`{from, till, ...}`).
- **Возвращает:** `{block: [строки]}`.
- **Пример:** `moex_query(322, {"engine":"stock"}, {})`

### 🟢 `moex_company_info(query)`
Справка об организации по ИНН/ОГРН/тикеру/названию (ISS securities search, дедупликация по emitent_id).
- **Принимает:** `query` — ИНН, ОГРН, тикер или фрагмент названия.
- **Возвращает:** `{companies: [{basis_company_id, inn, name_short_ru, name_full_ru, okpo, secid}]}` (до 20 шт).
- **Пример:** `moex_company_info("Сбербанк")`

### 🟢 `moex_company_info_by_id(company_id)`
Справка об организации по внутреннему ID MOEX (basis_company_id).
- **Принимает:** `company_id` — числовой ID (узнаётся из `moex_company_info`).
- **Возвращает:** `{basis_company_id, inn, name_short_ru, name_full_ru, okpo, secid, ...}` или `{}`.

### 🟢 `moex_ir_calendar(limit=50)`
Календарь IR-мероприятий (даты отчётов публичных компаний).
- **Принимает:** `limit` — сколько строк (по умолч. 50).
- **Возвращает:** `[{company_name_short_ru, event_type_name, event_date, event_link, ...}]`.

### 🟢 `moex_market_capitalization()`
Капитализация фондового рынка (₽).
- **Принимает:** ничего.
- **Возвращает:** `{capitalization: { capitalization, tradedate }, issuecapitalization: { issuecapitalization, updatetime }}`.
- **Пример:** `moex_market_capitalization()` → `{"capitalization":{"capitalization":42950671877090.26,"tradedate":"2026-09-01"}, ...}`

### 🟢 `moex_correlations(secid)`
Коэффициенты корреляции и бета для бумаги.
- **Принимает:** `secid` — тикер (`"SBER"`).
- **Возвращает:** `[{secid, fxsecid, tradedate, coeff_correlation, coeff_beta}]` — все пары с другими бумагами/индексами.
- **Пример:** `moex_correlations("SBER")` → пары с GAZP, LKOH, IMOEX, ...

### 🟢 `moex_splits(secid=None)`
Справочник дроблений и консолидаций бумаг.
- **Принимает:** `secid` (опционально). Без параметра — все сплиты.
- **Возвращает:** `[{tradedate, secid, before, after}]`.
- **Пример:** `moex_splits("VTB")` → `{tradedate:"2021-04-12", secid:"VTBB", before:1, after:10}`

### 🟢 `moex_bond_market_aggregates(frm=None, till=None)`
Агрегированные показатели рынка облигаций.
- **Принимает:** `frm`/`till` (`"YYYY-MM-DD"`, опционально).
- **Возвращает:** `[{tradedate, type_bond, iss_nominal, vol_nominal, coeff_nominal, avg_years, ...}]`.
- Типы: Корпоративные, ОФЗ, Муниципальные и т.д.
- **Пример:** `moex_bond_market_aggregates(frm="2026-09-01")`

### 🟢 `moex_emitent_bonds(query, min_duration=None, max_duration=None)`
Все облигации эмитента (по названию/тикеру).
- **Принимает:** `query` — имя/тикер эмитента (`"Газпром"`, `"Сбербанк"`, `"ГТЛК"`); `min_duration`/`max_duration` — фильтр по дюрации (лет).
- **Возвращает:** `{emitent_id, count, bonds: [{secid, isin, shortname, price, ytm, duration, coupon, maturity, ...}]}`. Резолвит эмитента по `emitent_id`, затем ищет все бумаги с тем же `emitent_id`.
- **Пример:** `moex_emitent_bonds("Газпром")`, `moex_emitent_bonds("ГТЛК", max_duration=5)`

### 🟢 `moex_bond_coupons(query)`
Расписание купонов (прошлых + будущих) из НРД/MOEX.
- **Принимает:** `query` — ISIN или номер ОФЗ.
- **Возвращает:** `[{coupondate, value, valueprc, facevalue, faceunit, is_past, recorddate, startdate}]`. `is_past=True` → уже выплачен. Пустой список для ОФЗ (данные НРД не доступны через ISS).
- **Пример:** `moex_bond_coupons("RU000A106FP4")` → `[{coupondate:"2026-04-15", value:36.87, valueprc:7.65, is_past:true, ...}, ...]`

### 🟢 `moex_zcyc_history(frm, till)`
История параметров КБД (Кривая Бескупонной Доходности).
- **Принимает:** `frm`/`till` (`"YYYY-MM-DD"`).
- **Возвращает:** `[{tradedate, b1, b2, b3, t1, g1..g9}]` — параметры НСС-модели для каждого дня.
- Для бэктестинга кривой. См. также `curve_yield()` для NSS-модели на произвольном сроке.
- **Пример:** `moex_zcyc_history("2026-06-01","2026-09-01")`

### 🟢 `moex_turnovers()`
Сводные обороты по рынкам (биржевые итоги).
- **Принимает:** ничего.
- **Возвращает:** `[{name, valtoday, valtoday_usd, numtrades, updatetime, title}]`.
- Рынки: stock, currency, futures, commodity, ...
- **Пример:** `moex_turnovers()` → фондовый ~13.5 млрд ₽, срочный ~51.3 млрд ₽

### 🟢 `moex_sitenews(limit=20)`
Новости Московской биржи.
- **Принимает:** `limit` — сколько строк (по умолч. 20).
- **Возвращает:** `[{id, tag, title, published_at, modified_at}]`.
- **Пример:** `moex_sitenews(limit=5)`

### 🟢 `moex_aggregates(query, date)`
Агрегированные итоги торгов за дату по бумаге.
- **Принимает:** `query` (тикер), `date` (`"YYYY-MM-DD"`).
- **Возвращает:** `{securities: [...], marketdata: [...]} — полные итоги дня (SECID, SHORTNAME, ISSUESIZE, ...)`.

### 🟢 `moex_indicative_rates(frm=None, till=None)`
Индикативные курсы валют срочного рынка.
- **Принимает:** `frm`/`till` (`"YYYY-MM-DD"`, опционально).
- **Возвращает:** `[{tradedate, tradetime, secid, rate, clearing}]`.
- `secid` — валютная пара (`"CNY/RUB"`, `"USD/RUB"`, ...).
- **Пример:** `moex_indicative_rates()` → текущие курсы

### 🟢 `moex_futures_list(asset_code=None)`
Каталог фьючерсных контрактов FORTS с рыночными данными и спецификацией.
- **Принимает:** `asset_code` — код базисного актива (`"Si"`, `"RTS"`, `"BR"`, `"GAZR"` и т. д.). Без параметра — все торгуемые контракты.
- **Возвращает:** `[{secid, name, shortname, asset_code, expiry_date, lot_volume, min_step, step_price, initial_margin, prev_settle_price, last_settle_price, open_interest, prev_open_interest, oichange, prev_price, bid, offer, spread, last, high, low, volume_today, value_today, num_trades, high_limit, low_limit, buy_sell_fee, scalper_fee}]`.
- **Пример:** `moex_futures_list("Si")` → `[{secid:"SiU6", name:"Si-9.26", last_settle_price:87119, open_interest:8139998, initial_margin:13260.16, ...}, ...]` — 6 контрактов Si.
- **Использование:** сравнить обеспечения, спреды bid/ask, объёмы по разным экспирациям; найти самый ликвидный контракт.

### 🟢 `moex_futures_open_interest(asset)`
Открытый интерес по базисному активу: разбивка на юрлица / физлица.
- **Принимает:** `asset` — код базисного (`"Si"`, `"RTS"`, `"BR"`, `"SBRF"` и т. д.).
- **Возвращает:** `{asset, tradedate, juridical: {persons_long, persons_short, oi_long, oi_short, oi_change_long, oi_change_short}, physical: {...}, total_oi_long, total_oi_short}`.
- **Пример:** `moex_futures_open_interest("Si")` → `juridical.oi_long: 4451580, physical.oi_long: 1340390` — юрлица держат 3/4 длинных позиций.
- **Использование:** дивергенция «умных денег» vs розницы. Резкий рост `oi_change_long` юрлиц при падении цены → возможен разворот.

### 🟢 `moex_futures_series(asset=None)`
Календарь экспираций фьючерсов.
- **Принимает:** `asset` — код базисного (`"Si"`, `"RTS"`...). Без параметра — все серии.
- **Возвращает:** `[{secid, name, start_date, expiration_date, asset_code, underlying_asset, is_traded, is_expired, days_to_expiry}]`.
- `days_to_expiry` — дней до экспирации (`< 0` — уже истёк).
- **Пример:** `moex_futures_series("Si")` → `[{secid:"SiU7", expiration_date:"2027-09-16", days_to_expiry:379, is_traded:1}, ...]` — 6 контрактов.
- **Использование:** выбор контракта для ролла (сравнить `days_to_expiry`); построение кривой фьючерсных цен.

### 🟢 `moex_futures_promo()`
Агрегированная статистика срочного рынка (FORTS).
- **Принимает:** ничего.
- **Возвращает:** `{fee_forts, fee_options, fee_all, updated_at}` — совокупные комиссионные сборы рынка.
- **Использование:** грубый proxy активности рынка в динамике (выше сборы → больше торгов).

### 🧮 `moex_futures_basis(asset_code)`
Contango/backwardation — annualised carry от basis фьючерса.
- **Принимает:** `asset_code` — код базисного (`"Si"`, `"GAZP"`, `"SiM5"`, ...).
- **Возвращает:** `{asset, asset_code, futures_secid, futures_settle, spot_price, spot_source, basis_pct, annualized_return_pct, days_to_expiry, expiry, direction}`. `direction` = `contango` (`>0`, futures дороже spot) / `backwardation` (`<0`, futures дешевле spot). Спот-источник: FX → курс ЦБ, акции → MOEX quote, индекс → MOEX index, товар → CBR/MOEX.
- **Примеры:** `moex_futures_basis("Si")`, `moex_futures_basis("GAZP")`, `moex_futures_basis("SiM5")`.

### 🟢 `moex_options_assets()`
Базисные активы опционов FORTS с рыночными данными.
- **Принимает:** ничего.
- **Возвращает:** `[{tradedate, asset, shortname, asset_type, asset_last_price, asset_last_to_prev_price, asset_high_price, asset_low_price, valtoday, voltoday, numtrades, openposition, oichange, option_secid}]`.
- `asset_type`: `S` — акция, `F` — фьючерс, `M` — фьючерс (мини), `C` — валюта.
- **Пример:** `moex_options_assets()` → `[{asset:"GAZP", asset_last_price:86.76, openposition:36528744, vol_today:148375, ...}, ...]` — 90 активов.
- **Использование:** отобрать активы с высоким опционным OI; найти Calling/лесенку strikes по конкретному активу.

### 🟢 `moex_options_board(asset)`
Опционная доска (волатильность, страйки, OI) по базисному активу.
- **Принимает:** `asset` — код базисного (`"Si"`, `"GAZP"`, `"SBRF"`, `"GAZR"`, `"BR"` и т. д.).
- Для фьючерсных базисных активов (`Si`, `GAZR`, `BR`, `CNY`, `MIX`...) автоматически резолвит код серии (`SiU6`, `GZU6`, `BRV6`...) через regular ISS, если statistics-эндпоинт вернул пустой ответ.
- **Возвращает:** `{asset_info: {central_strike, underlying_settle, last_del_date},
  calls: [{secid, strike, iv, last, theor_price, bid, offer, oi, volume}],
  puts: [{secid, strike, iv, last, theor_price, bid, offer, oi, volume}]}`.
- **Пример:** `moex_options_board("Si")` → 45 strike'ов, `asset_info.central_strike: 87000`, `asset_info.underlying: SiU6`, calls[0].iv: 22.5%.
- **Использование:** оценка implied volatility (сравнить iv со скользящей vol спота); построение профилей risk reversal; выбор strike для хеджа.

### 🟢 `moex_option_quote(secid)`
Котировка конкретного опционного инструмента.
- **Принимает:** `secid` — код инструмента (`"Si87000BI6A"`, `"GZ85CU6A"` и т. д.).
- **Возвращает:** `{secid, shortname, secname, assetcode, option_type, strike, underlying_asset, underlying_settle, expiration_date, last_trade_date, min_step, step_price, prev_settle, prev_oi, last, bid, offer, spread, open, high, low, volume, value, num_trades, oi, oi_change, settle_price, last_change, last_change_pct, update_time, im_np, im_sp, im_buy}`.
  - `option_type`: `C` — call, `P` — put.
  - `im_np/im_sp/im_buy` — гарантийное обеспечение (ГО) по непокрытой/синтетической/покупке.
- **Пример:** `moex_option_quote("Si87000BI6A")` → `{secid:"Si87000BI6A", strike:87000, option_type:"C", last:500, bid:485, offer:528, oi:8644, volume:2109, im_np:12791.23, im_buy:588.0, ...}`.
- **Использование:** детальный анализ конкретного опциона — премия, спред bid/offer, ГО.

### 🟢 `moex_option_orderbook(secid)`
Лучшие bid/offer стакана опционного инструмента.
- **Примечание:** полный стакан (depth-of-market) для опционов недоступен через ISS REST (эндпоинт `/orderbook` отдаёт HTML). Возвращаем лучшие bid/offer и спред из котировок.
- **Принимает:** `secid` — код инструмента (`"Si87000BI6A"`, `"GZ85CU6A"` и т. д.).
- **Возвращает:** `{secid, bid, offer, spread, bid_depth, offer_depth, bid_depth_total, offer_depth_total}`.
- **Пример:** `moex_option_orderbook("Si87000BI6A")` → `{secid:"Si87000BI6A", bid:486, offer:530, spread:44, ...}`.
- **Использование:** оценка ликвидности опционов; проверка спреда bid/offer.

### 🟢 `moex_option_history(secid, frm=None, till=None)`
История сделок опционного инструмента.
- **Принимает:** `secid` — код инструмента; `frm/till` — даты `'YYYY-MM-DD'` (опционально).
- **Возвращает:** `[{tradedate, secid, close, open, high, low, volume, value, oi, oi_value, settle_price, waprice, num_trades, theor_price, change, qty}]`.
- **Пример:** `moex_option_history("GZ85CU6A")` → `[{tradedate:"2026-08-14", close:3.19, volume:1270, oi:6602, theor_price:2.97, ...}, ...]`.
- **Использование:** анализ динамики премии и OI опционов.

### 🟢 `smartlab_dividends(limit=50)`
Календарь ближайших дивидендов со smart-lab.ru.
- **Принимает:** `limit` — сколько строк (по умолч. 50).
- **Возвращает:** список `{name, ticker, period, dividend_rub, yield_pct, board_approved, last_buy_date, close_date, payment_date, price}`. `dividend_rub` — ₽ за акцию; `yield_pct` — див. доходность %.
- **Пример:** `smartlab_dividends(limit=5)` → `[{"ticker":"YDEX","period":"2кв 2026","dividend_rub":110.0,...}, ...]`

### 🟢 `smartlab_dividend_history(ticker)`
История дивидендов по тикеру со smart-lab.ru (все выплаты по эмитенту).
- **Принимает:** `ticker` — тикер («SBER», «LKOH»).
- **Возвращает:** список `{ticker, date_t1, cutoff_date, period, dividend_rub, price, yield_pct}`. `dividend_rub` — ₽ за акцию; `yield_pct` — див. доходность %.
- **Пример:** `smartlab_dividend_history("SBER")` → 18 строк, последняя: `{ticker:"SBER", period:"2025 год", dividend_rub:37.64, yield_pct:13.6}`

### 🟢 `smartlab_company_financials(ticker, period="annual", standard="rsbu")`
Детальная финансовая отчётность компании со smart-lab.ru (мульти-годовая).
- **Принимает:** `ticker` — тикер; `period` — `"annual"` (по умолч.) или `"quarter"`; `standard` — `"rsbu"` (РСБУ, по умолч.) или `"ifrs"` (МСФО).
- **Возвращает:** `{ticker, name, years, data: {field: {label, values: {"2022": val, "2023": val, ..., "LTM": val}}}}`.
- **Пример:** `smartlab_company_financials("SBER")` → `{ticker:"SBER", years:["2020","2021",...,"LTM"], data:{revenue:{label:"Выручка", values:{...}}, ...}}`

### 🟢 `smartlab_company_financials_multi(tickers, period="annual", standard="rsbu")`
Детальная финансовая отчётность несколькиих компаний (batch).
- **Принимает:** `tickers` — список тикеров (`["SBER","LKOH","GAZP"]`); `period`/`standard` — см. `smartlab_company_financials`.
- **Возвращает:** `{ticker: {name, years, data: ...}, ...}` — аналог `smartlab_company_financials` для каждого тикера; ошибки по отдельным тикерам не останавливают обработку остальных.
- **Пример:** `smartlab_company_financials_multi(["SBER","LKOH"])` → `{SBER:{...}, LKOH:{...}}`

### 🧮 `smartlab_stock_screener(...)`
Фундаментальный скринер акций MOEX (LTM-множители от smart-lab.ru, кэш 4 ч).
- **Принимает:** `market_cap_min`/`market_cap_max` — капитализация (₽); `pe_min`/`pe_max` — P/E; `ps_min`/`ps_max` — P/S; `pb_min`/`pb_max` — P/B; `ev_ebitda_min`/`ev_ebitda_max` — EV/EBITDA; `roe_min`/`roe_max` — ROE (%); `roa_min`/`roa_max` — ROA (%); `ebitda_margin_min`/`ebitda_margin_max` — рентаб. EBITDA (%); `div_yield_min`/`div_yield_max` — див. доходность (%); `sort_field` / `sort_desc`; `limit` (макс. 200, по умолч. 30).
- **Возвращает:** `{count, limit, stocks: [{ticker, name, market_cap, pe, ps, pb, ev_ebitda, dividend_yield, roe, roa, ebitda_margin, ebitda, debt_ebitda, ...}]}`
- **Пример:** `smartlab_stock_screener(market_cap_min=100_000_000_000, div_yield_min=5, sort_field="dividend_yield")`

### 🧮 `stock_f_score(ticker)`
Piotroski F-Score (0–9) — 9 бинарных сигналов из финансовой отчётности.
- **Принимает:** `ticker` — тикер.
- **Возвращает:** `{ticker, f_score, signals: {roa_positive, cfo_positive, roa_improving, cfo_gt_ni, debt_decreasing, current_ratio_improving, no_dilution, gross_margin_improving, asset_turnover_improving}, annual_data: {year, roa, cfo_to_assets, ...}}`.
- F ≥ 7 — сильные фундаменталы; F ≤ 3 — слабые.
- **Пример:** `stock_f_score("SBER")` → `{"ticker":"SBER","f_score":7, ...}`

### 🧮 `stock_z_score(ticker)`
Altman Z-Score (модифицированный для emerging markets) — банкротный риск.
- **Принимает:** `ticker` — тикер.
- **Возвращает:** `{ticker, z_score, interpretation, components: {x1, x2, x3, x4}, data: {total_assets, working_capital, retained_earnings, ebit, market_cap, total_liabilities, revenue}}`.
- Z > 2.9 → безопасная зона; 1.23–2.9 → серая зона; < 1.23 → зона риска.
- **Пример:** `stock_z_score("SBER")` → `{"z_score":3.29,"interpretation":"safe_zone",...}`

### 🧮 `stock_peer_comparison(ticker, top_n=15)`
Сравнение мультипликаторов акции с топ-N peers по капитализации.
- **Принимает:** `ticker`; `top_n` — сколько peers (по умолч. 15).
- **Возвращает:** `{ticker, rank: {pe, ps, pb, ev_ebitda, ebitda_margin, debt_ebitda, div_yield, payout}, peer_median: {pe, ps, ...}, peer_count, peers_tickers}`.
- **Пример:** `stock_peer_comparison("SBER")` → `{rank:{pe:3,...}, peer_median:{pe:8.5,...}}`

### 🧮 `dividend_analysis(ticker)`
Агрегированный дивидендный анализ: CAGR, средняя доходность, стабильность выплат.
- **Принимает:** `ticker` — тикер.
- **Возвращает:** `{ticker, total_years, dividend_cagr_pct, avg_yield_pct, payments_with_dividends, years_with_payments, payments_count, last_3_years: [{year, total_dividends, avg_yield}], consistency_pct}`.
- **Пример:** `dividend_analysis("SBER")` → `{"dividend_cagr_pct":19.5,"avg_yield_pct":7.8,"consistency_pct":87.5,...}`

### 🧮 `stock_growth_analysis(ticker)`
Рост выручки/EBITDA/чистой прибыли, тренды ROE/ROA/маржинальности (мульти-год).
- **Принимает:** `ticker` — тикер.
- **Возвращает:** `{ticker, years, data: {year, revenue, ebitda, net_income, roe, roa, ebitda_margin, net_margin, gross_margin}, analysis: {revenue_cagr, net_income_cagr, latest_roe, latest_roa, latest_ebitda_margin}}`.
- **Пример:** `stock_growth_analysis("SBER")` → `{"analysis":{"revenue_cagr":14.2,"latest_roe":25.3,...}}`

### 🧮 `bank_benchmark(tickers, short_names=None)`
Бенчмарк банков по ключевым банковским метрикам.
- **Принимает:** `tickers` — список тикеров банков (`["SBER","VTBR","TCSG"]`); `short_names` — опционально, отображаемые имена.
- **Возвращает:** `{banks: [{ticker, name, nim, cir, npl, car, ldr, cor, bank_margin, roa, roe}], medians: {nim, cir, ...}}`.
- **Пример:** `bank_benchmark(["SBER","VTBR"])` → `{banks:[{ticker:"SBER", nim:5.9, roe:25.3, ...},...], medians:{nim:5.5,...}}`

### 🧮 `bank_peer_comparison(ticker)`
Ранжирование банка по банковским метрикам относительно всего сектора (~15 крупнейших).
- **Принимает:** `ticker` — тикер банка.
- **Возвращает:** `{ticker, rank: {nim, cir, npl, car, ldr, cor, bank_margin}, median: {nim, cir, ...}, bank_count, note}`.
- **Пример:** `bank_peer_comparison("SBER")` → `{rank:{nim:3,...}, bank_count:15,...}`

### 🧮 `company_fundamental_report(ticker)`
Всё в одном: мультипликаторы + дивиденды + рост + Z-Score + F-Score + peer-сравнение.
- **Принимает:** `ticker` — тикер.
- **Возвращает:** `{ticker, name, market_cap, mpe, ps, pb, ev_ebitda, roe, roa, margins, debt, dividend: {cagr, avg_yield, consistency, last_years}, growth: {revenue_cagr, ...}, z_score, f_score, peer_rank}`.
- **Пример:** `company_fundamental_report("SBER")`

---

## Кредитные рейтинги (raexpert.ru)

Рейтинги Эксперт РА — старейшего российского рейтингового агентства.
Источник — серверные HTML-таблицы raexpert.ru/ratings/{category}/ (20 последних
рейтинговых действий по каждой из 10 категорий). Кэш 4 ч.

### 🟢 `raexpert_rating(query)`
Кредитный рейтинг эмитента или облигации от Эксперт РА.
- **Принимает:** `query` — название эмитента (`"Сбербанк"`, `"ЛУКОЙЛ"`) или облигации (`"Атомэнергопром"`). Регистр не важен.
- **Возвращает:** `[{name, rating, outlook, date, category, type, agency}]`.
  Для облигаций (`type="emission"`) также `emitent`. Пустой список, если рейтинг не найден.
  - `rating`: `ruAAA` (макс) → `ruCCC`, `ruD` (дефолт), `отозван`. `SB` = стандартные облигации.
  - `outlook`: `Стабильный`, `Позитивный`, `Развивающийся`, или пустая строка.
- **Пример:** `raexpert_rating("ЛУКОЙЛ")` → `[{name:"ПАО \"ЛУКОЙЛ\"", rating:"ruAAA", outlook:"Стабильный", date:"25.08.2026", category:"Нефинансовые companies", type:"emitent", agency:"Эксперт РА"}]`
- **Шкала:** `ruBBB−` и выше — investment grade. `ruBB+` и ниже — speculative.

### 🟢 `raexpert_emitent_ratings(rating_min=None, sector=None)`
Эмитенты облигаций с фильтрацией по кредитному рейтингу и/или отрасли.
- **Принимает** (опционально):
  - `rating_min` — минимальный рейтинг: `"ruBBB-"`, `"ruA"`, `"ruA+"`, ... Записи с `отозван` при фильтрации исключаются.
  - `sector` — название отрасли MOEX (подстрока: полное имя индекса). Доступные: `Финансовый`, `Нефтегазовый`, `Потребительский`, `Телекоммуникации`, `Электроэнергетика`, `Транспорт`, `Металлургия и добыча`, `Недвижимость`, `Химия`, `Инновации и IT`.
- **Возвращает:** `[{name, rating, outlook, date, category, sector?, agency}]`. Сортировка по рейтингу (лучшие первые), затем по имени.
- **Пример:** `raexpert_emitent_ratings(rating_min="ruBBB-", sector="Нефтегазовый")` → найдёт все нефтегазовые эмитенты с рейтингом не ниже ruBBB−.
- **Примечание по сектору:** маппинг ~100 эмитентов из отраслевых индексов МосБиржи (MOEXFN, MOEXOG, MOEXMM, ...). Покрывает только публичные компании; непубличные эмитенты в этих подотраслях не попадут в фильтр по сектору.

---

## ЗПИФ выплаты (vsezpif.ru)

Данные по выплатам ЗПИФ (закрытых паевых инвестиционных фондов) недвижимости.
Источник — vsezpif.ru, единственный бесплатный агрегатор данных по 40+ фондам.
Кэш в памяти на 4 часа.

### 🟢 `zpif_payments(fund_name=None, isin=None, limit=50)`
Календарь выплат ЗПИФ недвижимости.

- **Принимает:**
  - `fund_name` (опц.) — название фонда или часть: "Акцент", "Парус", "СФН", "ВИМ";
  - `isin` (опц.) — международный идентификатор;
  - `limit` (опц.) — макс. количество записей.
- Без параметров — все ближайшие выплаты на 12 месяцев.
- **Возвращает:**
  - `payments[]` — `{date, date_iso, fund_name, amount_per_unit}` (₽ за 1 пай);
  - `next_payment` — `{date_iso, fund_name, amount}` (оценка следующей выплаты);
  - `funds_total` — общее число фондов в календаре.
- **Пример:** `zpif_payments(fund_name="Акцент")` → `{"payments": [{"date":"07.09.2026","fund_name":"АКЦЕНТ ФОНД IV","amount":13.33},...], "next_payment":...}`
- **Примечание:** календарь оценочный — даты и суммы по последним фактическим выплатам и периодичности.

### 🟢 `zpif_funds_list()`
Список всех ЗПИФ недвижимости с vsezpif.ru.

- **Возвращает:** список `{slug, fund_name, url}` — `slug` можно использовать в `zpif_payments`.
- **Пример:** `zpif_funds_list()` → `[{"slug":"vim-rentnyj-dohod-pro","fund_name":"ВИМ Рентный доход ПРО","url":"https://vsezpif.ru/zpif-vim-rentnyj-dohod-pro"}, ...]`

---

## ЦБ РФ

Все возвращают `{latest, latest_date, series[]}` (ряд) или `{latest, latest_date, rows[]}` (таблица). Даты опциональны (`"YYYY-MM-DD"`), `tail` — сколько последних точек.

### 🟢 `cbr_key_rate(first_date=None, last_date=None, tail=30)`
Ключевая ставка ЦБ — главный драйвер облигаций и рубля.
- **Пример:** `cbr_key_rate(tail=1)` → `{"latest":14.25,"latest_date":"2026-06-26"}`

### 🟢 `cbr_ruonia(...)`
RUONIA overnight (% годовых) — рыночный ориентир ставки денежного рынка. (Приведено к процентам.)

### 🟢 `cbr_ruonia_index(...)`
RUONIA-индекс + срочные средние `RUONIA_AVG_1M/3M/6M` (% годовых) — короткая кривая ставок денежного рынка. Живая замена прекращённому ROISfix. Таблица.

### 🟢 `cbr_ibor(...)`
MIACR — фактические средневзвешенные ставки межбанка по срокам (D1/D7/...). MosPrime/MIBOR прекращены — пустые колонки отфильтрованы. Таблица.

### 🟢 `cbr_currency(symbol, first_date, last_date, tail=30)`
Курс валюты ЦБ к рублю. `symbol`: `"USD"`, `"EUR"`, `"CNY"`. Даты обязательны.
- **Пример:** `cbr_currency("USD","2026-01-01","2026-06-27")`

### 🟢 `cbr_metals(...)`
Учётные цены ЦБ на драгметаллы (золото/серебро/платина/палладий). Таблица.

### 🟢 `cbr_reserves(...)`
Международные (золотовалютные) резервы РФ. Таблица.

### 🟢 `cbr_inflation(first_date=None, last_date=None, tail=24)`
Инфляция (CPI, % г/г) и ключевая ставка ЦБ РФ — помесячно с 2013 г.
- **Источник:** HTML-таблица cbr.ru/hd_base/infl/ (Росстат + ЦБ РФ). Скрейпинг + URL-параметры для фильтрации по дате (кэш 4 ч).
- **Принимает:** `first_date`/`last_date` — `'YYYY-MM'` или `'YYYY-MM-DD'` (опц., по умолчанию: 5 лет назад / сегодня); `tail` — последние N записей (опц., по умолч. 24).
- **Возвращает:** `{latest_inflation, latest_key_rate, latest_inflation_target, latest_date, series: [{date, key_rate, inflation_yoy, inflation_target}, ...]}`.
  - `inflation_yoy` — годовая инфляция Росстат (% г/г);
  - `inflation_target` — целевой уровень инфляции ЦБ РФ (%).
- **Пример:** `cbr_inflation(tail=3)` → `{"latest_inflation":5.98,"latest_key_rate":14.0,"latest_date":"2026-07","series":[{"date":"2026-05","key_rate":14.5,"inflation_yoy":5.31,...}, ...]}`
- **Использование:** расчёт реальной доходности облигаций и портфеля (YTM − инфляция вместо YTM − ключевая ставка); анализ динамики инфляции и её отклонения от цели ЦБ.

---

## Облигационная математика

### 🧮 `bond_report(query)`
Глубокий разбор облигации: метрики + сценарии + спред к кривой + конвексность.
- **Принимает:** `query` — номер ОФЗ/ISIN.
- **Возвращает:**
  - `bond` — как `moex_bond`;
  - `years_to_maturity` — срок до погашения в годах;
  - `coupon_schedule` — `[{date, rate_pct}, ...]` реальное расписание будущих купонов (если есть; для ступенчатых облигаций);
  - `macaulay_duration_years` — дюрация Маколея (всегда пересчитывается, для ступенчатых купонов — с реальным расписанием);
  - `modified_duration_years` — модифицированная дюрация (с правильным freq);
  - `convexity` — конвексность (поправка к duration при больших сдвигах);
  - `accrued_interest` — `{accrued_rub, accrued_pct, days_accrued, coupon_period_days, last_coupon, next_coupon}`;
  - `gry` — gross redemption yield (YTM с учётом НКД): `{gry_pct, dirty_price, accrued_rub}`. Для ступенчатых купонов пересчитывается с реальным расписанием и замещает MOEX YTM;
  - `spread_to_curve` — спред YTM к G-кривой: `{spread_pp, bond_ytm, curve_yield}`;
  - `scenarios` — `{macaulay_years, breakeven_yield_rise_pp, scenarios:[{delta_pp, total_return_pct}]}` (полный доход за год при параллельном сдвиге ±п.п. + точка безубытка);
  - `twist_scenarios` — сценарии сужения/расширения кривой: `[{name, delta_pp, total_return_pct, description}]` (steepener/flattener/twist_short/twist_long);
  - `real_return` — `{actual_inflation_pct, actual_real_return_pct, scenarios: [{inflation_pct, real_return_pct}]}` (реальная доходность при фактическом CPI Росстат + сценарии при разных допущениях).
- **Пример:** `bond_report("26253")` → при −2 п.п. годовой доход ≈ +27%, безубыток при росте доходности до ~+3.4 п.п.

### 🧮 `bond_accrued_interest(query)`
НКД облигации (накопленный купонный доход) — расчёт из календаря купонных дат.
- **Принимает:** `query` — номер ОФЗ/ISIN.
- **Возвращает:** `{accrued_rub, accrued_pct, days_accrued, coupon_period_days, last_coupon, next_coupon}`.

### 🧮 `bond_screener(...)`
Скринер облигаций: фильтрация по множеству параметров одновременно.
Загружает все облигации с бордов TQCB (корпоративные) и TQOB (ОФЗ, валютные),
применяет фильтры, опционально обогащает кредитным рейтингом (Эксперт РА) и
статусом квалифицированного инвестора.
- **Принимает (все опционально):**
  - `ytm_min`, `ytm_max` — доходность к погашению (%).
  - `coupon_min`, `coupon_max` — купонная ставка (%).
  - `price_min`, `price_max` — чистая цена (% от номинала).
  - `maturity_from`, `maturity_to` — дата погашения (`YYYY-MM-DD`).
  - `duration_min`, `duration_max` — дюрация Маколея (годы).
  - `years_to_maturity_min`, `years_to_maturity_max` — срок до погашения (годы).
  - `has_offer` — `True`: только с офертой; `False`: только без.
  - `has_amortization` — `True`: только амортизируемые; `False`: только без.
  - `coupon_type` — `"fixed"` (фиксированный), `"float"` (плавающий),
    `"amortization"` (амортизируемые).
  - `coupon_freq_min`, `coupon_freq_max` — купонов в год (1, 2, 4, 6, 12).
  - `currency` — валюта номинала (`SUR`, `USD`, `EUR`, `CNY`).
  - `issue_volume_min`, `issue_volume_max` — объём выпуска (штук бумаг).
  - `accrued_int_min`, `accrued_int_max` — НКД (₽ за бумагу).
  - `rating_min` — минимальный рейтинг Эксперт РА (`ruBBB-` = investment grade).
    Загружается с задержкой при первом вызове, кэш 4 ч.
  - `sector` — MOEX-сектор эмитента (`Финансовый`, `Нефтегазовый` и т.д.).
    Ограничение: карта секторов покрывает ~100 крупнейших эмитентов.
  - `include_qualified` — `True`: добавить поле `is_qualified`
    (ISQUALIFIEDINVESTORS). Доп. запрос на каждую бумагу (параллельно, до 200).
  - `qualified_only` — `True`: только для квалифицированных; `False`: только для
    неквалифицированных. Требует `include_qualified=True`.
  - `sort_by` — поле сортировки: `ytm`, `duration`, `maturity`, `price`,
    `coupon`, `issue_volume`. По умолчанию `ytm`.
  - `sort_desc` — `True` = по убыванию (по умолчанию).
  - `limit` — максимум результатов (1..500, по умолчанию 15).
- **Возвращает:** `{count_shown, count_total_matching, count_all_bonds, bonds: [{secid, shortname, isin, board, emitent, price_pct, ytm, coupon_pct, coupon_freq, duration_years, mod_duration_years, maturity, years_to_maturity, offer_date, has_offer, bond_type, is_amortization, face_unit, face_value, accrued_int, issue_size, issue_size_placed, list_level, value_today, vol_today, num_trades, bid_ask_spread_pct, rating?, sector?, is_qualified?}]}`.
  - `bond_type` — ISS-классификация: `Фикс с известным купоном`,
    `Фикс с неизвестным купоном`, `Флоатер`, `Амортизируемые облигации`,
    `Валютные облигации`.
  - `is_amortization` — `True` если `bond_type` = амортизируемые.
  - `has_offer` — `True` если есть дата оферты (put/call).
  - `value_today` — оборот в ₽ за текущую сессию.
  - `bid_ask_spread_pct` — спред bid/ask из marketdata (%).
- **Пример:** `bond_screener(ytm_min=12, ytm_max=20, currency="SUR", coupon_type="fixed", has_offer=False, sort_by="ytm", sort_desc=True, limit=10)` → 325 облигаций; лучшая 19.9% YTM.
- **Использование:** предварительный отбор облигаций по портфелю; для глубокого
  анализа отобранных бумаг → `bond_report`.

### 🧮 `bond_prescreener(...)`
Компактный прескринер облигаций: те же параметры фильтрации, что и `bond_screener`,
но возвращает только `secname` и `isin` по каждой облигации. Оптимизирован для
проверки наличия бондов без переполнения контекста.
- **Принимает:** все параметры идентичны `bond_screener`.
- **Возвращает:** `{count_total_matching, count_all_bonds, bonds: [{secname, isin}, ...]}`.
  - `count_total_matching` — количество облигаций, прошедших фильтры.
  - `count_all_bonds` — общее количество облигаций на бордах.
  - `bonds` — массив объектов `{secname, isin}` (в порядке сортировки).
- **Пример:** `bond_prescreener(ytm_min=12, currency="SUR", coupon_type="fixed")` → `{"count_total_matching": 325, "count_all_bonds": 1200, "bonds": [{"secname": "Сбербанк (ПАО) обл.сер.А20", "isin": "RU000A1038V6"}, ...]}`
- **Использование:** предварительная проверка: какие бумаги доступны по заданным
  критериям; для детального списка → `bond_screener`, для анализа конкретной
  бумаги → `bond_report`.

### 🧮 `bond_synthetic_yield(query, horizon_years, reinvest_rate_ytm_pct=None)`
Синтетическая доходность с реинвестированием купонов на горизонте инвестирования.
- **Принимает:** `query` — тикер/ISIN; `horizon_years` — горизонт (лет); `reinvest_rate_ytm_pct` — ставка реинвестирования (% годовых, по умолчанию = текущая YTM бумаги).
- **Возвращает:** `{secid, isin, horizon_years, reinvest_rate, purchase_price_dirty, coupons_received, coupons_reinvested_at, future_value, synthetic_yield_pct, ...}`.
  - `synthetic_yield_pct` — IRR полного денежного потока: покупка по грязной цене, купоны реинвестированы, продажа по ожидаемой YTM на горизонте (или погашение по номиналу, если горизонт ≥ погашения).
- **Пример:** `bond_synthetic_yield("SU26253RMFS2", 3)` → `{"synthetic_yield_pct":16.14,...}`
- **Использование:** сравнение облигаций на едином горизонте; sensitivity на реинвест.

### 🧮 `price_volatility(query, days=90, rf_annual=16.0)`
Волатильность, Sharpe ratio, max drawdown по дневным свечам.
- **Принимает:** `query` (тикер), `days` (90 по умолчанию), `rf_annual` (безрисковая ставка, % годовых).
- **Возвращает:** `{annual_vol_pct, daily_vol_pct, sharpe, max_drawdown_pct, total_return_pct, high_price, low_price, trading_days, ...}`.
- `sharpe` = (mean excess return / volatility) × sqrt(252); `max_drawdown_pct` — максимальная просадка от пика.
- **Пример:** `price_volatility("SBER", 180)` → `{"annual_vol_pct":28.5,"sharpe":0.42,"max_drawdown_pct":12.3,...}`

### 🧮 `liquidity_assessment(query, days=90)`
Единая оценка ликвидности бумаги по данным свечей и текущих котировок.
- **Принимает:** `query` (тикер), `days` (90 по умолчанию).
- **Возвращает:** `{secid, trading_days, calendar_days, trading_day_ratio, zero_volume_days, avg_daily_turnover_rub, avg_daily_volume_lots, amihud_bps_per_mln, spread (%), spread_rub, spread_sources, composite_score (0-10), grade (A-E), note}`.
  - `amihud_bps_per_mln` — коэффициент Амихуда: mean(|r_t|/V_t) × 10¹⁰ (bps на 1 млн ₽). Ниже = ликвиднее;
  - `spread` — медиана оценок спреда: Corwin-Schultz из OHLC + фактический bid/ask (если доступен);
  - `spread_sources` — [{method ("hl_proxy"|"bid_ask"), spread_pct, spread_rub}];
  - `composite_score` — скор 0-10 на основе turnover и Amihud. Рассчитывается как log-линейная комбинация: высокий оборот + низкий Amihud = высокий скор;
  - `grade` — `A` (отличная) ≥8, `B` (хорошая) ≥6, `C` (умеренная) ≥4, `D` (низкая) ≥2, `E` (<2).
- **Пример:** `liquidity_assessment("SBER")` → `{"secid":"SBER","avg_daily_turnover_rub":9038700000,"amihud_bps_per_mln":0.02,"spread":0.053,"composite_score":9.7,"grade":"A",...}`
- **Использование:** сравнение ликвидности бумаг; при large-cap score ≥8 (grade A) стакан плотный; score <4 (grade D-E) — сложность входа/выхода при крупных позициях.

### 🧮 `technical_indicators(query, days=90)`
Полный набор технических индикаторов из дневных свечей (OHLCV). Для MA200/Ichimoku рекомендуется `days ≥ 200`.
- **Принимает:** `query` — тикер/ISIN; `days` — период (по умолч. 90).
- **Возвращает:** `{secid, period_days, trading_days, ...}` с полями (присутствуют при достаточном количестве данных):
  - **Тренд:** `rsi_14`, `stochastic_k`/`stochastic_d` (14,3,3), `adx` + `plus_di`/`minus_di` + `trend_strength` (weak/moderate/strong/very_strong), `macd` + `macd_signal_line` + `macd_histogram` + `macd_signal` (bullish/bearish/neutral), `atr_14` + `atr_14_pct`, `ichimoku_tenkan`/`ichimoku_kijun`/`ichimoku_senkou_a`/`ichimoku_senkou_b`/`ichimoku_chikou` + `ichimoku_signal` (bullish/bearish/in_cloud), `psar` + `psar_direction` (long/short), `ema_12`/`ema_26`, `ma_50`/`ma_200` + `ma_signal` (golden_cross/death_cross);
  - **Волатильность:** `bollinger_upper`/`bollinger_middle`/`bollinger_lower` + `bollinger_width (%Bandwidth)` + `bollinger_pct (%B)`;
  - **Объём:** `obv` + `obv_trend` (rising/falling), `cmf_20` + `cmf_signal` (buying_pressure/selling_pressure/neutral), `vwap`;
  - **Осцилляторы:** `cci_20`, `williams_r` (−100..0);
  - **Моментум:** `momentum_10`, `roc_10 (%)`;
  - **Уровни:** `pivot`/`pivot_s1..s3`/`pivot_r1..r3`, `fib_236/382/500/618/786` + `fib_high`/`fib_low`.
- **Пример:** `technical_indicators("SBER")` → `{"rsi_14":52.3,"stochastic_k":72.1,"macd_signal":"bullish","adx":18.5,"trend_strength":"weak","adx_plus_di":25.1,...}`
- **Использование:** комплексная TA-оценка любого инструмента (акция, ETF, облигация) — один вызов вместо ручных расчётов.

### 🕯 `candlestick_patterns(query, days=90)`
Распознавание классических свечных паттернов из дневных свечей MOEX (OHLCV).
- **Принимает:** `query` — тикер/ISIN; `days` — период (по умолч. 90).
- **Возвращает:** `{secid, period_days, trading_days, candles_analyzed,
  total_bullish, total_bearish, total_neutral, signal_score_bullish, signal_score_bearish, signal, patterns}`.
  - `patterns` — список обнаруженных паттернов, отсортированных по силе и близости к концу периода:
    - `pattern` — имя паттерна (14 штук): `doji` (sub: standard/dragonfly/gravestone/long_legged), `hammer`, `hanging_man`, `shooting_star`, `bullish_marubozu`, `bearish_marubozu`, `bullish_engulfing`, `bearish_engulfing`, `bullish_harami`, `bearish_harami`, `piercing_line`, `dark_cloud_cover`, `tweezer_top`, `tweezer_bottom`, `morning_star`, `evening_star`, `three_white_soldiers`, `three_black_crows`;
    - `bar_type` — `single` / `double` / `triple`;
    - `signal` — `bullish` / `bearish` / `neutral`;
    - `strength` — 1 (слабый), 2 (средний), 3 (сильный); усиливается при совпадении с позицией в периоде (near-high → bearish stronger, near-low → bullish stronger);
    - `bars_back` — сколько баров назад от последней свечи;
    - `date` — дата последней свечи паттерна;
    - `pattern_details` — структурированные детали (для star: gap_below_mid/gap_above_mid, pierce_50pct, body_ratio; для tweezer: near_period_extreme; для piercing_line/dark_cloud: intrusion_pct%; для marubozu: normalized_range);
  - `signal` — сводный сигнал: `bullish` / `slightly_bullish` / `neutral` / `slightly_bearish` / `bearish` (взвешенная сумма strength по direction).
- **Пример:** `candlestick_patterns("SBER")` → `{"secid":"SBER","signal":"slightly_bullish","patterns":[{"pattern":"hammer","bar_type":"single","signal":"bullish","strength":2,"bars_back":2,"date":"2025-06-05"}],...}`
- **Использование:** дополнение к `technical_indicators` — числовые индикаторы + визуальная геометрия свечей. Сильный паттерн (strength ≥ 2) + подтверждение индикатором (RSI, MACD) повышает уверенность сигнала.

---

## ETF / БПИФ

Анализ биржевых паевых инвестиционных фондов (БПИФ) на MOEX. Каждый БПИФ
имеет iNAV-спутник (TMOS→TMOSA) — инструмент на доске `INAV` с
индикативной чистой стоимостью фонда. iNAV доступен только в торговые часы.

### 🧮 `etf_fund_info(query)`
Информация о БПИФ: iNAV, бенчмарк, категория, премия к NAV.
- **Принимает:** `query` — тикер (`"TMOS"`, `"SBMX"`, `"TGLD"`, `"TMON"`).
- **Возвращает:** `{secid, shortname, isin, group, type, issuedate, emitent_id, emitent_title, category, category_ru, benchmark, benchmark_name, inav_ticker, inav_price, inav_updatetime, fund_price, premium_discount_pct}`.
  - `category`: `equity_russia`, `equity_foreign`, `equity_sector`, `bond_gov`, `bond_corp`, `money_market`, `commodity`, `fx`, `mixed`;
  - `inav_price` — индикативная NAV с доски INAV (только в торговые часы);
  - `premium_discount_pct` — премия/дисконт: `> 0` = премия (рыночная цена выше NAV), `< 0` = дисконт.
- **Пример:** `etf_fund_info("TMOS")` → `{"secid":"TMOS","category":"equity_russia","benchmark":"IMOEX","benchmark_name":"Индекс МосБиржи","inav_ticker":"TMOSA","fund_price":5.65,"emitent_title":"Т-Капитал",...}`
- **Использование:** понять тип фонда, бенчмарк для сравнения, текущую премию/дисконт.

### 🧮 `etf_premium_discount(query)`
Текущая премия/дисконт БПИФ к iNAV (индикативная чистая стоимость).
- **Принимает:** `query` — тикер (`"TMOS"`, `"SBMX"`, `"TGLD"`).
- **Возвращает:** `{secid, fund_price, inav_ticker, inav_price, premium_discount_pct, inav_updatetime, note}`.
  - `premium_discount_pct` > 0 = премия, < 0 = дисконт;
  - `inav_price` = `None` вне торговых часов.
- **Пример:** `etf_premium_discount("TMOS")` → `{"secid":"TMOS","fund_price":5.65,"inav_ticker":"TMOSA","inav_price":5.63,"premium_discount_pct":0.36,...}`
- **⚠️ iNAV:** данные только во время торгов (09:50–18:40 МСК). Вне сессии `inav_price` = `null`.

### 🧮 `etf_tracking_error(query, days=90)`
Трекинг-ошибка БПИФ относительно индекса-бенчмарка за N дней.
- **Принимает:** `query` — тикер фонда; `days` — период (90 по умолчанию).
- **Возвращает:** `{secid, benchmark, benchmark_name, period_days, fund_return_pct, benchmark_return_pct, excess_return_pct, tracking_error_ann_pct, trading_days, start_date, end_date}`.
  - `tracking_error_ann_pct` — annualized σ(r_fund − r_bench), %;
  - `excess_return_pct` — доходность фонда минус доходность бенчмарка за период.
  - Бенчмарк определяется автоматически из маппинга (TMOS→IMOEX, SBMX→MOEXTR, TGLD→GOLD...).
- **Пример:** `etf_tracking_error("TMOS", 90)` → `{"secid":"TMOS","benchmark":"IMOEX","fund_return_pct":2.17,"benchmark_return_pct":-2.80,"excess_return_pct":4.97,"tracking_error_ann_pct":18.12,...}`
- **Использование:** оценка качества слежения за индексом; высокий tracking error может указывать на дивидендные распределения, ребалансировку или отсутствие полного совпадения с индексом.

### 🧮 `etf_screener(...)` 
Скринер БПИФ/ETF на MOEX с фильтрацией по множеству параметров и расчётом технических индикаторов.

Загружает все фонды с бордов TQIF/TQTF, обогащает метаданными из `ETF_BENCHMARK_MAP`, рассчитывает RSI, Stochastic, MA, MACD, ADX, Bollinger, ATR, VWAP, CCI, Williams %R, Ichimoku, Parabolic SAR, Momentum/ROC, CMF, Beta.

- **Принимает (все опционально):**
  - `category` — класс активов: `equity_russia`, `equity_foreign`, `equity_sector`, `equity_dividend`, `bond_gov`, `bond_corp`, `money_market`, `commodity`, `fx`, `mixed`
  - `emitent` — управляющая компания: `Т-Капитал`, `Сбер`, `Альфа`, `ВТБ`
  - `benchmark` — тикер бенчмарка: `IMOEX`, `GOLD`, `RGBITR`
  - `currency` — валюта: `SUR`, `USD`, `EUR`, `CNY`, `HKD`
  - `price_min/price_max` — цена фонда (₽)
  - `volume_min/volume_max` — среднедневной объём торгов (₽)
  - `spread_max` — макс. Bid-Ask spread (%)
  - `volatility_min/volatility_max` — годовая волатильность (%)
  - `sharpe_min/sharpe_max` — коэффициент Шарпа
  - `beta_min/beta_max` — бета (относительно IMOEX)
  - `performance_min/performance_max` — доходность (%)
  - `performance_period` — период: `1m`, `3m`, `6m`, `1y`, `ytd`
  - `rsi_min/rsi_max` — RSI(14)
  - `ma_signal` — `golden_cross`, `death_cross`
  - `adx_min` — минимальный ADX (сила тренда)
  - `macd_signal` — `bullish`, `bearish`
  - `stochastic_min/stochastic_max` — Stochastic %K(14,3,3)
  - `cci_min/cci_max` — CCI(20)
  - `williams_min/williams_max` — Williams %R(14) (−100..0)
  - `ichimoku_signal` — `bullish`, `bearish`, `in_cloud`
  - `psar_direction` — `long`, `short`
  - `cmf_signal` — `buying_pressure`, `selling_pressure`, `neutral`
  - `roc_min/roc_max` — Rate of Change(10), %
  - `premium_discount_max` — макс. премия/дисконт к NAV (%)
  - `tracking_error_max` — макс. трекинг-ошибка (%)
  - `include_indicators` — рассчитывать TA (default `true`)
  - `sort_by` — сортировка: `performance`, `volatility`, `sharpe`, `volume`, `spread`, `premium`, `rsi`, `adx`, `beta`, `stochastic`, `cci`, `williams`, `roc`, `cmf`, `momentum`
  - `sort_desc` — `true` = по убыванию (default)
  - `limit` — максимум результатов (1..200, default 15)

- **Возвращает:** `{count_shown, count_total_matching, count_all_funds, funds[]}`.
  Каждый фонд: `{secid, shortname, isin, emitent, category, category_ru, benchmark, benchmark_name, currency, price, change_pct, bid, ask, spread_pct, value_today, vol_today, inav_price, premium_discount_pct, performance_1m/3m/6m/1y, ytd, volatility_ann, sharpe, max_drawdown, beta, rsi_14, stochastic_k, stochastic_d, ma_50, ma_200, ma_signal, macd, macd_signal_line, macd_histogram, macd_signal, bollinger_pct, bollinger_width, adx, trend_strength, atr_14, atr_14_pct, cci_20, williams_r, ichimoku_signal, psar, psar_direction, momentum_10, roc_10, cmf_20, cmf_signal}`.

- **Примеры:**
  - `etf_screener(category="equity_russia")` → все российские акционные фонды
  - `etf_screener(category="commodity", sort_by="performance")` → сырьевые, отсортированные по доходности
  - `etf_screener(ma_signal="golden_cross", cci_min=100, cmf_signal="buying_pressure")` → фонды с бычьими TA-сигналами
  - `etf_screener(sharpe_min=1.0, volatility_max=20)` → фонды с Sharpe > 1 и волатильностью < 20%
  - `etf_screener(ma_signal="golden_cross", adx_min=25)` → сильный восходящий тренд
  - `etf_screener(emitent="Т-Капитал")` → все фонды Т-Капитала
  - `etf_screener(benchmark="IMOEX")` → фонды, следующие за индексом МосБиржи

- **Использование:** поиск фондов по инвестиционным критериям; сравнение фондов одного класса; мониторинг технических сигналов.

---

## Ожидания по ставке (G-кривая ОФЗ)

### 🧮 `rate_expectations(key_rate=None)`
Рыночные ожидания по ключевой ставке из кривой бескупонной доходности ОФЗ (КБД/zcyc). Только числа — интерпретация на стороне агента.
- **Принимает:** `key_rate` опц. (иначе из `cbr_key_rate`).
- **Возвращает:** `{as_of, key_rate, ruonia, curve[], signals, note}`.
  - `signals`: `slope_10y_1y`, `slope_2y_3m`; **брутто**-спреды `short_vs_key_1y/05y`, `priced_cut_1y_pp_gross` (⚠️ включают срочную премию, не чистое ожидание); якорь `short_vs_ruonia_1y`; форварды `fwd_1y_in_1y`, `fwd_1y_in_2y`, `fwd_3m_in_1y`; `inverted`; машинная метка `read` (`cuts_priced`|`hikes_priced`|`flat`, деадбенд ±0.25 пп).
- **Пример:** `slope_10y_1y` 2.72; `short_vs_key_1y` −0.69; `fwd_1y_in_1y` 14.48; `read` `cuts_priced`.

### 🧮 `curve_yield(years)`
Доходность G-кривой ОФЗ на произвольном сроке (% годовых) — привязка кривой к дюрации бумаги. **Точная NSS-модель MOEX** (Нельсон-Сигель + 9 гауссовых поправок), сверена с узлами `yearyields` до **0.0000 пп**; гладкая на изгибах и **экстраполирует за 20 лет**.
- **Пример:** `curve_yield(5.9)` → ~15.69%; `curve_yield(30)` → ~16.79% (экстраполяция).

---

## Портфель (доменные отчёты)

Портфель ВСЕГДА передаётся параметром `assets` (markdown). **P&L приблизительный** (средняя цена покупки, без полученных купонов/дивидендов и налогов).

**Формат `assets`:**
```
refresh date: 2026-06-27        # опц.
# ИИС                            # '# ...'  = счёт
## Облигации                     # '## ...' = класс
- ОФЗ 26249: 125 шт. (88,837 -> 86,100)                 # облигация — цена в % номинала
- Сбербанк (SBER): 51 шт. (321,26 ₽ -> 301 ₽)           # акция/фонд — цена в ₽
- ГТЛК (RU000A10C6F7): 14 шт. (101,69 -> 100,80)        # ISIN в скобках — для надёжного резолва
```
Тип бумаги (акция/облигация) определяется автоматически по ISIN/тикеру на MOEX. Ключ резолва: тикер/ISIN из скобок → номер ОФЗ → само название.

### 🧮 `portfolio_snapshot(assets)`
Главная ручка — полный снимок портфеля. Включает дивидендную доходность акций, спред облигаций к кривой и реальную доходность.
- **Возвращает:** `{as_of, key_rate, inflation_pct, inflation_source, total_value, total_cost, pnl, pnl_pct, positions[], allocation[], rate_risk, income, income_risk}`.
  - `inflation_pct` — фактическая CPI (% г/г) из Росстат; `inflation_source` = `"cpi"` (если CPI доступна) или `"key_rate_proxy"` (fallback);
  - `positions[]` — `{name, secid, account, bucket, qty, price, value, weight_pct, pnl_pct, change_pct, ytm, duration_years, spread_to_curve_pp, div_yield_pct}`;
    - `spread_to_curve_pp` — спред YTM облигации к G-кривой на сопоставимой дюрации (п.п.);
    - `div_yield_pct` — дивидендная доходность акции (последний объявленный дивиденд / цена);
  - `allocation[]` — по корзинам (Длинные ОФЗ / Фонды акций / Акции / Корп. облигации / Денежный рынок);
  - `rate_risk` — `{portfolio_mod_duration_years, per_plus_1pp_pct/rub, per_minus_1pp_pct/rub}`;
  - `income` — `{annual_coupons, annual_money_market, annual_dividends, annual_total_est, running_yield_pct}`;
  - `income_risk` — `{running_yield_pct, inflation_pct, inflation_source, real_yield_est_pct}` — реальная доходность портфеля (running_yield − CPI Росстат).

### 🧮 `portfolio_rate_whatif(delta_pp, assets)`
Что станет с портфелем при сдвиге доходностей облигаций на `delta_pp` п.п.
- **Возвращает:** `{delta_pp, portfolio_value_change_rub, portfolio_value_change_pct, new_total_value, bond_detail[]}`.

### 🧮 `portfolio_income_calendar(assets)`
Ближайшие поступления: следующий купон по каждой облигации + последний объявленный дивиденд.
- **Возвращает:** `{events:[{date, type, name, amount_rub}]}` (отсортировано по дате).

### 🧮 `portfolio_movers(assets)`
Кто вырос/просел.
- **Возвращает:** `{day_losers, day_gainers, worst_vs_cost, best_vs_cost}` (топ-3 в каждую сторону, по дневному изменению и по P&L против цены покупки).

---

## Опционный калькулятор MOEX

API: `iss.moex.com/iss/apps/option-calc/v1`. Расчёт Greeks, IV, волатильных
кривых, опционных стратегий и гарантийного обеспечения по данным FORTS.

### 🟢 `option_calc_assets(asset_type=None, asset_subtype=None, query=None)`
Список базовых активов опционного калькулятора.
- **Принимает:** `asset_type` (`'commodity'|'currency'|'futures'|'index'|'share'`), `asset_subtype` (`'commodity'|'currency'|'index'|'share'`, для фьючерсов), `query` — фильтр по названию (макс. 8 символов).
- **Возвращает:** `[{asset_code, title, asset_type, asset_subtype}]`.
- **Пример:** `option_calc_assets(asset_type='index')` → `[{asset_code:"IMOEX", title:"IMOEX (Индекс МосБиржи)", ...}]`.

### 🟢 `option_calc_asset_detail(asset_code, asset_type=None)`
Описание базового актива.
- **Принимает:** `asset_code` (`'Si'`, `'GAZR'`, `'RTS'`), `asset_type`.
- **Возвращает:** `{asset_code, title, asset_type, asset_subtype}`.

### 🟢 `option_calc_futures(asset_code, expiration_date=None)`
Фьючерсы для базового актива.
- **Принимает:** `asset_code`, `expiration_date` (`'YYYY-MM-DD'`, опц.).
- **Возвращает:** `[{futures_code, asset_code, asset_type, expiration_date}]`.
- **Пример:** `option_calc_futures('Si')` → `[{futures_code:"SIU6", expiration_date:"2026-09-18", ...}, ...]`.

### 🟢 `option_calc_options(asset_code, asset_type=None, expiration_date=None, series_type=None, strike=None, option_type=None)`
Опционы на базовый актив с фильтрами.
- **Принимает:** `asset_code`, `asset_type`, `expiration_date`, `series_type` (`'W'|'M'|'Q'`), `strike`, `option_type` (`'call'|'put'`).
- **Возвращает:** `[{secid, asset_code, asset_type, futures_code, expiration_date, series_type, strike, option_type}]`.
- **Пример:** `option_calc_options('Si', option_type='call', strike=84000)` → все call-опционы Si со страйком 84000.

### 🟢 `option_calc_option_brief(asset_code, secid, asset_type=None, days_until_expiring=None, underlying_price=None, volatility=None)`
Сводка по опциону: Greeks, теор. цена, IV.
- **Принимает:** `asset_code` (`'Si'`), `secid` (`'Si70000BI6A'`), `asset_type`, `days_until_expiring` (override дней до экспирации), `underlying_price` (override цены БА, ₽), `volatility` (override IV, %).
- **Возвращает:** `{secid, delta, gamma, vega, theta, rho, theorprice, volatility, underlying_price, days_until_expiring, fee, expiring_date, lastprice, settleprice, underlying_asset, underlying_type}`.
- **Пример:** `option_calc_option_brief('Si', 'Si84000BC6A')` → `{secid:"Si84000BC6A", delta:-0.42, gamma:0.00003, theta:-15.2, volatility:22.5, theorprice:1850.0, ...}`.
- **What-if:** передайте `underlying_price` и/или `volatility` для расчёта при других условиях.

### 🟢 `option_calc_series(asset_code, asset_type=None)`
Серии опционов (циклы экспираций) для базового актива.
- **Принимает:** `asset_code` (`'Si'`, `'RTS'`).
- **Возвращает:** `[{optionseries_code, asset_code, asset_type, futures_code, series_type, expiration_date, central_strike, call: {volume_rub, volume_contracts, openposition, oichange}, put: {...}, updatetime}]`.
- **Пример:** `option_calc_series('Si')` → `[{optionseries_code:"SI-9.26M100926XA", expiration_date:"2026-09-10", central_strike:84000, ...}, ...]`.

### 🟢 `option_calc_series_detail(asset_code, optionseries_code, asset_type=None)`
Описание одной серии опционов.
- **Возвращает:** то же, что `option_calc_series`, но для одной серии.

### 🟢 `option_calc_series_options(asset_code, optionseries_code, asset_type=None, strike=None, option_type=None)`
Опционы в конкретной серии.
- **Принимает:** `asset_code`, `optionseries_code` (из `option_calc_series`), `strike` (фильтр), `option_type`.
- **Возвращает:** `[{secid, asset_code, asset_type, futures_code, expiration_date, series_type, strike, option_type}]`.

### 🟢 `option_calc_optionboard(asset_code, optionseries_code, asset_type=None, rows=None)`
Доска опционов: страйки с Greeks, IV, bid/ask, теор. цена.
- **Принимает:** `asset_code`, `optionseries_code`, `asset_type`, `rows` — кол-во страйков от центрального (опц.).
- **Возвращает:** `{call: [{secid, strike, delta, gamma, vega, theta, rho, theorprice, theorprice_rub, last, bid, offer, volatility, intrinsic_value, timed_value, numtrades}], put: [same]}`.
- **Пример:** `option_calc_optionboard('Si', 'SI-9.26M100926XA', rows=5)` → 5 страйков по каждой стороне.

### 🟢 `option_calc_volatility_graph(asset_code, optionseries_code, asset_type=None)`
График волатильности (smile) для серии опционов.
- **Возвращает:** `[{strike, volatility}]` — кривая implied volatility по страйкам.
- **Пример:** `option_calc_volatility_graph('Si', 'SI-9.26M100926XA')` → smile с U-образной формой.

### 🧮 `option_calc_portfolio(asset_code, positions, delta_sigma=None, date_of_calculation=None)`
Расчёт опционного портфеля: агрегированные Greeks, P&L, гарантийное обеспечение.
- **Принимает:** `asset_code` (`'Si'`), `positions` — список позиций `[{secid, type, quantity, price?, volatility?, netted_im?}]`.
  - `secid` — код из `option_calc_options` или `moex_futures_list` (НЕ конструировать вручную!).
  - `type` — **обязательно**: `'option'` или `'futures'`. Без этого API считает что это фьючерс и вернёт 422.
  - `quantity`: положительное = покупка, отрицательное = продажа.
  - `delta_sigma` (сдвиг волатильности, %, для what-if), `date_of_calculation` (`'YYYY-MM-DD'`, для what-if).
- **Возвращает:** `{positions: [{secid, type, quantity, price, delta, gamma, vega, theta, rho, profit_and_loss, profit_and_loss_rub, fee, theorprice, strike, volatility, expiration_date, days_until_expiring, expired}], total: {delta, gamma, vega, theta, rho, profit_and_loss, profit_and_loss_rub, fee}, initial_margin}`.
- **Пример:** `option_calc_portfolio('Si', [{secid:'Si84000BI6', type:'option', quantity:-10}, {secid:'SIU6', type:'futures', quantity:5}])` → совокупный портфель с Greeks.
- **What-if:** `delta_sigma=-5` — что будет, если IV упадёт на 5%.

### 🧮 `option_calc_portfolio_graph(asset_code, positions, indicator, delta_sigma=None, date_of_calculation=None)`
График P&L или Greeks в зависимости от цены базового актива.
- **Принимает:** `asset_code`, `positions` (как в `option_calc_portfolio`, **с полем `type`**), `indicator` — `'profit_and_loss'|'delta'|'gamma'|'vega'|'theta'|'rho'`, остальные параметры опциональны.
- **Возвращает:** `{now: [{underlying_price, value}], on_expiration: [{underlying_price, value}], on_what_if: [{underlying_price, value}]}`.
  - `now` — текущий момент (с текущей IV);
  - `on_expiration` — на дату экспирации;
  - `on_what_if` — при заданном сдвиге IV / дате (если переданы).
- **Пример:** `option_calc_portfolio_graph('Si', [{secid:'Si84000BC6A', quantity:-10}], 'profit_and_loss')` → точки для построения P&L-графика стратегии.

### 🧮 `option_calc_initial_margin(positions)`
Гарантийное обеспечение для произвольного набора позиций (кросс-БА).
- **Принимает:** `positions` — `[{secid, type, quantity, price, netted_im?}]`.
  - `secid` — любой код фьючерса/опциона FORTS.
  - `type` — **обязательно**: `'option'` или `'futures'`.
  - `netted_im` — неттирование ГО (по умолч. `true`).
- **Возвращает:** `{initial_margin: float}` (₽).
- **Пример:** `option_calc_initial_margin([{secid:'SIU6', type:'futures', quantity:1, price:84000}])` → `{"initial_margin":13260.16}`.

## Технические заметки

- **Сеть:** `iss.moex.com` капризен (случайные таймауты) — в `session.py` зашиты ретраи. `cbr.ru` стабилен.
- **Шаблоны ISS** грузятся один раз при старте сервера (кэш на уровне класса aioboy/moex).
- **Дивиденды** — ISS-эндпоинт `/securities/{secid}/dividends` пуст (данные не отдаётся), «правильный» эндпоинт пейволлен. Календарь и история берутся скрейпингом со smart-lab.ru (`ru_finance/smartlab.py`, кэш в памяти на 4 ч).
- **G-кривая** — NSS-модель MOEX из блока `params` zcyc, узлы `a_i = a_{i-1}+0.6·1.6^(i-1)`, масштабы `b_i = 0.6·1.6^(i-1)`, параметры в б.п. (÷10000), КБД = `100·(e^GT−1)`.
- **Соответствие ToS:** личное использование, задержанные данные, без перераспространения.
