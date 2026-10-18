# Changelog

## 0.4.1 — HACS/GitHub packaging

- Add HACS metadata and repository identity setup script.
- Add GitHub tests, hassfest, optional full HACS validation and tag-triggered release workflow.
- Add MIT license, publishing guide, development dependencies and release builder.
- Retain all v0.4.0 runtime behavior, historical statistics IDs and YAML configuration.
- Include both dashboard YAML examples and the optional device automation example.

## 0.4.0 — Electricity price signals

- Add configurable five-level and high/low electricity price sensors.
- Compare the current quarter against today, published tomorrow and recent history.
- Add an always-cheap override, optional margin/multiplier, caching and API fallback.
- Include a published today/tomorrow price chart and consumption comparison v1.8.

## 0.3.3 — Historical electricity costs

- Support historical hourly readings alongside quarter-hour prices/readings.
- Fill missing electricity prices from the public Estfeed endpoint.
- Recheck historical electricity cost gaps without deleting consumption statistics.

