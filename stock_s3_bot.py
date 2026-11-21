#!/usr/bin/env python3
"""S3-only NSE stock-option value-gap research scanner. No orders or Kronos.
Python 3.11+. See README for valuation, coverage and monitoring assumptions.
"""
import argparse
from collections import Counter
from datetime import datetime, timedelta, time as dt_time
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import quote
from zoneinfo import ZoneInfo
import requests

IST=ZoneInfo('Asia/Kolkata')
FILTER_POLICY='BUY_ATM_GAP_DEPTH_V2'
ROOT=Path(__file__).resolve().parent
STATE_DIR=Path(os.getenv('STATE_DIR',str(ROOT/'state_s3')))
MASTER_URL='https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz'
GAP=float(os.getenv('MIN_VALUE_GAP','0.15'))
MAX_GAP=float(os.getenv('MAX_VALUE_GAP','0.17'))
MIN_DEPTH_LOTS=float(os.getenv('MIN_DEPTH_LOTS','3'))
if not (0 < GAP < MAX_GAP < 1) or not math.isfinite(MIN_DEPTH_LOTS) or MIN_DEPTH_LOTS < 3:
    raise ValueError('Require 0 < MIN_VALUE_GAP < MAX_VALUE_GAP < 1 and MIN_DEPTH_LOTS >= 3')
MAX_SPREAD=float(os.getenv('MAX_SPREAD','0.05'))
MIN_PREMIUM=float(os.getenv('MIN_PREMIUM','1'))
MIN_OI_LOTS=float(os.getenv('MIN_OI_LOTS','20'))
MIN_VOLUME_LOTS=float(os.getenv('MIN_VOLUME_LOTS','5'))
FRESH_SECONDS=int(os.getenv('FRESH_SECONDS','180'))
RISK_RATE=float(os.getenv('RISK_FREE_RATE','.06'))
DIVIDEND_YIELDS=json.loads(os.getenv('DIVIDEND_YIELDS_JSON','{}'))


def now():return datetime.now(IST)
def iso(t):return t.isoformat()
def parse(t):return datetime.fromisoformat(t).astimezone(IST)
def num(v,default=0.):
    try:
        f=float(v)
        return f if math.isfinite(f) else default
    except (TypeError,ValueError):return default


def expiry_date(value):
    try:
        if isinstance(value,(int,float)) or str(value).isdigit():
            return datetime.fromtimestamp(float(value)/1000,IST).date().isoformat()
        return datetime.fromisoformat(str(value).replace('Z','+00:00')).date().isoformat()
    except (ValueError,TypeError,OverflowError):return None


def universe_from_master(rows,date):
    equities={x['instrument_key']:x for x in rows if x.get('segment')=='NSE_EQ' and x.get('instrument_type')=='EQ'}
    grouped={};metadata={}
    for row in rows:
        if row.get('segment')!='NSE_FO' or row.get('instrument_type') not in ('CE','PE'):
            continue
        key=row.get('underlying_key','');expiry=expiry_date(row.get('expiry'))
        if row.get('underlying_type')!='EQUITY' or key not in equities or not expiry or expiry<date:
            continue
        if num(row.get('lot_size'))<=0 or num(row.get('strike_price'))<=0:
            continue
        symbol=row.get('underlying_symbol') or equities[key].get('trading_symbol')
        if not symbol or 'DUMMY' in key or 'NSETEST' in symbol:continue
        grouped.setdefault(key,{'underlying':key,'symbol':symbol,'expiries':set()})['expiries'].add(expiry)
        metadata[row['instrument_key']]={'lot':int(row['lot_size']),'expiry':expiry,
            'strike':num(row['strike_price']),'side':row['instrument_type'],'underlying':key,'symbol':symbol}
    universe=[{'underlying':g['underlying'],'symbol':g['symbol'],'expiry':min(g['expiries'])} for g in grouped.values()]
    return sorted(universe,key=lambda x:x['symbol']),metadata


def atm_strike(chain,spot):
    strikes={c['strike'] for c in chain}
    return min(strikes,key=lambda k:(abs(k-spot),-k)) if strikes else None


def select_band(chain,spot):
    strikes=sorted({c['strike'] for c in chain})
    if not strikes:return []
    # Nearest actual listed strike; ties choose the higher strike.
    atm=atm_strike(chain,spot);index=strikes.index(atm)
    selected=set(strikes[max(0,index-2):index+3])
    return [c for c in chain if c['strike'] in selected]


def liquid(c):
    m=c['market'];lot=c['lot'];bid=num(m.get('bid_price'));ask=num(m.get('ask_price'))
    return (lot>0 and num(m.get('ltp'))>=MIN_PREMIUM and 0<bid<=ask and
            (ask-bid)/max((ask+bid)/2,.01)<=MAX_SPREAD and
            num(m.get('oi'))>=MIN_OI_LOTS*lot and num(m.get('volume'))>=MIN_VOLUME_LOTS*lot and
            num(m.get('bid_qty'))>=lot and num(m.get('ask_qty'))>=lot)


def fresh(q,t):
    ms=num(q.get('last_trade_time'))
    if ms<=0:return False
    try:
        traded=datetime.fromtimestamp(ms/1000,IST)
        return traded.date()==t.date() and -5<=(t-traded).total_seconds()<=FRESH_SECONDS
    except (ValueError,OverflowError):return False


def quote_contract(c,q):
    buys=q.get('depth',{}).get('buy') or []
    sells=q.get('depth',{}).get('sell') or []
    if not buys or not sells:return None
    return {**c,'market':{'ltp':num(q.get('last_price')),'oi':num(q.get('oi')),'volume':num(q.get('volume')),
        'bid_price':num(buys[0].get('price')),'ask_price':num(sells[0].get('price')),
        'bid_qty':num(buys[0].get('quantity')),'ask_qty':num(sells[0].get('quantity'))},
        'quote_time':q.get('timestamp'),'last_trade_time':q.get('last_trade_time')}


def peer_iv(c,band):
    peers=[p for p in band if p['side']==c['side'] and p['key']!=c['key'] and liquid(p)
           and 0<num(p['greeks'].get('iv'))<200]
    below=sorted([p for p in peers if p['strike']<c['strike']],key=lambda p:-p['strike'])[:2]
    above=sorted([p for p in peers if p['strike']>c['strike']],key=lambda p:p['strike'])[:2]
    if not below or not above:return None
    def weighted(group):
        w=[math.sqrt(num(p['market']['oi']))/max(num(p['market']['ask_price'])-num(p['market']['bid_price']),.01) for p in group]
        return (sum(a*p['strike'] for a,p in zip(w,group))/sum(w),
                sum(a*num(p['greeks']['iv']) for a,p in zip(w,group))/sum(w))
    kl,vl=weighted(below);kh,vh=weighted(above)
    return (vl+(vh-vl)*(c['strike']-kl)/(kh-kl))/100


def bs(s,k,years,vol,side,r=RISK_RATE,q=0.):
    if min(s,k,years,vol)<=0:return max(0.,s-k if side=='CE' else k-s)
    n=lambda x:(1+math.erf(x/math.sqrt(2)))/2
    d1=(math.log(s/k)+(r-q+vol*vol/2)*years)/(vol*math.sqrt(years));d2=d1-vol*math.sqrt(years)
    if side=='CE':return s*math.exp(-q*years)*n(d1)-k*math.exp(-r*years)*n(d2)
    return k*math.exp(-r*years)*n(-d2)-s*math.exp(-q*years)*n(-d1)


def candidate(c,band,spot,t):
    if not liquid(c):return None,'liquidity'
    if min(num(c['market'].get('bid_qty')),num(c['market'].get('ask_qty'))) < MIN_DEPTH_LOTS*c['lot']:
        return None,'depth_below_threshold'
    expiry=datetime.fromisoformat(c['expiry']).replace(hour=15,minute=30,tzinfo=IST)
    if (expiry-t).total_seconds()<1800:return None,'near_expiry'
    vol=peer_iv(c,band)
    if vol is None:return None,'insufficient_peer_iv'
    years=(expiry-t).total_seconds()/(365*86400)
    div=num(DIVIDEND_YIELDS.get(c['symbol']),num(os.getenv('DIVIDEND_YIELD','0')))
    fair=bs(spot,c['strike'],years,vol,c['side'],q=div)
    if fair<MIN_PREMIUM:return None,'tiny_model_value'
    m=c['market'];ltp=num(m['ltp']);gap=(fair-ltp)/fair
    if fair<=ltp:return None,'sell_direction_excluded'
    executable=num(m['ask_price']) if gap>0 else num(m['bid_price'])
    executable_gap=(fair-executable)/fair
    # Explicit same-sign check: a crossed/stale LTP must not reverse the executable gap.
    if abs(gap)<GAP or gap*executable_gap<=0 or abs(executable_gap)<GAP:return None,'gap_below_threshold'
    if gap>=MAX_GAP:return None,'gap_above_band'
    return {'target':round(fair,4),'entry':ltp,'direction':'UP' if gap>0 else 'DOWN',
        'gap_pct':gap*100,'executable_gap_pct':executable_gap*100,'peer_iv':vol*100,
        'spot':spot,'years':years,'r':RISK_RATE,'q':div},None


class APIError(RuntimeError):pass
class AuthError(APIError):pass


class API:
    def __init__(self):
        token=os.getenv('UPSTOX_ACCESS_TOKEN','').strip()
        if not token:raise AuthError('Missing UPSTOX_ACCESS_TOKEN secret')
        self.session=requests.Session();self.session.headers.update({'Authorization':'Bearer '+token,'Accept':'application/json'})
        self.last_call=0.
        self.interval=max(1.2,num(os.getenv('REQUEST_INTERVAL','1.2'),1.2))

    def get(self,path,params=None):
        for attempt in range(3):
            wait=self.interval-(time.monotonic()-self.last_call)
            if wait>0:time.sleep(wait)
            self.last_call=time.monotonic()
            try:r=self.session.get('https://api.upstox.com'+path,params=params,timeout=20)
            except requests.RequestException:
                if attempt==2:raise APIError('Upstox connection failed') from None
                time.sleep(2**attempt);continue
            if r.status_code in (401,403):raise AuthError(f'Upstox HTTP {r.status_code}; check access token/data entitlement')
            if r.status_code==429 or r.status_code>=500:
                time.sleep(min(30,max(2**attempt,num(r.headers.get('Retry-After'),1))));continue
            if r.status_code!=200:raise APIError(f'Upstox HTTP {r.status_code}')
            try:body=r.json()
            except ValueError:raise APIError('Invalid Upstox JSON response') from None
            if body.get('status')!='success':raise APIError('Upstox unsuccessful response')
            return body['data']
        raise APIError('Upstox retry limit')

    def master(self):
        # Never attach broker authorization to a public file host.
        try:
            r=requests.get(MASTER_URL,timeout=45);r.raise_for_status()
            raw=gzip.decompress(r.content) if r.content[:2]==b'\x1f\x8b' else r.content
            rows=json.loads(raw)
            if not isinstance(rows,list):raise ValueError()
            return rows
        except (requests.RequestException,ValueError,OSError):raise APIError('Instrument master download failed') from None

    def chain(self,underlying,metadata):
        rows=self.get('/v2/option/chain',{'instrument_key':underlying['underlying'],'expiry_date':underlying['expiry']})
        result=[]
        for row in rows:
            for field,side in [('call_options','CE'),('put_options','PE')]:
                obj=row.get(field) or {};key=obj.get('instrument_key');meta=metadata.get(key)
                if not meta or meta['underlying']!=underlying['underlying'] or meta['expiry']!=underlying['expiry']:continue
                result.append({'key':key,'symbol':underlying['symbol'],'underlying':underlying['underlying'],
                    'expiry':underlying['expiry'],'strike':num(row['strike_price']),'side':side,'lot':meta['lot'],
                    'spot':num(row.get('underlying_spot_price')),'market':obj.get('market_data') or {},
                    'greeks':obj.get('option_greeks') or {}})
        return result

    def quotes(self,keys):
        out={};keys=list(dict.fromkeys(keys))
        for offset in range(0,len(keys),100):
            data=self.get('/v2/market-quote/quotes',{'instrument_key':','.join(keys[offset:offset+100])})
            for q in data.values():
                if q.get('instrument_token'):out[q['instrument_token']]=q
        return out

    def candles(self,key,date):
        k=quote(key,safe='')
        path=(f'/v3/historical-candle/intraday/{k}/minutes/1' if date==now().date().isoformat()
              else f'/v3/historical-candle/{k}/minutes/1/{date}/{date}')
        return sorted(self.get(path).get('candles',[]),key=lambda b:b[0])


class Scanner:
    def __init__(self,api,directory=STATE_DIR):
        self.api=api;self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        (self.directory/'reports').mkdir(exist_ok=True)
        self.path=self.directory/'state.json'
        self.state=json.loads(self.path.read_text()) if self.path.exists() else {'version':1,'kind':'stock_s3','days':{},'watches':[],'outbox':[]}
        if self.state.get('kind')!='stock_s3':raise RuntimeError('Wrong state folder: use separate s3-stock-state branch')
        self.universe=[];self.metadata={};self.master_day=None;self.last_sync=0.;self.last_quotes=0.

    def save(self):
        p=self.path.with_suffix('.tmp');p.write_text(json.dumps(self.state,indent=2,allow_nan=False));p.replace(self.path)

    def sync(self,force=False):
        self.save()
        if os.getenv('GIT_STATE')!='true' or (not force and time.monotonic()-self.last_sync<300):return
        def git(*args):
            r=subprocess.run(['git','-C',str(self.directory),*args],capture_output=True,text=True)
            if r.returncode:raise RuntimeError('Cannot checkpoint state; check Actions write permission')
            return r.stdout
        git('add','state.json','reports')
        if git('status','--porcelain').strip():git('commit','-m','Save S3 stock research state [skip ci]')
        git('push','origin','HEAD:s3-stock-state');self.last_sync=time.monotonic()

    def emit(self,id,text):
        # Split into Telegram-sized messages; event IDs make normal retries idempotent.
        lines=text.splitlines();chunks=[];chunk=''
        for line in lines:
            if len(chunk)+len(line)+1>3500:chunks.append(chunk);chunk=''
            chunk+=line+'\n'
        if chunk:chunks.append(chunk)
        for i,part in enumerate(chunks):
            eid=f'{id}:{i}'
            if not any(m['id']==eid for m in self.state['outbox']):self.state['outbox'].append({'id':eid,'text':part,'sent':False})
        self.save()

    def flush(self):
        token=os.getenv('TELEGRAM_BOT_TOKEN');chat=os.getenv('TELEGRAM_CHAT_ID')
        if not token or not chat:raise RuntimeError('Missing Telegram secrets')
        sent=0
        for message in self.state['outbox']:
            if message['sent']:continue
            try:
                r=requests.post(f'https://api.telegram.org/bot{token}/sendMessage',json={'chat_id':chat,'text':message['text']},timeout=20)
                data=r.json()
                if not r.ok or not data.get('ok'):
                    if r.status_code==429:
                        time.sleep(min(30,num(data.get('parameters',{}).get('retry_after'),5)));return
                    raise RuntimeError(f'Telegram HTTP {r.status_code}; pending message retained')
            except (requests.RequestException,ValueError):raise RuntimeError('Telegram connection failed; pending message retained') from None
            message['sent']=True;self.save();sent+=1
            time.sleep(max(3.1,num(os.getenv('TELEGRAM_SEND_INTERVAL','3.1'),3.1)))
            if sent>=5:break  # bound work per tick, leaving scanning/monitoring time

    def prepare(self,t):
        date=t.date().isoformat()
        if self.master_day!=date:
            self.universe,self.metadata=universe_from_master(self.api.master(),date)
            if not self.universe:raise APIError('No active NSE stock options found in instrument master')
            self.master_day=date
        day=self.state['days'].setdefault(date,{'date':date,'cursor':0,'sweeps':0,'symbols':{},'counts':{},'last_error':None})
        day['universe_count']=len(self.universe)
        return day

    def choose_buy_atm(self,verified,chain,spot,t,day):
        atm=atm_strike(chain,spot)
        options=[]
        for c in verified:
            if c['strike']!=atm:
                self.count(day,'non_atm_excluded');continue
            if any(w['day']==day['date'] and w['key']==c['key'] for w in self.state['watches']):continue
            value,reason=candidate(c,verified,spot,t)
            if reason:self.count(day,reason);continue
            options.append((c,{**value,'policy':FILTER_POLICY,'atm_at_entry':atm}))
        if not options:return None
        underlying=options[0][0]['underlying']
        if any(w['day']==day['date'] and w['underlying']==underlying and
               w.get('policy') in ('BUY_ATM_V1',FILTER_POLICY) and w['status']=='OPEN' for w in self.state['watches']):
            self.count(day,'existing_open_stock_watch');return None
        # If both ATM sides qualify, keep only the larger ask-based value gap.
        options.sort(key=lambda cv:(-cv[1]['executable_gap_pct'],cv[0]['side']))
        if len(options)>1:self.count(day,'other_atm_side_suppressed',len(options)-1)
        return options[0]

    def count(self,day,reason,n=1):day['counts'][reason]=day['counts'].get(reason,0)+n

    def add_watch(self,c,value,t,day):
        id=hashlib.sha256(f'{day["date"]}:{c["key"]}'.encode()).hexdigest()[:16]
        if any(w['id']==id for w in self.state['watches']):return None
        w={**c,**value,'id':id,'day':day['date'],'entered':iso(t),'status':'OPEN','last_price':value['entry'],
           'last_observed_at':iso(t),'checked_until':iso(t),'last_candle_attempt':None,
           'hit_time':None,'hit_price':None,'hit_evidence':None,'hit_candle_close':None}
        self.state['watches'].append(w)
        label='BUY-ONLY ATM WATCH' if value.get('policy')==FILTER_POLICY else ('BELOW MODEL VALUE' if value['direction']=='UP' else 'ABOVE MODEL VALUE')
        g=c['greeks'];m=c['market']
        self.emit('entry-'+id,f'S3 STOCK OPTIONS | {c["symbol"]} {c["strike"]:g} {c["side"]}\n'
            f'{label} | expiry {c["expiry"]}\nLTP ₹{value["entry"]:.2f} | fixed model target ₹{value["target"]:.2f}\n'
            f'Gap {abs(value["gap_pct"]):.1f}% of model value | direction {value["direction"]}\n'
            f'Bid ₹{num(m["bid_price"]):.2f} | ask ₹{num(m["ask_price"]):.2f}\n'
            f'Best-level depth: bid {num(m["bid_qty"])/c["lot"]:g} lots | ask {num(m["ask_qty"])/c["lot"]:g} lots\n'
            f'IV {num(g.get("iv")):.2f}% | peer IV {value["peer_iv"]:.2f}%\n'
            f'Delta {num(g.get("delta")):.3f} | Gamma {num(g.get("gamma")):.5f}\n'
            f'Theta {num(g.get("theta")):.2f} | Vega {num(g.get("vega")):.2f}\n'
            f'OI {num(m["oi"]):g} | volume {num(m["volume"]):g} | lot {c["lot"]}\n'
            f'{t:%H:%M:%S} IST | ID {id}\nValuation watch only; no buy/sell execution or stop-loss.')
        return w

    def finish(self,w,t,price,evidence,candle_close=None):
        if w['status']!='OPEN':return
        w.update(status='VALUE_REACHED',hit_time=iso(t),hit_price=price,hit_evidence=evidence,hit_candle_close=candle_close)
        note=(f'\nTouching candle close: ₹{candle_close:.2f} (not the touch price)' if candle_close is not None else '')
        self.emit('hit-'+w['id'],f'S3 VALUE REACHED | {w["symbol"]} {w["strike"]:g} {w["side"]}\n'
            f'Direction {w["direction"]} | reference ₹{w["entry"]:.2f}\nFixed target ₹{w["target"]:.2f}\n'
            f'Touch/reference price ₹{price:.2f} | {t:%Y-%m-%d %H:%M:%S} IST\nEvidence: {evidence}'+note+
            f'\nID {w["id"]}\nModel-price convergence; not a guaranteed trade fill or profit.')

    def scan_one(self,day,t):
        index=day['cursor']%len(self.universe);u=self.universe[index]
        day.setdefault('sweep_started',iso(t))
        try:
            chain=self.api.chain(u,self.metadata)
            checked=now();day['symbols'][u['symbol']]={'checked_at':iso(checked),'status':'chain_received'}
            self.count(day,'chains_received')
            if not chain:self.count(day,'empty_chain');return
            band=select_band(chain,chain[0]['spot'])
            if not any(liquid(c) for c in band):self.count(day,'no_liquid_contracts');return
            quotes=self.api.quotes([u['underlying']]+[c['key'] for c in band]);checked=now()
            if not dt_time(9,20)<=checked.time()<=dt_time(15,0):return
            underlying_quote=quotes.get(u['underlying'],{})
            if not fresh(underlying_quote,checked):self.count(day,'stale_underlying');return
            spot=num(underlying_quote.get('last_price'))
            if spot<=0:self.count(day,'invalid_spot');return
            # Recompute actual strike band if the spot moved while fetching quotes.
            desired=select_band(chain,spot)
            if {c['key'] for c in desired}!={c['key'] for c in band}:
                self.count(day,'atm_shift_during_request');return
            verified=[]
            for c in band:
                q=quotes.get(c['key'],{})
                if not fresh(q,checked):self.count(day,'stale_option_or_missing_quote');continue
                updated=quote_contract(c,q)
                if updated:verified.append(updated)
            # Chain IV is only seconds older than the quote; reject a long request delay.
            if (checked-t).total_seconds()>60:self.count(day,'chain_quote_delay');return
            choice=self.choose_buy_atm(verified,chain,spot,checked,day)
            if choice:
                c,value=choice
                if self.add_watch(c,value,checked,day):self.count(day,'new_alerts')
        except AuthError:raise
        except APIError as e:
            day['symbols'][u['symbol']]={'checked_at':iso(now()),'status':'error','reason':str(e)}
            self.count(day,'symbol_errors');day['last_error']=f'{u["symbol"]}: {e}'
        finally:
            day['cursor']=(index+1)%len(self.universe)
            if day['cursor']==0:
                day['sweeps']+=1;day['last_sweep_finished']=iso(now())
                day['last_sweep_seconds']=round((now()-parse(day['sweep_started'])).total_seconds())
                day['sweep_started']=iso(now())
            self.save()

    def monitor_quotes(self,t):
        if time.monotonic()-self.last_quotes<60:return
        open_w=[w for w in self.state['watches'] if w['status']=='OPEN' and w['day']==t.date().isoformat()]
        # API batches all open keys; monitoring may take longer when many watches exist.
        if open_w:
            quotes=self.api.quotes([w['key'] for w in open_w]);observed=now()
            if observed.date()!=t.date() or observed.time()>dt_time(15,30):return
            for w in open_w:
                q=quotes.get(w['key'],{})
                if not fresh(q,observed):continue
                ltp=num(q.get('last_price'))
                if ltp<=0:continue
                w.update(last_price=ltp,last_observed_at=iso(observed))
                hit=ltp>=w['target'] if w['direction']=='UP' else ltp<=w['target']
                if hit:self.finish(w,observed,ltp,'fresh polled LTP')
        self.last_quotes=time.monotonic()

    def candle_check(self,w,t):
        w['last_candle_attempt']=iso(t)
        try:bars=self.api.candles(w['key'],w['day'])
        except AuthError:raise
        except APIError:
            w['candle_error']=True;return
        w['candle_error']=False
        start=parse(w['entered']).replace(second=0,microsecond=0)+timedelta(minutes=1)
        cursor=parse(w['checked_until'])
        valid_minutes=set()
        for bar in bars:
            bt=parse(bar[0])
            if not dt_time(9,15)<=bt.time()<dt_time(15,30):continue
            if len(bar)<5 or any(num(x)<=0 for x in bar[1:5]):continue
            if num(bar[2])<max(num(bar[1]),num(bar[3]),num(bar[4])) or num(bar[3])>min(num(bar[1]),num(bar[4])):continue
            valid_minutes.add(bt)
            if bt.date().isoformat()!=w['day'] or bt<start or bt<=cursor or bt+timedelta(minutes=1)>t:continue
            w['checked_until']=iso(bt)
            if bt+timedelta(minutes=1)>=parse(w['last_observed_at']):
                w.update(last_price=num(bar[4]),last_observed_at=iso(bt+timedelta(minutes=1)))
            hit=num(bar[2])>=w['target'] if w['direction']=='UP' else num(bar[3])<=w['target']
            if hit:
                self.finish(w,bt+timedelta(minutes=1),w['target'],'completed 1m candle high/low; time is candle end',num(bar[4]));return
        if t.time()>=dt_time(15,32) or w['day']<t.date().isoformat():
            expected=start
            close=start.replace(hour=15,minute=30,second=0,microsecond=0)
            while expected<close and expected in valid_minutes:expected+=timedelta(minutes=1)
            if expected==close:w['status']='NO_TOUCH_IN_MONITORED_BARS'

    def monitor_candles(self,t,budget=1):
        watches=[w for w in self.state['watches'] if w['status']=='OPEN' and
                 (not w['last_candle_attempt'] or (t-parse(w['last_candle_attempt'])).total_seconds()>=300)]
        watches.sort(key=lambda w:w['last_candle_attempt'] or '')
        for w in watches[:budget]:self.candle_check(w,t)

    def report(self,day,t):
        if day.get('reported'):return
        rows=[w for w in self.state['watches'] if w['day']==day['date']]
        for w in rows:
            if w['status']=='OPEN':w['status']='UNVERIFIED_AT_REPORT'
        counts=Counter(w['status'] for w in rows)
        cohort=[w for w in rows if w.get('policy')==FILTER_POLICY]
        hits=sum(w['status']=='VALUE_REACHED' for w in cohort)
        rate=f'{hits/len(cohort)*100:.1f}%' if cohort else 'N/A'
        good=sum(x.get('status')=='chain_received' for x in day['symbols'].values())
        lines=[f'S3 STOCK OPTIONS DAILY REPORT | {day["date"]}',
            f'BUY-ONLY ATM: {len(cohort)} alerts; {hits} targets touched; touch rate {rate} (not net win rate)',
            f'Legacy watches from earlier rules: {len(rows)-len(cohort)}; included separately in the full totals below.',
            f'Stocks in universe: {day["universe_count"]}; stocks with a successful latest chain check: {good}',
            f'Completed sweeps: {day["sweeps"]}; last completed sweep: {day.get("last_sweep_finished","none")}',
            f'Last sweep duration: {day.get("last_sweep_seconds","unknown")} seconds',
            f'Alerts: {len(rows)} | model targets touched: {counts["VALUE_REACHED"]}',
            f'No touch in monitored full-minute bars: {counts["NO_TOUCH_IN_MONITORED_BARS"]} | unverified: {counts["UNVERIFIED_AT_REPORT"]}',
            'The partial entry minute is excluded from candle checks; its unobserved movements are unknown.',
            'A model-value touch is not a profitable buy trade. DOWN watches describe falling option premiums.','']
        for side in ('CE','PE'):
            subset=[w for w in cohort if w['side']==side]
            h=sum(w['status']=='VALUE_REACHED' for w in subset)
            lines.append(f'BUY-ONLY ATM {side}: {h}/{len(subset)} targets touched')
        for band in sorted({int(w['gap_pct']) for w in cohort}):
            subset=[w for w in cohort if int(w['gap_pct'])==band]
            h=sum(w['status']=='VALUE_REACHED' for w in subset)
            lines.append(f'BUY-ONLY ATM gap {band}-{band+1}%: {h}/{len(subset)} touched ({h/len(subset)*100:.1f}%)')
        for w in rows:
            line=f'{w["symbol"]} {w["strike"]:g} {w["side"]} ({w["expiry"]}) | {w["direction"]} | {w["status"]}\nReference ₹{w["entry"]:.2f}; fixed target ₹{w["target"]:.2f}'
            if w['status']=='VALUE_REACHED':
                line+=f'; touch ₹{w["hit_price"]:.2f} at {parse(w["hit_time"]):%H:%M:%S} IST ({w["hit_evidence"]})'
                if w['hit_candle_close'] is not None:line+=f'; touching-candle close ₹{w["hit_candle_close"]:.2f}'
            else:
                gap=(w['target']-w['last_price'])/w['target']*100
                line+=f'; last observed ₹{w["last_price"]:.2f} at {parse(w["last_observed_at"]):%H:%M:%S}; gap {gap:+.1f}%'
            lines.append(line)
        lines+=['','WHY CONTRACTS WERE SKIPPED (checks, not unique contracts)']
        lines += [f'{k}: {v}' for k,v in sorted(day['counts'].items())]
        if day.get('last_error'):lines.append('Last data issue: '+day['last_error'])
        text='\n'.join(lines);folder=self.directory/'reports'
        (folder/f'{day["date"]}.txt').write_text(text)
        (folder/f'{day["date"]}.json').write_text(json.dumps({'day':day,'watches':rows},indent=2))
        self.emit('report-'+day['date'],text);day['reported']=True;self.save()

    def tick(self,t):
        if t.weekday()>=5:return
        day=self.prepare(t)
        self.emit('online-'+FILTER_POLICY+'-'+day['date'],f'S3 STOCK OPTIONS ONLINE | {day["date"]}\nBUY-ONLY ATM: CE or PE only when its premium must RISE to the target.\nUniverse: {len(self.universe)} NSE F&O stocks; nearest expiry. ATM ±2 supplies IV peers; only ATM can alert.\nOne active new-policy watch per stock; if both sides qualify, larger ask-based gap wins.\nStarting gap {GAP*100:g}% to below {MAX_GAP*100:g}%; ask gap at least {GAP*100:g}%. Best bid AND ask depth at least {MIN_DEPTH_LOTS:g} lots each. S3 only.\nFull sweeps take several minutes; report around 16:15 IST.')
        # Finish interrupted prior days as unverified instead of inventing outcomes.
        for date,old in list(self.state['days'].items()):
            if date<day['date'] and not old.get('reported'):
                self.report(old,t)
        if t.time()>=dt_time(16,15):self.report(day,t);return
        if not dt_time(9,20)<=t.time()<=dt_time(16,15):return
        if t.time()<=dt_time(15,30):self.monitor_quotes(t)
        self.monitor_candles(now(),budget=1 if t.time()<=dt_time(15,30) else 10)
        if t.time()<=dt_time(15,0):self.scan_one(day,now())


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--mode',choices=['test','run','once'],default='run');args=ap.parse_args()
    scanner=Scanner(API());start=now()
    if args.mode=='test':
        scanner.prepare(start);u=scanner.universe[0]
        chain=scanner.api.chain(u,scanner.metadata)
        scanner.emit('test-'+iso(start),f'S3 stock scanner connection OK. Discovered {len(scanner.universe)} stock-option underlyings.\nSample API check {u["symbol"]}: {len(chain)} contracts in full chain; {len(select_band(chain,chain[0]["spot"])) if chain else 0} IV-support contracts at ATM ±2. New alerts: BUY-ONLY ATM CE or PE; no falling-premium watches.\nS3 only; no S1/S2. This verifies connectivity, not live signal quality.')
        for _ in range(3):scanner.flush()
        scanner.sync(force=True);return
    end=start.replace(hour=14,minute=10,second=0,microsecond=0) if start.time()<dt_time(14,10) else start.replace(hour=16,minute=45,second=0,microsecond=0)
    end=min(end,start+timedelta(hours=5,minutes=15));failures=0
    try:
        while True:
            t=now()
            try:
                scanner.tick(t);scanner.flush();scanner.sync();failures=0
            except AuthError:
                raise
            except Exception as e:
                failures+=1
                reason=str(e) if isinstance(e,RuntimeError) else type(e).__name__
                print(f'{t:%H:%M} {reason}',flush=True)
                if failures in (1,10):
                    scanner.emit(f'error-{t:%Y%m%d%H}-{failures}','S3 stock scanner issue: '+reason)
                    try:scanner.flush()
                    except Exception:pass
                scanner.save()
                if failures>=20:raise RuntimeError('20 consecutive failures; check workflow logs') from None
                time.sleep(15)
            if args.mode=='once' or now()>=end or t.weekday()>=5:break
            time.sleep(2 if dt_time(9,20)<=now().time()<=dt_time(15,0) else 15)
    finally:scanner.sync(force=True)


if __name__=='__main__':main()
