# Справочник инструментов `ru-finance`

38 ручек. Полное описание сигнатур, входов/выходов и примеров. Краткий обзор — в
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
Определить, как ISS адресует бумагу (акция/облигация/фонд/индекс).
- **Принимает:** `query` — тикер/ISIN/номер ОФЗ/название (`"SBER"`, `"26253"`, `"RU000A10C6F7"`).
- **Возвращает:** `{secid, engine, market, board, type, shortname, isin, group, is_traded}`.
- **Пример:** `moex_resolve("26253")` → `{"secid":"SU26253RMFS3","market":"bonds","board":"TQOB","type":"ofz_bond",...}`

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

### 🟢 `moex_candles(query, frm, till, interval="24")`
Свечи OHLCV за период.
- **Принимает:** `query`; `frm`/`till` (`"YYYY-MM-DD"`); `interval` — `1,10,60`(час)`,24`(день)`,7`(нед)`,31`(мес)`,4`(кв).
- **Возвращает:** список `{begin, open, high, low, close, value, volume}`.
- **Пример:** `moex_candles("SBER","2026-06-22","2026-06-26","24")`

### 🟢 `moex_history(query, frm, till)`
Дневная история торгов (для доходностей/волатильности/просадок).
- **Принимает:** `query`; `frm`/`till` (`"YYYY-MM-DD"`).
- **Возвращает:** список строк истории (TRADEDATE, CLOSE, VOLUME, VALUE...).
- **Пример:** `moex_history("26253","2025-12-01","2026-06-27")`

### 🟢 `moex_search_endpoints(pattern)`
Найти ISS-эндпоинт (шаблон) по подстроке пути — для доступа к данным без готовой ручки.
- **Принимает:** `pattern` — `"/candles"`, `"turnovers"`, `"/dividends"`.
- **Возвращает:** `[{id, path, variables}]`. `id` → в `moex_query`.
- **Пример:** `moex_search_endpoints("turnovers")`

### 🟢 `moex_query(template_id, vars=None, params=None)`
Запасной доступ к ЛЮБОМУ из ~252 эндпоинтов ISS.
- **Принимает:** `template_id` (из `moex_search_endpoints`); `vars` — переменные пути (`{engine, market, board, security}`); `params` — query-параметры (`{from, till, ...}`).
- **Возвращает:** `{block: [строки]}`.
- **Пример:** `moex_query(322, {"engine":"stock"}, {})`

### 🟢 `moex_company_info(query)`
Справка об организации по ИНН/ОГРН/названию (данные НРД/CCI).
- **Принимает:** `query` — ИНН, ОГРН или фрагмент названия.
- **Возвращает:** `{companies: [{basis_company_id, inn, ogrn, name_short_ru, name_full_ru, lei_code, ...}]}`.
- **Пример:** `moex_company_info("Сбербанк")`

### 🟢 `moex_company_info_by_id(company_id)`
Справка об организации по внутреннему ID MOEX (basis_company_id).
- **Принимает:** `company_id` — числовой ID (узнаётся из `moex_company_info`).
- **Возвращает:** `{inn, ogrn, name_short_ru, name_full_ru, lei_code, egrul_date, ...}`.

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

---

## Облигационная математика

### 🧮 `bond_report(query)`
Глубокий разбор облигации: метрики + сценарии по ставке + реальная доходность.
- **Принимает:** `query` — номер ОФЗ/ISIN.
- **Возвращает:**
  - `bond` — как `moex_bond`;
  - `scenarios` — `{macaulay_years, breakeven_yield_rise_pp, scenarios:[{delta_pp, total_return_pct}]}` (полный доход за год при сдвиге доходности на ±п.п. + точка безубытка);
  - `real_return` — `[{inflation_pct, real_return_pct}]` (доходность к погашению за вычетом инфляции).
- **Пример:** `bond_report("26253")` → при −2 п.п. годовой доход ≈ +27%, безубыток при росте доходности до ~+3.4 п.п.

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
- ОФЗ 26249: 125 шт. (88,837 % -> 86,100 %)        # '%' → облигация (цена в % номинала)
- Сбербанк (SBER): 51 шт. (321,26 ₽ -> 301 ₽)      # без '%' → акция/фонд (цена в ₽)
- ГТЛК (RU000A10C6F7): 14 шт. (101,69 % -> 100,80 %) # тикер/ISIN в скобках — для надёжного резолва
```
Ключ резолва: тикер/ISIN из скобок → номер ОФЗ → само название.

### 🧮 `portfolio_snapshot(assets)`
Главная ручка — полный снимок портфеля.
- **Возвращает:** `{as_of, key_rate, total_value, total_cost, pnl, pnl_pct, positions[], allocation[], rate_risk, income}`.
  - `positions[]` — `{name, secid, account, bucket, qty, price, value, weight_pct, pnl_pct, change_pct, ytm, duration_years}`;
  - `allocation[]` — по корзинам (Длинные ОФЗ / Фонды акций / Акции / Корп. облигации / Денежный рынок);
  - `rate_risk` — `{portfolio_mod_duration_years, per_plus_1pp_pct/rub, per_minus_1pp_pct/rub}`;
  - `income` — `{annual_coupons, annual_money_market, annual_total_est, running_yield_pct}`.

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

## Технические заметки

- **Сеть:** `iss.moex.com` капризен (случайные таймауты) — в `session.py` зашиты ретраи. `cbr.ru` стабилен.
- **Шаблоны ISS** грузятся один раз при старте сервера (кэш на уровне класса aioboy/moex).
- **Дивиденды** — ISS-эндпоинт `/securities/{secid}/dividends` пуст (данные не отдаётся), «правильный» эндпоинт пейволлен. Календарь и история берутся скрейпингом со smart-lab.ru (`ru_finance/smartlab.py`, кэш в памяти на 4 ч).
- **G-кривая** — NSS-модель MOEX из блока `params` zcyc, узлы `a_i = a_{i-1}+0.6·1.6^(i-1)`, масштабы `b_i = 0.6·1.6^(i-1)`, параметры в б.п. (÷10000), КБД = `100·(e^GT−1)`.
- **Соответствие ToS:** личное использование, задержанные данные, без перераспространения.
