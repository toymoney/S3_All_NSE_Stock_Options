#!/usr/bin/env python3
"""Apply frozen V7 entry rules to saved summary JSONs, without API calls.

Usage: python tools/replay_v6.py path/to/day1.json path/to/day2.json ...
This checks saved watches, not a full reconstruction of every raw candidate.
"""
import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stock_s3_bot as b


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('summaries', nargs='+', type=Path)
    args = parser.parse_args()
    writer = csv.writer(sys.stdout)
    writer.writerow(['day', 'symbol', 'strike', 'side', 'entered', 'volume_oi',
                     'abs_delta', 'entry_ask', 'model_target', 'status',
                     'hit_time', 'hit_evidence'])
    counts = {}
    seen = set()
    for path in args.summaries:
        data = json.loads(path.read_text())
        for w in sorted(data['watches'], key=lambda w: w['entered']):
            m = dict(w.get('metrics', {}))
            # Older summaries did not store minute. Use entry time in IST.
            entered = b.parse(w['entered']).astimezone(b.IST)
            m['minute'] = entered.hour * 60 + entered.minute
            if 'vol_oi' not in m:
                oi = b.num(w['market'].get('oi'))
                m['vol_oi'] = b.num(w['market'].get('volume')) / oi if oi > 0 else None
            if 'delta' not in m:
                m['delta'] = w.get('greeks', {}).get('delta')
            # The source set must itself be buy-only ATM watches.
            if w.get('direction') != 'UP' or b.research_failures(m):
                continue
            atm = w.get('atm_at_entry')
            if atm is not None and w['strike'] != atm:
                continue
            day = entered.date().isoformat()
            key = (day, w['symbol'], w['expiry'], w['strike'], w['side'])
            if key in seen:
                continue
            seen.add(key)
            counts.setdefault(day, Counter())[w['status']] += 1
            writer.writerow([day, w['symbol'], w['strike'], w['side'],
                w['entered'], round(m['vol_oi'], 6), abs(m['delta']),
                w.get('entry_ask', w['market'].get('ask_price')), w['target'],
                w['status'], w.get('hit_time'), w.get('hit_evidence')])
    for day, statuses in sorted(counts.items()):
        print(f'{day}: selected={sum(statuses.values())}; outcomes={dict(statuses)}',
              file=sys.stderr)
    print('Historical model-target labels only; no fill or stop-order replay.',
          file=sys.stderr)


if __name__ == '__main__':
    main()
