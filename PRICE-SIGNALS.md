# Electricity price signals and published future prices (v0.4.0)

These optional sensors guide flexible loads using **published Estonian electricity prices**. They are independent of delayed meter readings, gas prices, and the historical cost importer. They do not operate devices by themselves.

## Enable and install

1. Replace `/config/custom_components/estfeed_energy/` with this package's directory and merge `price_signals` from `price-signals-example.yaml` into your existing `estfeed_energy:` configuration. Keep the existing credentials, `history_start`, and dated billing tariff entries. There must be only one `estfeed_energy:` block.
2. Check Home Assistant configuration and restart Home Assistant. Existing quantity and cost statistics keep the same IDs. No rescan/rebuild is required for these new sensors.
3. The first public price request runs after startup. If necessary, run `estfeed_energy.sync` in Developer Tools → Actions. The public-price request shares the integration's six-second request spacing and retry/backoff with metering requests.
4. For the price dashboard, install **ApexCharts Card v2.2 or later** through HACS, refresh your browser, and paste `dashboards/estfeed-electricity-prices.yaml` into a manual dashboard card. It uses built-in tiles and Markdown, with no Mushroom requirement. Configure Home Assistant and your profile/browser for Europe/Tallinn to align the chart day with the price comparison day.

## Sensors

Entity IDs below are defaults. If you renamed an entity or HA adds a suffix, use its actual ID in dashboards/automations.

| Entity | Value |
| --- | --- |
| `sensor.estfeed_electricity_price_level` | `very_low`, `low`, `normal`, `high`, `very_high` |
| `sensor.estfeed_electricity_price_high_low` | `low` or `high` |
| `sensor.estfeed_electricity_current_price` | Current 15-minute price, EUR/kWh |
| `sensor.estfeed_electricity_previous_price` | Previous 15-minute price, EUR/kWh |
| `sensor.estfeed_electricity_next_price` | Next 15-minute price, EUR/kWh, when published |
| `sensor.estfeed_electricity_today_low` | Lowest price over the complete local day |
| `sensor.estfeed_electricity_today_high` | Highest price over the complete local day |
| `sensor.estfeed_electricity_today_average` | Arithmetic average over the complete local day |
| `sensor.estfeed_electricity_tomorrow_average` | Arithmetic average when all of tomorrow is published |
| `sensor.estfeed_electricity_history_average` | Arithmetic average over the available recent-history intervals |
| `sensor.estfeed_electricity_price_forecast` | Tomorrow availability; attributes contain today's and tomorrow's published intervals |

The five-level sensor deliberately uses lowercase underscore states for reliable automation conditions. Home Assistant marks the signal/current/next sensor unavailable when its required value is missing. Missing data never becomes zero or a stale prior-quarter value. The previous sensor can still be available when the current one is unavailable.

## Price basis and the always-cheap floor

All prices use:

```
(market EUR/kWh + price_signals.margin) × price_signals.multiplier
```

Defaults are the **pre-tax market price**, with margin `0` and multiplier `1`. This is not automatically the total bill price. The signal settings are intentionally separate from the dated historical billing tariffs: use your current electricity margin and multiplier here if you want signals and the graph to include them. The same adjustment is applied to past reference prices for a like-for-like comparison under today's chosen settings. It does not change historical cost calculations. Gas settings are irrelevant here.

`cheap_price` uses that same adjusted EUR/kWh basis. The integration default is `0.05`; the example uses `0.10` (10 cents/kWh). At or below the configured value, both signals are forced to **very_low / low**, irrespective of percentile rank. It is an override threshold, not a clamp on the actual price. Negative prices remain negative. A missing current price cannot activate the override.

For example, a price of €0.04/kWh is always cheap with a €0.10 threshold, even if the rest of the day is €0.01. If margin/tax raises the adjusted price above the threshold, the relative classifier applies instead.

## Relative ranking

Each available reference group ranks the current adjusted price by percentile. Default weights are:

- Today's complete published local day: 50%.
- Tomorrow's complete published local day: 30%.
- Available prices in the previous 30 local days: 20%.

For each group, percentile = `100 × (number of lower prices + half the number of equal prices) / group size`. The resulting percentiles are averaged using the weights. This handles negative prices without dividing by a possibly zero/negative average; a flat set of equal prices ranks at 50. History contributes 20% as a group, rather than overwhelming tomorrow just because it contains more intervals. All quarter-hours count equally, regardless of household consumption.

| Weighted percentile | Five-level state | Two-state default |
| --- | --- | --- |
| ≤20 | very_low | low |
| >20 to 40 | low | low |
| >40 to 60 | normal | low |
| >60 to 80 | high | high |
| >80 | very_high | high |

Change `level_percentiles` for the four five-level boundaries, `low_percentile` for the separate high/low cutoff, and the three weights for emphasis. Weights must be nonnegative with a positive total; they need not sum to 1. If tomorrow is not yet published, its weight is omitted and the remaining weights are normalized. Partially published days appear in the graph but are omitted from day-based ranking and min/max/average sensors until complete. History uses available values and explicitly reports coverage.

If no weighted references exist, the relative classification is unavailable. A known current price below the absolute cheap threshold can still return very_low / low. If all selected references are equal to the current price, the result is normal, unless the cheap override applies. These are relative signals, not predictions or an optimizer: the ranking can change when tomorrow is published or missing history arrives, even within the current quarter.

## Updates, persistence and diagnostics

Prices are fetched every 15 minutes; failed fetches retry after 5 minutes, in addition to the HTTP client's bounded retries. Cached prices switch at :00/:15/:30/:45 independently of the network and of consumption backfill. The minute timer also refreshes the clock view. Previously published interval values survive outages and restarts, but never extend beyond their exact timestamps.

The public Estfeed electricity endpoint is primary for these sensors. Elering's public NPS endpoint fills missing values when available. Historical hourly prices are normalized to quarters using the existing v0.3.3 logic; genuine gaps are not interpolated. Existing billing price history seeds the new cache. Missing recent reference history is fetched in at most one seven-day window per refresh, newest first. Failed windows do not prevent older windows from being attempted, and remaining gaps are revisited. `history_days` is configurable from 1 to 365. Several years of billing history are not automatically all weighted together; select the relevant recent window.

The cache is separate at `.storage/estfeed_energy_price_signals`. The historical ledger remains unchanged. The forecast's two interval arrays are excluded from Recorder attribute history; they remain available to dashboards. Changing `recorder.purge_keep_days` is unnecessary.

The current-price and both signal entities expose:

- `interval_start`, `interval_end`, `timezone`.
- `relative_percentile`, `relative_level`, `component_percentiles`, `effective_weights`.
- `reference_averages`, `history_coverage`, `history_intervals`, `history_expected_intervals`, `reference_provisional`.
- `cheap_price`, `cheap_price_override`, `margin`, `multiplier`.
- `today_complete`, `tomorrow_status`, expected/available interval counts.
- `last_successful_fetch`, `last_error`, `history_error`.

Dates are grouped in Europe/Tallinn by default. Spring/autumn clock changes correctly produce 92/100 quarters instead of 96. Internal UTC timestamps keep repeated local times distinct.


Official component references:
- https://github.com/RomRider/apexcharts-card (future `data_generator`, step curves and `extend_to`).
- https://developers.home-assistant.io/docs/core/entity/sensor/ (enum sensor states/options).
- https://developers.home-assistant.io/docs/integration_listen_events/ (quarter-hour callbacks).
