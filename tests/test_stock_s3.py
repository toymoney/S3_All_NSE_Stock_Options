import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import stock_s3_bot as b


T = datetime(2026, 11, 19, 10, 0, 30, tzinfo=b.IST)


def contract(k=100, side="CE", iv=25):
    return {
        "key": f"FO|{k}{side}",
        "symbol": "SAMPLE",
        "underlying": "EQ|1",
        "expiry": "2026-12-29",
        "strike": k,
        "side": side,
        "lot": 100,
        "spot": 100,
        "market": {
            "ltp": 10,
            "bid_price": 9.9,
            "ask_price": 10.1,
            "oi": 300000,
            "volume": 10000,
            "bid_qty": 300,
            "ask_qty": 300,
        },
        "greeks": {
            "iv": iv,
            "delta": 0.5,
            "gamma": 0.01,
            "theta": -0.1,
            "vega": 0.2,
        },
        "quote_time": T.isoformat(),
        "last_trade_time": str(int(T.timestamp() * 1000)),
    }


class FakeAPI:
    bars = []

    def candles(self, *args):
        return self.bars

    def chain(self, *args):
        raise b.APIError("fixture error")


class StockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api = FakeAPI()
        self.api.bars = []

        # Preserve historical rule-engine regression fixtures.
        # The separate test_quality_v5.py checks the live Q filter.
        settings = (
            ("RULE_ORDER", "TDOAVGEIPS"),
            ("OBSERVE_BAND", False),
            ("MAIN_ALSO_IF", ""),
            ("GAP", 0.15),
            ("MAIN_RULES", "TOAVEI"),
            ("RULE_S_MAX_GAP", 0.30),
        )

        for name, value in settings:
            patcher = patch.object(b, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.s = b.Scanner(self.api, self.tmp.name)
        self.day = {
            "date": "2026-11-19",
            "cursor": 0,
            "sweeps": 0,
            "symbols": {},
            "counts": {},
            "universe_count": 2,
        }
        self.s.state["days"][self.day["date"]] = self.day

    def tearDown(self):
        self.tmp.cleanup()

    def watch(self, direction="UP"):
        value = {
            "entry": 10,
            "target": 12 if direction == "UP" else 8,
            "direction": direction,
            "gap_pct": 20,
            "peer_iv": 30,
        }
        return self.s.add_watch(contract(), value, T, self.day)

    def bar(self, minute, high=11, low=9, close=10, hour=10):
        return [
            T.replace(
                hour=hour,
                minute=minute,
                second=0,
            ).isoformat(),
            10,
            high,
            low,
            close,
            100,
            1000,
        ]

    def test_band_actual_strikes(self):
        chain = [
            contract(k, side)
            for k in range(50, 156, 5)
            for side in ["CE", "PE"]
        ]
        band = b.select_band(chain, 102.5)

        self.assertEqual(len(band), 10)
        self.assertEqual(min(x["strike"] for x in band), 95)
        self.assertEqual(max(x["strike"] for x in band), 115)

    def test_peer_iv_excludes_own(self):
        c = contract(iv=199)
        peers = [
            contract(90, iv=20),
            c,
            contract(110, iv=40),
        ]
        self.assertAlmostEqual(b.peer_iv(c, peers), 0.30)

    def test_no_extrapolation(self):
        self.assertIsNone(
            b.peer_iv(
                contract(90),
                [contract(90), contract(100), contract(110)],
            )
        )

    def test_peers_same_side(self):
        self.assertIsNone(
            b.peer_iv(
                contract(),
                [
                    contract(90, "PE"),
                    contract(),
                    contract(110, "PE"),
                ],
            )
        )

    def test_lot_liquidity(self):
        c = contract()
        self.assertTrue(b.liquid(c))

        c["market"]["bid_qty"] = 99
        self.assertFalse(b.liquid(c))

    def test_wide_spread(self):
        c = contract()
        c["market"]["ask_price"] = 12
        self.assertFalse(b.liquid(c))

    def test_fresh_quote(self):
        self.assertTrue(
            b.fresh(
                {"last_trade_time": str(T.timestamp() * 1000)},
                T,
            )
        )
        self.assertFalse(
            b.fresh(
                {
                    "last_trade_time": (
                        T - timedelta(minutes=4)
                    ).timestamp() * 1000
                },
                T,
            )
        )
        self.assertFalse(b.fresh({}, T))

    def test_candidate_below(self):
        c = contract()
        band = [contract(90), c, contract(110)]

        with patch.object(b, "bs", return_value=12):
            value, reason = b.candidate(c, band, 100, T)

        self.assertIsNone(reason)
        self.assertEqual(value["direction"], "UP")
        self.assertEqual(value["target"], 12)
        self.assertEqual(value["channel"], "MAIN")
        self.assertEqual(value["rules_passed"], "TDOAVGEIPS")
        self.assertEqual(value["rules_failed"], "")
        self.assertIn(
            "MAIN | T✓ D✓ O✓ A✓ V✓ G✓ E✓ I✓ P✓ S✓ | 10/10",
            value["header"],
        )

    def test_candidate_above(self):
        c = contract()
        band = [contract(90), c, contract(110)]

        with patch.object(b, "bs", return_value=8):
            value, reason = b.candidate(c, band, 100, T)

        self.assertIsNone(value)
        self.assertEqual(reason, "sell_direction_excluded")

    def test_executable_gap_gate(self):
        c = contract()
        c["market"].update(
            ltp=8,
            bid_price=9.9,
            ask_price=10.1,
        )

        with patch.object(b, "bs", return_value=10):
            value, _ = b.candidate(
                c,
                [contract(90), c, contract(110)],
                100,
                T,
            )

        self.assertIsNone(value)

    def test_expiry_guard(self):
        c = contract()
        c["expiry"] = "2026-11-19"

        value, reason = b.candidate(
            c,
            [contract(90), c, contract(110)],
            100,
            T.replace(hour=15, minute=10),
        )

        self.assertIsNone(value)
        self.assertEqual(reason, "near_expiry")

    def test_bs_parity(self):
        import math

        self.assertAlmostEqual(
            b.bs(100, 100, 0.1, 0.3, "CE")
            - b.bs(100, 100, 0.1, 0.3, "PE"),
            100 - 100 * math.exp(-b.RISK_RATE * 0.1),
        )

    def test_entry_dedup_and_reload(self):
        self.watch()
        self.assertIsNone(self.watch())

        other = b.Scanner(self.api, self.tmp.name)
        self.assertEqual(len(other.state["watches"]), 1)

    def test_candle_hit_close_separate(self):
        watch = self.watch()
        self.api.bars = [self.bar(1, high=13, close=10)]

        self.s.candle_check(
            watch,
            T.replace(minute=2, second=1),
        )

        self.assertEqual(watch["status"], "VALUE_REACHED")
        self.assertEqual(watch["hit_price"], 12)
        self.assertEqual(watch["hit_candle_close"], 10)
        self.assertIn("10:02:00", watch["hit_time"])

    def test_down_touch(self):
        watch = self.watch("DOWN")
        self.api.bars = [self.bar(1, low=7)]

        self.s.candle_check(
            watch,
            T.replace(minute=2, second=1),
        )

        self.assertEqual(watch["status"], "VALUE_REACHED")

    def test_entry_minute_excluded(self):
        watch = self.watch()
        self.api.bars = [self.bar(0, high=15)]

        self.s.candle_check(watch, T.replace(minute=2))
        self.assertEqual(watch["status"], "OPEN")

    def test_incomplete_candle_excluded(self):
        watch = self.watch()
        self.api.bars = [self.bar(1, high=15)]

        self.s.candle_check(watch, T.replace(minute=1))
        self.assertEqual(watch["status"], "OPEN")

    def test_invalid_zero_low_not_hit(self):
        watch = self.watch("DOWN")
        self.api.bars = [self.bar(1, low=0)]

        self.s.candle_check(watch, T.replace(minute=2))
        self.assertEqual(watch["status"], "OPEN")

    def test_recent_quote_not_overwritten(self):
        watch = self.watch()
        watch["last_price"] = 11
        watch["last_observed_at"] = b.iso(T.replace(minute=5))
        self.api.bars = [self.bar(1)]

        self.s.candle_check(watch, T.replace(minute=6))
        self.assertEqual(watch["last_price"], 11)

    def test_missing_bars_not_claimed_complete(self):
        watch = self.watch()
        self.api.bars = [self.bar(29, hour=15)]

        self.s.candle_check(
            watch,
            T.replace(hour=15, minute=50),
        )

        self.assertEqual(watch["status"], "OPEN")

    def test_complete_bar_coverage(self):
        watch = self.watch()
        bar_time = T.replace(minute=1, second=0)

        while bar_time < b.close_at(T):
            self.api.bars.append(
                [b.iso(bar_time), 10, 11, 9, 10, 100, 1000]
            )
            bar_time += timedelta(minutes=1)

        self.s.candle_check(
            watch,
            T.replace(hour=15, minute=50),
        )

        self.assertEqual(
            watch["status"],
            "NO_TOUCH_IN_MONITORED_BARS",
        )

    def test_report_unknown_and_idempotent(self):
        watch = self.watch()

        self.s.report(
            self.day,
            T.replace(hour=16, minute=15),
        )

        self.assertEqual(
            watch["status"],
            "UNVERIFIED_AT_REPORT",
        )

        message_count = len(self.s.state["outbox"])
        self.s.report(self.day, T)

        self.assertEqual(
            message_count,
            len(self.s.state["outbox"]),
        )

    def test_hit_report_never_shows_remaining_gap(self):
        watch = self.watch()
        self.s.finish(watch, T, 12, "fixture", 10)
        self.s.report(self.day, T)

        report = (
            self.s.directory / "reports/2026-11-19.txt"
        ).read_text(encoding="utf-8")

        self.assertIn("touch ₹12.00", report)
        self.assertIn("touching-candle close ₹10.00", report)
        self.assertNotIn("gap +", report)

    def test_hit_idempotent(self):
        watch = self.watch()
        self.s.finish(watch, T, 12, "fixture")
        message_count = len(self.s.state["outbox"])

        self.s.finish(watch, T, 14, "fixture")

        self.assertEqual(
            message_count,
            len(self.s.state["outbox"]),
        )
        self.assertEqual(watch["hit_price"], 12)

    def test_api_failure_advances_universe(self):
        self.s.universe = [{"symbol": "A"}, {"symbol": "B"}]

        self.s.scan_one(self.day, T)
        self.assertEqual(self.day["cursor"], 1)

        self.s.scan_one(self.day, T)
        self.assertEqual(self.day["sweeps"], 1)

    def test_universe_equities_nearest_expiry(self):
        equity = {
            "instrument_key": "EQ|1",
            "segment": "NSE_EQ",
            "instrument_type": "EQ",
            "trading_symbol": "A",
        }

        option = {
            "instrument_key": "FO|1",
            "segment": "NSE_FO",
            "instrument_type": "CE",
            "underlying_key": "EQ|1",
            "underlying_type": "EQUITY",
            "underlying_symbol": "A",
            "expiry": "2026-11-24",
            "lot_size": 100,
            "strike_price": 100,
        }

        rows = [
            equity,
            option,
            {
                **option,
                "instrument_key": "FO|2",
                "expiry": "2026-12-29",
            },
            {
                **option,
                "instrument_key": "FO|3",
                "expiry": "2026-10-27",
            },
            {
                **option,
                "instrument_key": "INDEX|1",
                "underlying_type": "INDEX",
            },
        ]

        universe, metadata = b.universe_from_master(
            rows,
            "2026-11-19",
        )

        self.assertEqual(len(universe), 1)
        self.assertEqual(
            universe[0]["expiry"],
            "2026-12-29",
        )
        self.assertNotIn("INDEX|1", metadata)
        self.assertNotIn("FO|3", metadata)

    def test_quotes_use_instrument_token(self):
        api = b.API.__new__(b.API)
        api.get = lambda *args: {
            "NSE_FO:SYMBOL": {
                "instrument_token": "NSE_FO|123",
                "last_price": 10,
            }
        }

        self.assertEqual(
            api.quotes(["NSE_FO|123"])["NSE_FO|123"]["last_price"],
            10,
        )

    def test_buy_pe_eligible(self):
        c = contract(side="PE")
        band = [
            contract(90, "PE"),
            c,
            contract(110, "PE"),
        ]

        with patch.object(b, "bs", return_value=12):
            value, reason = b.candidate(c, band, 100, T)

        self.assertIsNone(reason)
        self.assertEqual(value["direction"], "UP")

    def test_atm_only_selection_keeps_peer_band(self):
        chain = [
            contract(k, side)
            for k in [90, 95, 100, 105, 110]
            for side in ["CE", "PE"]
        ]

        def values(c, peers, spot, t):
            self.assertEqual(len(peers), 10)
            return {
                "direction": "UP",
                "entry": 10,
                "target": 13,
                "executable_gap_pct": (
                    20 if c["side"] == "CE" else 25
                ),
                "channel": "MAIN",
                "rules_passed": "TDOAVGEIPS",
            }, None

        with patch.object(
            b, "candidate", side_effect=values
        ) as mock:
            choice = self.s.choose_buy_atm(
                chain, chain, 101, T, self.day
            )

        self.assertEqual(mock.call_count, 2)
        self.assertEqual(choice[0]["strike"], 100)
        self.assertEqual(choice[0]["side"], "PE")
        self.assertEqual(choice[1]["atm_at_entry"], 100)

    def test_missing_atm_does_not_substitute_other_strike(self):
        chain = [
            contract(k)
            for k in [90, 95, 100, 105, 110]
        ]

        with patch.object(b, "candidate") as mock:
            choice = self.s.choose_buy_atm(
                [c for c in chain if c["strike"] != 100],
                chain,
                100,
                T,
                self.day,
            )
            self.assertIsNone(choice)

        mock.assert_not_called()

    def test_active_stock_watch_blocks_other_side_and_new_atm(self):
        watch = self.watch()
        watch["policy"] = b.FILTER_POLICY
        watch["channel"] = "MAIN"

        chain = [
            contract(k, "PE")
            for k in [90, 95, 100, 105, 110]
        ]

        with patch.object(b, "bs", return_value=12):
            self.assertIsNone(
                self.s.choose_buy_atm(
                    chain, chain, 100, T, self.day
                )
            )

        self.assertEqual(
            self.day["counts"]["existing_open_stock_watch"],
            1,
        )

    def test_resolved_watch_allows_new_contract(self):
        watch = self.watch()
        watch["policy"] = b.FILTER_POLICY
        watch["channel"] = "MAIN"
        watch["status"] = "VALUE_REACHED"

        chain = [
            contract(k, "PE")
            for k in [90, 95, 100, 105, 110]
        ]

        with patch.object(b, "bs", return_value=12):
            choice = self.s.choose_buy_atm(
                chain, chain, 100, T, self.day
            )

        self.assertEqual(choice[0]["side"], "PE")

    def test_post_close_candle_cannot_hit(self):
        watch = self.watch()
        self.api.bars = [self.bar(45, high=15, hour=15)]

        self.s.candle_check(
            watch,
            T.replace(hour=16, minute=0),
        )

        self.assertEqual(watch["status"], "OPEN")
        self.assertEqual(watch["last_price"], 10)

    def test_quote_request_finishing_after_close_cannot_hit(self):
        watch = self.watch()

        self.api.quotes = lambda keys: {
            watch["key"]: {
                "last_price": 20,
                "last_trade_time": T.replace(
                    hour=15,
                    minute=40,
                    second=0,
                ).timestamp() * 1000,
            }
        }

        with patch.object(
            b,
            "now",
            return_value=T.replace(hour=15, minute=41),
        ):
            self.s.monitor_quotes(
                T.replace(hour=15, minute=29)
            )

        self.assertEqual(watch["status"], "OPEN")

    def test_report_separates_new_policy_from_legacy(self):
        watch = self.watch()
        watch["policy"] = b.FILTER_POLICY
        watch["channel"] = "MAIN"

        self.s.finish(watch, T, 12, "fixture")
        self.s.report(self.day, T)

        report = (
            self.s.directory / "reports/2026-11-19.txt"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "MAIN ALERTS sent to Telegram "
            "(all of TOAVEI passed): 1; 1 targets touched",
            report,
        )
        self.assertIn("MAIN CE: 1/1", report)

    def test_gap_band_boundaries(self):
        c = contract()
        band = [contract(90), c, contract(110)]

        cases = [
            (0.15, 0.15, None),
            (0.159, 0.15, None),
            (0.16999, 0.16, None),
            (0.149, 0.149, "gap_below_threshold"),
            (0.16, 0.149, "gap_below_threshold"),
        ]

        for gap, ask_gap, expected in cases:
            with self.subTest(gap=gap, ask_gap=ask_gap):
                c["market"].update(
                    ltp=100 * (1 - gap),
                    bid_price=100 * (1 - ask_gap) - 0.1,
                    ask_price=100 * (1 - ask_gap),
                )

                with patch.object(b, "bs", return_value=100):
                    _, reason = b.candidate(c, band, 100, T)

                self.assertEqual(reason, expected)

    def test_depth_is_a_rule_not_a_rejection(self):
        for side in ["bid_qty", "ask_qty"]:
            c = contract()
            band = [contract(90), c, contract(110)]
            c["market"][side] = 299

            with patch.object(b, "bs", return_value=12):
                value, reason = b.candidate(c, band, 100, T)

            self.assertIsNone(reason)
            self.assertFalse(value["rules"]["D"])
            self.assertEqual(value["channel"], "MAIN")
            self.assertIn("D✗", value["header"])

            with (
                patch.object(b, "MAIN_RULES", "TOAVEID"),
                patch.object(b, "bs", return_value=12),
            ):
                value, _ = b.candidate(c, band, 100, T)
                self.assertEqual(value["channel"], "SILENT")

            for quantity in [0, None]:
                c["market"][side] = quantity

                with patch.object(b, "bs", return_value=12):
                    value, _ = b.candidate(c, band, 100, T)

                self.assertIsNone(value)

    def test_each_rule_can_fail_alone(self):
        def run(mutate, letter, failed=None):
            c = contract()
            band = [contract(90), c, contract(110)]
            mutate(c)

            with patch.object(b, "bs", return_value=12):
                value, reason = b.candidate(c, band, 100, T)

            self.assertIsNone(reason)
            self.assertEqual(
                value["rules_failed"],
                failed or letter,
                (letter, value["header"]),
            )
            self.assertEqual(
                value["channel"],
                "MAIN" if letter not in b.MAIN_RULES else "SILENT",
            )
            self.assertIn(letter + "✗", value["header"])

        run(
            lambda c: c["market"].update(bid_qty=299),
            "D",
            "DP",
        )
        run(
            lambda c: c["market"].update(oi=499 * 100),
            "O",
        )
        run(
            lambda c: c.update(
                last_trade_time=str(
                    int(
                        (T - timedelta(seconds=11)).timestamp()
                        * 1000
                    )
                )
            ),
            "A",
        )
        run(
            lambda c: c["market"].update(
                volume=3.01 * c["market"]["oi"]
            ),
            "V",
        )
        run(
            lambda c: c["greeks"].update(gamma=0.0059),
            "G",
        )

        c = contract()
        band = [contract(90), c, contract(110)]
        c["market"].update(
            ltp=8,
            bid_price=7.9,
            ask_price=8.0,
            oi=1000 * 100,
        )

        with patch.object(b, "bs", return_value=10):
            value, reason = b.candidate(c, band, 100, T)

        self.assertIsNone(reason)
        self.assertEqual(value["rules_failed"], "TP")

    def test_delta_and_iv_rules(self):
        def run(mutate):
            c = contract()
            band = [contract(90), c, contract(110)]
            mutate(c)

            with patch.object(b, "bs", return_value=12):
                value, reason = b.candidate(c, band, 100, T)

            self.assertIsNone(reason)
            return value

        self.assertEqual(
            run(
                lambda c: c["greeks"].update(delta=0.349)
            )["rules_failed"],
            "E",
        )
        self.assertEqual(
            run(
                lambda c: c["greeks"].update(delta=-0.35)
            )["rules_failed"],
            "",
        )
        self.assertEqual(
            run(
                lambda c: c["greeks"].update(delta=-0.34)
            )["rules_failed"],
            "E",
        )
        self.assertEqual(
            run(
                lambda c: c["greeks"].update(iv=28)
            )["rules_failed"],
            "",
        )

        value = run(
            lambda c: c["greeks"].update(iv=28.01)
        )
        self.assertEqual(value["rules_failed"], "I")
        self.assertEqual(value["channel"], "SILENT")

        value = run(lambda c: c["greeks"].pop("iv"))
        self.assertEqual(value["rules_failed"], "I")

        value = run(lambda c: c["greeks"].pop("delta"))
        self.assertEqual(value["rules_failed"], "E")

    def test_rule_p_gap_band_and_depth(self):
        def run(gap, bid_qty=300):
            c = contract()
            band = [contract(90), c, contract(110)]
            premium = 100 * (1 - gap)

            c["market"].update(
                ltp=premium,
                bid_price=premium - 0.1,
                ask_price=premium,
                bid_qty=bid_qty,
            )

            with patch.object(b, "bs", return_value=100):
                return b.candidate(c, band, 100, T)[0]

        self.assertTrue(run(0.15)["rules"]["P"])
        self.assertTrue(run(0.1699)["rules"]["P"])
        self.assertFalse(run(0.17)["rules"]["P"])
        self.assertFalse(run(0.25)["rules"]["P"])

        value = run(0.16, bid_qty=299)
        self.assertFalse(value["rules"]["P"])
        self.assertFalse(value["rules"]["D"])

    def test_p_route_to_main_and_switches(self):
        def run(mutate, also="P", main=None):
            c = contract()
            band = [contract(90), c, contract(110)]
            mutate(c)

            with (
                patch.object(b, "MAIN_ALSO_IF", also),
                patch.object(b, "bs", return_value=12),
            ):
                if main:
                    with patch.object(b, "MAIN_RULES", main):
                        return b.candidate(c, band, 100, T)[0]

                return b.candidate(c, band, 100, T)[0]

        def weak(c):
            c["greeks"].update(iv=40)
            c["market"].update(oi=100 * 100)

        value = run(weak)
        self.assertEqual(value["channel"], "MAIN")
        self.assertTrue(
            value["header"].startswith("MAIN via P |"),
            value["header"],
        )

        value = run(lambda c: None)
        self.assertTrue(
            value["header"].startswith("MAIN |"),
            value["header"],
        )

        self.assertEqual(
            run(weak, also="")["channel"],
            "SILENT",
        )

        def thin(c):
            weak(c)
            c["market"].update(bid_qty=299)

        self.assertEqual(run(thin)["channel"], "SILENT")

        c = contract()
        band = [contract(90), c, contract(110)]
        c["market"].update(
            ltp=80,
            bid_price=79.9,
            ask_price=80,
        )

        with (
            patch.object(b, "MAIN_ALSO_IF", ""),
            patch.object(b, "MAIN_RULES", "TOAVEIP"),
            patch.object(b, "bs", return_value=100),
        ):
            value = b.candidate(c, band, 100, T)[0]

        self.assertEqual(value["channel"], "SILENT")
        self.assertEqual(value["rules_failed"], "P")

    def scan_api(self, ltp=10, stale_underlying=False):
        class API:
            def chain(self, underlying, metadata):
                return [
                    {
                        **contract(k, "CE"),
                        "spot": 100,
                        "market": {
                            **contract(k, "CE")["market"],
                            "ltp": ltp,
                        },
                    }
                    for k in [90, 95, 100, 105, 110]
                ]

            def quotes(self, keys):
                old = str(
                    int(
                        (T - timedelta(minutes=30)).timestamp()
                        * 1000
                    )
                )
                current = str(int(T.timestamp() * 1000))
                result = {}

                for key in keys:
                    if key == "EQ|1":
                        result[key] = {
                            "instrument_token": key,
                            "last_price": 100,
                            "last_trade_time": (
                                old if stale_underlying else current
                            ),
                        }
                    else:
                        result[key] = {
                            "instrument_token": key,
                            "last_price": ltp,
                            "oi": 300000,
                            "volume": 10000,
                            "timestamp": T.isoformat(),
                            "last_trade_time": current,
                            "depth": {
                                "buy": [
                                    {
                                        "price": ltp - 0.1,
                                        "quantity": 300,
                                    }
                                ],
                                "sell": [
                                    {
                                        "price": ltp + 0.1,
                                        "quantity": 300,
                                    }
                                ],
                            },
                        }

                return result

        return API()

    def run_scan(self, api, bs_value=12):
        self.s.api = api
        self.s.universe = [
            {
                "underlying": "EQ|1",
                "symbol": "SAMPLE",
                "expiry": "2026-12-29",
            }
        ]
        self.day["cursor"] = 0

        with (
            patch.object(b, "now", return_value=T),
            patch.object(b, "bs", return_value=bs_value),
        ):
            self.s.scan_one(self.day, T)

    def obs_rows(self):
        path = (
            self.s.directory
            / "observations"
            / f'{self.day["date"]}.jsonl'
        )

        if not path.exists():
            return []

        return [
            json.loads(line)
            for line in path.read_text(
                encoding="utf-8"
            ).splitlines()
        ]

    def test_observation_log_keeps_rejected_contracts_with_bid_ask_and_greeks(self):
        self.run_scan(self.scan_api(), bs_value=10.5)
        rows = self.obs_rows()

        self.assertEqual(len(rows), 1)

        row = rows[0]
        self.assertEqual(
            row["reason"],
            "gap_below_threshold",
        )
        self.assertEqual(row["k"], 100)
        self.assertEqual(
            (
                row["bid"],
                row["ask"],
                row["bid_lots"],
                row["ask_lots"],
            ),
            (9.9, 10.1, 3, 3),
        )
        self.assertEqual(row["greeks"]["delta"], 0.5)
        self.assertEqual(row["greeks"]["iv"], 25)
        self.assertEqual(row["oi_lots"], 3000)
        self.assertAlmostEqual(row["fair"], 10.5)
        self.assertAlmostEqual(
            row["gap_pct"],
            4.762,
            places=2,
        )
        self.assertEqual(row["src"], "quote")
        self.assertEqual(self.s.state["watches"], [])
        self.assertEqual(self.s.state["outbox"], [])

    def test_observation_log_marks_selected_candidate(self):
        self.run_scan(self.scan_api())
        row = self.obs_rows()[0]

        self.assertEqual(row["reason"], "CANDIDATE")
        self.assertEqual(row["action"], "SELECTED")
        self.assertEqual(row["chan"], "MAIN")
        self.assertEqual(len(self.s.state["watches"]), 1)

    def test_observation_log_early_exit_uses_chain_data(self):
        self.run_scan(
            self.scan_api(stale_underlying=True)
        )
        row = self.obs_rows()[0]

        self.assertEqual(row["reason"], "stale_underlying")
        self.assertEqual(row["src"], "chain")
        self.assertEqual(row["bid"], 9.9)
        self.assertEqual(row["greeks"]["gamma"], 0.01)

    def test_observation_band_and_switch(self):
        with patch.object(b, "OBSERVE_BAND", True):
            self.run_scan(self.scan_api(), bs_value=10.5)

        rows = self.obs_rows()

        self.assertEqual(len(rows), 5)
        self.assertEqual(
            sum(row["reason"] == "non_atm" for row in rows),
            4,
        )

        path = (
            self.s.directory
            / "observations"
            / f'{self.day["date"]}.jsonl'
        )
        path.unlink()

        with patch.object(b, "OBSERVE_ALL", False):
            self.run_scan(self.scan_api(), bs_value=10.5)

        self.assertFalse(path.exists())

    def test_observation_write_error_never_raises(self):
        self.s.observe(
            self.day,
            [
                {"t": "x", "greeks": {"iv": float("nan")}},
                {"t": "y", "greeks": {"iv": 1}},
            ],
        )

        self.assertEqual(
            self.day["counts"]["observation_write_error"],
            1,
        )
        self.assertEqual(len(self.obs_rows()), 1)

    def test_report_summarises_and_compresses_observations(self):
        def row(minute, ltp, reason, gap=20, fair=12):
            return {
                "t": f"2026-11-19T10:{minute:02d}:00+05:30",
                "sym": "AAA",
                "exp": "2026-12-29",
                "k": 100,
                "side": "CE",
                "ltp": ltp,
                "reason": reason,
                "gap_pct": gap,
                "fair": fair,
            }

        other = {
            **row(0, 10, "liquidity"),
            "sym": "BBB",
        }

        self.s.observe(
            self.day,
            [
                row(0, 10, "spread"),
                row(10, 12.5, "spread"),
                other,
                {
                    **row(0, 10, "CANDIDATE"),
                    "sym": "CCC",
                    "chan": "SILENT",
                    "action": "SELECTED",
                },
            ],
        )

        self.s.report(self.day, T)

        report = (
            self.s.directory / "reports/2026-11-19.txt"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "OBSERVATION LOG | 4 snapshots of "
            "3 ATM contracts in 3 stocks",
            report,
        )
        self.assertIn(
            "spread: 1 contracts, 1 later traded at/above "
            "the model value (100%)",
            report,
        )
        self.assertIn(
            "liquidity: 1 contracts, 0 later traded at/above "
            "the model value (0%)",
            report,
        )
        self.assertIn(
            "silent record: 1 contracts, 0 later",
            report,
        )

        folder = self.s.directory / "observations"
        self.assertFalse(
            (folder / "2026-11-19.jsonl").exists()
        )

        import gzip

        with gzip.open(
            folder / "2026-11-19.jsonl.gz",
            "rt",
            encoding="utf-8",
        ) as stream:
            self.assertEqual(
                len(stream.read().splitlines()),
                4,
            )

    def test_lower_gap_band_default_and_p_keeps_15_percent_edge(self):
        self.assertEqual(b.RULE_P_MIN_GAP, 0.15)

        def run(gap, bid_qty=300):
            c = contract()
            band = [contract(90), c, contract(110)]
            premium = 100 * (1 - gap)

            c["market"].update(
                ltp=premium,
                bid_price=premium - 0.1,
                ask_price=premium,
                bid_qty=bid_qty,
            )

            with (
                patch.object(b, "GAP", 0.05),
                patch.object(b, "bs", return_value=100),
            ):
                return b.candidate(c, band, 100, T)

        value, reason = run(0.05)
        self.assertIsNone(reason)
        self.assertTrue(value["rules"]["T"])
        self.assertFalse(value["rules"]["P"])

        value, reason = run(0.1499)
        self.assertIsNone(reason)
        self.assertFalse(value["rules"]["P"])

        value, _ = run(0.15)
        self.assertTrue(value["rules"]["P"])

        value, _ = run(0.1699)
        self.assertTrue(value["rules"]["P"])

        self.assertEqual(
            run(0.0499)[1],
            "gap_below_threshold",
        )

        c = contract()
        band = [contract(90), c, contract(110)]
        c["market"].update(
            ltp=92,
            bid_price=91.9,
            ask_price=92,
            oi=100 * 100,
        )

        with (
            patch.object(b, "MAIN_ALSO_IF", "P"),
            patch.object(b, "GAP", 0.05),
            patch.object(b, "bs", return_value=100),
        ):
            value, _ = b.candidate(c, band, 100, T)

        self.assertEqual(value["channel"], "SILENT")

    def test_rule_s_gap_band_and_alert_time(self):
        def run(gap, when=T):
            c = contract()
            band = [contract(90), c, contract(110)]
            premium = 100 * (1 - gap)

            c["market"].update(
                ltp=premium,
                bid_price=premium - 0.1,
                ask_price=premium,
            )

            with (
                patch.object(b, "GAP", 0.05),
                patch.object(b, "RULE_S_MAX_GAP", 0.08),
                patch.object(b, "bs", return_value=100),
            ):
                return b.candidate(c, band, 100, when)

        self.assertEqual(
            run(0.0499)[1],
            "gap_below_threshold",
        )

        for gap, expected in [
            (0.05, True),
            (0.0799, True),
            (0.08, True),
            (0.0801, False),
            (0.12, False),
            (0.20, False),
        ]:
            self.assertEqual(
                run(gap)[0]["rules"]["S"],
                expected,
                gap,
            )

        self.assertTrue(
            run(
                0.06,
                T.replace(hour=14, minute=59),
            )[0]["rules"]["S"]
        )
        self.assertFalse(
            run(
                0.06,
                T.replace(hour=15, minute=0),
            )[0]["rules"]["S"]
        )

        with patch.object(b, "MAIN_RULES", "S"):
            value = run(0.06)[0]

            self.assertEqual(value["channel"], "MAIN")
            self.assertTrue(
                value["header"].startswith("MAIN |")
            )
            self.assertEqual(
                run(0.10)[0]["channel"],
                "SILENT",
            )
            self.assertEqual(
                run(
                    0.06,
                    T.replace(hour=15, minute=5),
                )[0]["channel"],
                "SILENT",
            )

    def test_shipped_defaults_make_r_the_main_rule(self):
        import os
        import subprocess
        import sys

        excluded = {
            "MAIN_RULES",
            "MAIN_ALSO_IF",
            "MIN_VALUE_GAP",
            "RULE_S_MIN_GAP",
            "RULE_S_MAX_GAP",
            "RULE_S_CUTOFF",
            "RULE_P_MIN_GAP",
        }

        env = {
            key: value
            for key, value in os.environ.items()
            if key not in excluded
        }

        code = (
            "import stock_s3_bot as b;"
            "print("
            "b.MAIN_RULES,"
            "b.MAIN_ALSO_IF,"
            "b.GAP,"
            "b.RULE_S_MIN_GAP,"
            "b.RULE_S_MAX_GAP,"
            "b.RULE_S_CUTOFF_MIN,"
            "b.RULE_ORDER"
            ")"
        )

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            cwd=os.path.dirname(os.path.abspath(b.__file__)),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.split(),
            [
                "R",
                "0.05",
                "0.05",
                "0.08",
                "900",
                "TDOAVGEIPSQR",
            ],
            result.stderr,
        )

    def test_rule_boundaries_pass(self):
        c = contract()
        band = [contract(90), c, contract(110)]

        c["market"].update(
            oi=500 * 100,
            volume=3 * 500 * 100,
        )
        c["greeks"]["gamma"] = 0.006
        c["last_trade_time"] = str(
            int(
                (T - timedelta(seconds=10)).timestamp()
                * 1000
            )
        )

        with patch.object(b, "bs", return_value=12):
            value, reason = b.candidate(c, band, 100, T)

        self.assertIsNone(reason)
        self.assertEqual(value["rules_failed"], "")
        self.assertEqual(value["channel"], "MAIN")

    def test_missing_data_fails_rules(self):
        c = contract()
        band = [contract(90), c, contract(110)]
        c.pop("last_trade_time")
        c["greeks"].pop("gamma")

        with patch.object(b, "bs", return_value=12):
            value, reason = b.candidate(c, band, 100, T)

        self.assertIsNone(reason)
        self.assertEqual(value["rules_failed"], "AG")
        self.assertIsNone(value["metrics"]["trade_age_s"])

    def test_main_rules_setting_changes_channel(self):
        c = contract()
        band = [contract(90), c, contract(110)]
        c["greeks"]["gamma"] = 0.001

        with patch.object(b, "bs", return_value=12):
            value, _ = b.candidate(c, band, 100, T)

        self.assertEqual(b.MAIN_RULES, "TOAVEI")
        self.assertEqual(value["channel"], "MAIN")
        self.assertEqual(value["rules_failed"], "G")

        with (
            patch.object(b, "MAIN_RULES", "TOAVEIG"),
            patch.object(b, "bs", return_value=12),
        ):
            value, _ = b.candidate(c, band, 100, T)

        self.assertEqual(value["channel"], "SILENT")

    def test_silent_record_sends_nothing(self):
        c = contract()
        value = {
            "entry": 10,
            "target": 12,
            "direction": "UP",
            "gap_pct": 20,
            "peer_iv": 30,
            "channel": "SILENT",
            "policy": b.FILTER_POLICY,
            "header": "SILENT | T✓",
        }

        watch = self.s.add_watch(c, value, T, self.day)
        self.assertEqual(self.s.state["outbox"], [])

        self.s.finish(watch, T, 12, "fixture")

        self.assertEqual(watch["status"], "VALUE_REACHED")
        self.assertEqual(self.s.state["outbox"], [])

    def test_main_alert_message_has_header_and_rule_values(self):
        c = contract()
        band = [contract(90), c, contract(110)]

        with patch.object(b, "bs", return_value=12):
            value, _ = b.candidate(c, band, 100, T)

        value = {**value, "policy": b.FILTER_POLICY}
        watch = self.s.add_watch(c, value, T, self.day)
        text = self.s.state["outbox"][0]["text"]

        self.assertIn(
            "MAIN | T✓ D✓ O✓ A✓ V✓ G✓ E✓ I✓ P✓ S✓ | 10/10",
            text,
        )
        self.assertIn("Rule values: OI 3000 lots", text)

        self.s.finish(watch, T, 12, "fixture")

        self.assertIn(
            "MAIN | T✓",
            self.s.state["outbox"][1]["text"],
        )

    def test_silent_and_main_do_not_block_each_other(self):
        watch = self.watch()
        watch["policy"] = b.FILTER_POLICY
        watch["channel"] = "SILENT"

        chain = [
            contract(k, "PE")
            for k in [90, 95, 100, 105, 110]
        ]

        with patch.object(b, "bs", return_value=12):
            choice = self.s.choose_buy_atm(
                chain, chain, 100, T, self.day
            )

        self.assertEqual(choice[1]["channel"], "MAIN")

        watch["channel"] = "MAIN"

        for c in chain:
            c["market"]["oi"] = 499 * 100

        with patch.object(b, "bs", return_value=12):
            choice = self.s.choose_buy_atm(
                chain, chain, 100, T, self.day
            )

        self.assertEqual(choice[1]["channel"], "SILENT")

        watch["channel"] = "SILENT"

        with patch.object(b, "bs", return_value=12):
            self.assertIsNone(
                self.s.choose_buy_atm(
                    chain, chain, 100, T, self.day
                )
            )

    def test_silent_contract_can_later_become_main(self):
        chain = [
            contract(k, "PE")
            for k in [90, 95, 100, 105, 110]
        ]
        silent_chain = copy.deepcopy(chain)

        for c in silent_chain:
            c["market"]["oi"] = 499 * 100

        with patch.object(b, "bs", return_value=12):
            first = self.s.choose_buy_atm(
                silent_chain,
                silent_chain,
                100,
                T,
                self.day,
            )
            self.assertEqual(first[1]["channel"], "SILENT")

            silent_watch = self.s.add_watch(
                first[0], first[1], T, self.day
            )

            self.assertIsNone(
                self.s.choose_buy_atm(
                    silent_chain,
                    silent_chain,
                    100,
                    T,
                    self.day,
                )
            )

            silent_watch["status"] = "NO_TOUCH_IN_MONITORED_BARS"

            self.assertIsNone(
                self.s.choose_buy_atm(
                    silent_chain,
                    silent_chain,
                    100,
                    T,
                    self.day,
                )
            )

            second = self.s.choose_buy_atm(
                chain, chain, 100, T, self.day
            )

        self.assertEqual(second[1]["channel"], "MAIN")

        main_watch = self.s.add_watch(
            second[0], second[1], T, self.day
        )

        self.assertNotEqual(
            silent_watch["id"],
            main_watch["id"],
        )

    def test_report_rule_lab_counts(self):
        def add(key, flags, status, last=10, channel=None):
            c = contract(key)
            value = {
                "entry": 10,
                "target": 12,
                "direction": "UP",
                "gap_pct": 20,
                "peer_iv": 30,
                "policy": b.FILTER_POLICY,
                "rules": flags,
                "channel": channel or (
                    "MAIN" if all(flags.values()) else "SILENT"
                ),
                "header": "H",
            }

            watch = self.s.add_watch(c, value, T, self.day)
            watch["status"] = status
            watch["last_price"] = last

            if status == "VALUE_REACHED":
                watch.update(
                    hit_price=12,
                    hit_time=b.iso(T),
                    hit_evidence="fixture",
                )

            return watch

        good = {key: True for key in b.RULE_ORDER}
        bad = {**good, "O": False}

        add(101, good, "VALUE_REACHED", 12)
        add(102, good, "VALUE_REACHED", 12)
        add(103, bad, "NO_TOUCH_IN_MONITORED_BARS", 5)
        add(104, bad, "VALUE_REACHED", 12)

        self.s.report(self.day, T)

        report = (
            self.s.directory / "reports/2026-11-19.txt"
        ).read_text(encoding="utf-8")

        self.assertIn("O pass 2/2 (100.0%)", report)
        self.assertIn("| fail 1/2 (50.0%)", report)
        self.assertIn("misses end -50% vs entry", report)
        self.assertIn(
            "MAIN channel (TOAVEI): 2/2 (100.0%)",
            report,
        )
        self.assertIn(
            "Gap 15-30%: all records 3/4 (75.0%)",
            report,
        )
        self.assertIn(
            "All main rules except O passed: 3/4 (75.0%)",
            report,
        )
        self.assertIn(
            "9/10 rules passed: 1/2 (50.0%)",
            report,
        )
        self.assertIn(
            "SILENT RECORDS (header shows each rule",
            report,
        )

    def test_previous_policy_open_watch_still_blocks(self):
        watch = self.watch()
        watch["policy"] = "BUY_ATM_V1"

        chain = [
            contract(k, "PE")
            for k in [90, 95, 100, 105, 110]
        ]

        with patch.object(b, "bs", return_value=12):
            self.assertIsNone(
                self.s.choose_buy_atm(
                    chain, chain, 100, T, self.day
                )
            )

        self.assertEqual(
            self.day["counts"]["existing_open_stock_watch"],
            1,
        )

    def test_rollover_calendar_boundaries(self):
        dates = {"2026-11-24", "2026-12-29"}

        self.assertEqual(
            b.selected_expiry(dates, "2026-11-18"),
            "2026-11-24",
        )
        self.assertEqual(
            b.selected_expiry(dates, "2026-11-19"),
            "2026-12-29",
        )
        self.assertEqual(
            b.selected_expiry(dates, "2026-11-24"),
            "2026-12-29",
        )
        self.assertIsNone(
            b.selected_expiry(
                {"2026-11-24"},
                "2026-11-19",
            )
        )

    def test_close_effective_dates(self):
        self.assertEqual(
            b.stock_close(T.replace(month=7)),
            b.dt_time(15, 30),
        )
        self.assertEqual(
            b.stock_close(T),
            b.dt_time(15, 40),
        )
        self.assertEqual(
            b.stock_close(T.replace(month=12, day=7)),
            b.dt_time(15, 45),
        )
        self.assertEqual(
            b.alert_cutoff(T.replace(month=12, day=7)),
            b.dt_time(15, 20),
        )

    def test_extended_gap_oi_boundaries(self):
        c = contract()
        band = [contract(90), c, contract(110)]

        cases = [
            (0.1699, 20, None, True),
            (0.17, 1999, None, False),
            (0.17, 2000, None, True),
            (0.25, 2000, None, True),
            (0.2999, 2000, None, True),
            (0.30, 2000, "gap_above_band", None),
        ]

        for gap, oi, expected, tier_ok in cases:
            with self.subTest(gap=gap, oi=oi):
                premium = 100 * (1 - gap)

                c["market"].update(
                    ltp=premium,
                    bid_price=premium - 0.1,
                    ask_price=premium,
                    oi=oi * c["lot"],
                )

                with patch.object(b, "bs", return_value=100):
                    value, reason = b.candidate(
                        c, band, 100, T
                    )

                self.assertEqual(reason, expected)

                if value:
                    self.assertEqual(
                        value["gap_tier"],
                        "EXTENDED_OI" if gap >= 0.17 else "BASE",
                    )
                    self.assertEqual(
                        value["rules"]["T"],
                        tier_ok,
                    )

    def test_v2_open_watch_still_blocks(self):
        watch = self.watch()
        watch["policy"] = "BUY_ATM_GAP_DEPTH_V2"

        chain = [
            contract(k, "PE")
            for k in [90, 95, 100, 105, 110]
        ]

        with patch.object(b, "bs", return_value=12):
            self.assertIsNone(
                self.s.choose_buy_atm(
                    chain, chain, 100, T, self.day
                )
            )

        self.assertEqual(
            self.day["counts"]["existing_open_stock_watch"],
            1,
        )

    def test_wrong_state_rejected(self):
        self.s.path.write_text(
            json.dumps({"kind": "nifty"})
        )

        with self.assertRaises(RuntimeError):
            b.Scanner(self.api, self.tmp.name)


if __name__ == "__main__":
    unittest.main()
