"""Coordinator contract tests with Home Assistant boundaries mocked (not an HA runtime test)."""
import asyncio
import copy
from datetime import datetime, timezone
from enum import IntEnum
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]/'custom_components'/'estfeed_energy'
mods={}
for name in ['homeassistant','homeassistant.components','homeassistant.components.recorder',
 'homeassistant.components.recorder.models','homeassistant.components.recorder.statistics',
 'homeassistant.helpers','homeassistant.helpers.aiohttp_client','homeassistant.helpers.dispatcher','homeassistant.helpers.storage',
 'test_estfeed_package']:
 mods[name]=types.ModuleType(name)
class Mean(IntEnum): NONE=0
mods['homeassistant.components.recorder'].get_instance=lambda hass:hass
mods['homeassistant.components.recorder.models'].StatisticMeanType=Mean
mods['homeassistant.components.recorder.statistics'].async_add_external_statistics=lambda *args:None
mods['homeassistant.helpers.aiohttp_client'].async_get_clientsession=lambda hass:None
mods['homeassistant.helpers.dispatcher'].async_dispatcher_send=lambda *args:None
mods['homeassistant.helpers.storage'].Store=object
mods['test_estfeed_package'].__path__=[str(ROOT)]
with patch.dict(sys.modules,mods):
 for name in ['api','model','manager']:
  spec=importlib.util.spec_from_file_location('test_estfeed_package.'+name,ROOT/(name+'.py'))
  obj=importlib.util.module_from_spec(spec);sys.modules[spec.name]=obj;spec.loader.exec_module(obj)
 manager=obj
 model=sys.modules['test_estfeed_package.model']
 pricing=sys.modules['test_estfeed_package.pricing']

class Hass:
 async def async_add_executor_job(self,func,*args):return func(*args)
 async def async_block_till_done(self):pass

class Subject(manager.Manager):
 def __init__(self):
  self.hass=Hass();self.lock=asyncio.Lock();self.next_due=0;self.status='starting';self.error=None
  self.config={'overlap_days':14,'poll_hours':6,'backfill_windows_per_run':2}
  self.data={'points':{}};self.saved=[];self.calls=[]
 async def save(self):self.saved.append(copy.deepcopy(self.data))
 def notify(self):pass

class ManagerTests(unittest.IsolatedAsyncioTestCase):
 async def test_error_inside_200_preserves_good_meter(self):
  sub=Subject();t=model.stamp('2026-10-01T00:00:00Z')
  sub.data={'active_eics':['good','bad'],'points':{e:{'commodity':'ELECTRICITY','periods':[{'from':model.iso(t)}],'intervals':{},'dirty':{}} for e in ['good','bad']}}
  class Client:
   async def get(self,*args):return [
    {'meteringPointEic':'good','accountingIntervals':[{'periodStart':model.iso(t),'consumptionKwh':2}]},
    {'meteringPointEic':'bad','accountingIntervals':[],'error':{'code':'internal-error'}}]
  sub.client=Client()
  with self.assertRaises(manager.ApiError):await sub.fetch_window(t,t+3600,publish=False)
  self.assertEqual(sub.saved[-1]['points']['good']['intervals'][str(t)]['consumptionKwh'],2)
  self.assertEqual(sub.data['points']['bad']['intervals'],{})
 async def test_failed_backfill_is_queued_while_recent_succeeds(self):
  sub=Subject();now=int(datetime.now(timezone.utc).timestamp())//3600*3600
  first=now-100*manager.DAY
  sub.data.update(first=first,cursor=first,discovered=now)
  async def fetch(start,end,publish=True):
   sub.calls.append((start,end,publish))
   if start<now-14*manager.DAY:raise manager.ApiError('older source error')
  sub.fetch_window=fetch
  await sub.run(force=True)
  self.assertEqual(sub.data['recent_at'],now)
  self.assertEqual(sub.data['cursor'],first+60*manager.DAY)
  self.assertEqual(len(sub.data['pending']),2)
  self.assertEqual(sub.status,'backfilling')
  self.assertTrue(sub.saved[-1]['staging_history'])
 async def test_statistics_failure_keeps_dirty_marker_for_replay(self):
  sub=Subject();t=model.stamp('2026-10-01T00:00:00Z')
  sub.data['points']['abc']={'commodity':'NATURAL_GAS','intervals':{str(t):{'consumptionM3':1}},'dirty':{'consumptionM3':t}}
  with patch.object(manager,'async_add_external_statistics',side_effect=RuntimeError('recorder failed')):
   with self.assertRaises(RuntimeError):await sub.flush()
  self.assertEqual(sub.data['points']['abc']['dirty'],{'consumptionM3':t})
 async def test_metadata_and_replay_are_stable(self):
  sub=Subject();t=model.stamp('2026-10-01T00:00:00Z')
  sub.data['points']['38Z-ABC']={'commodity':'NATURAL_GAS','intervals':{str(t):{'consumptionM3':1}},'dirty':{'consumptionM3':t}}
  with patch.object(manager,'async_add_external_statistics') as write:
   await sub.flush()
  metadata=write.call_args.args[1]
  self.assertEqual(metadata['unit_class'],'volume')
  self.assertEqual(metadata['mean_type'],Mean.NONE)
  self.assertEqual(metadata['statistic_id'],'estfeed_energy:38z_abc_consumptionm3')
  self.assertEqual(sub.saved[-1]['points']['38Z-ABC']['dirty'],{})

if __name__=='__main__':unittest.main()

class PricingManagerTests(unittest.IsolatedAsyncioTestCase):
 async def test_fixed_cost_upgrade_idempotent_and_correction(self):
  sub=Subject();sub.start=model.stamp('2026-10-01T00:00:00Z');t=sub.start
  sub.config['pricing']={'electricity':[{'from':'2026-10-01T00:00:00Z','mode':'fixed','price':.1}]}
  sub.data['points']['abc']={'commodity':'ELECTRICITY','intervals':{str(t):{'consumptionKwh':2}},'dirty':{}}
  engine=manager.Pricing(sub)
  await engine.run(True)
  state=sub.data['pricing']['points']['abc']
  self.assertEqual(state['intervals'][str(t)]['cost'],.2)
  await engine.run(True)
  self.assertEqual(state['intervals'][str(t)]['cost'],.2)
  sub.data['points']['abc']['intervals'][str(t)]['consumptionKwh']=3
  await engine.run(True)
  self.assertEqual(state['intervals'][str(t)]['cost'],.3)
  self.assertNotIn('dirty',state)
 async def test_spot_fetches_quarters_and_prices_then_imports(self):
  sub=Subject();now=int(datetime.now(timezone.utc).timestamp())//3600*3600;t=now-3600;sub.start=t
  sub.config['pricing']={'electricity':[{'from':model.iso(t),'mode':'spot','margin':.01}]}
  sub.data.update(first=t,active_eics=['abc'])
  sub.data['points']['abc']={'commodity':'ELECTRICITY','intervals':{str(t):{'consumptionKwh':1}},'dirty':{}}
  class Client:
   async def get(self,*args):return [{'meteringPointEic':'abc','accountingIntervals':[{'periodStart':model.iso(t+i*900),'consumptionKwh':.25} for i in range(4)]}]
   async def prices(self,*args):return {'success':True,'data':{'ee':[{'timestamp':t+i*900,'price':100} for i in range(5)]}}
  sub.client=Client();engine=manager.Pricing(sub)
  await engine.run(True)
  state=sub.data['pricing']['points']['abc']
  self.assertEqual(state['intervals'][str(t)]['cost'],.11)
  self.assertEqual(state['unpriced_hours'],0)
 async def test_cost_failure_does_not_raise_through_quantity_manager(self):
  sub=Subject()
  class Broken:
   async def run(self,*args):raise manager.ApiError('Prices unavailable')
  sub.pricing=Broken()
  await sub.price(True)
  self.assertEqual(sub.data['pricing']['last_error'],'Prices unavailable')

class GasPricingManagerTests(unittest.IsolatedAsyncioTestCase):
 async def test_gas_spot_uses_public_daily_prices_without_quarter_calls(self):
  sub=Subject();now=int(datetime.now(timezone.utc).timestamp())//3600*3600;t=now-3600;sub.start=t
  sub.config['pricing']={'gas':[{'from':model.iso(t),'mode':'spot','margin':.01}]}
  sub.data.update(first=t,active_eics=['gas'])
  sub.data['points']['gas']={'commodity':'NATURAL_GAS','intervals':{str(t):{'consumptionKwh':2}},'dirty':{}}
  class Client:
   async def gas_prices(self,*args):
    return [{'fromDateTime':model.iso(t),'toDateTime':model.iso(t+86400), 'eurPerMwh':70}]
  sub.client=Client();engine=manager.Pricing(sub)
  await engine.run(True)
  state=sub.data['pricing']['points']['gas']
  self.assertEqual(state['intervals'][str(t)]['cost'],.16)
  self.assertEqual(state['unpriced_hours'],0)
  self.assertFalse(state['quarters'])

class HistoryExtensionTests(unittest.IsolatedAsyncioTestCase):
 def subject(self, stored_start='2024-01-01T00:00:00Z', configured_start='2021-01-01T00:00:00Z'):
  sub=Subject();sub.start=model.stamp(configured_start);old=model.stamp(stored_start)
  state={'configured_start':old,'first':old,'cursor':old+90*manager.DAY,'discovered':old+100*manager.DAY,
   'audit_cursor':old+30*manager.DAY,'audited_at':old+90*manager.DAY,'pending':[[old,old+manager.DAY]],
   'points':{'gas':{'commodity':'NATURAL_GAS','periods':[{'from':'2020-01-01T00:00:00Z'}],
    'intervals':{str(old):{'consumptionKwh':2}},'dirty':{}}},
   'pricing':{'gas_start':old,'points':{'gas':{'cursor':old,'intervals':{str(old):{'cost':.2}},'quarters':{},'pending':[[old,old+3600]]}}}}
  class Store:
   async def async_load(self):return copy.deepcopy(state)
  sub.store=Store()
  return sub,state
 async def test_earlier_date_preserves_readings_costs_and_pending_work(self):
  sub,state=self.subject();await sub.load()
  self.assertEqual(sub.data['configured_start'],sub.start)
  self.assertEqual(sub.data['points'],state['points'])
  self.assertEqual(sub.data['pending'],state['pending'])
  self.assertEqual(sub.data['pricing']['points']['gas']['intervals'],state['pricing']['points']['gas']['intervals'])
  self.assertEqual(sub.data['pricing']['gas_start'],state['pricing']['gas_start'])
  self.assertNotIn('cursor',sub.data['pricing']['points']['gas'])
  self.assertEqual(sub.saved[-1]['discovered'],0)
 async def test_restart_before_discovery_keeps_extension_scheduled(self):
  sub,_=self.subject();await sub.load();saved=copy.deepcopy(sub.data)
  class Store:
   async def async_load(self):return copy.deepcopy(saved)
  sub.store=Store();sub.saved=[];await sub.load()
  self.assertEqual(sub.data['discovered'],0)
  self.assertEqual(sub.data['configured_start'],sub.start)
  self.assertFalse(sub.saved)
 async def test_discovery_rewinds_to_accessible_contract_date(self):
  sub,_=self.subject();await sub.load()
  class Client:
   async def get(self,endpoint,params):
    self.params=params
    return [{'eic':'gas','commodityType':'NATURAL_GAS','periods':[{'from':'2022-04-01T00:00:00Z'}]}]
  sub.client=Client();await sub.discover(model.stamp('2026-10-12T00:00:00Z'))
  self.assertEqual(sub.client.params['startDateTime'],'2021-01-01T00:00:00Z')
  self.assertEqual(sub.data['first'],model.stamp('2022-04-01T00:00:00Z'))
  self.assertEqual(sub.data['cursor'],sub.data['first'])
  self.assertEqual(len(sub.data['points']['gas']['intervals']),1)
 async def test_later_date_still_rejected_without_saving(self):
  sub,_=self.subject(configured_start='2025-01-01T00:00:00Z')
  with self.assertRaisesRegex(ValueError,'may move earlier'):await sub.load()
  self.assertFalse(sub.saved)
 async def test_unchanged_date_preserves_backfill_progress(self):
  sub,state=self.subject(configured_start='2024-01-01T00:00:00Z');await sub.load()
  self.assertEqual(sub.data,state);self.assertFalse(sub.saved)
 async def test_prepend_rebases_existing_statistics_without_duplicate_consumption(self):
  sub,_=self.subject();await sub.load();point=sub.data['points']['gas']
  old=model.stamp('2024-01-01T00:00:00Z');earlier=old-86400
  old_rows=model.statistics(point,'consumptionKwh',old)
  payload={'accountingIntervals':[{'periodStart':model.iso(earlier),'consumptionKwh':3}]}
  model.merge_point(point,payload,earlier,old)
  first=model.statistics(point,'consumptionKwh',point['dirty']['consumptionKwh'])
  model.merge_point(point,payload,earlier,old)
  second=model.statistics(point,'consumptionKwh',point['dirty']['consumptionKwh'])
  self.assertEqual(first,second)
  self.assertEqual(first[-1]['sum'],5)
  self.assertEqual(first[-1]['sum']-first[-2]['sum'],old_rows[-1]['sum']-old_rows[-2]['sum'])
