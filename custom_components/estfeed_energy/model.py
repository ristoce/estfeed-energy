"""Pure interval normalization and deterministic, correction-safe statistics."""
from datetime import datetime, timezone
from decimal import Decimal
import math
import re

FIELDS = ('consumptionKwh', 'productionKwh', 'consumptionM3', 'productionM3')

def stamp(value):
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('API timestamp lacks timezone')
    ts = int(dt.timestamp())
    if ts % 3600 or dt.microsecond:
        raise ValueError('Expected one_hour timestamps aligned to UTC hours')
    return ts

def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace('+00:00', 'Z')

def object_id(eic, field):
    return re.sub('[^a-z0-9_]', '_', eic.lower()) + '_' + field.lower()

def merge_point(point, payload, start, end):
    """Validate entire point before mutating; absent/null fields never erase data."""
    if payload.get('error'):
        raise ValueError('API returned an error for this metering point')
    rows = payload.get('accountingIntervals')
    if not isinstance(rows, list):
        raise ValueError('Missing accountingIntervals')
    normalized = {}
    missing = 0
    for row in rows:
        ts = stamp(row['periodStart'])
        if not start <= ts < end:
            continue  # Protect against inclusive end boundaries.
        values = {}
        for field in FIELDS:
            raw = row.get(field)
            if raw is None:
                continue
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or raw < 0:
                raise ValueError('Invalid consumption/production value')
            values[field] = raw
        if not values:
            missing += 1
        if ts in normalized and normalized[ts] != values:
            raise ValueError('Conflicting duplicate timestamps')
        normalized[ts] = values
    intervals = point.setdefault('intervals', {})
    dirty = point.setdefault('dirty', {})
    for ts, values in normalized.items():
        if not values:
            continue
        existing = intervals.setdefault(str(ts), {})
        for field, value in values.items():
            if existing.get(field) != value:
                existing[field] = value
                dirty[field] = min(dirty.get(field, ts), ts)
    point['last_query'] = {'start': iso(start), 'end': iso(end),
                           'returned_slots': len(normalized), 'empty_slots': missing}


def statistics(point, field, changed_since):
    """End-of-hour running sum, including a zero baseline before the first value.

    Carrying the sum through absent hours places sparse gas increments at their
    reported hour. It is bookkeeping, not a claim that empty source hours are zero.
    The raw ledger preserves that distinction.
    """
    values = {int(ts): Decimal(str(row[field])) for ts, row in point['intervals'].items() if field in row}
    if not values:
        return []
    first, last = min(values), max(values)
    total = Decimal(0)
    result = []
    for ts in range(first - 3600, last + 1, 3600):
        total += values.get(ts, Decimal(0))
        if ts >= changed_since - 3600:
            result.append({'start': datetime.fromtimestamp(ts, timezone.utc),
                           'sum': float(total), 'state': float(total)})
    return result
