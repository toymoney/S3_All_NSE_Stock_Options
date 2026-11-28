# Filename retained to replace the V6 tests when uploading this package.
import gzip
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import stock_s3_bot as b


class ResearchV7Tests(unittest.TestCase):
    def test_default_route(self):
        self.assertEqual(b.FILTER_POLICY, 'BUY_ATM_ACTIVITY_V7')
        self.assertEqual(b.MAIN_RULES, 'R')
        self.assertEqual(b.MAIN_ALSO_IF, '')
        self.assertTrue(b.OBSERVE_ALL)
        self.assertTrue(b.OBSERVE_BAND)

    def test_boundary_missing_and_nonfinite_inputs(self):
        good = {'vol_oi': 3, 'minute': 899}
        self.assertEqual(b.research_failures(good), [])
        for field, value in [('vol_oi', 2.999), ('minute', 900), ('minute', -1)]:
            self.assertTrue(b.research_failures({**good, field: value}))
        for field in good:
            for value in [None, float('nan'), float('inf'), True, '3']:
                self.assertTrue(b.research_failures({**good, field: value}))
            missing = dict(good)
            del missing[field]
            self.assertTrue(b.research_failures(missing))

    def test_delta_is_recorded_but_is_not_an_r_gate(self):
        for delta in [.1, -.2, .49, .50, .9, None]:
            self.assertEqual(b.research_failures(
                {'vol_oi': 3, 'minute': 899, 'delta': delta}), [])

    def test_q_cannot_bypass_r_and_r_does_not_require_q(self):
        with patch.object(b, 'MAIN_RULES', 'R'), patch.object(b, 'MAIN_ALSO_IF', ''):
            flags = dict.fromkeys(b.RULE_ORDER, True)
            flags['R'] = False
            self.assertEqual(b.rule_header(flags)[0], 'SILENT')
            flags = dict.fromkeys(b.RULE_ORDER, False)
            flags['R'] = True
            self.assertEqual(b.rule_header(flags)[0], 'MAIN')

    def test_real_eight_entry_rule_metrics(self):
        fixtures = [(2850/515, 770), (2590/773, 784), (5198/769, 811),
                    (766/239, 594), (576/176, 714), (3340/571, 756),
                    (594/195, 656), (8406/2490, 817)]
        for ratio, minute in fixtures:
            self.assertEqual(b.research_failures(
                dict(vol_oi=ratio, minute=minute)), [])

    def contract(self, t):
        return {'key': 'FO|100CE', 'symbol': 'SAMPLE', 'underlying': 'EQ|1',
             'expiry': '2026-12-29', 'strike': 100, 'side': 'CE', 'lot': 100,
             'spot': 100, 'market': {'ltp': 94, 'bid_price': 94.5,
             'ask_price': 95, 'oi': 20000, 'volume': 60000,
             'bid_qty': 100, 'ask_qty': 100},
             'greeks': {'iv': 35, 'delta': .3, 'gamma': .001},
             'quote_time': t.isoformat(),
             'last_trade_time': str(int(t.timestamp()*1000))}

    def test_cutoff_and_rejected_observation_recording(self):
        t = datetime(2026, 11, 30, 14, 59, 59, tzinfo=b.IST)
        c = self.contract(t)
        with patch.object(b, 'peer_iv', return_value=.35), \
             patch.object(b, 'bs', return_value=100):
            value, reason = b.candidate(c, [c], 100, t)
            self.assertIsNone(reason)
            self.assertEqual(value['channel'], 'MAIN')
            self.assertFalse(value['rules']['Q'])
            self.assertEqual(value['target'], 100)
            later = t + timedelta(seconds=1)
            value, reason = b.candidate(c, [c], 100, later)
            self.assertIsNone(reason)
            self.assertEqual(value['channel'], 'SILENT')
            self.assertIn('entry_before_15_00_ist', value['research_failures'])
            row = b.snapshot_row(c, [c], 100, later, 'CANDIDATE', value)
            self.assertEqual(row['research_failures'], value['research_failures'])
            self.assertEqual(row['metrics']['vol_oi'], 3)

    def test_first_qualifying_contract_only_survives_restart_and_resolution(self):
        t = datetime(2026, 11, 30, 13, 30, tzinfo=b.IST)
        c = self.contract(t)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(b, 'peer_iv', return_value=.35), \
             patch.object(b, 'bs', return_value=100):
            scanner = b.Scanner(None, directory)
            day = {'date': t.date().isoformat(), 'counts': {}}
            scanner.state['days'][day['date']] = day
            # Earlier rejected/silent watch must not consume the main slot.
            value, _ = b.candidate(c, [c], 100, t)
            scanner.add_watch(c, {**value, 'channel': 'SILENT',
                                  'policy': b.FILTER_POLICY}, t, day)
            selected = scanner.choose_buy_atm([c], [c], 100, t, day)
            self.assertIsNotNone(selected)
            watch = scanner.add_watch(*selected, t, day)
            self.assertEqual(watch['channel'], 'MAIN')
            self.assertIsNone(scanner.choose_buy_atm([c], [c], 100, t, day))
            scanner.finish(watch, t + timedelta(minutes=1), 100, 'fixture')
            scanner.save()
            restored = b.Scanner(None, directory)
            restored_day = restored.state['days'][day['date']]
            self.assertIsNone(restored.choose_buy_atm(
                [c], [c], 100, t + timedelta(minutes=2), restored_day))
            # Rejected duplicate observations remain in the raw recorder.
            restored.save()
            self.assertEqual(sum(w['channel'] == 'MAIN'
                for w in restored.state['watches']), 1)
            # New trading day permits the contract again.
            next_t = t + timedelta(days=1)
            next_day = {'date': next_t.date().isoformat(), 'counts': {}}
            restored.state['days'][next_day['date']] = next_day
            self.assertIsNotNone(restored.choose_buy_atm(
                [c], [c], 100, next_t, next_day))
