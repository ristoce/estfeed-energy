# Estfeed electricity and gas → Home Assistant Energy

A custom YAML integration for Elering's **customer portal API**. It imports historical, hourly long-term statistics into Recorder, retaining the original timestamps. It does not treat delayed consumption as live power or as a meter increment that happened when the API was polled.

## Install using HACS

Requires Home Assistant Core 2026.9.0 or newer. This is an unofficial, YAML-configured custom integration, independent of Elering and Home Assistant.

1. Open HACS → three-dot menu → **Custom repositories**.
2. Add `https://github.com/ristoce/estfeed-energy.git` with type **Integration**.
3. Find **Estfeed Energy History**, download it, and restart Home Assistant.
4. Configure your credentials and options in `configuration.yaml` / `secrets.yaml` using the examples below, then restart again. There is currently no Devices & services setup wizard.

HACS installs and updates `custom_components/estfeed_energy/`. It does not install your credentials, automations or dashboard YAML. Copy those examples separately from this repository:

- [Consumption comparison v1.8](dashboards/estfeed-consumption-comparison-v1.8.yaml)
- [Published today/tomorrow prices](dashboards/estfeed-electricity-prices.yaml)
- [Price signals and their configuration](PRICE-SIGNALS.md)
- [Example flexible-load automation](automations/price-controlled-device.yaml)
- [Configuration explanation](ESTFEED-CONFIGURATION.md)

For an existing manual installation, add this same repository to HACS and download it over the existing integration. Keep the domain `estfeed_energy`, your YAML, and `.storage/estfeed_energy*` files unchanged. Restart HA afterwards; the existing statistics and entity unique IDs are retained. Future published releases can then be installed through HACS.

##  Configuration and setup

1. **Skip this step if installed the integration via HACS:** Extract the ZIP. Copy the entire `custom_components/estfeed_energy` directory into your Home Assistant configuration directory. The result must be `/config/custom_components/estfeed_energy/manifest.json` (or the equivalent configuration path for Container/Core).
2. Add these entries to your existing `secrets.yaml`, using your replacement Elering API credentials:

   ```yaml
   estfeed_client_id: "YOUR_CLIENT_ID"
   estfeed_client_secret: "YOUR_CLIENT_SECRET"
   ```

3. Merge this into your existing `configuration.yaml`:

   ```yaml
   estfeed_energy:
     client_id: !secret estfeed_client_id
     client_secret: !secret estfeed_client_secret
     history_start: "2021-01-01"
     poll_hours: 6
     overlap_days: 14
     backfill_windows_per_run: 2
   ```

   `history_start` is an inclusive **UTC date**. The default above discovers accessible contracts from that date and starts fetching at the earliest returned contract date. For the tested credentials, access begins in October 2020. This does not prove the provider can return every reading from 2020: that query repeatedly returned a per-meter error.

   Choose `history_start` to limit the initial import. You can later move it **earlier** to extend existing history. Moving it later remains blocked because that would shrink the stored history scope. Using the defaults attempts all accessible history. No separate `sensor:` configuration is needed.

4. Ensure Recorder is enabled (normally through `default_config:`) and Home Assistant's time zone is **Europe/Tallinn**. Run Home Assistant's configuration check, then restart Home Assistant.
5. In **Developer Tools → States**, find `sensor.estfeed_import_status` and `sensor.estfeed_last_successful_sync`. Entity IDs can have a suffix if an existing entity already uses the name. The status sensor's `meters` attribute lists every imported statistic ID and the latest source timestamp for each measurement.
6. Open **Developer Tools → Statistics** and search for **Estfeed**. Recent data is imported first. Historical downloads continue in the background. Several years can take tens of minutes or longer, especially when the API fails. Older data is staged and then published together to avoid repeatedly rewriting years of totals.
7. In **Settings → Dashboards → Energy**, configure the sources below. Search for the friendly names beginning **Estfeed**, rather than selecting the diagnostic status sensors.

| Energy section | Statistic to select |
|---|---|
| Electricity grid → Grid consumption | Estfeed Electricity consumption … (kWh) |
| Electricity grid → Return to grid | Estfeed Electricity export … (kWh), if you export electricity |
| Gas consumption | Estfeed Gas consumption … (kWh), **or** the corresponding (m³) statistic |

Choose **one** gas representation. Both represent the same consumption. The kWh value comes directly from Elering; the integration does not apply a fixed kWh/m³ factor. In the tested week that conversion varied slightly across days.

The statistics use IDs such as `estfeed_energy:<normalized_eic>_consumptionkwh`, with a colon. They are external statistics, not ordinary `sensor.*` entities. This is deliberate: a conventional sensor would timestamp an update at arrival time. Do not put these values through a Riemann-sum integration or utility-meter helper to supply this dashboard.

If the statistics are missing, check the status and logs first. If they exist in Developer Tools → Statistics but the Energy picker does not offer them, stop there and check the installed Home Assistant version and statistics metadata; do not edit `.storage/energy` manually. This frontend path has not been verified.

## Example configuration of with all the supported features

```yaml
estfeed_energy: # Estfeed integration configuration
  client_id: !secret estfeed_client_id # API client ID from secrets.yaml
  client_secret: !secret estfeed_client_secret # API client secret from secrets.yaml
  # metering_point_eics: ["2222-xxxxxx-J", "3333-xxxxxx-I"] # Optional: import only these meters; omit to auto-discover
  history_start: "2020-01-01" # Import history from this date, starting at midnight UTC
  poll_hours: 6 # Fetch consumption updates every 6 hours
  overlap_days: 14 # Recheck the last 14 days for delayed or corrected readings
  backfill_windows_per_run: 2 # Process up to 2 historical consumption windows per run
  pricing: # Historical consumption cost settings
    electricity: # Electricity tariff periods
      - from: "2020-01-01T00:00:00+03:00" # Tariff start; +03:00 is a fixed UTC offset
        mode: spot # spot = market price; fixed = manually configured price
        # price: 0.10 # Required for mode: fixed; EUR/kWh before margin and multiplier
        margin: 0.0 # Supplier margin in EUR/kWh, before multiplier
        multiplier: 1.24 # Multiply spot + margin by 1.24 throughout this tariff period
    gas: # Gas tariff periods
      - from: "2020-01-01T00:00:00+03:00" # Tariff start; +03:00 is a fixed UTC offset
        mode: spot # Use daily gas market prices; fixed = manually configured price
        # price: 0.10 # Required for mode: fixed; EUR/kWh before margin and multiplier
        margin: 0.00465 # Supplier margin: 0.465 cents/kWh, before multiplier
        multiplier: 1.24 # Multiply spot + margin by 1.24 throughout this tariff period
        unit: kWh # Required for gas spot pricing; m3 is supported for fixed pricing
  price_signals: # Electricity price classifications for automations. Signal pricing is independent of historical pricing.
    enabled: true # Enable current price and classification sensors
    timezone: Europe/Tallinn # Local timezone for today, tomorrow and historical days
    cheap_price: 0.10 # Adjusted price <= EUR 0.10/kWh always gives very_low / low
    history_days: 30 # Previous local days used for comparison; allowed range: 1–365
    margin: 0.0 # Electricity margin in EUR/kWh for price signals
    multiplier: 1.24 # Signal price = (spot + signal margin) × 1.24
    today_weight: 0.5 # Weight of today's prices in the comparison
    tomorrow_weight: 0.3 # Weight of tomorrow's prices when available
    history_weight: 0.2 # Weight of available historical prices
    level_percentiles: [20, 40, 60, 80] # Boundaries for very_low, low, normal, high and very_high
    low_percentile: 60 # Weighted percentile <= 60 gives low; above 60 gives high
```

## What the dashboard will show

- Electricity is imported at **hourly resolution**, the long-term-statistics resolution used by this importer. Quarter-hour raw records are retained separately when optional spot-cost pricing is enabled.
- Gas remains a **daily lump at Elering's reported timestamp**. The tested summer entries were at 04:00 UTC; winter entries at 05:00 UTC. Both display as 07:00 in Tallinn. The API's daily aggregation used exactly the same timestamp. The importer makes no assumption that the value belongs to the previous calendar date and does not shift it or invent an hourly distribution.
- Therefore, a gas hourly chart has one daily spike. It does not mean the gas was physically burned in that hour. Calendar comparisons follow the provider's day label; this is not a measured midnight-to-midnight gas profile.
- The newest hours/day can remain missing until the provider publishes them. A successful API request does not guarantee fresh or complete readings.
- Blank/null fields do not overwrite known values. Explicit numeric zero is a real value and can replace an earlier reading.
- A carried-forward cumulative total fills bookkeeping gaps in the imported statistics, so consumption remains assigned to its reported hour. The raw ledger still distinguishes missing source intervals from explicit zeros. As a result, a graph can show no increment where the provider has not supplied a reading; do not interpret that as proof of zero usage.
- The base integration imports quantities, including supplied export values. Optional pricing can now calculate historical variable consumption costs; see PRICING.md. It does not import bills or standing charges. Current-price sensors should not be used to infer historic dynamic prices for delayed readings; accurate time-varying costs require a separate timestamp-matched calculation (especially with quarter-hour electricity pricing).

## Reliability and corrections

| Behavior | Implementation |
|---|---|
| Rate limit | At least 6 seconds between all request starts, including token calls and retries, within this integration instance |
| Authentication | OAuth client credentials; reuse token until 30 seconds before expiry; renew after 401 |
| Transient failure | Up to 4 attempts, exponential backoff plus jitter; honor numeric/date `Retry-After` |
| Permanent/auth failure | Stop that request and expose a retrying status; update credentials if expired/revoked |
| Normal schedule | Recent window every 6 hours by default; no real-time claims |
| Recent correction window | Re-read the preceding 14 days, configurable up to 30 |
| Backfill | At most 30 days/request and 10 EICs/request; 2 historical windows per pass by default |
| HTTP 200 with meter error | Commit other valid meters, retain failed historical range in a durable retry queue |
| Historical failures | Rotate one queued range per 30-minute recovery pass; recent imports continue |
| Old revisions/missing history | Audit one older 30-day window per day; explicit full rescan action is available |
| Restart/repeat | Persistent source ledger keyed by EIC, UTC hour, and field; duplicates do not add consumption twice |
| Historical correction | Recompute the cumulative series from the corrected hour through all subsequent readings |
| Interrupted import | Source plus dirty markers saved atomically before Recorder work; outstanding work can be replayed |

The throttle is local to this Home Assistant instance. Other scripts or instances using the same API key must coordinate their requests, or use separate credentials if the provider's limits permit it.

A six-year history takes about 73 successful daily audit passes to recheck fully. The 14-day overlap is not a promise that older values cannot change; use `rescan` when you need a quicker full reconciliation. The importer cannot distinguish a withdrawn reading from a temporarily omitted field, so it preserves the last supplied numeric value when later responses omit it. Elering must send an explicit replacement (including zero) to correct that value automatically.

## Status and actions

Status values:

- `starting` / `syncing`: initial setup or active work.
- `backfilling`: historical requests are still progressing.
- `up_to_date`: the scheduled requests/imports succeeded; this **does not certify source completeness**.
- `historical_gaps`: one or more historical request windows still fail; the count is in `pending_historical_windows`.
- `retrying`: request, credentials, or storage failure; next normal attempt in 30 minutes.
- `error`: unexpected exception; consult Home Assistant logs.

The `meters` attribute provides `latest_reported_intervals`, the statistic IDs, and the last request's returned/empty slot counts. The last query may be a historical audit; use `latest_reported_intervals` to judge freshness. Gas naturally has many empty hourly slots.

In **Developer Tools → Actions**, you can run:

```yaml
action: estfeed_energy.sync
data: {}
```

Refresh recent data and continue backfill. If a sync is already running, this action does not start another concurrent run. Long actions may outlast the frontend's request timeout; watch the status sensor before triggering again.

```yaml
action: estfeed_energy.rescan
data: {}
```

Restart the historical API scan without deleting the ledger. Requests remain batched and throttled. Existing statistics stay available while corrections are collected.

```yaml
action: estfeed_energy.rebuild_statistics
data: {}
```

Rebuild external statistics from the existing local source ledger without API calls. Useful after restoring or repairing Recorder, provided the ledger still exists. Re-imports upsert the same statistic IDs and timestamps.

Rotate API credentials by editing `secrets.yaml` and restarting Home Assistant. Keep the key authorized for the same meters to continue their history. If you grant access to additional older meters later, run `rescan` to fetch their earlier readings promptly.

## Data retention and recovery

Home Assistant's `.storage/estfeed_energy` contains the persistent source ledger and progress; Recorder contains the dashboard statistics. Back up both as part of a full Home Assistant backup. The ledger contains private consumption data but no API secret. Credentials remain in your configured secrets file; access tokens are held only in memory.

Do not delete the ledger while keeping the integration's existing Recorder statistics: replay scope and cumulative baseline need to remain consistent. Do not change statistic IDs/units after importing. For a scope reset, use a separate test instance or restore a consistent backup rather than partially deleting files. The integration never directly edits Recorder's SQL tables.

## References

- API contract: the Elering customer-portal manual/OpenAPI supplied with this request.
- Home Assistant statistics model: https://data.home-assistant.io/docs/statistics/
- Recorder metadata changes (`unit_class`, `mean_type`): https://developers.home-assistant.io/blog/2025/10/16/recorder-statistics-api-changes/
- Gas Energy dashboard: https://www.home-assistant.io/docs/energy/gas/
- Recorder implementation: https://github.com/home-assistant/core/blob/2026.9.3/homeassistant/components/recorder/statistics.py
