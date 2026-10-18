import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1] / 'custom_components' / 'estfeed_energy'
spec=importlib.util.spec_from_file_location('pricing_model',ROOT/'pricing_model.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
T=p.timestamp('2026-10-01T00:00:00Z')

def schedule(mode='fixed',**kw):
 return p.tariffs([{'from':'2026-10-01T00:00:00Z','mode':mode,**kw}], 'electricity')
def point(value=1):return {'intervals':{str(T):{'consumptionKwh':value}}}

class PricingTests(unittest.TestCase):
 def test_fixed_plus_margin_multiplier(self):
  costs,gaps=p.calculate(point(2),schedule(price=.1,margin=.01,multiplier=1.24),{}, {})
  self.assertEqual(costs[str(T)]['cost'],.2728);self.assertFalse(gaps)
 def test_spot_is_consumption_weighted(self):
  q={str(T+i*900):v for i,v in enumerate([.1,.2,.3,.4])}
  prices={str(T+i*900):v for i,v in enumerate([.01,.02,.03,.04])}
  costs,gaps=p.calculate(point(),schedule('spot',margin=.01),q,prices)
  self.assertEqual(costs[str(T)]['cost'],.04);self.assertFalse(gaps)
  self.assertNotEqual(costs[str(T)]['cost'],.035)
 def test_missing_price_does_not_become_zero(self):
  q={str(T+i*900):.25 for i in range(4)}
  costs,gaps=p.calculate(point(),schedule('spot'),q,{})
  self.assertFalse(costs);self.assertEqual(gaps,[T])
 def test_missing_consumption_quarter_is_unpriced(self):
  prices={str(T+i*900):.1 for i in range(4)}
  costs,gaps=p.calculate(point(),schedule('spot'),{str(T):1},prices)
  self.assertFalse(costs);self.assertEqual(gaps,[T])
 def test_mismatching_hourly_revision_is_not_priced(self):
  q={str(T+i*900):.25 for i in range(4)}
  costs,gaps=p.calculate(point(2),schedule('spot'),q,{t:.1 for t in q})
  self.assertFalse(costs);self.assertEqual(gaps,[T])
 def test_negative_spot_prices_preserved(self):
  q={str(T+i*900):.25 for i in range(4)}
  costs,_=p.calculate(point(),schedule('spot'),q,{t:-.05 for t in q})
  self.assertEqual(costs[str(T)]['cost'],-.05)
 def test_dated_changes_do_not_change_past(self):
  rates=schedule(price=.1)+p.tariffs([{'from':'2026-10-01T01:00:00Z','mode':'fixed','price':.2}],'electricity')
  values=point();values['intervals'][str(T+3600)]={'consumptionKwh':1}
  costs,_=p.calculate(values,rates,{}, {})
  self.assertEqual(costs[str(T)]['cost'],.1);self.assertEqual(costs[str(T+3600)]['cost'],.2)
 def test_gas_m3(self):
  rates=p.tariffs([{'from':'2026-10-01T00:00:00Z','mode':'fixed','price':.8,'unit':'m3','margin':.1}],'gas')
  costs,_=p.calculate({'intervals':{str(T):{'consumptionM3':2,'consumptionKwh':21}}},rates,{}, {})
  self.assertEqual(costs[str(T)]['cost'],1.8)
 def test_no_tariff_no_cost(self):
  self.assertEqual(p.calculate(point(),[],{},{}),({},[]))
 def test_gas_spot_m3_rejected(self):
  with self.assertRaises(ValueError):p.tariffs([{'from':'2026-10-01T00:00:00Z','mode':'spot','unit':'m3'}],'gas')
 def test_duplicate_effective_dates_rejected(self):
  row={'from':'2026-10-01T00:00:00Z','mode':'fixed','price':.1}
  with self.assertRaises(ValueError):p.tariffs([row,row],'electricity')
 def test_timezone_required(self):
  with self.assertRaises(ValueError):p.timestamp('2026-10-01T00:00:00')
 def test_mwh_conversion_and_exclusive_end(self):
  payload={'success':True,'data':{'ee':[{'timestamp':T+i*900,'price':100} for i in range(5)]}}
  prices=p.normalize_prices(payload,T,T+3600)
  self.assertEqual(len(prices),4);self.assertEqual(prices[str(T)],.1)
 def test_hourly_historical_prices_expand(self):
  old=p.timestamp('2024-10-01T00:00:00Z')
  payload={'success':True,'data':{'ee':[{'timestamp':old,'price':100},{'timestamp':old+3600,'price':200}]}}
  prices=p.normalize_prices(payload,old,old+3600)
  self.assertEqual(len(prices),4);self.assertEqual(set(prices.values()),{.1})
 def test_post_transition_missing_quarters_never_become_hourly(self):
  payload={'success':True,'data':{'ee':[{'timestamp':T,'price':100},{'timestamp':T+3600,'price':200}]}}
  self.assertEqual(len(p.normalize_prices(payload,T,T+3600)),1)
 def test_quarter_price_gap_not_interpolated(self):
  payload={'success':True,'data':{'ee':[{'timestamp':T,'price':100},{'timestamp':T+900,'price':100},{'timestamp':T+3600,'price':200}]}}
  prices=p.normalize_prices(payload,T,T+3600)
  self.assertEqual(len(prices),2)
 def test_null_quarter_preserves_known(self):
  values={str(T):.2}
  p.merge_quarters(values,{'accountingIntervals':[{'periodStart':'2026-10-01T00:00:00Z','consumptionKwh':None}]},T,T+3600)
  self.assertEqual(values[str(T)],.2)
 def test_zero_consumption_does_not_require_prices(self):
  costs,gaps=p.calculate(point(0),schedule('spot'),{}, {})
  self.assertEqual(costs[str(T)]['cost'],0);self.assertFalse(gaps)

if __name__=='__main__':unittest.main()

class GasSpotTests(unittest.TestCase):
 def gas_row(self, start='2026-09-01T07:00:00+03:00', end='2026-09-02T06:59:59.999999999+03:00'):
  return {'eurPerMwh':72.744,'eurPerMwhWithVat':90.20256,'centsPerKwh':7.2744,
          'fromDateTime':start,'toDateTime':end}
 def test_pre_vat_price_and_nanos(self):
  a=p.timestamp('2026-09-01T04:00:00Z')
  values=p.normalize_gas_prices([self.gas_row()],a,a+86400)
  self.assertEqual(len(values),24)
  self.assertEqual(values[str(a)],.072744)
 def test_extra_returned_day_clipped(self):
  a=p.timestamp('2026-09-01T04:00:00Z')
  extra=self.gas_row('2026-09-02T07:00:00+03:00','2026-09-03T06:59:59.999999999+03:00')
  values=p.normalize_gas_prices([self.gas_row(),extra],a,a+86400)
  self.assertEqual(len(values),24);self.assertNotIn(str(a+86400),values)
 def test_spring_gas_day_has_23_hours(self):
  a=p.timestamp('2026-03-28T07:00:00+02:00')
  values=p.normalize_gas_prices([self.gas_row('2026-03-28T07:00:00+02:00','2026-03-29T06:59:59.999999999+03:00')],a,a+86400)
  self.assertEqual(len(values),23)
 def test_autumn_gas_day_has_25_hours(self):
  a=p.timestamp('2026-10-24T07:00:00+03:00')
  values=p.normalize_gas_prices([self.gas_row('2026-10-24T07:00:00+03:00','2026-10-25T06:59:59.999999999+02:00')],a,a+25*3600)
  self.assertEqual(len(values),25)
 def test_missing_day_not_filled(self):
  a=p.timestamp('2026-09-01T04:00:00Z')
  values=p.normalize_gas_prices([self.gas_row()],a,a+2*86400)
  self.assertNotIn(str(a+86400),values)
 def test_daily_spot_cost_margin_and_multiplier(self):
  rates=p.tariffs([{'from':'2026-10-01T00:00:00Z','mode':'spot','margin':.01,'multiplier':1.24}],'gas')
  costs,gaps=p.calculate(point(2),rates,{}, {},{str(T):.072744})
  self.assertEqual(costs[str(T)]['cost'],.20520512);self.assertFalse(gaps)
 def test_daily_spot_missing_price(self):
  rates=p.tariffs([{'from':'2026-10-01T00:00:00Z','mode':'spot'}],'gas')
  costs,gaps=p.calculate(point(2),rates,{}, {},{})
  self.assertFalse(costs);self.assertEqual(gaps,[T])
 def test_daily_spot_correction(self):
  rates=p.tariffs([{'from':'2026-10-01T00:00:00Z','mode':'spot'}],'gas')
  first,_=p.calculate(point(2),rates,{}, {},{str(T):.07})
  corrected,_=p.calculate(point(2),rates,{}, {},{str(T):.08})
  self.assertEqual(first[str(T)]['cost'],.14);self.assertEqual(corrected[str(T)]['cost'],.16)
