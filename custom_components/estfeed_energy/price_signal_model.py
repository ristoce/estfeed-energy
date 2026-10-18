"""Quarter-hour price guidance, independent of consumption and billing history."""
from datetime import datetime, time, timedelta, timezone
import math
from zoneinfo import ZoneInfo

STEP = 900
LEVELS = ('very_low', 'low', 'normal', 'high', 'very_high')
DEFAULTS = {
    'enabled': False, 'timezone': 'Europe/Tallinn', 'history_days': 30,
    'cheap_price': 0.05, 'margin': 0.0, 'multiplier': 1.0,
    'today_weight': 0.5, 'tomorrow_weight': 0.3, 'history_weight': 0.2,
    'level_percentiles': [20, 40, 60, 80], 'low_percentile': 60,
}


def options(values):
    result = {**DEFAULTS, **values}
    ZoneInfo(result['timezone'])
    for key in ('cheap_price', 'margin', 'multiplier', 'today_weight', 'tomorrow_weight',
                'history_weight', 'low_percentile'):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f'price_signals.{key} must be finite')
    if result['multiplier'] <= 0:
        raise ValueError('price_signals.multiplier must be positive')
    weights = [result[k + '_weight'] for k in ('today', 'tomorrow', 'history')]
    if any(x < 0 for x in weights) or sum(weights) <= 0:
        raise ValueError('price_signals weights must be nonnegative with a positive total')
    levels = result['level_percentiles']
    if (not isinstance(levels, list) or len(levels) != 4 or
        any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in levels) or
        not 0 < levels[0] < levels[1] < levels[2] < levels[3] < 100):
        raise ValueError('price_signals.level_percentiles needs four increasing values between 0 and 100')
    if not 0 <= result['low_percentile'] <= 100:
        raise ValueError('price_signals.low_percentile must be between 0 and 100')
    if isinstance(result['history_days'], bool) or not isinstance(result['history_days'], int) or not 1 <= result['history_days'] <= 365:
        raise ValueError('price_signals.history_days must be 1..365')
    return result


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def bounds(now, config):
    """Local calendar days; 92/96/100 quarters on DST days, never assume 24h."""
    zone = ZoneInfo(config['timezone'])
    day = datetime.fromtimestamp(now, zone).date()
    midnight = lambda d: int(datetime.combine(d, time.min, zone).timestamp())
    return (midnight(day - timedelta(days=config['history_days'])), midnight(day),
            midnight(day + timedelta(days=1)), midnight(day + timedelta(days=2)))


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def clean_prices(prices, start, end):
    result = {}
    for key, value in prices.items():
        try:
            ts = int(key)
        except (TypeError, ValueError):
            continue
        if start <= ts < end and ts % STEP == 0 and finite(value):
            result[str(ts)] = value
    return result


def percentile(value, sample):
    # Midrank keeps a flat-price day normal, handles ties and negative prices.
    return 100 * (sum(x < value for x in sample) + 0.5 * sum(x == value for x in sample)) / len(sample)


def snapshot(prices, now, config):
    history_start, today_start, tomorrow_start, end = bounds(now, config)
    current = int(now) // STEP * STEP
    def price(ts):
        raw = prices.get(str(ts))
        return round((raw + config['margin']) * config['multiplier'], 9) if finite(raw) else None
    def rows(start, finish):
        return [{'start': iso(t), 'end': iso(t + STEP), 'price': price(t)} for t in range(start, finish, STEP)]
    today, tomorrow = rows(today_start, tomorrow_start), rows(tomorrow_start, end)
    today_values = [r['price'] for r in today if r['price'] is not None]
    tomorrow_values = [r['price'] for r in tomorrow if r['price'] is not None]
    historical = [price(t) for t in range(history_start, today_start, STEP) if price(t) is not None]
    complete_today = len(today_values) == len(today)
    complete_tomorrow = len(tomorrow_values) == len(tomorrow)
    groups = {'today': today_values if complete_today else [],
              'tomorrow': tomorrow_values if complete_tomorrow else [], 'history': historical}
    active = {k: config[k + '_weight'] for k, v in groups.items() if v and config[k + '_weight'] > 0}
    total_weight = sum(active.values())
    weights = {k: v / total_weight for k, v in active.items()}
    value = price(current)
    ranks = {k: percentile(value, v) if v and value is not None else None for k, v in groups.items()}
    score = sum(weights[k] * ranks[k] for k in weights) if weights and value is not None else None
    level = None if score is None else LEVELS[sum(score > threshold for threshold in config['level_percentiles'])]
    cheap = value is not None and value <= config['cheap_price']
    binary = None if score is None else ('low' if score <= config['low_percentile'] else 'high')
    raw_level = level
    if cheap:
        level, binary = 'very_low', 'low'
    averages = {k: round(sum(v) / len(v), 9) if v else None for k, v in groups.items()}
    return {
        'current_price': value, 'previous_price': price(current - STEP), 'next_price': price(current + STEP),
        'market_price': prices.get(str(current)), 'price_level': level, 'price_high_low': binary,
        'today_low': min(today_values) if complete_today else None,
        'today_high': max(today_values) if complete_today else None,
        'today_average': averages['today'], 'tomorrow_average': averages['tomorrow'],
        'history_average': averages['history'],
        'attributes': {
            'interval_start': iso(current), 'interval_end': iso(current + STEP),
            'timezone': config['timezone'], 'unit': 'EUR/kWh',
            'price_formula': '(market_price + margin) * multiplier',
            'margin': config['margin'], 'multiplier': config['multiplier'],
            'cheap_price': config['cheap_price'], 'cheap_price_override': cheap,
            'relative_level': raw_level, 'relative_percentile': round(score, 3) if score is not None else None,
            'component_percentiles': {k: round(v, 3) if v is not None else None for k, v in ranks.items()},
            'effective_weights': weights, 'reference_averages': averages,
            'level_percentiles': config['level_percentiles'], 'low_percentile': config['low_percentile'],
            'today_complete': complete_today,
            'tomorrow_status': 'available' if complete_tomorrow else ('partial' if tomorrow_values else 'not_published'),
            'today_intervals': len(today_values), 'today_expected_intervals': len(today),
            'tomorrow_intervals': len(tomorrow_values), 'tomorrow_expected_intervals': len(tomorrow),
            'history_days': config['history_days'], 'history_intervals': len(historical),
            'history_expected_intervals': (today_start - history_start) // STEP,
            'history_coverage': round(len(historical) / ((today_start - history_start) // STEP), 4),
            'reference_provisional': (config['today_weight'] > 0 and not complete_today) or
                (config['history_weight'] > 0 and len(historical) < (today_start - history_start) // STEP),
        },
        'forecast': {'today': today, 'tomorrow': tomorrow},
    }
