#!/usr/bin/env python3
"""S3-only NSE stock-option value-gap research scanner. No orders or Kronos.
Python 3.11+. See README for valuation, coverage and monitoring assumptions.
V4 rule lab: every buy-direction ATM candidate is scored against ten rules (T D O A V G E I P S).
Only candidates passing all MAIN_RULES (default S, or P on its own via MAIN_ALSO_IF; every other rule is recorded, not required) are sent to Telegram; the rest are recorded silently
with a header showing which rules passed, so the rules can be compared after a few days.
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
FILTER_POLICY='BUY_ATM_RULELAB_V4'
LEGACY_POLICIES=('BUY_ATM_V1','BUY_ATM_GAP_DEPTH_V2','BUY_ATM_GAP_DEPTH_OI_V3')
ROOT=Path(__file__).resolve().parent
STATE_DIR=Path(os.getenv('STATE_DIR',str(ROOT/'state_s3')))
MASTER_URL='https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz'
GAP=float(os.getenv('MIN_VALUE_GAP','0.05'))  # lowered from 0.15: next-month expiry has a much narrower gap range than the 2-3 day expiry
BASE_MAX_GAP=float(os.getenv('BASE_MAX_VALUE_GAP','0.17'))
MAX_GAP=float(os.getenv('MAX_VALUE_GAP','0.30'))
EXTENDED_MIN_OI_LOTS=float(os.getenv('EXTENDED_MIN_OI_LOTS','2000'))
MIN_DEPTH_LOTS=float(os.getenv('MIN_DEPTH_LOTS','3'))
if not (0 < GAP < BASE_MAX_GAP < MAX_GAP < 1) or not math.isfinite(MIN_DEPTH_LOTS) or MIN_DEPTH_LOTS < 3:
    raise ValueError('Invalid gap bands or depth floor')
if not math.isfinite(EXTENDED_MIN_OI_LOTS) or EXTENDED_MIN_OI_LOTS < 2000:
    raise ValueError('EXTENDED_MIN_OI_LOTS must be at least 2000')
# V4 rule thresholds. T (tier OI) and D (depth) reuse EXTENDED_MIN_OI_LOTS / MIN_DEPTH_LOTS above.
RULE_MIN_OI_LOTS=float(os.getenv('RULE_MIN_OI_LOTS','500'))
RULE_MAX_TRADE_AGE=float(os.getenv('RULE_MAX_TRADE_AGE','10'))
RULE_MAX_VOL_OI=float(os.getenv('RULE_MAX_VOL_OI','3'))
RULE_MIN_GAMMA=float(os.getenv('RULE_MIN_GAMMA','0.006'))
RULE_MIN_ABS_DELTA=float(os.getenv('RULE_MIN_ABS_DELTA','0.35'))
RULE_MAX_IV=float(os.getenv('RULE_MAX_IV','28'))
RULE_P_MIN_GAP=float(os.getenv('RULE_P_MIN_GAP','0.15'))  # P (gap + depth route) keeps its own 15% lower edge
RULE_S_MIN_GAP=float(os.getenv('RULE_S_MIN_GAP','0.05'))   # S: small gap band that hit 7 of 7 on 25 Nov 2026 ...
RULE_S_MAX_GAP=float(os.getenv('RULE_S_MAX_GAP','0.08'))
try:                                                          # ... and only for alerts before this time (IST, HH:MM)
    _h,_m=os.getenv('RULE_S_CUTOFF','15:00').split(':');RULE_S_CUTOFF_MIN=int(_h)*60+int(_m)
except ValueError:
    raise ValueError('RULE_S_CUTOFF must look like 15:00')
RULE_ORDER='TDOAVGEIPS'
MAIN_RULES=os.getenv('MAIN_RULES','S').strip().upper()  # every other rule is scored and recorded but not required by default
MAIN_ALSO_IF=os.getenv('MAIN_ALSO_IF','P').strip().upper()  # extra route to MAIN: all these rules passing is enough on their own; empty disables
OBSERVE_ALL=os.getenv('OBSERVE_ALL','true').strip().lower()!='false'      # log every ATM contract scanned, alert or not
OBSERVE_BAND=os.getenv('OBSERVE_BAND','false').strip().lower()=='true'    # also log the non-ATM peer contracts
OBS_MIN_GAP=float(os.getenv('OBSERVE_FUNNEL_MIN_GAP','0.05'))             # funnel counts contracts valued at least this far above price
REPORT_SILENT_LINES=os.getenv('REPORT_SILENT_LINES','true').strip().lower()!='false'
if (not MAIN_RULES or any(k not in RULE_ORDER for k in MAIN_RULES) or len(set(MAIN_RULES))!=len(MAIN_RULES)
        or any(k not in RULE_ORDER for k in MAIN_ALSO_IF) or len(set(MAIN_ALSO_IF))!=len(MAIN_ALSO_IF)
        or not (0<RULE_P_MIN_GAP<BASE_MAX_GAP) or not (0<RULE_S_MIN_GAP<RULE_S_MAX_GAP<1) or not (0<=RULE_S_CUTOFF_MIN<=24*60)
        or not all(math.isfinite(x) and x>0 for x in (RULE_MIN_OI_LOTS,RULE_MAX_TRADE_AGE,RULE_MAX_VOL_OI,RULE_MIN_GAMMA,RULE_MIN_ABS_DELTA,RULE_MAX_IV))):
    raise ValueError('Invalid MAIN_RULES (letters from TDOAVGEIP, no repeats; MAIN_ALSO_IF may be empty) or rule thresholds')
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


def stock_close(t):
    day=t.date().isoformat()
    return dt_time(15,45) if day>='2026-12-07' else (dt_time(15,40) if day>='2026-08-03' else dt_time(15,30))


def close_at(t):
    return t.replace(hour=stock_close(t).hour,minute=stock_close(t).minute,second=0,microsecond=0)


def alert_cutoff(t):
    return dt_time(15,20)


def selected_expiry(expiries,day):
    # Five calendar days; actual expiry dates come from the instrument master.
    eligible=sorted(e for e in expiries if
        (datetime.fromisoformat(e).date()-datetime.fromisoformat(day).date()).days>5)
    return eligible[0] if eligible else None


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
    universe=[]
    for g in grouped.values():
        expiry=selected_expiry(g['expiries'],date)
        if expiry:universe.append({'underlying':g['underlying'],'symbol':g['symbol'],'expiry':expiry})
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


RULE_TEXT={'T':f'tier OI (gap below {BASE_MAX_GAP*100:g}%: any; {BASE_MAX_GAP*100:g}-<{MAX_GAP*100:g}%: >= {EXTENDED_MIN_OI_LOTS:g} lots)',
    'D':f'depth >= {MIN_DEPTH_LOTS:g} lots on best bid AND ask',
    'O':f'OI >= {RULE_MIN_OI_LOTS:g} lots','A':f'last trade <= {RULE_MAX_TRADE_AGE:g} s old',
    'V':f'volume/OI <= {RULE_MAX_VOL_OI:g}','G':f'gamma >= {RULE_MIN_GAMMA:g}',
    'E':f'|delta| >= {RULE_MIN_ABS_DELTA:g}','I':f'IV <= {RULE_MAX_IV:g}%',
    'P':f'gap {RULE_P_MIN_GAP*100:g}% to below {BASE_MAX_GAP*100:g}% AND depth >= {MIN_DEPTH_LOTS:g} lots on best bid and ask',
    'S':f'gap {RULE_S_MIN_GAP*100:g}% to {RULE_S_MAX_GAP*100:g}% AND alert before {RULE_S_CUTOFF_MIN//60:02d}:{RULE_S_CUTOFF_MIN%60:02d} IST'}


def main_rule_text():
    return MAIN_RULES+(f' or {MAIN_ALSO_IF}' if MAIN_ALSO_IF else '')


def rule_legend():
    return 'Rules: '+' | '.join(f'{k}={RULE_TEXT[k]}' for k in RULE_ORDER)


def trade_age_seconds(c,t):
    """Seconds between the quote timestamp (or check time) and the contract's last trade."""
    ms=num(c.get('last_trade_time'))
    if ms<=0:return None
    try:traded=datetime.fromtimestamp(ms/1000,IST)
    except (ValueError,OverflowError):return None
    ref=t
    if c.get('quote_time'):
        try:ref=parse(str(c['quote_time']))
        except (ValueError,TypeError):ref=t
    return (ref-traded).total_seconds()


def rule_metrics(c,gap,t):
    m=c['market'];lot=c['lot'];g=c.get('greeks') or {}
    oi=num(m.get('oi'));volume=num(m.get('volume'))
    bid=num(m.get('bid_price'));ask=num(m.get('ask_price'));mid=(bid+ask)/2
    bid_lots=num(m.get('bid_qty'))/lot;ask_lots=num(m.get('ask_qty'))/lot
    age=trade_age_seconds(c,t)
    return {'gap_pct':gap*100,'minute':t.hour*60+t.minute,'oi_lots':oi/lot,'volume_lots':volume/lot,
        'vol_oi':volume/oi if oi>0 else None,'bid_lots':bid_lots,'ask_lots':ask_lots,
        'depth_lots':min(bid_lots,ask_lots),'trade_age_s':age,'gamma':num(g.get('gamma')),
        'delta':num(g.get('delta')),'iv':num(g.get('iv')),'theta':num(g.get('theta')),'vega':num(g.get('vega')),
        'spread_pct':(ask-bid)/mid*100 if mid>0 else None}


def evaluate_rules(metrics):
    """True/False per rule letter. Missing data fails the rule (never passes by default)."""
    gap=metrics['gap_pct']/100;age=metrics['trade_age_s'];ratio=metrics['vol_oi']
    depth=metrics['depth_lots']>=MIN_DEPTH_LOTS
    return {'T':gap<BASE_MAX_GAP or metrics['oi_lots']>=EXTENDED_MIN_OI_LOTS,
        'D':depth,
        'O':metrics['oi_lots']>=RULE_MIN_OI_LOTS,
        'A':age is not None and -5<=age<=RULE_MAX_TRADE_AGE,
        'V':ratio is not None and ratio<=RULE_MAX_VOL_OI,
        'G':metrics['gamma']>=RULE_MIN_GAMMA,
        'E':abs(metrics['delta'])>=RULE_MIN_ABS_DELTA,
        'I':0<metrics['iv']<=RULE_MAX_IV,
        'P':RULE_P_MIN_GAP<=gap<BASE_MAX_GAP and depth,
        'S':RULE_S_MIN_GAP<=gap<=RULE_S_MAX_GAP and metrics['minute']<RULE_S_CUTOFF_MIN}


def rule_header(flags):
    """Header shown on every record: channel, tick/cross per rule, count passed."""
    via_rules=all(flags[k] for k in MAIN_RULES)
    via_also=bool(MAIN_ALSO_IF) and all(flags[k] for k in MAIN_ALSO_IF)
    channel='MAIN' if via_rules or via_also else 'SILENT'
    label='MAIN' if via_rules or not via_also else f'MAIN via {MAIN_ALSO_IF}'
    if channel=='SILENT':label='SILENT'
    marks=' '.join(f'{k}{"✓" if flags[k] else "✗"}' for k in RULE_ORDER)
    return channel,f'{label} | {marks} | {sum(flags.values())}/{len(RULE_ORDER)} rules passed'


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
    expiry=close_at(datetime.fromisoformat(c['expiry']).replace(tzinfo=IST))
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
    # Tier OI and depth are scored as rules (T, D) rather than hard rejections, so failing
    # candidates can be recorded silently and compared with the ones that pass.
    metrics=rule_metrics(c,gap,t);flags=evaluate_rules(metrics);channel,header=rule_header(flags)
    return {'target':round(fair,4),'entry':ltp,'direction':'UP' if gap>0 else 'DOWN',
        'gap_pct':gap*100,'executable_gap_pct':executable_gap*100,'peer_iv':vol*100,
        'entry_oi_lots':num(m.get('oi'))/c['lot'],
        'gap_tier':'EXTENDED_OI' if gap>=BASE_MAX_GAP else 'BASE',
        'rules':flags,'rules_passed':''.join(k for k in RULE_ORDER if flags[k]),
        'rules_failed':''.join(k for k in RULE_ORDER if not flags[k]),
        'metrics':metrics,'channel':channel,'header':header,
        'spot':spot,'years':years,'r':RISK_RATE,'q':div},None


def obs_value(c,band,spot,t):
    """Model value and gap for a snapshot row, computed with no gating at all; {} when not computable."""
    try:
        expiry=close_at(datetime.fromisoformat(c['expiry']).replace(tzinfo=IST))
        years=(expiry-t).total_seconds()/(365*86400)
        vol=peer_iv(c,band)
        if years<=0 or vol is None:return {}
        div=num(DIVIDEND_YIELDS.get(c['symbol']),num(os.getenv('DIVIDEND_YIELD','0')))
        fair=bs(spot,c['strike'],years,vol,c['side'],q=div)
        m=c['market'];ltp=num(m.get('ltp'))
        if fair<=0:return {}
        gap=(fair-ltp)/fair;executable=num(m.get('ask_price')) if gap>0 else num(m.get('bid_price'))
        return {'peer_iv':round(vol*100,3),'fair':round(fair,4),'gap_pct':round(gap*100,3),
                'xgap_pct':round((fair-executable)/fair*100,3)}
    except (ValueError,ZeroDivisionError,OverflowError,KeyError,TypeError):return {}


def snapshot_row(c,band,spot,t,reason,value=None,source='quote'):
    """One observation-log row: market data, greeks and model value as seen at scan time."""
    m=c.get('market') or {};lot=c['lot'];bid=num(m.get('bid_price'));ask=num(m.get('ask_price'));mid=(bid+ask)/2
    row={'t':iso(t),'sym':c['symbol'],'exp':c['expiry'],'k':c['strike'],'side':c['side'],'lot':lot,'spot':spot,'src':source,
         'ltp':num(m.get('ltp')),'bid':bid,'ask':ask,'bid_lots':round(num(m.get('bid_qty'))/lot,2),
         'ask_lots':round(num(m.get('ask_qty'))/lot,2),'oi_lots':round(num(m.get('oi'))/lot,1),
         'vol_lots':round(num(m.get('volume'))/lot,1),'spread_pct':round((ask-bid)/mid*100,3) if mid>0 else None,
         'qt':c.get('quote_time'),'ltt':c.get('last_trade_time'),
         'age_s':trade_age_seconds(c,t) if c.get('last_trade_time') else None,
         'greeks':c.get('greeks') or {},'reason':reason}
    row.update(obs_value(c,band,spot,t))
    if value:row.update({'chan':value.get('channel'),'passed':value.get('rules_passed'),'failed':value.get('rules_failed')})
    return row


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
        git('add','state.json','reports',*(['observations'] if any((self.directory/'observations').glob('*')) else []))
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
        options=[];rows={}
        def mark(c,action):
            if c['key'] in rows:rows[c['key']]['action']=action
        for c in verified:
            if c['strike']!=atm:
                self.count(day,'non_atm_excluded')
                if OBSERVE_ALL and OBSERVE_BAND:rows[c['key']]=snapshot_row(c,verified,spot,t,'non_atm')
                continue
            value,reason=candidate(c,verified,spot,t)
            if OBSERVE_ALL:rows[c['key']]=snapshot_row(c,verified,spot,t,reason or 'CANDIDATE',value)
            if reason:self.count(day,reason);continue
            prior=[w for w in self.state['watches'] if w['day']==day['date'] and w['key']==c['key']]
            # A contract already alerted (MAIN or legacy) is never repeated. A SILENT record only
            # blocks another SILENT record, so the same contract can still become a MAIN alert.
            if any(w.get('channel')!='SILENT' for w in prior) or (prior and value['channel']=='SILENT'):
                mark(c,'DUPLICATE_CONTRACT');continue
            options.append((c,{**value,'policy':FILTER_POLICY,'atm_at_entry':atm}))
        def flush():
            if rows:self.observe(day,list(rows.values()))
        if not options:flush();return None
        underlying=options[0][0]['underlying']
        def blocked(channel):
            for w in self.state['watches']:
                if w['day']!=day['date'] or w['underlying']!=underlying or w['status']!='OPEN':continue
                if channel=='MAIN' and (w.get('policy') in LEGACY_POLICIES or
                        (w.get('policy')==FILTER_POLICY and w.get('channel')=='MAIN')):return True
                if channel=='SILENT' and w.get('policy')==FILTER_POLICY and w.get('channel')=='SILENT':return True
            return False
        # MAIN first, then most rules passed, then larger ask-based gap (CE wins an exact tie).
        options.sort(key=lambda cv:(cv[1]['channel']!='MAIN',-len(cv[1]['rules_passed']),
                                    -cv[1]['executable_gap_pct'],cv[0]['side']))
        chosen=next((cv for cv in options if not blocked(cv[1]['channel'])),None)
        if chosen is None:
            for c,_ in options:mark(c,'BLOCKED_BY_OPEN_WATCH')
            flush();self.count(day,'existing_open_stock_watch');return None
        for c,_ in options:mark(c,'SELECTED' if c is chosen[0] else 'OTHER_SIDE_SUPPRESSED')
        flush()
        if len(options)>1:self.count(day,'other_atm_side_suppressed',len(options)-1)
        return chosen

    def count(self,day,reason,n=1):day['counts'][reason]=day['counts'].get(reason,0)+n

    def observe(self,day,rows):
        """Append rows to observations/<date>.jsonl; never raises into the scan loop."""
        if not OBSERVE_ALL or not rows:return
        lines=[]
        for r in rows:
            try:lines.append(json.dumps(r,separators=(',',':'),allow_nan=False))
            except (TypeError,ValueError):self.count(day,'observation_write_error')
        if not lines:return
        try:
            folder=self.directory/'observations';folder.mkdir(exist_ok=True)
            with open(folder/f'{day["date"]}.jsonl','a',encoding='utf-8') as f:f.write('\n'.join(lines)+'\n')
            day['observed_rows']=day.get('observed_rows',0)+len(lines)
        except OSError as e:day['last_error']=f'observation log: {e}'

    def observation_summary(self,day):
        if not OBSERVE_ALL:return ['OBSERVATION LOG | disabled (OBSERVE_ALL=false)']
        path=self.directory/'observations'/f'{day["date"]}.jsonl'
        if not path.exists():return ['OBSERVATION LOG | no snapshots were saved today']
        rows=[]
        for line in path.read_text(encoding='utf-8').splitlines():
            try:rows.append(json.loads(line))
            except ValueError:continue
        by={}
        for r in rows:by.setdefault((r['sym'],r['exp'],r['k'],r['side']),[]).append(r)
        lines=[f'OBSERVATION LOG | {len(rows)} snapshots of {len(by)} ATM contracts in {len({r["sym"] for r in rows})} stocks, '
               f'each with bid, ask, depth, OI, volume, last trade, IV, greeks and model value, saved to observations/{day["date"]}.jsonl.gz '
               '(every contract scanned, whether or not an alert was sent)',
               'Snapshots by result: '+', '.join(f'{k} {v}' for k,v in Counter(r['reason'] for r in rows).most_common())]
        funnel=Counter();later=Counter()
        for seq in by.values():
            seq.sort(key=lambda r:r['t'])
            for i,r in enumerate(seq):
                gap=r.get('gap_pct');fair=r.get('fair')
                if gap is None or not fair or gap<OBS_MIN_GAP*100:continue
                if r['reason']!='CANDIDATE':cat=r['reason']
                elif r.get('action')=='SELECTED':cat='MAIN alert' if r.get('chan')=='MAIN' else 'silent record'
                else:cat='candidate not recorded ('+str(r.get('action','unknown')).lower()+')'
                funnel[cat]+=1
                if any(num(x.get('ltp'))>=fair for x in seq[i+1:]):later[cat]+=1
                break
        if funnel:
            lines+=['',f'FILTER FUNNEL | first sighting of each contract valued at least {OBS_MIN_GAP*100:g}% above its price. '
                    '"Later traded at/above value" is checked only at scan times, so it understates real touches']
            lines+=[f'{cat}: {n} contracts, {later[cat]} later traded at/above the model value ({later[cat]/n*100:.0f}%)'
                    for cat,n in funnel.most_common(14)]
        return lines

    def compress_observations(self,day):
        path=self.directory/'observations'/f'{day["date"]}.jsonl'
        if not path.exists():return
        try:
            with open(path,'rb') as src,gzip.open(path.with_suffix('.jsonl.gz'),'wb') as dst:dst.write(src.read())
            path.unlink()
        except OSError as e:day['last_error']=f'observation compress: {e}'

    def add_watch(self,c,value,t,day):
        channel=value.get('channel','MAIN')
        id=hashlib.sha256(f'{day["date"]}:{c["key"]}:{channel}'.encode()).hexdigest()[:16]
        if any(w['id']==id for w in self.state['watches']):return None
        w={**c,**value,'channel':channel,'id':id,'day':day['date'],'entered':iso(t),'status':'OPEN','last_price':value['entry'],
           'last_observed_at':iso(t),'checked_until':iso(t),'last_candle_attempt':None,
           'hit_time':None,'hit_price':None,'hit_evidence':None,'hit_candle_close':None}
        self.state['watches'].append(w)
        if channel=='SILENT':return w  # recorded for the rule lab; no Telegram alert
        label='BUY-ONLY ATM WATCH' if value.get('policy')==FILTER_POLICY else ('BELOW MODEL VALUE' if value['direction']=='UP' else 'ABOVE MODEL VALUE')
        g=c['greeks'];m=c['market'];hdr=value.get('header')
        self.emit('entry-'+id,f'S3 STOCK OPTIONS | {c["symbol"]} {c["strike"]:g} {c["side"]}\n'
            +(f'{hdr}\n' if hdr else '')+
            f'{label} | expiry {c["expiry"]}\nLTP ₹{value["entry"]:.2f} | fixed model target ₹{value["target"]:.2f}\n'
            f'Gap {abs(value["gap_pct"]):.1f}% of model value | direction {value["direction"]}\n'
            f'Bid ₹{num(m["bid_price"]):.2f} | ask ₹{num(m["ask_price"]):.2f}\n'
            f'Best-level depth: bid {num(m["bid_qty"])/c["lot"]:g} lots | ask {num(m["ask_qty"])/c["lot"]:g} lots\n'
            f'IV {num(g.get("iv")):.2f}% | peer IV {value["peer_iv"]:.2f}%\n'
            f'Delta {num(g.get("delta")):.3f} | Gamma {num(g.get("gamma")):.5f}\n'
            f'Theta {num(g.get("theta")):.2f} | Vega {num(g.get("vega")):.2f}\n'
            f'OI {num(m["oi"]):g} | volume {num(m["volume"]):g} | lot {c["lot"]}\n'
            +(self.value_line(value)+'\n' if value.get('metrics') else '')+
            f'{t:%H:%M:%S} IST | ID {id}\nValuation watch only; no buy/sell execution or stop-loss.')
        return w

    @staticmethod
    def value_line(value):
        x=value['metrics'];age=x.get('trade_age_s');ratio=x.get('vol_oi')
        return (f'Rule values: OI {x["oi_lots"]:g} lots | last trade '
                f'{"n/a" if age is None else f"{age:.1f}s"} ago | vol/OI '
                f'{"n/a" if ratio is None else f"{ratio:.2f}"} | depth {x["depth_lots"]:g} lots | gamma {x["gamma"]:.5f}')

    def finish(self,w,t,price,evidence,candle_close=None):
        if w['status']!='OPEN':return
        w.update(status='VALUE_REACHED',hit_time=iso(t),hit_price=price,hit_evidence=evidence,hit_candle_close=candle_close)
        if w.get('channel')=='SILENT':return  # silent record: outcome only appears in reports
        note=(f'\nTouching candle close: ₹{candle_close:.2f} (not the touch price)' if candle_close is not None else '')
        self.emit('hit-'+w['id'],f'S3 VALUE REACHED | {w["symbol"]} {w["strike"]:g} {w["side"]}\n'
            +(f'{w["header"]}\n' if w.get('header') else '')+
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
            spot0=chain[0]['spot'];band=select_band(chain,spot0);atm0=atm_strike(chain,spot0)
            def drop(reason):
                self.count(day,reason)
                if OBSERVE_ALL and dt_time(9,20)<=checked.time()<=alert_cutoff(checked):
                    self.observe(day,[snapshot_row(c,band,spot0,checked,reason,source='chain') for c in chain if c['strike']==atm0])
            if not any(liquid(c) for c in band):drop('no_liquid_contracts');return
            quotes=self.api.quotes([u['underlying']]+[c['key'] for c in band]);checked=now()
            if not dt_time(9,20)<=checked.time()<=alert_cutoff(checked):return
            underlying_quote=quotes.get(u['underlying'],{})
            if not fresh(underlying_quote,checked):drop('stale_underlying');return
            spot=num(underlying_quote.get('last_price'))
            if spot<=0:drop('invalid_spot');return
            # Recompute actual strike band if the spot moved while fetching quotes.
            desired=select_band(chain,spot)
            if {c['key'] for c in desired}!={c['key'] for c in band}:
                drop('atm_shift_during_request');return
            verified=[]
            for c in band:
                q=quotes.get(c['key'],{})
                if not fresh(q,checked):self.count(day,'stale_option_or_missing_quote');continue
                updated=quote_contract(c,q)
                if updated:verified.append(updated)
            # Chain IV is only seconds older than the quote; reject a long request delay.
            if OBSERVE_ALL:
                vkeys={c['key'] for c in verified};atm=atm_strike(chain,spot)
                self.observe(day,[snapshot_row(c,band,spot,checked,'stale_option_or_missing_quote',source='chain')
                                  for c in chain if c['strike']==atm and c['key'] not in vkeys])
            if (checked-t).total_seconds()>60:
                self.count(day,'chain_quote_delay')
                if OBSERVE_ALL:
                    atm=atm_strike(chain,spot)
                    self.observe(day,[snapshot_row(c,verified,spot,checked,'chain_quote_delay') for c in verified if c['strike']==atm])
                return
            choice=self.choose_buy_atm(verified,chain,spot,checked,day)
            if choice:
                c,value=choice
                if self.add_watch(c,value,checked,day):
                    self.count(day,'new_alerts' if value['channel']=='MAIN' else 'new_silent_records')
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
            if observed.date()!=t.date() or observed.time()>stock_close(observed):return
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
            if not dt_time(9,15)<=bt.time()<stock_close(bt):continue
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
        if t>=close_at(t)+timedelta(minutes=2) or w['day']<t.date().isoformat():
            expected=start
            close=close_at(start)
            while expected<close and expected in valid_minutes:expected+=timedelta(minutes=1)
            if expected==close:w['status']='NO_TOUCH_IN_MONITORED_BARS'

    def monitor_candles(self,t,budget=1):
        watches=[w for w in self.state['watches'] if w['status']=='OPEN' and
                 (not w['last_candle_attempt'] or (t-parse(w['last_candle_attempt'])).total_seconds()>=300)]
        watches.sort(key=lambda w:w['last_candle_attempt'] or '')
        for w in watches[:budget]:self.candle_check(w,t)

    @staticmethod
    def resolved(rows):
        return [w for w in rows if w['status'] in ('VALUE_REACHED','NO_TOUCH_IN_MONITORED_BARS')]

    @staticmethod
    def rate(rows):
        done=[w for w in rows if w['status'] in ('VALUE_REACHED','NO_TOUCH_IN_MONITORED_BARS')]
        hits=sum(w['status']=='VALUE_REACHED' for w in done)
        misses=[(w['last_price']/w['entry']-1)*100 for w in done if w['status']!='VALUE_REACHED' and num(w.get('entry'))>0]
        text=f'{hits}/{len(done)} ({hits/len(done)*100:.1f}%)' if done else '0/0 (n/a)'
        if misses:text+=f', misses end {sum(misses)/len(misses):+.0f}% vs entry on average'
        return text

    def rule_lab(self,rows,title):
        lab=[w for w in rows if w.get('policy')==FILTER_POLICY and w.get('rules')]
        lines=[title,rule_legend(),f'Records: {len(lab)}; resolved: {len(self.resolved(lab))} (touch rate counts only resolved records)']
        if not self.resolved(lab):return lines+['No resolved records yet.']
        for k in RULE_ORDER:
            ok=[w for w in lab if w['rules'].get(k)];no=[w for w in lab if not w['rules'].get(k)]
            lines.append(f'{k} pass {self.rate(ok)} | fail {self.rate(no)}')
        lines.append(f'MAIN channel ({main_rule_text()}): {self.rate([w for w in lab if w.get("channel")=="MAIN"])}')
        for lo,hi in ((GAP*100,RULE_S_MAX_GAP*100),(RULE_S_MAX_GAP*100,15.0),(15.0,MAX_GAP*100)):
            if lo>=hi:continue
            grp=[w for w in lab if lo<=num(w.get('gap_pct'))<hi]
            if grp:lines.append(f'Gap {lo:g}-{hi:g}%: all records {self.rate(grp)} | MAIN channel {self.rate([w for w in grp if w.get("channel")=="MAIN"])}')
        for k in MAIN_RULES:
            others=[w for w in lab if all(w['rules'].get(j) for j in MAIN_RULES if j!=k)]
            lines.append(f'All main rules except {k} passed: {self.rate(others)}')
        for n in range(len(RULE_ORDER),-1,-1):
            group=[w for w in lab if sum(bool(v) for v in w['rules'].values())==n]
            if group:lines.append(f'{n}/{len(RULE_ORDER)} rules passed: {self.rate(group)}')
        return lines

    def record_line(self,w):
        line=f'{w["symbol"]} {w["strike"]:g} {w["side"]} ({w["expiry"]}) | {w["direction"]} | {w["status"]}'
        if w.get('header'):line=f'{w["header"]} | '+line
        line+=f'\nReference ₹{w["entry"]:.2f}; fixed target ₹{w["target"]:.2f}'
        if w.get('metrics'):line+='\n'+self.value_line(w)
        if w['status']=='VALUE_REACHED':
            line+=f'; touch ₹{w["hit_price"]:.2f} at {parse(w["hit_time"]):%H:%M:%S} IST ({w["hit_evidence"]})'
            if w['hit_candle_close'] is not None:line+=f'; touching-candle close ₹{w["hit_candle_close"]:.2f}'
        else:
            gap=(w['target']-w['last_price'])/w['target']*100
            line+=f'; last observed ₹{w["last_price"]:.2f} at {parse(w["last_observed_at"]):%H:%M:%S}; gap {gap:+.1f}%'
        return line

    def report(self,day,t):
        if day.get('reported'):return
        rows=[w for w in self.state['watches'] if w['day']==day['date']]
        for w in rows:
            if w['status']=='OPEN':w['status']='UNVERIFIED_AT_REPORT'
        counts=Counter(w['status'] for w in rows)
        new=[w for w in rows if w.get('policy')==FILTER_POLICY]
        main=[w for w in new if w.get('channel')=='MAIN'];silent=[w for w in new if w.get('channel')=='SILENT']
        legacy=[w for w in rows if w.get('policy')!=FILTER_POLICY]
        hits=lambda x:sum(w['status']=='VALUE_REACHED' for w in x)
        good=sum(x.get('status')=='chain_received' for x in day['symbols'].values())
        lines=[f'S3 STOCK OPTIONS DAILY REPORT | {day["date"]}',
            f'MAIN ALERTS sent to Telegram (all of {main_rule_text()} passed): {len(main)}; {hits(main)} targets touched; '
            f'touch rate {hits(main)/len(main)*100:.1f}%' if main else
            f'MAIN ALERTS sent to Telegram (all of {main_rule_text()} passed): 0',
            f'SILENT RECORDS (failed at least one main rule; no Telegram alert): {len(silent)}; {hits(silent)} targets touched'
            +(f'; touch rate {hits(silent)/len(silent)*100:.1f}%' if silent else ''),
            f'Legacy watches from earlier rules: {len(legacy)}; included separately in the full totals below.',
            f'Stocks in universe: {day["universe_count"]}; stocks with a successful latest chain check: {good}',
            f'Completed sweeps: {day["sweeps"]}; last completed sweep: {day.get("last_sweep_finished","none")}',
            f'Last sweep duration: {day.get("last_sweep_seconds","unknown")} seconds',
            f'All records: {len(rows)} | model targets touched: {counts["VALUE_REACHED"]}',
            f'No touch in monitored full-minute bars: {counts["NO_TOUCH_IN_MONITORED_BARS"]} | unverified: {counts["UNVERIFIED_AT_REPORT"]}',
            'The partial entry minute is excluded from candle checks; its unobserved movements are unknown.',
            'A model-value touch is not a profitable buy trade. DOWN watches describe falling option premiums.','']
        for name,group in (('MAIN',main),('SILENT',silent)):
            for side in ('CE','PE'):
                subset=[w for w in group if w['side']==side]
                lines.append(f'{name} {side}: {hits(subset)}/{len(subset)} targets touched')
        lines.append('')
        lines+=self.rule_lab(rows,'RULE LAB | THIS DAY')
        every=[w for w in self.state['watches'] if w.get('policy')==FILTER_POLICY]
        days=len({w['day'] for w in every})
        if days>1:
            lines+=['']+self.rule_lab(every,f'RULE LAB | ALL DAYS SO FAR ({days} days; this report includes today)')
        lines+=['']+self.observation_summary(day)
        lines+=['','MAIN ALERTS']+([self.record_line(w) for w in main] or ['none'])
        if silent:
            lines+=['','SILENT RECORDS (header shows each rule: ✓ passed, ✗ failed)']
            lines+=[self.record_line(w) for w in silent] if REPORT_SILENT_LINES else ['(per-record lines hidden; set REPORT_SILENT_LINES=true to list them; the saved JSON report has all of them)']
        if legacy:lines+=['','LEGACY WATCHES']+[self.record_line(w) for w in legacy]
        lines+=['','WHY CONTRACTS WERE SKIPPED (checks, not unique contracts)']
        lines += [f'{k}: {v}' for k,v in sorted(day['counts'].items())]
        if day.get('last_error'):lines.append('Last data issue: '+day['last_error'])
        text='\n'.join(lines);folder=self.directory/'reports'
        (folder/f'{day["date"]}.txt').write_text(text,encoding='utf-8')
        (folder/f'{day["date"]}.json').write_text(json.dumps({'day':day,'watches':rows},indent=2,ensure_ascii=False))
        self.compress_observations(day)
        self.emit('report-'+day['date'],text);day['reported']=True;self.save()

    def tick(self,t):
        if t.weekday()>=5:return
        day=self.prepare(t)
        self.emit('online-'+FILTER_POLICY+'-'+day['date'],f'S3 STOCK OPTIONS ONLINE | {day["date"]} | RULE LAB V4\nBUY-ONLY ATM: CE or PE, only when its premium must RISE to the target (no selling, no CE/PE restriction).\nUniverse: {len(self.universe)} NSE F&O stocks; expiry more than 5 calendar days away (automatic rollover). ATM ±2 supplies IV peers; only ATM can alert.\nGap {GAP*100:g}% to below {MAX_GAP*100:g}%, ask gap at least {GAP*100:g}%. Every candidate is scored on six rules; only candidates passing {main_rule_text()} are sent here, the rest are recorded silently.\n{rule_legend()}\nAlerts 09:20-15:20 IST, Mon-Fri. Every ATM contract scanned is also logged (bid, ask, depth, greeks, model value) for later filter checks. One active MAIN and one active SILENT record per stock. Daily report around 16:15 IST with a rule comparison.')
        # Finish interrupted prior days as unverified instead of inventing outcomes.
        for date,old in list(self.state['days'].items()):
            if date<day['date'] and not old.get('reported'):
                self.report(old,t)
        if t.time()>=dt_time(16,15):self.report(day,t);return
        if not dt_time(9,20)<=t.time()<=dt_time(16,15):return
        if t.time()<=stock_close(t):self.monitor_quotes(t)
        self.monitor_candles(now(),budget=1 if t.time()<=stock_close(t) else 10)
        if t.time()<=alert_cutoff(t):self.scan_one(day,now())


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--mode',choices=['test','run','once'],default='run');args=ap.parse_args()
    scanner=Scanner(API());start=now()
    if args.mode=='test':
        scanner.prepare(start);u=scanner.universe[0]
        chain=scanner.api.chain(u,scanner.metadata)
        scanner.emit('test-'+iso(start),f'S3 stock scanner connection OK. Discovered {len(scanner.universe)} stock-option underlyings.\nSample API check {u["symbol"]}: {len(chain)} contracts in full chain; {len(select_band(chain,chain[0]["spot"])) if chain else 0} IV-support contracts at ATM ±2. New alerts: BUY-ONLY ATM CE or PE; no falling-premium watches.\nS3 only; no S1/S2. This verifies connectivity, not live signal quality.')
        for _ in range(3):scanner.flush()
        scanner.sync(force=True);return
    # A morning (or manually restarted) run ends at 14:10; anything started from 13:30 on runs to 16:45, so a late
    # start of the afternoon run can never end after a few minutes.
    end=start.replace(hour=14,minute=10,second=0,microsecond=0) if start.time()<dt_time(13,30) else start.replace(hour=16,minute=45,second=0,microsecond=0)
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
            time.sleep(2 if dt_time(9,20)<=now().time()<=alert_cutoff(now()) else 15)
    finally:scanner.sync(force=True)


if __name__=='__main__':main()
