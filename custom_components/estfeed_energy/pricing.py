"""Optional persistent cost import. Quantity import remains independent."""
import time
from datetime import datetime, timezone

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics

from .api import ApiError
from .model import iso, object_id, statistics
from .pricing_model import tariffs, calculate, merge_quarters, normalize_prices, normalize_gas_prices, normalize_estfeed_electricity_prices, historical_hour_price

DAY = 86400

class Pricing:
    def __init__(self, manager):
        self.manager = manager
        self.options = manager.config.get('pricing', {})
        self.schedules = {kind: tariffs(self.options.get(kind, []), kind) for kind in ('electricity', 'gas')}
        self.next_due = 0

    async def electricity_prices(self, start, end):
        """Preserve primary values and fill only missing quarters from Estfeed."""
        normalized = {}
        try:
            response = await self.manager.client.prices(iso(start), iso(end))
            normalized = normalize_prices(response, start, end)
        except (ApiError, ValueError, KeyError, TypeError):
            pass
        if any(str(t) not in normalized for t in range(start, end, 900)):
            try:
                response = await self.manager.client.estfeed_electricity_prices(iso(start), iso(end))
                fallback = normalize_estfeed_electricity_prices(response, start, end)
                for ts, value in fallback.items():
                    normalized.setdefault(ts, value)
            except (ApiError, ValueError, KeyError, TypeError):
                if not normalized:
                    raise ApiError('Both public electricity price sources failed; will retry later') from None
        return normalized

    async def run(self, force=False):
        m = self.manager
        if not any(self.schedules.values()) or (not force and time.monotonic() < self.next_due):
            return
        store = m.data.setdefault('pricing', {})
        store.setdefault('points', {})
        store.setdefault('prices', {})
        store.setdefault('gas_prices', {})
        store['last_error'] = None
        now = int(datetime.now(timezone.utc).timestamp()) // 3600 * 3600
        errors = []
        # Validate every boundary before changing any persisted progress. Extending
        # history is safe; shrinking it could leave orphaned Recorder costs.
        for kind, schedule in self.schedules.items():
            if not schedule:
                continue
            key = kind + '_start'
            if key in store and schedule[0]['start'] > store[key]:
                raise ValueError(
                    f'Earliest {kind} tariff from-date may move earlier, but not later; '
                    f'restore {iso(store[key])} or prepend a tariff starting there')
        for kind, schedule in self.schedules.items():
            if not schedule:
                continue
            key = kind + '_start'
            if key in store and schedule[0]['start'] < store[key]:
                for eic, state in store['points'].items():
                    point = m.data.get('points', {}).get(eic)
                    if point is None:
                        continue
                    point_kind = 'gas' if point['commodity'] == 'NATURAL_GAS' else 'electricity'
                    if point_kind == kind:
                        for progress in ('cursor', 'recent_at', 'audit_cursor', 'audit_at', 'retry_at'):
                            state.pop(progress, None)
                # Preserve cached prices, quarters, costs and pending retries.
                # Newly priced hours mark the ledger dirty and rebase subsequent
                # cumulative sums through the existing idempotent Recorder path.
            store[key] = schedule[0]['start']
        # Once after upgrading, revisit electricity price history without deleting
        # cached quantities/costs. Save the rewind atomically before network work.
        if store.get('electricity_repair_version', 0) < 1 and any(
                row['mode'] == 'spot' for row in self.schedules['electricity']):
            for eic, state in store['points'].items():
                if m.data.get('points', {}).get(eic, {}).get('commodity') == 'ELECTRICITY':
                    for progress in ('cursor', 'recent_at', 'audit_cursor', 'audit_at', 'retry_at'):
                        state.pop(progress, None)
            store['electricity_repair_version'] = 1
        # Persist boundaries and rewinds together before network/Recorder work.
        await m.save()
        for eic, point in m.data.get('points', {}).items():
            kind = 'gas' if point['commodity'] == 'NATURAL_GAS' else 'electricity'
            schedule = self.schedules[kind]
            if not schedule:
                continue
            state = store['points'].setdefault(eic, {'quarters': {}, 'intervals': {}, 'pending': []})
            first = max(schedule[0]['start'], m.data.get('first', m.start))
            spot_start = min((r['start'] for r in schedule if r['mode'] == 'spot'), default=None)
            if spot_start is not None and first < now and eic in m.data.get('active_eics', []):
                first = max(first, spot_start)
                recent = max(first, now - 14 * DAY)
                windows = []
                if force or now - state.get('recent_at', 0) >= 6 * 3600:
                    windows.append(('recent', recent, now))
                cursor = state.setdefault('cursor', first)
                if cursor < recent:
                    windows.append(('history', cursor, min(cursor + 30 * DAY, recent)))
                if state['pending'] and now - state.get('retry_at', 0) >= 3600:
                    windows.append(('retry', *state['pending'][0]))
                if cursor >= recent and now - state.get('audit_at', 0) >= DAY and first < recent:
                    audit = state.get('audit_cursor', first)
                    if audit >= recent: audit = first
                    windows.append(('audit', audit, min(audit + 30 * DAY, recent)))
                for purpose, start, end in windows:
                    if start >= end: continue
                    ok = True
                    try:
                        if kind == 'gas':
                            # Look back a day so a query beginning before 07:00
                            # still contains the gas interval which covers it.
                            response = await m.client.gas_prices(iso(start - DAY), iso(end))
                            normalized = normalize_gas_prices(response, start, end)
                            store['gas_prices'].update(normalized)
                            required = [int(t) for t, row in point.get('intervals', {}).items()
                                        if start <= int(t) < end and row.get('consumptionKwh', 0) != 0]
                            if any(str(t) not in normalized for t in required):
                                raise ValueError('Missing gas price for a reported consumption interval')
                        else:
                            normalized = await self.electricity_prices(start, end)
                            store['prices'].update(normalized)
                            required = [hour for hour in range(start, end, 3600)
                                        if point.get('intervals', {}).get(str(hour), {}).get('consumptionKwh') not in (None, 0)
                                        and historical_hour_price(hour, store['prices']) is None]
                            if required:
                                response = await m.client.get('metering-data', {
                                    'startDateTime': iso(start), 'endDateTime': iso(end),
                                    'resolution': 'fifteen_minutes', 'meteringPointEics': eic})
                                match = next((r for r in response if r.get('meteringPointEic') == eic), None)
                                if match is None: raise ValueError('Missing quarter-hour meter response')
                                merge_quarters(state['quarters'], match, start, end)
                            for hour in required:
                                known = point['intervals'][str(hour)]['consumptionKwh']
                                keys = [str(hour + offset) for offset in range(0, 3600, 900)]
                                if any(key not in state['quarters'] for key in keys):
                                    raise ValueError('Incomplete quarter-hour consumption')
                                if abs(sum(state['quarters'][key] for key in keys) - known) > 0.000005:
                                    raise ValueError('Quarter-hour and hourly consumption do not yet match')
                            # Partial price responses must remain in the retry queue.
                            if any(str(t) not in normalized for t in range(start, end, 900)):
                                raise ValueError('Incomplete historical electricity prices')
                    except (ApiError, ValueError, KeyError, TypeError) as err:
                        ok = False
                        errors.append(str(err))
                    window = [start, end]
                    if not ok and window not in state['pending']:
                        state['pending'].append(window)
                    if ok and window in state['pending']:
                        state['pending'].remove(window)
                    if purpose == 'history': state['cursor'] = end
                    if purpose == 'recent': state['recent_at'] = now
                    if purpose == 'audit': state.update(audit_at=now, audit_cursor=end)
                    if purpose == 'retry':
                        state['retry_at'] = now
                        if not ok and window in state['pending']:
                            state['pending'].remove(window); state['pending'].append(window)
                    await m.save()
            costs, gaps = await m.hass.async_add_executor_job(calculate, point, schedule, state['quarters'], store['prices'], store['gas_prices'])
            for ts, value in costs.items():
                if state['intervals'].get(ts) != value:
                    state['intervals'][ts] = value
                    state['dirty'] = min(state.get('dirty', int(ts)), int(ts))
            if gaps and spot_start is not None:
                # Catch late-arriving quantity history after the price cursor passed it.
                gap = gaps[0]
                if not any(start <= gap < end for start, end in state['pending']):
                    state['pending'].append([gap, min(gap + 30 * DAY, now)])
            state['unpriced_hours'] = len(gaps)
            state['first_unpriced_hour'] = iso(gaps[0]) if gaps else None
            state['last_priced_hour'] = iso(max(map(int, state['intervals']))) if state['intervals'] else None
            await m.save()
            await self.flush(eic, state, kind)
        store['last_error'] = '; '.join(sorted(set(errors))) or None
        store['last_run'] = datetime.now(timezone.utc).isoformat()
        await m.save()
        has_work = any(p['pending'] or p.get('unpriced_hours') or p.get('cursor', now) < now - 14 * DAY
                       for p in store['points'].values())
        self.next_due = time.monotonic() + (300 if has_work else m.config['poll_hours'] * 3600)

    async def flush(self, eic, state, kind):
        if 'dirty' not in state: return
        m = self.manager
        rows = await m.hass.async_add_executor_job(statistics, state, 'cost', state['dirty'])
        metadata = {'statistic_id': 'estfeed_energy:' + object_id(eic, 'consumption_cost'),
                    'source': 'estfeed_energy', 'name': f'Estfeed {kind.title()} consumption cost {eic} (EUR)',
                    'unit_of_measurement': 'EUR', 'unit_class': None,
                    'has_sum': True, 'mean_type': StatisticMeanType.NONE}
        for offset in range(0, len(rows), 1000):
            async_add_external_statistics(m.hass, metadata, rows[offset:offset + 1000])
            await get_instance(m.hass).async_block_till_done()
        state.pop('dirty', None)
        await m.save()
