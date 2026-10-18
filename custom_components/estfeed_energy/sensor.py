"""Import diagnostics and optional live price guidance; energy uses statistics."""
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from .manager import DOMAIN, SIGNAL
from .model import iso, object_id, FIELDS
from .price_signals import SIGNAL_PRICES
from .price_signal_model import LEVELS

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    if discovery_info is None:
        return
    manager = hass.data[DOMAIN]
    entities = [Status(manager), LastSync(manager)]
    if getattr(manager, 'price_signals', None):
        entities.extend(PriceSensor(manager.price_signals, key, name) for key, name in PRICE_SENSORS)
        entities.append(PriceForecast(manager.price_signals))
    async_add_entities(entities)

class Base(SensorEntity):
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    def __init__(self, manager):
        self.manager = manager
    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL, self.changed))
    @callback
    def changed(self):
        self.async_write_ha_state()

class Status(Base):
    _attr_name = 'Estfeed import status'
    _attr_unique_id = 'estfeed_energy_import_status'
    _attr_icon = 'mdi:database-sync'
    @property
    def native_value(self):
        return self.manager.status
    @property
    def extra_state_attributes(self):
        data = self.manager.data
        meters = {}
        for eic, point in data.get('points', {}).items():
            intervals = point.get('intervals', {})
            latest = {}
            ids = {}
            for field in FIELDS:
                timestamps = [int(t) for t, row in intervals.items() if field in row]
                if timestamps:
                    latest[field] = iso(max(timestamps))
                    ids[field] = DOMAIN + ':' + object_id(eic, field)
            meters[eic] = {'commodity': point['commodity'], 'latest_reported_intervals': latest,
                           'statistic_ids': ids, 'last_query': point.get('last_query')}
        pricing = data.get('pricing', {})
        cost_status = {eic: {
            'statistic_id': DOMAIN + ':' + object_id(eic, 'consumption_cost'),
            'unpriced_hours': p.get('unpriced_hours', 0),
            'first_unpriced_hour': p.get('first_unpriced_hour'),
            'last_priced_hour': p.get('last_priced_hour'),
            'pending_windows': len(p.get('pending', [])),
        } for eic, p in pricing.get('points', {}).items()}
        return {'last_error': self.manager.error,
                'pricing_error': pricing.get('last_error'),
                'costs': cost_status,
                'backfill_cursor': iso(data['cursor']) if 'cursor' in data else None,
                'pending_historical_windows': len(data.get('pending', [])),
                'meters': meters}

class LastSync(Base):
    _attr_name = 'Estfeed last successful sync'
    _attr_unique_id = 'estfeed_energy_last_sync'
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    @property
    def native_value(self):
        value = self.manager.data.get('last_sync')
        return dt_util.parse_datetime(value) if value else None


PRICE_SENSORS = [
    ('current_price', 'Current price'), ('previous_price', 'Previous price'),
    ('next_price', 'Next price'), ('today_low', 'Today low'), ('today_high', 'Today high'),
    ('today_average', 'Today average'), ('tomorrow_average', 'Tomorrow average'),
    ('history_average', 'History average'), ('price_level', 'Price level'),
    ('price_high_low', 'Price high low'),
]


class PriceBase(SensorEntity):
    _attr_should_poll = False
    def __init__(self, monitor):
        self.monitor = monitor
    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_PRICES, self.changed))
    @callback
    def changed(self):
        self.async_write_ha_state()


class PriceSensor(PriceBase):
    _attr_icon = 'mdi:currency-eur'
    def __init__(self, monitor, key, name):
        super().__init__(monitor)
        self.key = key
        self._attr_name = 'Estfeed electricity ' + name.lower()
        self._attr_unique_id = 'estfeed_energy_electricity_' + key
        if key in ('price_level', 'price_high_low'):
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = list(LEVELS) if key == 'price_level' else ['high', 'low']
            self._attr_icon = 'mdi:traffic-light'
        else:
            self._attr_native_unit_of_measurement = 'EUR/kWh'
            self._attr_suggested_display_precision = 4
    @property
    def available(self):
        return self.monitor.view.get(self.key) is not None
    @property
    def native_value(self):
        return self.monitor.view.get(self.key)
    @property
    def extra_state_attributes(self):
        if self.key in ('price_level', 'price_high_low', 'current_price'):
            return self.monitor.view.get('attributes', {})
        return {'timezone': self.monitor.config['timezone'], 'interval_minutes': 15}


class PriceForecast(PriceBase):
    _attr_name = 'Estfeed electricity price forecast'
    _attr_unique_id = 'estfeed_energy_electricity_price_forecast'
    _attr_icon = 'mdi:chart-timeline-variant'
    # Large published arrays need not be duplicated in Recorder history.
    _unrecorded_attributes = frozenset({'today', 'tomorrow'})
    @property
    def native_value(self):
        return self.monitor.view.get('attributes', {}).get('tomorrow_status', 'not_published')
    @property
    def extra_state_attributes(self):
        attrs = self.monitor.view.get('attributes', {})
        return {**self.monitor.view.get('forecast', {}),
                **{key: attrs.get(key) for key in (
                    'timezone', 'unit', 'cheap_price', 'margin', 'multiplier', 'today_complete',
                    'tomorrow_status', 'last_successful_fetch', 'last_error')},
                'description': 'Published prices, not predictions. Null means unavailable.'}
