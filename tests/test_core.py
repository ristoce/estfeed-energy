"""Run with python -m unittest discover -s tests -v (aiohttp required)."""
import asyncio
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'custom_components' / 'estfeed_energy'
def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj
m = module('model')
a = module('api')
T = m.stamp('2026-10-01T00:00:00Z')

def payload(*rows):
    return {'accountingIntervals': list(rows)}
def row(hour, **values):
    return {'periodStart': m.iso(T + hour * 3600), **values}

class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.point = {'intervals': {}, 'dirty': {}}
    def merge(self, *rows):
        m.merge_point(self.point, payload(*rows), T, T + 48 * 3600)
    def test_duplicate_poll_is_idempotent(self):
        self.merge(row(0, consumptionKwh=2), row(1, consumptionKwh=3))
        self.point['dirty'].clear()
        self.merge(row(0, consumptionKwh=2), row(1, consumptionKwh=3))
        self.assertEqual(self.point['dirty'], {})
        self.assertEqual(m.statistics(self.point, 'consumptionKwh', T)[-1]['sum'], 5)
    def test_first_interval_has_baseline(self):
        self.merge(row(0, consumptionKwh=2))
        result = m.statistics(self.point, 'consumptionKwh', T)
        self.assertEqual([r['sum'] for r in result], [0, 2])
        self.assertEqual(result[0]['start'].timestamp(), T - 3600)
    def test_missing_and_null_do_not_erase(self):
        self.merge(row(0, consumptionKwh=2))
        self.point['dirty'].clear()
        self.merge(row(0), row(1, consumptionKwh=None))
        self.assertEqual(self.point['intervals'], {str(T): {'consumptionKwh': 2}})
        self.assertFalse(self.point['dirty'])
    def test_explicit_zero_correction_is_retained(self):
        self.merge(row(0, consumptionKwh=2), row(1, consumptionKwh=3))
        self.merge(row(0, consumptionKwh=0))
        result = m.statistics(self.point, 'consumptionKwh', T)
        self.assertEqual([r['sum'] for r in result], [0, 0, 3])
    def test_older_correction_recalculates_entire_tail(self):
        self.merge(row(0, consumptionKwh=2), row(1, consumptionKwh=3), row(2, consumptionKwh=4))
        self.point['dirty'].clear()
        self.merge(row(1, consumptionKwh=1))
        self.assertEqual(self.point['dirty']['consumptionKwh'], T + 3600)
        result = m.statistics(self.point, 'consumptionKwh', T + 3600)
        self.assertEqual([r['sum'] for r in result], [2, 3, 7])
    def test_historical_backfill_preserves_recent_deltas(self):
        self.merge(row(24, consumptionKwh=4), row(25, consumptionKwh=5))
        self.merge(row(0, consumptionKwh=2))
        result = m.statistics(self.point, 'consumptionKwh', T)
        self.assertEqual(result[-1]['sum'], 11)
        self.assertEqual(result[-1]['sum'] - result[-2]['sum'], 5)
        self.assertEqual(result[-2]['sum'] - result[-3]['sum'], 4)
    def test_sparse_gas_is_lump_not_interpolated(self):
        self.merge(row(4, consumptionM3=1.165), row(28, consumptionM3=0.419))
        result = m.statistics(self.point, 'consumptionM3', T + 4 * 3600)
        changes = [round(b['sum'] - a['sum'], 6) for a,b in zip(result, result[1:])]
        self.assertEqual([v for v in changes if v], [1.165, 0.419])
        self.assertEqual(len(self.point['intervals']), 2)
    def test_decimal_sums(self):
        self.merge(*(row(h, consumptionKwh=.1) for h in range(10)))
        self.assertEqual(m.statistics(self.point, 'consumptionKwh', T)[-1]['sum'], 1)
    def test_point_error_changes_nothing(self):
        with self.assertRaises(ValueError):
            m.merge_point(self.point, {'error': {'code':'failed'}, 'accountingIntervals':[row(0, consumptionKwh=3)]}, T, T+3600)
        self.assertFalse(self.point['intervals'])
    def test_invalid_value_is_atomic(self):
        with self.assertRaises(ValueError):
            self.merge(row(0, consumptionKwh=2), row(1, consumptionKwh=float('nan')))
        self.assertFalse(self.point['intervals'])
    def test_end_is_exclusive(self):
        self.merge(row(48, consumptionKwh=5))
        self.assertFalse(self.point['intervals'])
    def test_dst_repeated_hour_stays_distinct(self):
        self.assertEqual(m.stamp('2026-10-25T03:00:00+02:00') - m.stamp('2026-10-25T03:00:00+03:00'), 3600)
    def test_quarters_rejected_in_hourly_mode(self):
        with self.assertRaises(ValueError):
            m.stamp('2026-10-01T00:15:00Z')
    def test_no_negative_or_boolean_values(self):
        for value in (-1, True, float('inf')):
            with self.assertRaises(ValueError):
                self.merge(row(0, consumptionKwh=value))

class Response:
    def __init__(self, status, data, headers=None):
        self.status, self.data, self.headers = status, data, headers or {}
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def json(self, **kwargs):
        if isinstance(self.data, Exception): raise self.data
        return self.data

class Session:
    def __init__(self, clock, responses):
        self.clock, self.responses, self.calls = clock, list(responses), []
    def request(self, method, url, **kwargs):
        self.calls.append((self.clock[0], method, url, kwargs))
        return self.responses.pop(0)

class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, responses, count=1):
        clock=[100.0]
        async def sleep(delay): clock[0] += delay
        session=Session(clock, responses)
        client=a.Client(session, 'test-id', 'test-secret')
        with patch.object(a.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(a.asyncio, 'sleep', side_effect=sleep):
            results=[await client.get('metering-data', {}) for _ in range(count)]
        return session, results
    async def test_token_reused_and_all_requests_throttled(self):
        session,_=await self.exercise([Response(200, {'access_token':'x','expires_in':300}),Response(200,[]),Response(200,[])],2)
        self.assertEqual([c[1] for c in session.calls], ['POST','GET','GET'])
        self.assertTrue(all(b[0]-a[0]>=6 for a,b in zip(session.calls,session.calls[1:])))
    async def test_401_reauthenticates(self):
        session,_=await self.exercise([Response(200,{'access_token':'x'}),Response(401,{}),Response(200,{'access_token':'y'}),Response(200,[])])
        self.assertEqual([c[1] for c in session.calls], ['POST','GET','POST','GET'])
    async def test_429_honors_retry_after(self):
        session,_=await self.exercise([Response(200,{'access_token':'x'}),Response(429,{}, {'Retry-After':'90'}),Response(200,[])])
        self.assertGreaterEqual(session.calls[-1][0]-session.calls[-2][0],90)
    async def test_server_error_retries(self):
        _,results=await self.exercise([Response(200,{'access_token':'x'}),Response(503,{}),Response(200,[])])
        self.assertEqual(results,[[]])
    async def test_invalid_json_retries(self):
        _,results=await self.exercise([Response(200,{'access_token':'x'}),Response(200,ValueError('bad JSON')),Response(200,[])])
        self.assertEqual(results,[[]])
    async def test_auth_errors_do_not_retry_forever(self):
        with self.assertRaises(a.AuthError):
            await self.exercise([Response(400,{})])
    async def test_finite_retry_budget(self):
        with self.assertRaises(a.ApiError):
            await self.exercise([Response(200,{'access_token':'x'})]+[Response(503,{}) for _ in range(4)])
    async def test_gas_prices_are_public_and_throttled(self):
        clock=[100.0]
        async def sleep(delay): clock[0] += delay
        session=Session(clock,[Response(200,[]),Response(200,[])])
        client=a.Client(session,'unused-id','unused-secret')
        with patch.object(a.time,'monotonic',side_effect=lambda:clock[0]),patch.object(a.asyncio,'sleep',side_effect=sleep):
            await client.gas_prices('start','end')
            await client.gas_prices('start','end')
        self.assertEqual([c[1] for c in session.calls],['GET','GET'])
        self.assertGreaterEqual(session.calls[1][0]-session.calls[0][0],6)
        self.assertNotIn('headers',session.calls[0][3])
        self.assertTrue(session.calls[0][2].endswith('/energy-price/gas'))
    def test_http_date_retry_after(self):
        self.assertGreater(a.retry_after('Fri, 01 Jan 2100 00:00:00 GMT'),0)

if __name__ == '__main__': unittest.main()
