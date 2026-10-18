"""Exercise real integration schema/setup with HA scheduling boundaries stubbed."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
try:
    import voluptuous as vol
except ImportError:
    vol = None

ROOT = Path(__file__).resolve().parents[1] / 'custom_components' / 'estfeed_energy'

@unittest.skipIf(vol is None, 'Install voluptuous to test the integration YAML schema')
class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_enable_defaults_entities_timers_and_sync(self):
        name = 'setup_signals_package'
        mods = {k: types.ModuleType(k) for k in [
            'homeassistant', 'homeassistant.const', 'homeassistant.core',
            'homeassistant.helpers', 'homeassistant.helpers.config_validation',
            'homeassistant.helpers.discovery', 'homeassistant.helpers.event',
            name + '.manager', name + '.price_signals']}
        const = mods['homeassistant.const']
        const.EVENT_HOMEASSISTANT_STARTED = 'started'; const.EVENT_HOMEASSISTANT_STOP = 'stop'
        mods['homeassistant.core'].callback = lambda f:f
        cv = mods['homeassistant.helpers.config_validation']
        cv.string = str; cv.boolean = bool; cv.time_zone = str
        cv.ensure_list = lambda v: v if isinstance(v, list) else [v]
        calls = []
        class Manager:
            def __init__(self, *args): pass
            async def load(self): calls.append('load')
            async def run(self, force=False): calls.append(('run', force))
            async def rescan(self): pass
            async def rebuild(self): pass
        class Signals:
            def __init__(self, manager): pass
            async def load(self): calls.append('signals_load')
            async def refresh(self, force=False): calls.append(('refresh', force))
            def update(self): calls.append('quarter')
        mods[name+'.manager'].Manager = Manager; mods[name+'.manager'].DOMAIN = 'estfeed_energy'
        mods[name+'.price_signals'].PriceSignals = Signals
        async def platform(*a): calls.append('platform')
        mods['homeassistant.helpers.discovery'].async_load_platform = platform
        timers = []
        def interval(hass, callback, period):
            timers.append(('interval', callback, period));return lambda:calls.append('cancel_interval')
        def quarter(hass, callback, **kwargs):
            timers.append(('quarter', callback, kwargs));return lambda:calls.append('cancel_quarter')
        mods['homeassistant.helpers.event'].async_track_time_interval = interval
        mods['homeassistant.helpers.event'].async_track_utc_time_change = quarter
        handlers = {}; services = {}
        hass = types.SimpleNamespace(data={}, is_running=False,
            bus=types.SimpleNamespace(async_listen_once=lambda event, f:handlers.update({event:f})),
            services=types.SimpleNamespace(async_register=lambda domain, action, f, **kw:services.update({action:f})))
        with patch.dict(sys.modules, mods):
            spec=importlib.util.spec_from_file_location(name, ROOT/'__init__.py', submodule_search_locations=[str(ROOT)])
            module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
            minimal={'estfeed_energy':{'client_id':'test','client_secret':'test'}}
            cfg=module.CONFIG_SCHEMA(minimal)
            self.assertFalse(cfg['estfeed_energy']['price_signals']['enabled'])
            cfg=module.CONFIG_SCHEMA({'estfeed_energy':{**minimal['estfeed_energy'], 'price_signals':{'enabled':True,'cheap_price':.1}}})
            self.assertEqual(cfg['estfeed_energy']['price_signals']['history_days'],30)
            self.assertTrue(await module.async_setup(hass,cfg))
            self.assertEqual(calls[:3],['load','signals_load','platform'])
            self.assertEqual(timers[1][2],{'minute':[0,15,30,45],'second':0})
            timers[1][1](None);self.assertEqual(calls[-1],'quarter')
            await services['sync'](None)
            self.assertEqual(calls[-2:],[('refresh',True),('run',True)])
            handlers['stop'](None)
            self.assertEqual(calls[-2:],['cancel_interval','cancel_quarter'])
            with self.assertRaises(vol.Invalid):
                module.CONFIG_SCHEMA({'estfeed_energy':{**minimal['estfeed_energy'], 'price_signals':{'multiplier':0}}})
