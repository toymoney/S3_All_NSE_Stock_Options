import gzip
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import stock_s3_bot as b


T = datetime(2026, 11, 27, 10, 0, 30, tzinfo=b.IST)


class QualityV5Tests(unittest.TestCase):
    def setUp(self):
        # Retain V5 comparison regression coverage under its explicit route.
        patcher = patch.object(b, "MAIN_RULES", "Q")
        patcher.start()
        self.addCleanup(patcher.stop)

    def metrics(self):
        return {
            "gap_pct": 6.0,
            "ask_upside_pct": 6.0,
            "delta": 0.51,
            "iv": 25.0,
            "oi_lots": 800.0,
            "volume_lots": 300.0,
            "spread_pct": 0.8,
            "trade_age_s": 5.0,
            "bid_lots": 1.0,
            "ask_lots": 1.0,
        }

    def contract(self):
        return {
            "key": "FO|100CE",
            "symbol": "SAMPLE",
            "underlying": "EQ|1",
            "expiry": "2026-12-29",
            "strike": 100,
            "side": "CE",
            "lot": 100,
            "spot": 100,
            "market": {
                "ltp": 94,
                "bid_price": 94.5,
                "ask_price": 95,
                "oi": 80000,
                "volume": 30000,
                "bid_qty": 100,
                "ask_qty": 100,
            },
            "greeks": {
                "iv": 25,
                "delta": 0.51,
                "gamma": 0.001,
                "theta": -0.1,
                "vega": 0.2,
            },
            "quote_time": T.isoformat(),
            "last_trade_time": str(int(T.timestamp() * 1000)),
        }

    def read_events(self, scanner, day):
        path = (
            scanner.directory
            / "observations"
            / f'{day["date"]}.events.jsonl.gz'
        )

        with gzip.open(path, "rt", encoding="utf-8") as stream:
            return [
                json.loads(line)
                for line in stream
                if line.strip()
            ]

    def test_frozen_defaults(self):
        self.assertEqual(b.FILTER_POLICY, "BUY_ATM_ACTIVITY_V7")
        self.assertEqual(b.MAIN_RULES, "Q")
        self.assertEqual(b.MAIN_ALSO_IF, "")
        self.assertTrue(b.OBSERVE_ALL)
        self.assertTrue(b.OBSERVE_BAND)

        self.assertEqual(
            b.QUALITY_SETTINGS,
            {
                "gap_min_pct": 5.0,
                "gap_max_exclusive_pct": 7.0,
                "min_ask_upside_pct": 5.0,
                "min_abs_delta": 0.50,
                "max_iv_pct": 28.0,
                "min_oi_lots": 500.0,
                "min_volume_lots": 100.0,
                "max_spread_pct": 1.0,
                "max_trade_age_s": 40.0,
                "min_depth_lots": 1.0,
            },
        )

    def test_boundaries_and_missing_inputs(self):
        good = self.metrics()
        self.assertEqual(b.quality_failures(good), [])

        passing_boundaries = {
            "gap_pct": 5.0,
            "ask_upside_pct": 5.0,
            "delta": -0.50,
            "iv": 28.0,
            "oi_lots": 500.0,
            "volume_lots": 100.0,
            "spread_pct": 1.0,
            "trade_age_s": 40.0,
            "bid_lots": 1.0,
            "ask_lots": 1.0,
        }

        for key, value in passing_boundaries.items():
            with self.subTest(check="passing", field=key):
                self.assertEqual(
                    b.quality_failures({**good, key: value}),
                    [],
                )

        failing_values = {
            "gap_pct": 7.0,
            "ask_upside_pct": 4.99,
            "delta": 0.4947,
            "iv": 28.01,
            "oi_lots": 499.0,
            "volume_lots": 99.0,
            "spread_pct": 1.01,
            "trade_age_s": 40.01,
            "bid_lots": 0.99,
            "ask_lots": 0.99,
        }

        for key, value in failing_values.items():
            with self.subTest(check="failing", field=key):
                self.assertTrue(
                    b.quality_failures({**good, key: value})
                )

            for invalid in (None, float("nan"), float("inf")):
                with self.subTest(
                    check="invalid",
                    field=key,
                    value=invalid,
                ):
                    self.assertTrue(
                        b.quality_failures({**good, key: invalid})
                    )

            missing = dict(good)
            missing.pop(key)

            with self.subTest(check="missing", field=key):
                self.assertTrue(b.quality_failures(missing))

        self.assertTrue(
            b.quality_failures({**good, "trade_age_s": -1})
        )
        self.assertTrue(
            b.quality_failures({**good, "gap_pct": 4.99})
        )
        self.assertTrue(
            b.quality_failures({**good, "iv": 0})
        )
        self.assertTrue(
            b.quality_failures({**good, "spread_pct": -0.1})
        )

    def test_reported_five_entry_metrics_pass(self):
        # Entry values from the supplied summaries.
        # These fixtures test selection, not future trade outcomes.
        entries = [
            {
                "name": "25 Nov EICHERMOT",
                "ask": 256.2,
                "target": 271.0867,
                "gap_pct": 5.971030019118606,
                "delta": 0.513,
                "iv": 25.34,
                "oi_lots": 773,
                "volume_lots": 2590,
                "spread_pct": 0.5871990604815033,
                "trade_age_s": 2.971,
                "bid_lots": 1,
                "ask_lots": 2,
            },
            {
                "name": "25 Nov MARUTI",
                "ask": 429.7,
                "target": 459.4506,
                "gap_pct": 6.409958842372188,
                "delta": 0.5083,
                "iv": 25.56,
                "oi_lots": 754,
                "volume_lots": 2069,
                "spread_pct": 0.735766423357659,
                "trade_age_s": 0.388,
                "bid_lots": 2,
                "ask_lots": 2,
            },
            {
                "name": "25 Nov DMART",
                "ask": 120.9,
                "target": 128.1336,
                "gap_pct": 6.425823750634961,
                "delta": 0.5387,
                "iv": 25.85,
                "oi_lots": 769,
                "volume_lots": 5198,
                "spread_pct": 0.7471980074719848,
                "trade_age_s": 3.743,
                "bid_lots": 1,
                "ask_lots": 1,
            },
            {
                "name": "26 Nov MARUTI",
                "ask": 453.95,
                "target": 479.3991,
                "gap_pct": 5.7152966700576835,
                "delta": 0.5345,
                "iv": 25.5,
                "oi_lots": 2368,
                "volume_lots": 340,
                "spread_pct": 0.4304873337380625,
                "trade_age_s": 0.576,
                "bid_lots": 1,
                "ask_lots": 2,
            },
            {
                "name": "26 Nov BAJAJ-AUTO",
                "ask": 306.05,
                "target": 325.8236,
                "gap_pct": 5.132716126147323,
                "delta": 0.5104,
                "iv": 24.09,
                "oi_lots": 1291,
                "volume_lots": 516,
                "spread_pct": 0.8366827987859935,
                "trade_age_s": 35.656,
                "bid_lots": 1,
                "ask_lots": 1,
            },
        ]

        for entry in entries:
            with self.subTest(entry=entry["name"]):
                metrics = {
                    **entry,
                    "ask_upside_pct": (
                        (entry["target"] - entry["ask"])
                        / entry["ask"]
                        * 100
                    ),
                }

                self.assertEqual(
                    b.quality_failures(metrics),
                    [],
                )

    def test_volume_oi_ratio_and_gamma_are_not_q_gates(self):
        metrics = self.metrics()
        metrics.update(
            vol_oi=6.76,
            gamma=0.0001,
        )

        self.assertEqual(b.quality_failures(metrics), [])

    def test_five_percent_ask_upside_uses_ask_denominator(self):
        c = self.contract()
        c["market"].update(
            ltp=94,
            ask_price=100 / 1.05,
            bid_price=95,
        )

        with (
            patch.object(b, "peer_iv", return_value=0.25),
            patch.object(b, "bs", return_value=100),
        ):
            value, reason = b.candidate(c, [c], 100, T)

        self.assertIsNone(reason)
        self.assertIsNotNone(value)
        self.assertEqual(value["channel"], "MAIN")
        self.assertTrue(value["rules"]["Q"])

        self.assertAlmostEqual(
            value["metrics"]["ask_upside_pct"],
            5,
        )
        self.assertEqual(value["target"], 100)
        self.assertAlmostEqual(
            value["fixed_5pct_target"],
            100,
        )

    def test_no_alternative_route_bypasses_q(self):
        flags = {key: True for key in b.RULE_ORDER}
        flags["Q"] = False

        channel, _ = b.rule_header(flags)
        self.assertEqual(channel, "SILENT")

        flags = {key: False for key in b.RULE_ORDER}
        flags["Q"] = True

        channel, _ = b.rule_header(flags)
        self.assertEqual(channel, "MAIN")

    def test_failed_candidate_is_still_recorded_silently(self):
        c = self.contract()
        c["market"]["oi"] = 49900

        with (
            patch.object(b, "peer_iv", return_value=0.25),
            patch.object(b, "bs", return_value=100),
        ):
            value, reason = b.candidate(c, [c], 100, T)

        self.assertIsNone(reason)
        self.assertEqual(value["channel"], "SILENT")
        self.assertIn(
            "oi_at_least_500_lots",
            value["quality_failures"],
        )

        with tempfile.TemporaryDirectory() as directory:
            scanner = b.Scanner(None, directory)
            day = {
                "date": T.date().isoformat(),
                "counts": {},
            }
            scanner.state["days"][day["date"]] = day

            watch = scanner.add_watch(
                c,
                {**value, "policy": b.FILTER_POLICY},
                T,
                day,
            )

            self.assertEqual(scanner.state["outbox"], [])

            events = self.read_events(scanner, day)

            self.assertEqual(
                events[0]["type"],
                "watch_entry",
            )
            self.assertEqual(
                events[0]["watch"]["channel"],
                "SILENT",
            )
            self.assertIn(
                "oi_at_least_500_lots",
                events[0]["watch"]["quality_failures"],
            )

            scanner.save()
            restored = b.Scanner(None, directory)

            self.assertEqual(
                restored.state["watches"][0]["id"],
                watch["id"],
            )

    def test_recording_continues_after_model_touch(self):
        c = self.contract()

        class API:
            def quotes(self, keys):
                return {
                    c["key"]: {
                        "last_price": 101,
                        "last_trade_time": str(
                            int(T.timestamp() * 1000)
                        ),
                        "depth": {
                            "buy": [
                                {
                                    "price": 100.5,
                                    "quantity": 100,
                                }
                            ],
                            "sell": [
                                {
                                    "price": 101,
                                    "quantity": 100,
                                }
                            ],
                        },
                    }
                }

            def candles(self, *args):
                candle_time = (
                    T - timedelta(minutes=2)
                ).replace(second=0, microsecond=0)

                return [
                    [
                        candle_time.isoformat(),
                        99,
                        102,
                        98,
                        101,
                        100,
                        80000,
                    ]
                ]

        with tempfile.TemporaryDirectory() as directory:
            scanner = b.Scanner(API(), directory)
            day = {
                "date": T.date().isoformat(),
                "counts": {},
            }
            scanner.state["days"][day["date"]] = day

            value = {
                "entry": 94,
                "entry_ask": 95,
                "target": 100,
                "fixed_5pct_target": 99.75,
                "direction": "UP",
                "channel": "SILENT",
                "policy": b.FILTER_POLICY,
            }

            watch = scanner.add_watch(
                c,
                value,
                T - timedelta(minutes=5),
                day,
            )

            scanner.finish(
                watch,
                T - timedelta(minutes=1),
                100,
                "fixture",
            )

            original_hit_time = watch["hit_time"]

            with patch.object(b, "now", return_value=T):
                scanner.monitor_quotes(T)

            scanner.candle_check(watch, T)

            events = self.read_events(scanner, day)
            event_types = [event["type"] for event in events]

            self.assertIn("monitor_quote", event_types)
            self.assertIn("completed_candles", event_types)

            quote_event = next(
                event
                for event in events
                if event["type"] == "monitor_quote"
            )

            self.assertEqual(
                quote_event["quote"]["depth"]["buy"][0]["price"],
                100.5,
            )

            self.assertEqual(
                watch["hit_time"],
                original_hit_time,
            )
            self.assertEqual(
                watch["status"],
                "VALUE_REACHED",
            )
            self.assertIn(
                "fixed_5pct_ltp_touch",
                watch,
            )
            self.assertEqual(
                scanner.state["outbox"],
                [],
            )


if __name__ == "__main__":
    unittest.main()
