"""Historical hourly meter recovery and independent public price fallbacks."""
import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from test_pricing import p
from test_manager import Subject, manager, model, pricing
from test_core import a, Session, Response

OLD=p.timestamp('2025-03-01T00:00:00Z')
NEW=p.timestamp('2026-10-01T00:00:00Z')
def row(t, value=100, duration=900):
    # Match Estfeed's nanosecond-inclusive end, deliberately different VAT fields.
    return {'fromDateTime':model.iso(t),
            'toDateTime':model.iso(t+duration-1).replace('Z','.999999999Z'),
            'eurPerMwh':value,'eurPerMwhWithVat':999,'centsPerKwh':999}
def source(start,end,value=100):
    return {'success':True,'data':{'ee':[{'timestamp':t,'price':value} for t in range(start,end,900)]}}

class NormalizerTests(unittest.TestCase):
    def test_quarter_boundaries_clipping_prevat_and_negative(self):
        result=p.normalize_estfeed_electricity_prices([row(NEW-900),row(NEW,-10),row(NEW+900,0),row(NEW+1800,None),row(NEW+3600)],NEW,NEW+3600)
        self.assertEqual(result,{str(NEW):-.01,str(NEW+900):0})
    def test_explicit_hour_can_expand_but_quarter_cannot(self):
        result=p.normalize_estfeed_electricity_prices([row(OLD,100,3600)],OLD,OLD+3600)
        self.assertEqual(result,{str(OLD+i*900):.1 for i in range(4)})
        result=p.normalize_estfeed_electricity_prices([row(OLD)],OLD,OLD+3600)
        self.assertEqual(len(result),1)
    def test_exclusive_end_and_dst_offsets(self):
        rows=[{'fromDateTime':'2025-10-26T03:00:00+03:00','toDateTime':'2025-10-26T04:00:00+03:00','eurPerMwh':100},
              {'fromDateTime':'2025-10-26T03:00:00+02:00','toDateTime':'2025-10-26T04:00:00+02:00','eurPerMwh':200}]
        t=p.timestamp(rows[0]['fromDateTime'])
        result=p.normalize_estfeed_electricity_prices(rows,t,t+7200)
        self.assertEqual(len(result),8);self.assertEqual(result[str(t+3600)],.2)
    def test_malformed_prices_rejected_atomically(self):
        bad=[row(NEW,True),row(NEW,float('nan')),row(NEW,100,1800),{'eurPerMwh':100},None,
             {**row(NEW),'toDateTime':model.iso(NEW+899)},
             {**row(NEW),'toDateTime':'2026-10-01T00:15:00'}]
        for item in bad:
            with self.subTest(item=item),self.assertRaises((ValueError,TypeError)):
                p.normalize_estfeed_electricity_prices([row(NEW),item],NEW,NEW+3600)
        with self.assertRaises(ValueError):p.normalize_estfeed_electricity_prices([row(NEW),row(NEW,200)],NEW,NEW+3600)
    def test_old_hourly_quantity_prices_exactly_without_fake_quarters(self):
        schedule=p.tariffs([{'from':model.iso(OLD),'mode':'spot','margin':.00465,'multiplier':1.22}],'electricity')
        point={'intervals':{str(OLD):{'consumptionKwh':2}}}
        prices={str(OLD+i*900):.1 for i in range(4)}
        costs,gaps=p.calculate(point,schedule,{str(OLD):2},prices)
        self.assertEqual(costs[str(OLD)]['cost'],.255346);self.assertFalse(gaps)
        # Missing or varying price coverage cannot be treated as one hourly rate.
        for changed in [{str(OLD):.1},{**prices,str(OLD+900):.2}]:
            self.assertEqual(p.calculate(point,schedule,{},changed),({},[OLD]))
    def test_modern_equal_prices_still_require_quarter_consumption(self):
        schedule=p.tariffs([{'from':model.iso(NEW),'mode':'spot'}],'electricity')
        prices={str(NEW+i*900):.1 for i in range(4)}
        self.assertEqual(p.calculate({'intervals':{str(NEW):{'consumptionKwh':2}}},schedule,{},prices),({},[NEW]))

class FallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_complete_skips_fallback(self):
        sub=Subject()
        class Client:
            async def prices(self,*args):return source(NEW,NEW+3600)
            async def estfeed_electricity_prices(self,*args):raise AssertionError('unneeded fallback')
        sub.client=Client()
        self.assertEqual(len(await manager.Pricing(sub).electricity_prices(NEW,NEW+3600)),4)
    async def test_partial_primary_only_fills_holes_and_preserves_primary(self):
        sub=Subject()
        class Client:
            async def prices(self,*args):return source(NEW,NEW+900,100)
            async def estfeed_electricity_prices(self,*args):return [row(NEW+i*900,200) for i in range(5)]
        sub.client=Client();result=await manager.Pricing(sub).electricity_prices(NEW,NEW+3600)
        self.assertEqual(result,{str(NEW):.1,**{str(NEW+i*900):.2 for i in range(1,4)}})
    async def test_primary_failure_uses_public_prices(self):
        sub=Subject()
        class Client:
            async def prices(self,*args):raise manager.ApiError('primary unavailable')
            async def estfeed_electricity_prices(self,*args):return [row(NEW+i*900) for i in range(4)]
        sub.client=Client();self.assertEqual(len(await manager.Pricing(sub).electricity_prices(NEW,NEW+3600)),4)
    async def test_both_fail_and_partial_fallback_remains_partial(self):
        sub=Subject()
        class Client:
            async def prices(self,*args):raise manager.ApiError('primary unavailable')
            async def estfeed_electricity_prices(self,*args):raise manager.ApiError('fallback unavailable')
        sub.client=Client()
        with self.assertRaises(manager.ApiError):await manager.Pricing(sub).electricity_prices(NEW,NEW+3600)
        async def partial(*args):return [row(NEW)]
        sub.client.estfeed_electricity_prices=partial
        self.assertEqual(await manager.Pricing(sub).electricity_prices(NEW,NEW+3600),{str(NEW):.1})
    async def test_public_fallback_shares_throttle_and_retries_without_auth(self):
        clock=[100.0]
        async def sleep(delay):clock[0]+=delay
        session=Session(clock,[Response(200,{'success':True}),Response(429,{}, {'Retry-After':'20'}),Response(200,[row(NEW)])])
        client=a.Client(session,'test-id','test-secret')
        with patch.object(a.time,'monotonic',side_effect=lambda:clock[0]),patch.object(a.asyncio,'sleep',side_effect=sleep):
            await client.prices(model.iso(NEW),model.iso(NEW+3600))
            await client.estfeed_electricity_prices(model.iso(NEW),model.iso(NEW+3600))
        self.assertGreaterEqual(session.calls[1][0]-session.calls[0][0],6)
        self.assertGreaterEqual(session.calls[2][0]-session.calls[1][0],20)
        self.assertTrue(all('Authorization' not in c[3].get('headers',{}) for c in session.calls))
        self.assertEqual(session.calls[-1][3]['params']['resolution'],'fifteen_minutes')

class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def subject(self,t=OLD):
        sub=Subject();sub.start=t;sub.config['pricing']={'electricity':[{'from':model.iso(t),'mode':'spot'}]}
        sub.data.update(first=t,active_eics=['abc']);sub.data['points']['abc']={'commodity':'ELECTRICITY','intervals':{str(t):{'consumptionKwh':2}},'dirty':{}}
        return sub
    async def test_old_window_skips_quarter_endpoint_rebases_and_is_idempotent(self):
        sub=self.subject();recent=int(datetime.now(timezone.utc).timestamp())//3600*3600
        sub.data['points']['abc']['intervals'][str(recent-3600)]={'consumptionKwh':1}
        state={'quarters':{},'intervals':{str(recent-3600):{'cost':.1}},'pending':[[OLD,OLD+30*86400]],'cursor':recent,'recent_at':recent}
        sub.data['pricing']={'points':{'abc':state}}
        class Client:
            async def prices(self,start,end):return source(model.stamp(start),model.stamp(end))
            async def get(self,endpoint,params):
                assert model.stamp(params['startDateTime'])>OLD+86400
                return [{'meteringPointEic':'abc','accountingIntervals':[{'periodStart':model.iso(recent-3600+i*900),'consumptionKwh':.25} for i in range(4)]}]
        sub.client=Client()
        with patch.object(pricing,'async_add_external_statistics') as write:
            await manager.Pricing(sub).run(True)
            self.assertEqual(state['intervals'][str(OLD)]['cost'],.2)
            self.assertEqual(write.call_args.args[2][-1]['sum'],.3)
            self.assertFalse(state['pending']);self.assertEqual(state['unpriced_hours'],0)
            self.assertNotIn('cursor',sub.saved[0]['pricing']['points']['abc'])
            self.assertEqual(sub.saved[0]['pricing']['electricity_repair_version'],1)
            write.reset_mock();before=state['cursor']
            await manager.Pricing(sub).run(True)
            self.assertGreater(state['cursor'],before);write.assert_not_called()
    async def test_missing_modern_quarters_stays_queued_even_with_full_prices(self):
        t=int(datetime.now(timezone.utc).timestamp())//3600*3600-3600;sub=self.subject(t)
        class Client:
            async def prices(self,start,end):return source(model.stamp(start),model.stamp(end))
            async def get(self,*args):return [{'meteringPointEic':'abc','accountingIntervals':[]}]
        sub.client=Client();await manager.Pricing(sub).run(True)
        state=sub.data['pricing']['points']['abc'];self.assertEqual(state['unpriced_hours'],1);self.assertTrue(state['pending']);self.assertFalse(state['intervals'])
