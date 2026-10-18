"""Estfeed historical electricity and gas import, configured through YAML."""
from datetime import datetime, timedelta, timezone
import voluptuous as vol

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv, discovery
from homeassistant.helpers.event import async_track_time_interval, async_track_utc_time_change

from .manager import DOMAIN, Manager
from .price_signals import PriceSignals
from .price_signal_model import options as price_signal_options

TARIFF_SCHEMA = vol.Schema({
    vol.Required('from'): cv.string,
    vol.Required('mode'): vol.In(['fixed', 'spot']),
    vol.Optional('price'): vol.Coerce(float),
    vol.Optional('margin', default=0): vol.Coerce(float),
    vol.Optional('multiplier', default=1): vol.Coerce(float),
    vol.Optional('unit', default='kWh'): vol.In(['kWh', 'm3']),
})

PRICE_SIGNALS_SCHEMA = vol.All(vol.Schema({
    vol.Optional('enabled', default=False): cv.boolean,
    vol.Optional('timezone', default='Europe/Tallinn'): cv.time_zone,
    vol.Optional('history_days', default=30): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
    vol.Optional('cheap_price', default=0.05): vol.Coerce(float),
    vol.Optional('margin', default=0.0): vol.Coerce(float),
    vol.Optional('multiplier', default=1.0): vol.Coerce(float),
    vol.Optional('today_weight', default=0.5): vol.Coerce(float),
    vol.Optional('tomorrow_weight', default=0.3): vol.Coerce(float),
    vol.Optional('history_weight', default=0.2): vol.Coerce(float),
    vol.Optional('level_percentiles', default=[20, 40, 60, 80]): [vol.Coerce(float)],
    vol.Optional('low_percentile', default=60): vol.Coerce(float),
}), price_signal_options)

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Required('client_id'): cv.string,
        vol.Required('client_secret'): cv.string,
        vol.Optional('history_start', default='2000-01-01'): cv.string,
        vol.Optional('metering_point_eics', default=[]): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional('poll_hours', default=6): vol.All(vol.Coerce(int), vol.Range(min=1, max=24)),
        vol.Optional('overlap_days', default=14): vol.All(vol.Coerce(int), vol.Range(min=2, max=30)),
        vol.Optional('price_signals', default={}): PRICE_SIGNALS_SCHEMA,
        vol.Optional('pricing', default={}): vol.Schema({
            vol.Optional('electricity', default=[]): [TARIFF_SCHEMA],
            vol.Optional('gas', default=[]): [TARIFF_SCHEMA],
        }),
        vol.Optional('backfill_windows_per_run', default=2): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
    })
}, extra=vol.ALLOW_EXTRA)

async def async_setup(hass, config):
    if DOMAIN not in config:
        return True
    options = config[DOMAIN]
    start = int(datetime.strptime(options['history_start'], '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())
    if start >= datetime.now(timezone.utc).timestamp():
        raise ValueError('history_start must be in the past')
    manager = Manager(hass, options, start)
    await manager.load()
    manager.price_signals = None
    if options.get('price_signals', {}).get('enabled'):
        manager.price_signals = PriceSignals(manager)
        await manager.price_signals.load()
    hass.data[DOMAIN] = manager
    await discovery.async_load_platform(hass, 'sensor', DOMAIN, {}, config)
    tasks = set()

    @callback
    def launch(_event=None):
        if manager.price_signals:
            price_task = hass.async_create_background_task(manager.price_signals.refresh(), 'Estfeed public prices')
            tasks.add(price_task)
            price_task.add_done_callback(tasks.discard)
        task = hass.async_create_background_task(manager.run(), 'Estfeed data sync')
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    cancel_timer = async_track_time_interval(hass, launch, timedelta(minutes=1))

    @callback
    def quarter_changed(_now):
        if manager.price_signals:
            manager.price_signals.update()

    cancel_quarter = async_track_utc_time_change(hass, quarter_changed, minute=[0, 15, 30, 45], second=0)

    @callback
    def stop(_event):
        cancel_timer()
        cancel_quarter()
        for task in tasks:
            task.cancel()
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
    if hass.is_running:
        launch()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, launch)

    async def sync(_call):
        if manager.price_signals:
            await manager.price_signals.refresh(force=True)
        await manager.run(force=True)
    async def rescan(_call):
        await manager.rescan()
    async def rebuild(_call):
        await manager.rebuild()
    hass.services.async_register(DOMAIN, 'sync', sync, schema=vol.Schema({}))
    hass.services.async_register(DOMAIN, 'rescan', rescan, schema=vol.Schema({}))
    hass.services.async_register(DOMAIN, 'rebuild_statistics', rebuild, schema=vol.Schema({}))
    return True
