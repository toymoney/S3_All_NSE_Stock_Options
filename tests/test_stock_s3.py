import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
import stock_s3_bot as b

T=datetime(2026,11,19,10,0,30,tzinfo=b.IST)

def contract(k=100,side='CE',iv=30):
    return {'key':f'FO|{k}{side}','symbol':'SAMPLE','underlying':'EQ|1','expiry':'2026-12-29',
            'strike':k,'side':side,'lot':100,'spot':100,
            'market':{'ltp':10,'bid_price':9.9,'ask_price':10.1,'oi':10000,'volume':10000,'bid_qty':100,'ask_qty':100},
            'greeks':{'iv':iv,'delta':.5,'gamma':.01,'theta':-.1,'vega':.2}}

class FakeAPI:
    bars=[]
    def candles(self,*args):return self.bars
    def chain(self,*args):raise b.APIError('fixture error')

class StockTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.api=FakeAPI();self.api.bars=[]
        self.s=b.Scanner(self.api,self.tmp.name)
        self.day={'date':'2026-11-19','cursor':0,'sweeps':0,'symbols':{},'counts':{},'universe_count':2}
        self.s.state['days'][self.day['date']]=self.day
    def tearDown(self):self.tmp.cleanup()
    def watch(self,direction='UP'):
        v={'entry':10,'target':12 if direction=='UP' else 8,'direction':direction,'gap_pct':20,'peer_iv':30}
        return self.s.add_watch(contract(),v,T,self.day)
    def bar(self,minute,high=11,low=9,close=10,hour=10):
        return [T.replace(hour=hour,minute=minute,second=0).isoformat(),10,high,low,close,100,1000]
    def test_band_actual_strikes(self):
        chain=[contract(k,side) for k in range(50,156,5) for side in ['CE','PE']]
        band=b.select_band(chain,102.5)
        self.assertEqual(len(band),10);self.assertEqual(min(x['strike'] for x in band),95)
        self.assertEqual(max(x['strike'] for x in band),115)
    def test_peer_iv_excludes_own(self):
        c=contract(iv=199);peers=[contract(90,iv=20),c,contract(110,iv=40)]
        self.assertAlmostEqual(b.peer_iv(c,peers),.30)
    def test_no_extrapolation(self):
        self.assertIsNone(b.peer_iv(contract(90),[contract(90),contract(100),contract(110)]))
    def test_peers_same_side(self):
        self.assertIsNone(b.peer_iv(contract(),[contract(90,'PE'),contract(),contract(110,'PE')]))
    def test_lot_liquidity(self):
        c=contract();self.assertTrue(b.liquid(c));c['market']['bid_qty']=99;self.assertFalse(b.liquid(c))
    def test_wide_spread(self):
        c=contract();c['market']['ask_price']=12;self.assertFalse(b.liquid(c))
    def test_fresh_quote(self):
        self.assertTrue(b.fresh({'last_trade_time':str(T.timestamp()*1000)},T))
        self.assertFalse(b.fresh({'last_trade_time':(T-timedelta(minutes=4)).timestamp()*1000},T))
        self.assertFalse(b.fresh({},T))
    def test_candidate_below(self):
        c=contract();band=[contract(90),c,contract(110)]
        with patch.object(b,'bs',return_value=13):v,reason=b.candidate(c,band,100,T)
        self.assertIsNone(reason);self.assertEqual(v['direction'],'UP');self.assertEqual(v['target'],13)
    def test_candidate_above(self):
        c=contract();band=[contract(90),c,contract(110)]
        with patch.object(b,'bs',return_value=8):v,reason=b.candidate(c,band,100,T)
        self.assertIsNone(reason);self.assertEqual(v['direction'],'DOWN')
    def test_executable_gap_gate(self):
        c=contract();c['market'].update(ltp=8,bid_price=9.9,ask_price=10.1)
        with patch.object(b,'bs',return_value=10):v,_=b.candidate(c,[contract(90),c,contract(110)],100,T)
        self.assertIsNone(v)
    def test_expiry_guard(self):
        c=contract();c['expiry']='2026-11-19'
        v,reason=b.candidate(c,[contract(90),c,contract(110)],100,T.replace(hour=15,minute=10))
        self.assertIsNone(v);self.assertEqual(reason,'near_expiry')
    def test_bs_parity(self):
        import math
        self.assertAlmostEqual(b.bs(100,100,.1,.3,'CE')-b.bs(100,100,.1,.3,'PE'),100-100*math.exp(-b.RISK_RATE*.1))
    def test_entry_dedup_and_reload(self):
        self.watch();self.assertIsNone(self.watch());other=b.Scanner(self.api,self.tmp.name)
        self.assertEqual(len(other.state['watches']),1)
    def test_candle_hit_close_separate(self):
        w=self.watch();self.api.bars=[self.bar(1,high=13,close=10)]
        self.s.candle_check(w,T.replace(minute=2,second=1))
        self.assertEqual(w['status'],'VALUE_REACHED');self.assertEqual(w['hit_price'],12)
        self.assertEqual(w['hit_candle_close'],10);self.assertIn('10:02:00',w['hit_time'])
    def test_down_touch(self):
        w=self.watch('DOWN');self.api.bars=[self.bar(1,low=7)]
        self.s.candle_check(w,T.replace(minute=2,second=1));self.assertEqual(w['status'],'VALUE_REACHED')
    def test_entry_minute_excluded(self):
        w=self.watch();self.api.bars=[self.bar(0,high=15)]
        self.s.candle_check(w,T.replace(minute=2));self.assertEqual(w['status'],'OPEN')
    def test_incomplete_candle_excluded(self):
        w=self.watch();self.api.bars=[self.bar(1,high=15)]
        self.s.candle_check(w,T.replace(minute=1));self.assertEqual(w['status'],'OPEN')
    def test_invalid_zero_low_not_hit(self):
        w=self.watch('DOWN');self.api.bars=[self.bar(1,low=0)]
        self.s.candle_check(w,T.replace(minute=2));self.assertEqual(w['status'],'OPEN')
    def test_recent_quote_not_overwritten(self):
        w=self.watch();w['last_price']=11;w['last_observed_at']=b.iso(T.replace(minute=5))
        self.api.bars=[self.bar(1)];self.s.candle_check(w,T.replace(minute=6))
        self.assertEqual(w['last_price'],11)
    def test_missing_bars_not_claimed_complete(self):
        w=self.watch();self.api.bars=[self.bar(29,hour=15)]
        self.s.candle_check(w,T.replace(hour=15,minute=40));self.assertEqual(w['status'],'OPEN')
    def test_complete_bar_coverage(self):
        w=self.watch();bt=T.replace(minute=1,second=0)
        while bt<T.replace(hour=15,minute=30,second=0):
            self.api.bars.append([b.iso(bt),10,11,9,10,100,1000]);bt+=timedelta(minutes=1)
        self.s.candle_check(w,T.replace(hour=15,minute=40))
        self.assertEqual(w['status'],'NO_TOUCH_IN_MONITORED_BARS')
    def test_report_unknown_and_idempotent(self):
        w=self.watch();self.s.report(self.day,T.replace(hour=16,minute=15))
        self.assertEqual(w['status'],'UNVERIFIED_AT_REPORT')
        n=len(self.s.state['outbox']);self.s.report(self.day,T);self.assertEqual(n,len(self.s.state['outbox']))
    def test_hit_report_never_shows_remaining_gap(self):
        w=self.watch();self.s.finish(w,T,12,'fixture',10);self.s.report(self.day,T)
        report=(self.s.directory/'reports/2026-11-19.txt').read_text()
        self.assertIn('touch ₹12.00',report);self.assertIn('touching-candle close ₹10.00',report)
        self.assertNotIn('gap +',report)
    def test_hit_idempotent(self):
        w=self.watch();self.s.finish(w,T,12,'fixture');n=len(self.s.state['outbox'])
        self.s.finish(w,T,14,'fixture');self.assertEqual(n,len(self.s.state['outbox']));self.assertEqual(w['hit_price'],12)
    def test_api_failure_advances_universe(self):
        self.s.universe=[{'symbol':'A'},{'symbol':'B'}]
        self.s.scan_one(self.day,T);self.assertEqual(self.day['cursor'],1)
        self.s.scan_one(self.day,T);self.assertEqual(self.day['sweeps'],1)
    def test_universe_equities_nearest_expiry(self):
        eq={'instrument_key':'EQ|1','segment':'NSE_EQ','instrument_type':'EQ','trading_symbol':'A'}
        opt={'instrument_key':'FO|1','segment':'NSE_FO','instrument_type':'CE','underlying_key':'EQ|1',
             'underlying_type':'EQUITY','underlying_symbol':'A','expiry':'2026-11-24','lot_size':100,'strike_price':100}
        rows=[eq,opt,{**opt,'instrument_key':'FO|2','expiry':'2026-12-29'},
              {**opt,'instrument_key':'FO|3','expiry':'2026-10-27'},
              {**opt,'instrument_key':'INDEX|1','underlying_type':'INDEX'}]
        u,m=b.universe_from_master(rows,'2026-11-19')
        self.assertEqual(len(u),1);self.assertEqual(u[0]['expiry'],'2026-11-24')
        self.assertNotIn('INDEX|1',m);self.assertNotIn('FO|3',m)
    def test_quotes_use_instrument_token(self):
        api=b.API.__new__(b.API)
        api.get=lambda *args:{'NSE_FO:SYMBOL':{'instrument_token':'NSE_FO|123','last_price':10}}
        self.assertEqual(api.quotes(['NSE_FO|123'])['NSE_FO|123']['last_price'],10)
    def test_wrong_state_rejected(self):
        self.s.path.write_text(json.dumps({'kind':'nifty'}))
        with self.assertRaises(RuntimeError):b.Scanner(self.api,self.tmp.name)

if __name__=='__main__':unittest.main()
