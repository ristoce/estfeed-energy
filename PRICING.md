# Prices and costs — version 0.3.3

This update adds optional, historical **consumption costs in EUR**. Existing electricity/gas quantity statistics and the source ledger remain compatible with versions 0.1.0 and 0.2.0. Pricing is disabled unless you configure it.

## Upgrade

1. Back up Home Assistant.
2. Replace `/config/custom_components/estfeed_energy/` with the updated directory from this ZIP, including the two new Python files `pricing.py` and `pricing_model.py`, plus all updated existing files.
3. Keep your existing credentials. Leave `history_start` unchanged unless extending it earlier as described in **UPGRADE-0.3.1.md**. Add a `pricing:` section inside your existing `estfeed_energy:` configuration, using `pricing-example.yaml` as a reference. Do not create two `estfeed_energy:` blocks.
4. Restart Home Assistant fully so it reloads the code. Check configuration after the new files have been discovered.
5. Run `estfeed_energy.sync` or wait for the first scheduled pass.
6. In Developer Tools → Statistics, find **Estfeed Electricity consumption cost … (EUR)** and/or **Estfeed Gas consumption cost … (EUR)**.
7. Edit each corresponding Energy source and select the option to use an entity/statistic tracking **total costs**. Select its matching Estfeed consumption-cost statistic. Do not select “current price” to calculate these delayed readings. If your installed frontend does not offer the external cost statistic, capture that screen and the Core version; do not manually edit Energy's storage file.

The cost statistic ID is `estfeed_energy:<normalized_eic>_consumption_cost`. The status sensor's `costs` attribute provides the exact ID, latest priced hour, first unpriced hour, unpriced-hour count, and queued historical window count. `pricing_error` records price-processing failures independently of the quantity import status.

These are external historical statistics, not new current-price sensor entities. Settings are edited in YAML; this version does not add UI sliders or a pricing configuration wizard. The package was tested with mocked Home Assistant boundaries and real Elering payloads, but the upgraded code and cost selector were not tested in your running instance.

## Choose a pricing mode

### Fixed/manual electricity

```yaml
pricing:
  electricity:
    - from: "2026-10-01T00:00:00+03:00"
      mode: fixed
      price: 0.15
      margin: 0.0
      multiplier: 1.0
```

Formula: `consumed kWh × (price + margin) × multiplier`.

Price and margin are in **EUR/kWh**: `0.01` means one cent per kWh. If you enter your final VAT-inclusive rate, leave the multiplier at 1.0. These are illustrative values, not recommended tariffs or actual contract details.

### Electricity market price plus margin

```yaml
pricing:
  electricity:
    - from: "2026-10-01T00:00:00+03:00"
      mode: spot
      margin: 0.01
      multiplier: 1.0
```

The integration retrieves Estonian (`ee`) historical day-ahead prices from `https://dashboard.elering.ee/api/nps/price`. If that request fails or omits intervals, it fills missing intervals from `https://estfeed.elering.ee/api/public/v1/energy-price/electricity` with `resolution=fifteen_minutes`. Existing primary values win when both sources return the same interval. No additional API key or Nord Pool integration is needed. Both sources use pre-VAT EUR/MWh, divided by 1000; the Estfeed VAT-inclusive fields are not used.

Formula for each quarter-hour:

`quarter-hour kWh × (market EUR/kWh + margin) × multiplier`

For modern quarter-hour market periods, those four costs are summed into the hourly cost statistic. It does **not** multiply an hourly consumption total by an unweighted average price. If your grid operator derives quarter-hour quantities by splitting an older meter's hourly total, the calculation naturally reduces to the corresponding average price. It uses the metering data supplied by the provider, without asserting that it was physically measured at quarter-hour resolution.

The electricity price API was live-tested with current quarter-hour data, a 30-day range, and October 2024 hourly data. The observed transition is 30 September 2025 at 22:00 UTC. Before that boundary, a confirmed equal price covering all four quarters can be multiplied directly by the reported hourly kWh. This fixes older meter responses that contain only hourly readings even when fifteen_minutes is requested. The rate must cover the entire hour and be identical throughout it; no quarter-hour consumption is invented. Afterwards all quarter prices and quarter-hour consumption readings are required. An absent price remains a gap, rather than being replaced with today's price or zero. Negative electricity prices are supported, so cumulative costs can decrease.

The same six-second limiter covers authenticated metering requests and both public price sources, including their retries. The fallback keeps missing/invalid intervals in the durable retry queue. The 0.3.3 upgrade rewinds electricity pricing history once and recalculates/rebases costs using the same statistic IDs, preserving quantity history and gas pricing. Existing tariff dates, margins and multipliers remain in force.

The same six-second limiter covers authenticated metering requests and public price requests. Quarter-hour electricity consumption and historical prices are cached persistently. Spot pricing therefore adds API requests and disk usage; enable it from the date for which you actually want costs, rather than requesting years unnecessarily.

### Manual gas price, optionally plus margin

```yaml
pricing:
  gas:
    - from: "2026-10-01T00:00:00+03:00"
      mode: fixed
      unit: kWh
      price: 0.07
      margin: 0.005
      multiplier: 1.0
```

Use `unit: m3` for a price in EUR/m³ instead. Both price and margin must use the selected unit. The importer uses Elering's actual kWh or m³ reading; it does not convert gas using a guessed fixed factor.

### Gas daily market price from Estfeed

```yaml
pricing:
  gas:
    - from: "2026-10-01T00:00:00+03:00"
      mode: spot
      unit: kWh
      margin: 0.01
      multiplier: 1.0
```

This uses the public endpoint supplied by the user:

`https://estfeed.elering.ee/api/public/v1/energy-price/gas`

The request includes `startDateTime`, `endDateTime`, and `resolution=one_day`; no Authorization header or extra key is needed. It shares the integration's six-second request throttle and retry handling.

The API returns `eurPerMwh`, `eurPerMwhWithVat`, `centsPerKwh`, `centsPerKwhWithVat`, `fromDateTime`, and `toDateTime`. The implementation always selects **`eurPerMwh / 1000`**, the pre-VAT price in EUR/kWh. It then calculates:

`reported gas kWh × (daily Estfeed EUR/kWh + margin) × multiplier`

The VAT-inclusive API fields are deliberately unused. A multiplier of 1.0 therefore adds no tax; configure a suitable multiplier or keep your final inclusive price in fixed mode. Gas spot mode requires `unit: kWh`; manual gas rates still support m3.

Explicit price-interval boundaries are matched to consumption timestamps. Observed daily price periods run from 07:00 Tallinn until just before 07:00 the following day, matching the daily gas readings. The code handles nanosecond inclusive end timestamps, 23/25-hour daylight-saving days, and extra following-day rows returned outside the requested range. It queries one extra preceding day to cover request windows starting before the gas day begins.

This mode means **Estfeed's published daily gas price plus your margin**. The JSON response does not identify the underlying exchange/index or its methodology. It has not been established that this series equals a particular supplier's billing index. It does **not** calculate a monthly EGSI/TTF tariff or a consumption-weighted monthly contract price. If your contract uses a monthly index, use dated manual monthly rates until that exact formula and source are configured. Do not assume the daily calculation will exactly reproduce such an invoice.

To switch an existing configured gas tariff from fixed to spot, retain its earliest `from` timestamp, set `mode: spot`, use `unit: kWh`, and remove the now-unused `price` field. This intentionally recalculates costs under that tariff from its effective date. Alternatively append a future-dated spot entry to preserve previous fixed costs.

## Tax, margins and later changes

`multiplier` applies to **both** the base price and margin. For example, `1.24` would apply a 24% uplift; it is not automatically selected, and no historical tax rates are assumed. Enter rates consistently: either before tax with the appropriate multiplier, or already inclusive with multiplier 1.0. If different components have different tax treatment, combine them into a suitable final fixed rate or use a margin expressed consistently with this formula.

To change a price/margin later, **append an effective-dated tariff** instead of editing the previous one:

```yaml
electricity:
  - from: "2026-10-01T00:00:00+03:00"
    mode: spot
    margin: 0.01
    multiplier: 1.0
  - from: "2026-11-01T00:00:00+02:00"
    mode: spot
    margin: 0.012
    multiplier: 1.0
```

Each entry applies from its timestamp until the next entry. Timestamps must have an explicit UTC offset and align to a whole hour. Tallinn's offset changes seasonally; the examples reflect that. Gas tariff changes use the date/time of its provider-labelled daily lump, not an invented hourly distribution.

Editing a historical entry intentionally recalculates previously stored costs on the next pricing pass. Version 0.3.2 allows the earliest configured `from` date to move earlier; see **UPGRADE-0.3.2.md**. Moving it later remains blocked to avoid leaving old costs outside the configured tariff history. Removing a pricing section stops updates and preserves already-imported costs; it does not delete statistics. Restart after YAML changes. Prices/margins apply to all discovered meters of that commodity; different per-meter contracts are not implemented in this version.

## Missing data, corrections and recovery

- Energy-quantity import continues independently when the price API fails.
- Fixed costs are derived directly from stored hourly quantities; no extra price calls are needed.
- Electricity spot costs require four matching quarter quantities and prices. The quarter quantities must sum to the stored hourly quantity, or the hour remains unpriced until revisions converge.
- Gas spot costs use the price interval containing the reported daily consumption timestamp; unpublished current-day prices do not block already-priced consumption.
- The preceding 14 days of electricity quarter quantities and commodity prices are refreshed on a nominal six-hour schedule. Older pricing history is fetched in 30-day windows; incomplete requests remain queued. One older window per day is audited after initial cost backfill.
- The cost worker checks for remaining work independently of the normal quantity polling interval, with a five-minute pause while gaps remain. Network calls still depend on its recent/history/retry schedules; it does not redownload every price every minute.
- A known previous cost is preserved during a temporary data omission. A newly unpriceable hour has no cost increment yet, and is listed in diagnostics. Dashboard cost totals can consequently be incomplete until the gaps are filled.
- Corrections rebuild subsequent cumulative cost sums. Repeated imports upsert the same timestamps and do not add costs twice.
- `estfeed_energy.rescan` also restarts electricity and gas spot-cost historical downloads. `estfeed_energy.rebuild_statistics` restores cached cost statistics without API calls.

## Scope and validation

This calculates the configured **variable consumption cost**. It does not automatically obtain your supplier margin, grid tariff, tax treatment, standing charge, day/night network rates, or export compensation. A single constant per-unit surcharge can be included in the margin, but changing network tariffs require matching dated entries. Exact invoice agreement depends on configuring all applicable components and on the supplier's rounding rules. Currency is EUR; use a matching Home Assistant currency when viewing these costs.

69 automated tests pass, including original quantity tests and new pricing, tariff-boundary, negative-price, missing-quarter, unit conversion, migration, correction and failure-isolation tests. In a live-data calculation for the local day 4 October 2026, 14.718 kWh matched 96 market-price intervals. With an **illustrative** 0.01 EUR/kWh margin and multiplier 1.0, calculated cost was **0.64039625 EUR**, before any other charges.

Official sources:
- Elering price endpoint: https://dashboard.elering.ee/api/nps/price
- Elering market-resolution guidance: https://www.elering.ee/en/transition-15-minute-market-time-unit
- Home Assistant Recorder source: https://github.com/home-assistant/core/blob/2026.9.3/homeassistant/components/recorder/statistics.py

