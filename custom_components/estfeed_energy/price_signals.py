"""Independent cached public-price polling; shares the API throttle with import."""
import asyncio
from datetime import datetime, timezone
import logging
import time

from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .api import ApiError
from .model import iso
from .price_signal_model import bounds, clean_prices, options, snapshot
from .pricing_model import normalize_estfeed_electricity_prices, normalize_prices

SIGNAL_PRICES = 'estfeed_energy_prices_updated'
_LOGGER = logging.getLogger(__name__)


class PriceSignals:
    def __init__(self, manager):
        self.manager = manager
        self.hass = manager.hass
        self.config = options(manager.config.get('price_signals', {}))
        self.store = Store(self.hass, 1, 'estfeed_energy_price_signals', private=True, atomic_writes=True,
                           serialize_in_event_loop=False)
        self.data = {'prices': {}}
        self.view = {}
        self.lock = asyncio.Lock()
        self.next_due = 0
        self.history_due = 0

    async def load(self):
        self.data = await self.store.async_load() or {'prices': {}}
        start, _, _, end = bounds(datetime.now(timezone.utc).timestamp(), self.config)
        # Reuse historical spot prices already downloaded by the billing importer.
        seed = clean_prices(self.manager.data.get('pricing', {}).get('prices', {}), start - 900, end)
        seed.update(clean_prices(self.data.get('prices', {}), start - 900, end))
        self.data['prices'] = seed
        self.update()

    def update(self, now=None):
        if now is None:
            now = datetime.now(timezone.utc).timestamp()
        self.view = snapshot(self.data['prices'], now, self.config)
        self.view['attributes'].update(
            last_successful_fetch=self.data.get('last_successful_fetch'),
            last_error=self.data.get('last_error'),
            history_error=self.data.get('history_error'),
            source='Estfeed public electricity prices; Elering NPS fallback',
        )
        async_dispatcher_send(self.hass, SIGNAL_PRICES)

    async def fetch(self, start, end):
        """Keep real published values only. No repetition into unpublished periods."""
        values = {}
        errors = []
        try:
            response = await self.manager.client.estfeed_electricity_prices(iso(start), iso(end))
            values = normalize_estfeed_electricity_prices(response, start, end)
        except (ApiError, ValueError, TypeError, KeyError) as err:
            errors.append(type(err).__name__)
        if any(str(t) not in values for t in range(start, end, 900)):
            try:
                response = await self.manager.client.prices(iso(start), iso(end))
                fallback = normalize_prices(response, start, end)
                for key, value in fallback.items():
                    values.setdefault(key, value)
            except (ApiError, ValueError, TypeError, KeyError) as err:
                errors.append(type(err).__name__)
        if not values and errors:
            raise ApiError('Public price fetch failed; retaining previously published intervals')
        return values

    async def refresh(self, force=False):
        self.update()  # Clock updates never depend on a successful network call.
        if self.lock.locked() or (not force and time.monotonic() < self.next_due):
            return
        async with self.lock:
            self.next_due = time.monotonic() + 900
            now = datetime.now(timezone.utc).timestamp()
            start, today, tomorrow, end = bounds(now, self.config)
            cache = self.data['prices']
            try:
                values = await self.fetch(today - 900, end)
                cache.update(values)
                current = int(now) // 900 * 900
                if str(current) not in cache:
                    raise ApiError('Current quarter-hour price is missing')
                self.data['last_error'] = None
                if values:
                    self.data['last_successful_fetch'] = iso(int(now))
            except (ApiError, ValueError, OSError) as err:
                self.data['last_error'] = str(err)
                self.next_due = time.monotonic() + 300
            # Publish tomorrow/current prices before any historical requests.
            self.update()
            try:
                if force or time.monotonic() >= self.history_due:
                    # One bounded history request per cycle, newest missing data first.
                    missing = [t for t in range(start, today, 900) if str(t) not in cache]
                    if missing:
                        cursor = self.data.get('history_cursor', today)
                        candidates = [t for t in missing if t < cursor] or missing
                        finish = candidates[-1] + 900
                        begin = max(start, finish - 7 * 86400)
                        # Advance even if one source window persistently fails, so older
                        # history is not starved; wrap to retry gaps on the next pass.
                        self.data['history_cursor'] = begin
                        cache.update(await self.fetch(begin, finish))
                        self.history_due = time.monotonic() + 900
                        self.data['history_error'] = ('Historical price gaps remain; retry scheduled'
                            if any(str(t) not in cache for t in range(begin, finish, 900)) else None)
                    else:
                        self.history_due = time.monotonic() + 86400
                        self.data['history_error'] = None
            except (ApiError, ValueError, OSError) as err:
                self.data['history_error'] = str(err)
                self.history_due = time.monotonic() + 1800
            self.data['prices'] = clean_prices(cache, start - 900, end)
            try:
                await self.store.async_save(self.data)
            except OSError:
                self.data['last_error'] = 'Price cache could not be saved; live prices remain available'
                _LOGGER.exception('Estfeed price signal cache save failed')
            self.update()
