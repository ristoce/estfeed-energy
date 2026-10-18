# Upgrade to 0.4.0

This release adds optional 15-minute electricity price signals for automations, a published today/tomorrow price dashboard, and the existing customized v1.8 consumption dashboard to the project.

1. Copy `custom_components/estfeed_energy/` over the existing integration directory. Confirm `manifest.json` says `0.4.0`.
2. Add `price_signals` to your EXISTING `estfeed_energy:` block. Do not replace the whole configuration or create a second block:

   ```yaml
   price_signals:
     enabled: true
     timezone: Europe/Tallinn
     cheap_price: 0.10
     history_days: 30
     margin: 0.0
     multiplier: 1.0
   ```

   The default price basis is pre-tax spot EUR/kWh. Set the electricity margin and tax multiplier here if desired; the cheap threshold is compared against the resulting adjusted price. These settings do not modify your dated billing tariffs. Full classification controls are in `price-signals-example.yaml`.
3. Check configuration and restart HA. The new entities start with `sensor.estfeed_electricity_`. `price_level` returns five levels; `price_high_low` returns high/low. They can be unavailable while the first price request completes.
4. Install/update ApexCharts Card through HACS and paste `dashboards/estfeed-electricity-prices.yaml` as a manual card. It replaces the broken historical mini-graph and reads actual published future intervals. Its previous/next values are 15-minute intervals, not hours.
5. `dashboards/estfeed-consumption-comparison-v1.8.yaml` is included unchanged. There is no need to replace your existing comparison card unless you want to restore that version.
6. Use `automations/price-controlled-device.yaml` as a disabled example, or use the high/low entity as a condition in an existing automation. No automation or switch is installed or enabled automatically.

Quantity imports, historical cost logic, existing statistics IDs and existing billing configuration are unchanged. The v0.3.3 historical repair is retained. You do not need to delete statistics, reset history or run a rebuild for this release. Price reference history may fill gradually; inspect `history_coverage` on the signal sensor.

See `PRICE-SIGNALS.md` for the exact percentile formula, weights, missing-data behavior, DST handling, dashboard colors, and diagnostics.
