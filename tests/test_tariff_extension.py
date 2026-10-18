"""Tariff extension and Recorder replay tests with mocked HA boundaries."""
import copy
import unittest
from unittest.mock import patch

from test_manager import Subject, manager, model, pricing


class TariffExtensionTests(unittest.IsolatedAsyncioTestCase):
    def subject(self, kind='electricity', mode='fixed'):
        sub = Subject()
        old = model.stamp('2024-01-01T00:00:00Z')
        early = old - 3600
        sub.start = early
        sub.config['pricing'] = {kind: [
            {'from': model.iso(early), 'mode': mode, 'price': .1},
            {'from': model.iso(old), 'mode': 'fixed', 'price': .2},
        ]}
        sub.data.update(first=early, active_eics=['abc'])
        sub.data['points']['abc'] = {
            'commodity': 'NATURAL_GAS' if kind == 'gas' else 'ELECTRICITY',
            'intervals': {str(early): {'consumptionKwh': 3}, str(old): {'consumptionKwh': 2}},
            'dirty': {},
        }
        state = {'intervals': {str(old): {'cost': .4}}, 'quarters': {},
                 'pending': [[old, old + 3600]], 'cursor': old + 86400,
                 'recent_at': old, 'audit_cursor': old, 'audit_at': old, 'retry_at': old}
        sub.data['pricing'] = {kind + '_start': old, 'points': {'abc': state},
                               'prices': {'cached': .1}, 'gas_prices': {}}
        return sub, early, old, state

    async def test_both_commodities_extend_preserve_rates_and_rebase_idempotently(self):
        for kind in ('electricity', 'gas'):
            with self.subTest(kind=kind):
                sub, early, old, state = self.subject(kind)
                engine = manager.Pricing(sub)
                with patch.object(pricing, 'async_add_external_statistics') as write:
                    await engine.run(True)
                    rows = write.call_args.args[2]
                    self.assertEqual([r['sum'] for r in rows], [0, .3, .7])
                    self.assertAlmostEqual(rows[-1]['sum'] - rows[-2]['sum'], .4)
                    self.assertEqual(write.call_args.args[1]['statistic_id'],
                                     'estfeed_energy:abc_consumption_cost')
                    write.reset_mock()
                    await engine.run(True)
                    write.assert_not_called()
                self.assertEqual(state['intervals'][str(old)]['cost'], .4)
                self.assertEqual(state['pending'], [[old, old + 3600]])
                self.assertEqual(sub.saved[0]['pricing'][kind + '_start'], early)
                self.assertNotIn('cursor', sub.saved[0]['pricing']['points']['abc'])

    async def test_spot_extension_rewinds_before_failure_and_retries_after_restart(self):
        sub, early, old, state = self.subject(mode='spot')
        calls = []
        class Client:
            async def prices(self, start, end):
                calls.append({'startDateTime': start, 'endDateTime': end})
                raise manager.ApiError('temporary failure')
            async def estfeed_electricity_prices(self, start, end):
                raise manager.ApiError('temporary failure')
        sub.client = Client()
        await manager.Pricing(sub).run(True)
        self.assertTrue(any(c['startDateTime'] == model.iso(early) for c in calls))
        self.assertEqual(state['intervals'], {str(old): {'cost': .4}})
        self.assertTrue(any(a <= early < b for a, b in state['pending']))
        self.assertEqual(state['first_unpriced_hour'], model.iso(early))
        # Persisted rewind survives a stop before any remote work.
        sub.data = copy.deepcopy(sub.saved[0])
        calls.clear()
        await manager.Pricing(sub).run(True)
        self.assertTrue(any(c['startDateTime'] == model.iso(early) for c in calls))

    async def test_recorder_failure_keeps_dirty_marker_and_replays(self):
        sub, early, old, state = self.subject()
        with patch.object(pricing, 'async_add_external_statistics', side_effect=RuntimeError('recorder')):
            with self.assertRaises(RuntimeError):
                await manager.Pricing(sub).run(True)
        self.assertEqual(state['dirty'], early)
        with patch.object(pricing, 'async_add_external_statistics') as write:
            await manager.Pricing(sub).run(True)
            self.assertEqual(write.call_args.args[2][-1]['sum'], .7)
        self.assertNotIn('dirty', state)

    async def test_later_boundary_rejected_before_any_commodity_rewind(self):
        sub, early, old, state = self.subject()
        sub.config['pricing']['gas'] = [{'from': model.iso(old + 3600), 'mode': 'fixed', 'price': .1}]
        sub.data['pricing']['gas_start'] = old
        original = copy.deepcopy(state)
        with self.assertRaisesRegex(ValueError, 'restore 2024-01-01T00:00:00Z'):
            await manager.Pricing(sub).run(True)
        self.assertEqual(sub.data['pricing']['electricity_start'], old)
        self.assertEqual(state, original)
        self.assertFalse(sub.saved)

    async def test_unchanged_boundary_does_not_rewind_existing_progress(self):
        sub, early, old, state = self.subject()
        sub.config['pricing']['electricity'] = sub.config['pricing']['electricity'][1:]
        await manager.Pricing(sub).run(True)
        self.assertEqual(state['cursor'], old + 86400)
        self.assertEqual(state['audit_cursor'], old)

    async def test_extension_only_rewinds_affected_commodity(self):
        sub, early, old, state = self.subject()
        sub.data['points']['gas'] = {'commodity': 'NATURAL_GAS', 'intervals': {}}
        other = {'intervals': {}, 'quarters': {}, 'pending': [], 'cursor': old, 'audit_at': old}
        sub.data['pricing']['points']['gas'] = copy.deepcopy(other)
        await manager.Pricing(sub).run(True)
        self.assertEqual(sub.data['pricing']['points']['gas'], other)
