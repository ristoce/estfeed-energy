"""Persistent source ledger, backfill, overlap reconciliation and recorder imports."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import logging
import time

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .api import Client, ApiError
from .pricing import Pricing
from .model import FIELDS, iso, stamp, merge_point, statistics, object_id

DOMAIN = 'estfeed_energy'
SIGNAL = DOMAIN + '_updated'
_LOGGER = logging.getLogger(__name__)
DAY = 86400

class Manager:
    def __init__(self, hass, config, start):
        self.hass = hass
        self.config = config
        self.start = start
        self.client = Client(async_get_clientsession(hass), config['client_id'], config['client_secret'])
        self.store = Store(hass, 1, DOMAIN, private=True, atomic_writes=True, serialize_in_event_loop=False)
        self.data = {}
        self.lock = asyncio.Lock()
        self.next_due = 0
        self.status = 'starting'
        self.error = None
        self.pricing = Pricing(self)

    async def load(self):
        self.data = await self.store.async_load() or {'points': {}, 'last_sync': None}
        old_start = self.data.get('configured_start')
        if old_start is not None and self.start > old_start:
            raise ValueError('history_start may move earlier, but not later; restore the earlier date')
        self.data['configured_start'] = self.start
        if old_start is not None and self.start < old_start:
            # Discovery clamps the new start to accessible contract periods and
            # rewinds the backfill cursor when the accessible range expands.
            # Retain raw intervals, dirty markers, pending retries and all IDs.
            self.data['discovered'] = 0
            self.data.pop('audit_cursor', None)
            self.data.pop('audited_at', None)
            for point in self.data.get('pricing', {}).get('points', {}).values():
                for key in ('cursor', 'recent_at', 'audit_cursor', 'audit_at', 'retry_at'):
                    point.pop(key, None)
            # Persist discovery invalidation with the changed boundary so a
            # restart before discovery still resumes the extension safely.
            await self.save()
            _LOGGER.info('Extending Estfeed history start from %s to %s', iso(old_start), iso(self.start))

    async def save(self):
        await self.store.async_save(self.data)

    def notify(self):
        async_dispatcher_send(self.hass, SIGNAL)

    async def discover(self, end):
        rows = await self.client.get('metering-point-eics', {
            'startDateTime': iso(self.start), 'endDateTime': iso(end)})
        selected = set(self.config.get('metering_point_eics', []))
        found = set()
        for row in rows:
            eic = row.get('eic')
            if selected and eic not in selected:
                continue
            if row.get('commodityType') not in ('ELECTRICITY', 'NATURAL_GAS'):
                continue
            periods = row.get('periods', [])
            if not periods:
                continue
            found.add(eic)
            point = self.data['points'].setdefault(eic, {'intervals': {}, 'dirty': {}})
            point.update(commodity=row['commodityType'], periods=periods)
        if not found or selected - found:
            raise ApiError('No accessible meters, or selected meters are not authorized')
        # Removed authorization is not permission to delete existing history.
        self.data['active_eics'] = sorted(found)
        first = min(max(self.start, min(stamp(p['from']) for p in self.data['points'][e]['periods'])) for e in found)
        previous_first = self.data.get('first', first)
        self.data['first'] = min(first, previous_first)
        self.data.setdefault('cursor', first)
        if first < previous_first:
            self.data['cursor'] = first
        self.data['discovered'] = end
        await self.save()

    def eligible(self, eic, start, end):
        for period in self.data['points'][eic]['periods']:
            if stamp(period['from']) < end and (not period.get('to') or stamp(period['to']) > start):
                return True
        return False

    async def fetch_window(self, start, end, publish=True):
        eics = [e for e in self.data['active_eics'] if self.eligible(e, start, end)]
        errors = []
        for offset in range(0, len(eics), 10):
            batch = eics[offset:offset + 10]
            response = await self.client.get('metering-data', {
                'startDateTime': iso(start), 'endDateTime': iso(end),
                'resolution': 'one_hour', 'meteringPointEics': ','.join(batch)})
            by_eic = {row.get('meteringPointEic'): row for row in response}
            for eic in batch:
                try:
                    if eic not in by_eic:
                        raise ValueError('Requested metering point absent from response')
                    merge_point(self.data['points'][eic], by_eic[eic], start, end)
                except (ValueError, KeyError, TypeError):
                    errors.append(eic)
            # Save raw data plus dirty markers BEFORE enqueueing recorder writes.
            await self.save()
            if publish:
                await self.flush()
        if errors:
            raise ApiError(f'{len(errors)} metering point response(s) incomplete or invalid; retry required')

    async def flush(self):
        for eic, point in self.data['points'].items():
            for field, earliest in list(point.get('dirty', {}).items()):
                rows = await self.hass.async_add_executor_job(statistics, point, field, earliest)
                volume = field.endswith('M3')
                direction = 'consumption' if field.startswith('consumption') else 'export'
                commodity = 'Gas' if point['commodity'] == 'NATURAL_GAS' else 'Electricity'
                unit = 'm³' if volume else 'kWh'
                metadata = {
                    'statistic_id': DOMAIN + ':' + object_id(eic, field),
                    'source': DOMAIN, 'name': f'Estfeed {commodity} {direction} {eic} ({unit})',
                    'unit_of_measurement': unit, 'unit_class': 'volume' if volume else 'energy',
                    'has_sum': True, 'mean_type': StatisticMeanType.NONE,
                }
                for offset in range(0, len(rows), 1000):
                    async_add_external_statistics(self.hass, metadata, rows[offset:offset + 1000])
                    await get_instance(self.hass).async_block_till_done()
                point['dirty'].pop(field, None)
            await self.save()

    async def run(self, force=False):
        if self.lock.locked():
            return
        if not force and time.monotonic() < self.next_due:
            async with self.lock:
                await self.price()
                self.notify()
            return
        async with self.lock:
            self.status = 'syncing'
            self.notify()
            try:
                now = int(datetime.now(timezone.utc).timestamp()) // 3600 * 3600
                if not self.data.get('staging_history'):
                    await self.flush()  # Replay an interrupted import.
                if now - self.data.get('discovered', 0) >= DAY:
                    await self.discover(now)
                recent_start = max(self.data['first'], now - self.config['overlap_days'] * DAY)
                # Recent data is independent of historical failures.
                if force or now - self.data.get('recent_at', 0) >= self.config['poll_hours'] * 3600:
                    await self.fetch_window(recent_start, now)
                    self.data['recent_at'] = now
                    await self.save()
                cursor = self.data['cursor']
                if cursor < recent_start:
                    self.data['staging_history'] = True
                    await self.save()
                pending = self.data.setdefault('pending', [])
                for _ in range(self.config['backfill_windows_per_run']):
                    if cursor >= recent_start:
                        break
                    end = min(cursor + 30 * DAY, recent_start)
                    try:
                        await self.fetch_window(cursor, end, publish=False)
                    except ApiError:
                        window = [cursor, end]
                        if window not in pending:
                            pending.append(window)
                    # Every skipped range remains in a durable retry queue.
                    cursor = end
                    self.data['cursor'] = cursor
                    await self.save()
                if pending and now - self.data.get('pending_at', 0) >= 1800:
                    window = pending[0]
                    try:
                        await self.fetch_window(*window, publish=not self.data.get('staging_history'))
                    except ApiError:
                        pending.append(pending.pop(0))
                    else:
                        pending.pop(0)
                    self.data['pending_at'] = now
                    await self.save()
                if cursor >= recent_start and self.data.get('staging_history'):
                    await self.flush()
                    self.data['staging_history'] = False
                    await self.save()
                if now - self.data.get('audited_at', 0) >= DAY and cursor >= recent_start and recent_start > self.data['first']:
                    audit = self.data.get('audit_cursor', self.data['first'])
                    if audit >= recent_start:
                        audit = self.data['first']
                    end = min(audit + 30 * DAY, recent_start)
                    try:
                        await self.fetch_window(audit, end)
                    except ApiError:
                        if [audit, end] not in pending:
                            pending.append([audit, end])
                    self.data.update(audit_cursor=end, audited_at=now)
                if cursor < recent_start:
                    self.status = 'backfilling'
                    self.next_due = time.monotonic() + 60
                else:
                    self.status = 'historical_gaps' if pending else 'up_to_date'
                    self.next_due = time.monotonic() + (1800 if pending else self.config['poll_hours'] * 3600)
                self.error = None
                self.data['last_sync'] = datetime.now(timezone.utc).isoformat()
                await self.save()
            except asyncio.CancelledError:
                raise
            except (ApiError, ValueError, OSError) as err:
                self.status = 'retrying'
                self.error = str(err)
                self.next_due = time.monotonic() + 1800
                _LOGGER.warning('Estfeed sync postponed: %s', err)
            except Exception as err:
                self.status = 'error'
                self.error = f'Unexpected {type(err).__name__}; inspect Home Assistant logs'
                self.next_due = time.monotonic() + 1800
                _LOGGER.exception('Unexpected Estfeed import failure')
            finally:
                await self.price(force)
                self.notify()

    async def price(self, force=False):
        if not getattr(self, 'pricing', None):
            return
        try:
            await self.pricing.run(force)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self.data.setdefault('pricing', {})['last_error'] = str(err)
            self.pricing.next_due = time.monotonic() + 1800
            _LOGGER.warning('Estfeed cost import postponed: %s', err)
            await self.save()

    async def rescan(self):
        async with self.lock:
            self.data['cursor'] = self.data.get('first', self.start)
            self.data['discovered'] = 0
            for point in self.data.get('pricing', {}).get('points', {}).values():
                for key in ('cursor', 'recent_at', 'audit_at', 'retry_at'):
                    point.pop(key, None)
            await self.save()
        await self.run(force=True)

    async def rebuild(self):
        async with self.lock:
            for point in self.data['points'].values():
                for field in FIELDS:
                    keys = [int(ts) for ts, row in point['intervals'].items() if field in row]
                    if keys:
                        point['dirty'][field] = min(keys)
            await self.save()
            await self.flush()
            if getattr(self, 'pricing', None):
                for eic, state in self.data.get('pricing', {}).get('points', {}).items():
                    if state['intervals']:
                        state['dirty'] = min(map(int, state['intervals']))
                        await self.save()
                        kind = 'gas' if self.data['points'][eic]['commodity'] == 'NATURAL_GAS' else 'electricity'
                        await self.pricing.flush(eic, state, kind)
        self.notify()
