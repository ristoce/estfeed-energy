"""Dated tariffs and exact interval-matched cost calculation (no HA dependencies)."""
from datetime import datetime, timezone
from decimal import Decimal
import math

D = lambda value: Decimal(str(value))


def timestamp(value, step=3600):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None or dt.microsecond or int(dt.timestamp()) % step:
        raise ValueError('Tariff timestamps need an explicit timezone and whole-hour alignment')
    return int(dt.timestamp())


def tariffs(entries, commodity):
    result = []
    for entry in entries:
        row = dict(entry)
        row['start'] = timestamp(row['from'])
        if row['mode'] not in ('fixed', 'spot'):
            raise ValueError('Supported pricing modes are fixed and spot')
        row.setdefault('unit', 'kWh')
        if row['unit'] not in ('kWh', 'm3') or (commodity == 'electricity' and row['unit'] != 'kWh'):
            raise ValueError('Electricity requires kWh; gas supports kWh or m3')
        if row['mode'] == 'spot' and row['unit'] != 'kWh':
            raise ValueError('Spot pricing requires kWh; use fixed mode for a manual m3 rate')
        row['commodity'] = commodity
        for key, default in [('margin', 0), ('multiplier', 1)]:
            row.setdefault(key, default)
        if row['mode'] == 'fixed' and 'price' not in row:
            raise ValueError('Fixed tariffs require price')
        for key in ('price', 'margin', 'multiplier'):
            if key in row and (isinstance(row[key], bool) or not math.isfinite(float(row[key]))):
                raise ValueError('Prices, margins and multipliers must be finite numbers')
        if row['multiplier'] <= 0:
            raise ValueError('Multiplier must be positive')
        result.append(row)
    result.sort(key=lambda r: r['start'])
    if len({r['start'] for r in result}) != len(result):
        raise ValueError('Duplicate tariff start dates')
    return result


def tariff_at(schedule, ts):
    return next((r for r in reversed(schedule) if r['start'] <= ts), None)


def normalize_prices(payload, start, end):
    if not isinstance(payload, dict) or payload.get('success') is not True:
        raise ValueError('Price API reported failure')
    data = payload.get('data')
    rows = data.get('ee') if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError('Price API returned no Estonian prices')
    raw = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid electricity price row')
        ts, value = row.get('timestamp'), row.get('price')
        if isinstance(ts, bool) or not isinstance(ts, int) or ts % 900:
            raise ValueError('Invalid price timestamp')
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('Invalid price')
        if ts in raw and raw[ts] != value:
            raise ValueError('Conflicting duplicate price')
        raw[ts] = value
    # This endpoint's live historical series changes at 2025-09-30 22:00 UTC.
    # Never infer an hourly price from missing post-transition quarter prices.
    hourly_end = int(datetime(2025, 9, 30, 22, tzinfo=timezone.utc).timestamp())
    result = {}
    for ts, value in raw.items():
        for offset in (range(0, 3600, 900) if ts < hourly_end and ts % 3600 == 0 else (0,)):
            if start <= ts + offset < end:
                result[str(ts + offset)] = float(D(value) / 1000)
    return result


def normalize_estfeed_electricity_prices(payload, start, end):
    """Explicit 15-minute/hour prices, pre-VAT EUR/MWh -> EUR/kWh.

    Do not expand a missing quarter or use the VAT-inclusive convenience fields.
    An explicit one-hour interval may safely populate all four quarters.
    """
    if not isinstance(payload, list):
        raise ValueError('Expected a list of Estfeed electricity prices')
    result = {}
    for row in payload:
        if not isinstance(row, dict):
            raise ValueError('Invalid Estfeed electricity price row')
        value = row.get('eurPerMwh')
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('Invalid Estfeed electricity price')
        if not isinstance(row.get('fromDateTime'), str) or not isinstance(row.get('toDateTime'), str):
            raise ValueError('Missing electricity price interval timestamps')
        begin = timestamp(row['fromDateTime'], 900)
        finish_dt = datetime.fromisoformat(row['toDateTime'].replace('Z', '+00:00'))
        if finish_dt.tzinfo is None:
            raise ValueError('Electricity price end timestamp lacks timezone')
        finish = int(finish_dt.timestamp())
        if finish_dt.second == 59 and finish_dt.microsecond == 999999 and (finish + 1) % 900 == 0:
            finish += 1
        elif finish_dt.second or finish_dt.microsecond or finish % 900:
            raise ValueError('Invalid electricity price interval boundary')
        if finish - begin not in (900, 3600) or (finish - begin == 3600 and begin % 3600):
            raise ValueError('Expected quarter-hour or aligned hourly electricity price')
        price = float(D(value) / 1000)
        for ts in range(max(begin, start), min(finish, end), 900):
            key = str(ts)
            if key in result and result[key] != price:
                raise ValueError('Conflicting overlapping electricity prices')
            result[key] = price
    return result


def historical_hour_price(ts, prices):
    """An exact old hourly rate; never estimate missing modern quarter readings."""
    hourly_end = int(datetime(2025, 9, 30, 22, tzinfo=timezone.utc).timestamp())
    if ts >= hourly_end:
        return None
    values = [prices.get(str(ts + offset)) for offset in range(0, 3600, 900)]
    if any(value is None for value in values) or any(value != values[0] for value in values):
        return None
    return values[0]


def merge_quarters(target, payload, start, end):
    if payload.get('error') or not isinstance(payload.get('accountingIntervals'), list):
        raise ValueError('Quarter-hour metering point failed')
    updates = {}
    for row in payload['accountingIntervals']:
        ts = timestamp(row['periodStart'], 900)
        value = row.get('consumptionKwh')
        if value is None or not start <= ts < end:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError('Invalid quarter-hour consumption')
        if str(ts) in updates and updates[str(ts)] != value:
            raise ValueError('Conflicting quarter-hour consumption')
        updates[str(ts)] = value
    target.update(updates)


def calculate(point, schedule, quarters, prices, gas_prices=None):
    """Return known hourly costs and known energy hours which cannot yet be priced."""
    costs, gaps = {}, []
    for key, values in point.get('intervals', {}).items():
        ts = int(key)
        tariff = tariff_at(schedule, ts)
        if tariff is None:
            continue
        field = 'consumptionM3' if tariff['unit'] == 'm3' else 'consumptionKwh'
        if field not in values:
            # A gas kWh measurement can exist before its requested volume value.
            if 'consumptionKwh' in values or 'consumptionM3' in values:
                gaps.append(ts)
            continue
        quantity = D(values[field])
        margin, multiplier = D(tariff['margin']), D(tariff['multiplier'])
        if tariff['mode'] == 'fixed':
            cost = quantity * (D(tariff['price']) + margin) * multiplier
        elif quantity == 0:
            cost = D(0)
        elif tariff.get('commodity') == 'gas':
            gas_price = (gas_prices or {}).get(key)
            if gas_price is None:
                gaps.append(ts)
                continue
            cost = quantity * (D(gas_price) + margin) * multiplier
        elif historical_hour_price(ts, prices) is not None:
            # Before quarter-hour market pricing, the rate is identical throughout
            # this hour. Its measured hourly quantity is sufficient and exact.
            cost = quantity * (D(historical_hour_price(ts, prices)) + margin) * multiplier
        else:
            times = [str(ts + offset) for offset in range(0, 3600, 900)]
            if any(t not in quarters or t not in prices for t in times):
                gaps.append(ts)
                continue
            if abs(sum((D(quarters[t]) for t in times), D(0)) - quantity) > D('0.000005'):
                gaps.append(ts)  # Revisions from different API queries have not converged yet.
                continue
            cost = sum((D(quarters[t]) * (D(prices[t]) + margin) * multiplier for t in times), D(0))
        costs[key] = {'cost': float(cost.quantize(D('0.000000000001')))}
    return costs, sorted(gaps)


def normalize_gas_prices(payload, start, end):
    """Map explicit gas-day intervals to UTC hours; always use the pre-VAT field.

    The live API may include the following day even when endDateTime excludes it.
    Clip by actual interval bounds, and preserve DST's 23/25-hour gas days.
    """
    if not isinstance(payload, list):
        raise ValueError('Expected a list of daily Estfeed gas prices')
    result = {}
    for row in payload:
        value = row.get('eurPerMwh')
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('Invalid gas price')
        begin = timestamp(row['fromDateTime'])
        finish_dt = datetime.fromisoformat(row['toDateTime'].replace('Z', '+00:00'))
        if finish_dt.tzinfo is None:
            raise ValueError('Gas price end timestamp lacks timezone')
        # API uses inclusive 06:59:59.999999999; Python truncates nanoseconds.
        finish = int(finish_dt.timestamp())
        if finish_dt.minute == 59 and finish_dt.second == 59 and finish_dt.microsecond == 999999:
            finish += 1
        elif finish_dt.minute or finish_dt.second or finish_dt.microsecond:
            raise ValueError('Unsupported gas price interval boundary')
        if finish <= begin or finish - begin not in (23*3600, 24*3600, 25*3600):
            raise ValueError('Expected a daily gas price interval')
        price = float(D(value) / 1000)
        for ts in range(max(begin, start), min(finish, end), 3600):
            key = str(ts)
            if key in result and result[key] != price:
                raise ValueError('Conflicting overlapping gas prices')
            result[key] = price
    return result
