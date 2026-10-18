"""Price guidance tests. HA boundaries are stubbed; no running HA required."""
import asyncio
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'custom_components' / 'estfeed_energy'
PKG = 'test_price_signal_package'
package = types.ModuleType(PKG); package.__path__ = [str(ROOT)]
sys.modules[PKG] = package

def load(name):
    spec = importlib.util.spec_from_file_location(PKG + '.' + name, ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

model = load('price_signal_model')
api = load('api')
load('model'); load('pricing_model')
modules = {k: types.ModuleType(k) for k in [
    'homeassistant', 'homeassistant.helpers', 'homeassistant.helpers.dispatcher',
    'homeassistant.helpers.storage']}
modules['homeassistant.helpers.dispatcher'].async_dispatcher_send = lambda *a: None
class Store:
    def __init__(self, *a, **kw): self.saved = None
    async def async_load(self): return self.saved
    async def async_save(self, data): self.saved = data.copy()
modules['homeassistant.helpers.storage'].Store = Store
with patch.dict(sys.modules, modules):
    monitor_module = load('price_signals')


def stamp(value): return int(datetime.fromisoformat(value).timestamp())
NOW = stamp('2026-10-17T12:00:00+03:00')
CONFIG = model.options({'cheap_price': -100, 'history_days': 1})

def fill(config=CONFIG, now=NOW, today=.2, tomorrow=.3, history=.1):
    start, day, next_day, end = model.bounds(now, config)
    return {str(t): (history if t < day else today if t < next_day else tomorrow)
            for t in range(start, end, 900)}


class SignalModelTests(unittest.TestCase):
    def test_blended_ranking_and_reference_weights(self):
        view = model.snapshot(fill(), NOW, CONFIG)
        # Today flat -> 50; tomorrow dearer -> 0; past cheaper -> 100.
        self.assertEqual(view['attributes']['relative_percentile'], 45)
        self.assertEqual(view['price_level'], 'normal')
        self.assertEqual(view['price_high_low'], 'low')

    def test_future_prices_can_change_decision(self):
        prices = fill(today=.2, tomorrow=.1, history=.1)
        view = model.snapshot(prices, NOW, CONFIG)
        self.assertEqual(view['attributes']['relative_percentile'], 75)
        self.assertEqual(view['price_level'], 'high')
        self.assertEqual(view['price_high_low'], 'high')

    def test_cheap_floor_overrides_relatively_expensive_quarter(self):
        config = model.options({'cheap_price': .1, 'history_days': 1})
        prices = fill(config, today=.01, tomorrow=.01, history=.01)
        prices[str(NOW)] = .1
        view = model.snapshot(prices, NOW, config)
        self.assertEqual(view['attributes']['relative_level'], 'very_high')
        self.assertTrue(view['attributes']['cheap_price_override'])
        self.assertEqual((view['price_level'], view['price_high_low']), ('very_low', 'low'))

    def test_missing_current_is_unavailable_not_cheap(self):
        prices = fill(); del prices[str(NOW)]
        view = model.snapshot(prices, NOW, model.options({'cheap_price': .5, 'history_days': 1}))
        self.assertIsNone(view['current_price']); self.assertIsNone(view['price_high_low'])
        self.assertIsNone(view['price_level']); self.assertFalse(view['attributes']['cheap_price_override'])

    def test_no_future_forward_fill(self):
        prices = fill(); _, _, tomorrow, end = model.bounds(NOW, CONFIG)
        for t in range(tomorrow, end, 900): prices.pop(str(t))
        view = model.snapshot(prices, NOW, CONFIG)
        self.assertEqual(view['attributes']['tomorrow_status'], 'not_published')
        self.assertTrue(all(r['price'] is None for r in view['forecast']['tomorrow']))
        self.assertNotIn('tomorrow', view['attributes']['effective_weights'])
        self.assertAlmostEqual(sum(view['attributes']['effective_weights'].values()), 1)

    def test_partial_tomorrow_displayed_but_not_used_for_ranking(self):
        prices = fill(); _, _, tomorrow, _ = model.bounds(NOW, CONFIG)
        del prices[str(tomorrow)]
        view = model.snapshot(prices, NOW, CONFIG)
        self.assertEqual(view['attributes']['tomorrow_status'], 'partial')
        self.assertNotIn('tomorrow', view['attributes']['effective_weights'])
        self.assertIsNone(view['tomorrow_average'])
        self.assertEqual(view['forecast']['tomorrow'][1]['price'], .3)

    def test_all_flat_prices_are_normal(self):
        view = model.snapshot(fill(today=.2, tomorrow=.2, history=.2), NOW, CONFIG)
        self.assertEqual(view['attributes']['relative_percentile'], 50)
        self.assertEqual(view['price_level'], 'normal')

    def test_negative_prices_rank_correctly(self):
        view = model.snapshot(fill(today=-.1, tomorrow=-.2, history=-.2), NOW, CONFIG)
        self.assertEqual(view['price_level'], 'high')
        view = model.snapshot(fill(today=-.1, tomorrow=-.2, history=-.2), NOW, model.options({}))
        self.assertEqual(view['price_level'], 'very_low')

    def test_price_basis_margin_tax_and_floor_use_same_units(self):
        config = model.options({'margin': .01, 'multiplier': 1.24, 'cheap_price': .12})
        view = model.snapshot(fill(config, today=.1), NOW, config)
        self.assertEqual(view['current_price'], .1364)
        self.assertFalse(view['attributes']['cheap_price_override'])

    def test_dst_day_lengths(self):
        for date, expected in [('2026-03-29', 92), ('2026-10-25', 100), ('2026-10-17', 96)]:
            now = stamp(date + 'T12:00:00+03:00')
            view = model.snapshot(fill(now=now), now, CONFIG)
            self.assertEqual(len(view['forecast']['today']), expected)
            self.assertTrue(view['attributes']['today_complete'])

    def test_quarter_boundary_and_midnight_rollover(self):
        prices = fill(); prices[str(NOW)] = .3; prices[str(NOW + 900)] = .4
        self.assertEqual(model.snapshot(prices, NOW + 899, CONFIG)['current_price'], .3)
        self.assertEqual(model.snapshot(prices, NOW + 900, CONFIG)['current_price'], .4)
        _, _, tomorrow, _ = model.bounds(NOW, CONFIG)
        view = model.snapshot(prices, tomorrow, CONFIG)
        self.assertEqual(view['current_price'], .3)
        self.assertEqual(view['previous_price'], .2)
        self.assertEqual(view['attributes']['tomorrow_status'], 'not_published')

    def test_partial_history_is_explicit(self):
        prices = fill(); start, day, _, _ = model.bounds(NOW, CONFIG)
        for t in range(start, day - 900, 900): del prices[str(t)]
        view = model.snapshot(prices, NOW, CONFIG)
        self.assertEqual(view['attributes']['history_intervals'], 1)
        self.assertTrue(view['attributes']['reference_provisional'])

    def test_no_references_returns_unknown_unless_absolute_cheap(self):
        self.assertIsNone(model.snapshot({str(NOW): .4}, NOW, CONFIG)['price_high_low'])
        self.assertEqual(model.snapshot({str(NOW): .01}, NOW, model.options({}))['price_high_low'], 'low')

    def test_config_rejects_invalid_and_nonfinite_values(self):
        for config in [{'multiplier': 0}, {'cheap_price': float('nan')}, {'margin': float('inf')},
                       {'today_weight': -1}, {'level_percentiles': [10, 40, 30, 90]},
                       {'level_percentiles': [1, 2, 3]}, {'low_percentile': 101},
                       {'history_days': 0}, {'history_days': 366},
                       {'today_weight': 0, 'tomorrow_weight': 0, 'history_weight': 0}]:
            with self.subTest(config=config), self.assertRaises(ValueError): model.options(config)


class SignalFetchTests(unittest.IsolatedAsyncioTestCase):
    def monitor(self, days=1):
        manager = types.SimpleNamespace(hass=object(), data={},
            config={'price_signals': {'enabled': True, 'history_days': days}}, client=object())
        return monitor_module.PriceSignals(manager)

    async def test_throttle_does_not_prevent_clock_recalculation(self):
        sub = self.monitor(); now = int(datetime.now(timezone.utc).timestamp()) // 900 * 900
        sub.data['prices'] = {str(now): .123}; sub.next_due = float('inf')
        await sub.refresh()
        self.assertEqual(sub.view['current_price'], .123)

    async def test_failed_fetch_preserves_published_prices(self):
        sub = self.monitor(); now = int(datetime.now(timezone.utc).timestamp()) // 900 * 900
        sub.data['prices'] = {str(now): .123}
        async def fail(*a): raise api.ApiError('temporary failure')
        sub.fetch = fail
        await sub.refresh()
        self.assertEqual(sub.view['current_price'], .123)
        self.assertEqual(sub.view['attributes']['last_error'], 'temporary failure')

    async def test_restart_reuses_existing_billing_history(self):
        sub = self.monitor(); now = int(datetime.now(timezone.utc).timestamp()) // 900 * 900
        sub.manager.data = {'pricing': {'prices': {str(now - 900): .15}}}
        sub.store.saved = {'prices': {str(now): .25}}
        await sub.load()
        self.assertEqual(sub.view['current_price'], .25)
        self.assertEqual(sub.view['previous_price'], .15)

    async def test_persistent_recent_gap_does_not_starve_older_history(self):
        sub = self.monitor(days=30); now = datetime.now(timezone.utc).timestamp()
        _, today, _, end = model.bounds(now, sub.config)
        calls = []
        async def fetch(begin, finish):
            calls.append((begin, finish))
            if begin >= today - 900: return {str(t): .2 for t in range(begin, finish, 900)}
            raise api.ApiError('historical gap')
        sub.fetch = fetch
        await sub.refresh(force=True)
        first = calls[-1]
        await sub.refresh(force=True)
        second = calls[-1]
        self.assertLess(second[1], first[1])
        self.assertIsNotNone(sub.view['current_price'])

    async def test_source_fallback_keeps_primary_and_fills_gap(self):
        sub = self.monitor()
        start = NOW
        primary = [{'fromDateTime': model.iso(start), 'toDateTime': model.iso(start + 900), 'eurPerMwh': 100}]
        class Client:
            async def estfeed_electricity_prices(self, *a): return primary
            async def prices(self, *a): return {'success': True, 'data': {'ee': [
                {'timestamp': start, 'price': 999}, {'timestamp': start + 900, 'price': 200}]}}
        sub.manager.client = Client()
        self.assertEqual(await sub.fetch(start, start + 1800), {str(start): .1, str(start + 900): .2})

    async def test_overlapping_refresh_is_not_duplicated(self):
        sub = self.monitor()
        async with sub.lock:
            await sub.refresh(force=True)
        self.assertEqual(sub.next_due, 0)
