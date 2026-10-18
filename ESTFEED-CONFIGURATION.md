# Estfeed Energy configuration explained

## Configuration

Place this block in `configuration.yaml`. If an `estfeed_energy:` block already exists, edit that block rather than adding a duplicate. Keep the actual credentials in `secrets.yaml`.

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
        mode: spot # Use daily gas market prices fixed = manually configured price
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

## Integration and history import

| Line | Explanation |
| --- | --- |
| `estfeed_energy:` | Starts the integration's configuration. The name must match its internal domain, even if the GitHub repository is named `estfeed-energy`. |
| `client_id: !secret estfeed_client_id` | Loads the Estfeed API client ID from the `estfeed_client_id` key in `secrets.yaml`. The `!secret` expression is a reference, not the credential itself. |
| `client_secret: !secret estfeed_client_secret` | Loads the matching API client secret from `secrets.yaml`. These credentials authorize access to your metering data. Public price requests do not require them. |
| `history_start: "2020-01-01"` | Requests accessible consumption history from this date. The importer interprets it as **2020-01-01 00:00 UTC**. Actual availability is restricted by your accessible meter/contract periods and the API's data. It does not guarantee that data exists back to 2020. The quoted value stays a string. In this version the date may move earlier to extend history, but not later after it has been stored. |
| `poll_hours: 6` | Sets the normal recent-consumption refresh interval to six hours. Allowed values are 1–24. Historical backfill and retries can run between these refreshes. It does **not** set the price-signal refresh interval. |
| `overlap_days: 14` | Re-queries the previous 14 days during recent-consumption refreshes, allowing delayed or corrected readings to be reconciled. Allowed values are 2–30. Repeated imports update existing intervals rather than adding their consumption twice. This setting does not delete older history. |
| `backfill_windows_per_run: 2` | Processes up to two new historical **consumption** windows per import run. Each window spans up to 30 days, so a normal pass can advance by up to 60 days. Allowed values are 1–10. This is not a two-request limit: meter batching, recent refreshes, pricing and retries may add requests. The historical pricing importer has its own schedule. |
| `metering_point_eics: ["2222-xxxxxx-J", "3333-xxxxxx-I"]` | Optional: import only these meters; omit to auto-discover |

API requests share a minimum six-second spacing and bounded retries. A larger backfill setting does not remove throttling. Electricity readings are delayed; gas can be reported once daily with the day's consumption in one slot. Polling more often cannot make the source readings real-time.

## Historical electricity pricing

| Line | Explanation |
| --- | --- |
| `pricing:` | Starts the historical cost-calculation settings. It does not configure the separate live price signals. |
| `electricity:` | Starts the list of tariffs used for electricity consumption costs. |
| `- from: "2020-01-01T00:00:00+03:00"` | Makes this tariff effective from the stated instant until a later tariff entry takes over. The offset is explicit and fixed: this instant is **2019-12-31 21:00 UTC**. It is not automatically interpreted using Estonia's daylight-saving rules. See the timezone note below. |
| `mode: spot` | Uses the Estonian electricity market price for the consumption interval. Historical hourly and modern quarter-hour pricing are supported. The current importer uses Elering's public electricity prices with the Estfeed public endpoint as a gap-filling fallback. |
| `margin: 0.0` | Adds no supplier margin. The unit is **EUR/kWh**. A margin of `0.01` would add one cent per kWh before applying the multiplier. |
| `multiplier: 1.24` | Multiplies both the market price and margin by 1.24, equivalent to adding 24%. The integration treats this as a numeric factor; it does not automatically select the historically applicable VAT rate. With this single tariff entry, the factor applies throughout the configured history. |

With these settings:

```text
Electricity unit price = (market price + 0.0) × 1.24
Electricity interval cost = interval consumption in kWh × electricity unit price
```

For modern spot pricing, the integration multiplies each quarter's consumption by that quarter's price, then sums the costs into hourly Home Assistant statistics. It does not replace this with an unweighted average of the four prices. The main consumption statistics are also imported hourly in v0.4.1.

These costs represent the energy price plus the configured margin/multiplier. Network charges, fixed monthly fees and other bill items are not automatically included.

## Historical gas pricing

| Line | Explanation |
| --- | --- |
| `gas:` | Starts the list of tariffs used for gas consumption costs. |
| `- from: "2020-01-01T00:00:00+03:00"` | Sets the effective start of this gas tariff, independently of the electricity tariff. It has the same fixed-offset meaning described above. |
| `mode: spot` | Uses the daily gas market-price series supplied by the public Estfeed gas-price endpoint. Prices are matched to the gas consumption data using the integration's gas-pricing logic; this is not the electricity price or an assumed monthly gas rate. |
| `margin: 0.00465` | Adds **€0.00465/kWh**, equal to **0.465 cents/kWh** (`0.465 s/kWh`), before the multiplier. With one dated entry, this margin applies to all gas history covered by it, not only your current contract period. |
| `# EUR/kWh` | Documents the unit of the margin; the comment itself has no effect. |
| `multiplier: 1.24` | Applies a factor of 1.24 to the gas market price plus margin. As with electricity, this factor applies throughout this tariff's period and is not adjusted automatically for historical tax changes. |
| `unit: kWh` | Calculates gas cost using the supplied gas consumption in kWh. Gas volume in m³ can still be imported separately for consumption reporting. |
| `# Or m3, with price and margin then expressed in EUR/m3` | **This comment only applies to a fixed gas tariff.** In this integration, `mode: spot` requires `unit: kWh`; changing this line to `m3` while keeping spot mode is rejected. |

With these settings:

```text
Gas unit price = (daily gas market price + 0.00465) × 1.24
Gas interval cost = reported gas consumption in kWh × gas unit price
```

The margin after the multiplier is `0.00465 × 1.24 = 0.005766 EUR/kWh`, or **0.5766 cents/kWh**. A daily consumption value concentrated in one slot is priced where it is reported; it is not multiplied by the number of hours in the day.

## Electricity price signals for automations

These settings create current/future price sensors and relative price classifications. They do not change historical billing costs.

| Line | Explanation |
| --- | --- |
| `price_signals:` | Starts the optional electricity-price guidance configuration. It is a sibling of `pricing`, not a child of the gas tariff. |
| `enabled: true` | Enables the price monitor and its sensors. The integration default is disabled when not configured. A separate HA automation must use these sensors to control equipment. |
| `timezone: Europe/Tallinn` | Defines the local calendar days used for today, tomorrow and historical comparison. Daylight-saving changes are handled, giving 92 or 100 quarter-hours on clock-change days instead of 96. It does not override `history_start` or a tariff's explicit `from` offset. |
| `cheap_price: 0.10` | Sets the always-cheap threshold to **€0.10/kWh**. At or below this adjusted price, the sensors report `very_low` and `low`, even if that interval ranks expensive relative to other intervals. It does not clamp the actual price. |
| `# <= 10 cents/kWh always becomes very_low / low` | Describes the override. It requires a known current price: missing prices are not treated as cheap. |
| `history_days: 30` | Uses the previous 30 local calendar days as the historical reference group. Today and tomorrow are separate groups. Existing cached prices are reused and missing reference history is fetched gradually. This does not limit imported consumption history. |
| `# Previous local days; configurable 1..365` | Documents the permitted history window: 1–365 days. It is not a count of calendar months or years. |
| `margin: 0.0` | Adds no margin to the signal/chart prices. This is independent of `pricing.electricity.margin`; changing one does not change the other. |
| `# EUR/kWh; set your electricity margin here if wanted` | Documents the margin unit and reminds you to configure it separately for automation guidance. |
| `multiplier: 1.24` | Multiplies the signal/chart market price plus margin by 1.24. It is applied consistently to current, future and historical reference prices for comparison under these chosen settings. |
| `# Example: 1.24 if adding 24% tax to price + margin` | Describes the arithmetic. In this supplied configuration, 1.24 is the active setting, not merely an example. |
| `# These are independent of historical billing tariffs. Default is pre-tax spot.` | The independence statement is correct. **The defaults** are margin 0 and multiplier 1, giving pre-tax spot prices. **Your settings** override the multiplier to 1.24, so the displayed/signal price includes that uplift. |
| `today_weight: 0.5` | Gives today's complete published price distribution a weight of 0.5: normally 50% of the comparison. |
| `tomorrow_weight: 0.3` | Gives tomorrow's complete published price distribution a weight of 0.3: normally 30%. An unavailable or incomplete tomorrow is omitted from ranking; published partial data can still appear in the chart. |
| `history_weight: 0.2` | Gives the available previous-30-day price distribution a weight of 0.2: normally 20%. The entire history is one weighted group, so its larger number of intervals does not automatically outweigh today or tomorrow. |
| `level_percentiles: [20, 40, 60, 80]` | Sets the four boundaries separating the five relative price levels. These are percentile scores, not euro amounts or percentage changes in price. Values must be strictly increasing between 0 and 100. |
| `low_percentile: 60` | Makes the two-state sensor `low` when the weighted percentile is at most 60, otherwise `high`. This cutoff is separately configurable from the five-level boundaries. |
| `# Lowest 60% of the weighted comparison -> low` | A shorthand for that percentile cutoff. It does not guarantee exactly 60% of today's intervals will qualify: tomorrow/history, tied prices and the cheap-price override can change the proportion. |

### What your cheap threshold includes

With margin 0 and multiplier 1.24:

```text
Signal/chart price = market price × 1.24
Always cheap when market price × 1.24 ≤ 0.10 EUR/kWh
```

The equivalent pre-multiplier market threshold is approximately **€0.080645/kWh**, or **8.0645 cents/kWh**. Thus a market price of €0.08 becomes €0.0992 and activates the override; €0.09 becomes €0.1116 and does not.

### How the relative classification works

For each available reference group, the integration calculates:

```text
Percentile = 100 × (number of lower prices + 0.5 × number of equal prices)
                 / number of prices in that group

Combined percentile = weighted average of the available group percentiles
```

Equal prices receive a midpoint rank, so a completely flat comparison ranks at 50. Negative prices work without dividing by a price average. When a reference group is unavailable, the remaining positive weights are normalized. For example, without tomorrow but with today and history, the effective weights become approximately **71.43% today** and **28.57% history**.

Today's and tomorrow's groups require a complete published day. History can be partial; the sensors expose `history_coverage` and `reference_provisional` so you can see this. A relative signal is unavailable if no usable weighted reference exists, unless a known current price qualifies for the absolute cheap override.

| Combined percentile | Five-level sensor | Two-state sensor with cutoff 60 |
| --- | --- | --- |
| ≤20 | `very_low` | `low` |
| >20 to 40 | `low` | `low` |
| >40 to 60 | `normal` | `low` |
| >60 to 80 | `high` | `high` |
| >80 | `very_high` | `high` |
| Known price ≤€0.10/kWh | `very_low`, overriding the relative level | `low` |
| Current quarter's price missing | Unavailable | Unavailable |

The automation entities are:

- `sensor.estfeed_electricity_price_level`
- `sensor.estfeed_electricity_price_high_low`

For example, this condition allows an existing automation to proceed only when the two-state signal is low:

```yaml
condition: state
entity_id: sensor.estfeed_electricity_price_high_low
state: low
```

Price signals normally fetch published prices every 15 minutes and select the cached current quarter at :00, :15, :30 and :45. Their refresh schedule is independent of `poll_hours: 6`. The ranking can also change when tomorrow is published or additional history arrives. These signals are guidance; minimum run times and device operating requirements belong in the device's automation/controller.

## Applying configuration edits

After editing YAML, run Home Assistant's configuration check and restart Home Assistant. Keep your existing storage files and statistics. A price signal being unavailable during its initial fetch is different from a restored obsolete entity. The active import-status entity in the supplied package is `sensor.estfeed_import_status`; it reports the import and pricing queues.
