#!/usr/bin/env python3
"""S3-only NSE stock-option value-gap research scanner.

Python 3.11+. No broker order execution.

V5 quality filter Q:
- Recorded model gap >=5% and <7%
- Model value at least 5% above entry ask
- Absolute delta >=0.50
- IV <=28%
- OI >=500 lots
- Volume >=100 lots
- Spread <=1%
- Last-trade age between 0 and 40 seconds
- Best bid and ask depth >=1 lot each

Existing research rules remain recorded for comparison.
Rejected observations, monitoring quotes and candle evidence are retained.
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


IST = ZoneInfo("Asia/Kolkata")
FILTER_POLICY = "BUY_ATM_QUALITY_V5"

LEGACY_POLICIES = (
    "BUY_ATM_RULELAB_V4",
    "BUY_ATM_V1",
    "BUY_ATM_GAP_DEPTH_V2",
    "BUY_ATM_GAP_DEPTH_OI_V3",
)

ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(os.getenv("STATE_DIR", str(ROOT / "state_s3")))

MASTER_URL = (
    "https://assets.upstox.com/"
    "market-quote/instruments/exchange/NSE.json.gz"
)

# Broad research collection settings.
# These are separate from the strict Q alert filter.
GAP = float(os.getenv("MIN_VALUE_GAP", "0.05"))
BASE_MAX_GAP = float(os.getenv("BASE_MAX_VALUE_GAP", "0.17"))
MAX_GAP = float(os.getenv("MAX_VALUE_GAP", "0.30"))
EXTENDED_MIN_OI_LOTS = float(
    os.getenv("EXTENDED_MIN_OI_LOTS", "2000")
)
MIN_DEPTH_LOTS = float(os.getenv("MIN_DEPTH_LOTS", "3"))

if (
    not (0 < GAP < BASE_MAX_GAP < MAX_GAP < 1)
    or not math.isfinite(MIN_DEPTH_LOTS)
    or MIN_DEPTH_LOTS < 3
):
    raise ValueError("Invalid research gap bands or depth floor")

if (
    not math.isfinite(EXTENDED_MIN_OI_LOTS)
    or EXTENDED_MIN_OI_LOTS < 2000
):
    raise ValueError("EXTENDED_MIN_OI_LOTS must be at least 2000")

# Historical rule flags retained for comparison.
# Q applies its own complete, frozen thresholds below.
RULE_MIN_OI_LOTS = float(os.getenv("RULE_MIN_OI_LOTS", "500"))
RULE_MAX_TRADE_AGE = float(os.getenv("RULE_MAX_TRADE_AGE", "10"))
RULE_MAX_VOL_OI = float(os.getenv("RULE_MAX_VOL_OI", "3"))
RULE_MIN_GAMMA = float(os.getenv("RULE_MIN_GAMMA", "0.006"))
RULE_MIN_ABS_DELTA = float(
    os.getenv("RULE_MIN_ABS_DELTA", "0.35")
)
RULE_MAX_IV = float(os.getenv("RULE_MAX_IV", "28"))
RULE_P_MIN_GAP = float(os.getenv("RULE_P_MIN_GAP", "0.15"))
RULE_S_MIN_GAP = float(os.getenv("RULE_S_MIN_GAP", "0.05"))
RULE_S_MAX_GAP = float(os.getenv("RULE_S_MAX_GAP", "0.08"))

try:
    _h, _m = os.getenv("RULE_S_CUTOFF", "15:00").split(":")
    RULE_S_CUTOFF_MIN = int(_h) * 60 + int(_m)
except ValueError:
    raise ValueError("RULE_S_CUTOFF must look like 15:00")

RULE_ORDER = "TDOAVGEIPSQ"

QUALITY_SETTINGS = {
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
}

MAIN_RULES = os.getenv("MAIN_RULES", "Q").strip().upper()
MAIN_ALSO_IF = os.getenv("MAIN_ALSO_IF", "").strip().upper()

OBSERVE_ALL = (
    os.getenv("OBSERVE_ALL", "true").strip().lower() != "false"
)
OBSERVE_BAND = (
    os.getenv("OBSERVE_BAND", "true").strip().lower() == "true"
)
OBS_MIN_GAP = float(
    os.getenv("OBSERVE_FUNNEL_MIN_GAP", "0.05")
)
REPORT_SILENT_LINES = (
    os.getenv("REPORT_SILENT_LINES", "true").strip().lower()
    != "false"
)

if (
    not MAIN_RULES
    or any(k not in RULE_ORDER for k in MAIN_RULES)
    or len(set(MAIN_RULES)) != len(MAIN_RULES)
    or any(k not in RULE_ORDER for k in MAIN_ALSO_IF)
    or len(set(MAIN_ALSO_IF)) != len(MAIN_ALSO_IF)
    or not (0 < RULE_P_MIN_GAP < BASE_MAX_GAP)
    or not (0 < RULE_S_MIN_GAP < RULE_S_MAX_GAP < 1)
    or not (0 <= RULE_S_CUTOFF_MIN <= 24 * 60)
    or not all(
        math.isfinite(x) and x > 0
        for x in (
            RULE_MIN_OI_LOTS,
            RULE_MAX_TRADE_AGE,
            RULE_MAX_VOL_OI,
            RULE_MIN_GAMMA,
            RULE_MIN_ABS_DELTA,
            RULE_MAX_IV,
        )
    )
):
    raise ValueError(
        "Invalid MAIN_RULES or rule thresholds. "
        "Letters must come from TDOAVGEIPSQ without repeats."
    )

MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.05"))
MIN_PREMIUM = float(os.getenv("MIN_PREMIUM", "1"))
MIN_OI_LOTS = float(os.getenv("MIN_OI_LOTS", "20"))
MIN_VOLUME_LOTS = float(os.getenv("MIN_VOLUME_LOTS", "5"))
FRESH_SECONDS = int(os.getenv("FRESH_SECONDS", "180"))
RISK_RATE = float(os.getenv("RISK_FREE_RATE", ".06"))
DIVIDEND_YIELDS = json.loads(
    os.getenv("DIVIDEND_YIELDS_JSON", "{}")
)


def now():
    return datetime.now(IST)


def iso(t):
    return t.isoformat()


def parse(t):
    return datetime.fromisoformat(t).astimezone(IST)


def num(v, default=0.0):
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def expiry_date(value):
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return (
                datetime.fromtimestamp(float(value) / 1000, IST)
                .date()
                .isoformat()
            )
        return (
            datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            .date()
            .isoformat()
        )
    except (ValueError, TypeError, OverflowError):
        return None


def stock_close(t):
    # Existing repository calendar behavior retained.
    day = t.date().isoformat()
    if day >= "2026-12-07":
        return dt_time(15, 45)
    if day >= "2026-08-03":
        return dt_time(15, 40)
    return dt_time(15, 30)


def close_at(t):
    closing = stock_close(t)
    return t.replace(
        hour=closing.hour,
        minute=closing.minute,
        second=0,
        microsecond=0,
    )


def alert_cutoff(t):
    return dt_time(15, 20)


def selected_expiry(expiries, day):
    # Five calendar days; actual expiries come from the master.
    eligible = sorted(
        e
        for e in expiries
        if (
            datetime.fromisoformat(e).date()
            - datetime.fromisoformat(day).date()
        ).days > 5
    )
    return eligible[0] if eligible else None


def universe_from_master(rows, date):
    equities = {
        x["instrument_key"]: x
        for x in rows
        if x.get("segment") == "NSE_EQ"
        and x.get("instrument_type") == "EQ"
    }

    grouped = {}
    metadata = {}

    for row in rows:
        if (
            row.get("segment") != "NSE_FO"
            or row.get("instrument_type") not in ("CE", "PE")
        ):
            continue

        key = row.get("underlying_key", "")
        expiry = expiry_date(row.get("expiry"))

        if (
            row.get("underlying_type") != "EQUITY"
            or key not in equities
            or not expiry
            or expiry < date
        ):
            continue

        if (
            num(row.get("lot_size")) <= 0
            or num(row.get("strike_price")) <= 0
        ):
            continue

        symbol = (
            row.get("underlying_symbol")
            or equities[key].get("trading_symbol")
        )

        if not symbol or "DUMMY" in key or "NSETEST" in symbol:
            continue

        grouped.setdefault(
            key,
            {
                "underlying": key,
                "symbol": symbol,
                "expiries": set(),
            },
        )["expiries"].add(expiry)

        metadata[row["instrument_key"]] = {
            "lot": int(row["lot_size"]),
            "expiry": expiry,
            "strike": num(row["strike_price"]),
            "side": row["instrument_type"],
            "underlying": key,
            "symbol": symbol,
        }

    universe = []

    for group in grouped.values():
        expiry = selected_expiry(group["expiries"], date)
        if expiry:
            universe.append(
                {
                    "underlying": group["underlying"],
                    "symbol": group["symbol"],
                    "expiry": expiry,
                }
            )

    return sorted(universe, key=lambda x: x["symbol"]), metadata


def atm_strike(chain, spot):
    strikes = {c["strike"] for c in chain}
    if not strikes:
        return None
    return min(strikes, key=lambda k: (abs(k - spot), -k))


def select_band(chain, spot):
    strikes = sorted({c["strike"] for c in chain})
    if not strikes:
        return []

    atm = atm_strike(chain, spot)
    index = strikes.index(atm)
    selected = set(strikes[max(0, index - 2):index + 3])

    return [c for c in chain if c["strike"] in selected]


def liquid(c):
    m = c["market"]
    lot = c["lot"]
    bid = num(m.get("bid_price"))
    ask = num(m.get("ask_price"))

    return (
        lot > 0
        and num(m.get("ltp")) >= MIN_PREMIUM
        and 0 < bid <= ask
        and (ask - bid) / max((ask + bid) / 2, 0.01) <= MAX_SPREAD
        and num(m.get("oi")) >= MIN_OI_LOTS * lot
        and num(m.get("volume")) >= MIN_VOLUME_LOTS * lot
        and num(m.get("bid_qty")) >= lot
        and num(m.get("ask_qty")) >= lot
    )


def fresh(q, t):
    ms = num(q.get("last_trade_time"))
    if ms <= 0:
        return False

    try:
        traded = datetime.fromtimestamp(ms / 1000, IST)
        return (
            traded.date() == t.date()
            and -5 <= (t - traded).total_seconds() <= FRESH_SECONDS
        )
    except (ValueError, OverflowError):
        return False


def quote_contract(c, q):
    buys = q.get("depth", {}).get("buy") or []
    sells = q.get("depth", {}).get("sell") or []

    if not buys or not sells:
        return None

    return {
        **c,
        "market": {
            "ltp": num(q.get("last_price")),
            "oi": num(q.get("oi")),
            "volume": num(q.get("volume")),
            "bid_price": num(buys[0].get("price")),
            "ask_price": num(sells[0].get("price")),
            "bid_qty": num(buys[0].get("quantity")),
            "ask_qty": num(sells[0].get("quantity")),
        },
        "quote_time": q.get("timestamp"),
        "last_trade_time": q.get("last_trade_time"),
    }


RULE_TEXT = {
    "T": (
        f"tier OI (gap below {BASE_MAX_GAP * 100:g}%: any; "
        f"{BASE_MAX_GAP * 100:g}-<{MAX_GAP * 100:g}%: "
        f">= {EXTENDED_MIN_OI_LOTS:g} lots)"
    ),
    "D": f"depth >= {MIN_DEPTH_LOTS:g} lots on best bid AND ask",
    "O": f"OI >= {RULE_MIN_OI_LOTS:g} lots",
    "A": f"last trade <= {RULE_MAX_TRADE_AGE:g} s old",
    "V": f"volume/OI <= {RULE_MAX_VOL_OI:g}",
    "G": f"gamma >= {RULE_MIN_GAMMA:g}",
    "E": f"|delta| >= {RULE_MIN_ABS_DELTA:g}",
    "I": f"IV <= {RULE_MAX_IV:g}%",
    "P": (
        f"gap {RULE_P_MIN_GAP * 100:g}% to below "
        f"{BASE_MAX_GAP * 100:g}% AND depth >= "
        f"{MIN_DEPTH_LOTS:g} lots on best bid and ask"
    ),
    "S": (
        f"gap {RULE_S_MIN_GAP * 100:g}% to "
        f"{RULE_S_MAX_GAP * 100:g}% AND alert before "
        f"{RULE_S_CUTOFF_MIN // 60:02d}:"
        f"{RULE_S_CUTOFF_MIN % 60:02d} IST"
    ),
    "Q": (
        "5<=gap<7%; ask upside>=5%; |delta|>=0.50; "
        "IV<=28%; OI>=500 lots; volume>=100 lots; "
        "spread<=1%; trade age 0..40s; depth>=1 lot each"
    ),
}


def quality_failures(metrics):
    """Frozen V5 thresholds. Missing/nonfinite inputs fail."""

    def finite(key):
        value = metrics.get(key)
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )

    checks = {
        "gap_5_to_below_7": (
            finite("gap_pct")
            and 5 <= metrics["gap_pct"] < 7
        ),
        "ask_upside_at_least_5": (
            finite("ask_upside_pct")
            and (
                metrics["ask_upside_pct"] >= 5
                or math.isclose(
                    metrics["ask_upside_pct"],
                    5,
                    rel_tol=0,
                    abs_tol=1e-10,
                )
            )
        ),
        "abs_delta_at_least_0_50": (
            finite("delta")
            and abs(metrics["delta"]) >= 0.50
        ),
        "iv_up_to_28": (
            finite("iv")
            and 0 < metrics["iv"] <= 28
        ),
        "oi_at_least_500_lots": (
            finite("oi_lots")
            and metrics["oi_lots"] >= 500
        ),
        "volume_at_least_100_lots": (
            finite("volume_lots")
            and metrics["volume_lots"] >= 100
        ),
        "spread_up_to_1_pct": (
            finite("spread_pct")
            and 0 <= metrics["spread_pct"] <= 1
        ),
        "trade_age_0_to_40s": (
            finite("trade_age_s")
            and 0 <= metrics["trade_age_s"] <= 40
        ),
        "bid_depth_at_least_1_lot": (
            finite("bid_lots")
            and metrics["bid_lots"] >= 1
        ),
        "ask_depth_at_least_1_lot": (
            finite("ask_lots")
            and metrics["ask_lots"] >= 1
        ),
    }

    return [name for name, passed in checks.items() if not passed]


def main_rule_text():
    return MAIN_RULES + (
        f" or {MAIN_ALSO_IF}" if MAIN_ALSO_IF else ""
    )


def rule_legend():
    return "Rules: " + " | ".join(
        f"{k}={RULE_TEXT[k]}" for k in RULE_ORDER
    )


def trade_age_seconds(c, t):
    """Age relative to the quote timestamp, or check time."""
    ms = num(c.get("last_trade_time"))
    if ms <= 0:
        return None

    try:
        traded = datetime.fromtimestamp(ms / 1000, IST)
    except (ValueError, OverflowError):
        return None

    ref = t

    if c.get("quote_time"):
        try:
            ref = parse(str(c["quote_time"]))
        except (ValueError, TypeError):
            ref = t

    return (ref - traded).total_seconds()


def rule_metrics(c, gap, t):
    m = c["market"]
    lot = c["lot"]
    greeks = c.get("greeks") or {}

    oi = num(m.get("oi"))
    volume = num(m.get("volume"))
    bid = num(m.get("bid_price"))
    ask = num(m.get("ask_price"))
    mid = (bid + ask) / 2

    bid_lots = num(m.get("bid_qty")) / lot
    ask_lots = num(m.get("ask_qty")) / lot

    return {
        "gap_pct": gap * 100,
        "minute": t.hour * 60 + t.minute,
        "oi_lots": oi / lot,
        "volume_lots": volume / lot,
        "vol_oi": volume / oi if oi > 0 else None,
        "bid_lots": bid_lots,
        "ask_lots": ask_lots,
        "depth_lots": min(bid_lots, ask_lots),
        "trade_age_s": trade_age_seconds(c, t),
        "gamma": num(greeks.get("gamma")),
        "delta": num(greeks.get("delta")),
        "iv": num(greeks.get("iv")),
        "theta": num(greeks.get("theta")),
        "vega": num(greeks.get("vega")),
        "spread_pct": (
            (ask - bid) / mid * 100 if mid > 0 else None
        ),
    }


def evaluate_rules(metrics):
    """Historical flags plus the complete Q filter."""
    gap = metrics["gap_pct"] / 100
    age = metrics["trade_age_s"]
    ratio = metrics["vol_oi"]
    depth = metrics["depth_lots"] >= MIN_DEPTH_LOTS

    flags = {
        "T": (
            gap < BASE_MAX_GAP
            or metrics["oi_lots"] >= EXTENDED_MIN_OI_LOTS
        ),
        "D": depth,
        "O": metrics["oi_lots"] >= RULE_MIN_OI_LOTS,
        "A": age is not None and -5 <= age <= RULE_MAX_TRADE_AGE,
        "V": ratio is not None and ratio <= RULE_MAX_VOL_OI,
        "G": metrics["gamma"] >= RULE_MIN_GAMMA,
        "E": abs(metrics["delta"]) >= RULE_MIN_ABS_DELTA,
        "I": 0 < metrics["iv"] <= RULE_MAX_IV,
        "P": RULE_P_MIN_GAP <= gap < BASE_MAX_GAP and depth,
        "S": (
            RULE_S_MIN_GAP <= gap <= RULE_S_MAX_GAP
            and metrics["minute"] < RULE_S_CUTOFF_MIN
        ),
        "Q": not quality_failures(metrics),
    }

    return {k: flags[k] for k in RULE_ORDER}


def rule_header(flags):
    via_rules = all(flags[k] for k in MAIN_RULES)
    via_also = bool(MAIN_ALSO_IF) and all(
        flags[k] for k in MAIN_ALSO_IF
    )

    channel = "MAIN" if via_rules or via_also else "SILENT"
    label = (
        "MAIN"
        if via_rules or not via_also
        else f"MAIN via {MAIN_ALSO_IF}"
    )

    if channel == "SILENT":
        label = "SILENT"

    marks = " ".join(
        f'{k}{"✓" if flags[k] else "✗"}'
        for k in RULE_ORDER
    )

    header = (
        f"{label} | {marks} | "
        f"{sum(flags.values())}/{len(RULE_ORDER)} rules passed"
    )
    return channel, header


def peer_iv(c, band):
    peers = [
        p
        for p in band
        if p["side"] == c["side"]
        and p["key"] != c["key"]
        and liquid(p)
        and 0 < num(p["greeks"].get("iv")) < 200
    ]

    below = sorted(
        [p for p in peers if p["strike"] < c["strike"]],
        key=lambda p: -p["strike"],
    )[:2]

    above = sorted(
        [p for p in peers if p["strike"] > c["strike"]],
        key=lambda p: p["strike"],
    )[:2]

    if not below or not above:
        return None

    def weighted(group):
        weights = [
            math.sqrt(num(p["market"]["oi"]))
            / max(
                num(p["market"]["ask_price"])
                - num(p["market"]["bid_price"]),
                0.01,
            )
            for p in group
        ]

        total = sum(weights)

        return (
            sum(
                weight * p["strike"]
                for weight, p in zip(weights, group)
            ) / total,
            sum(
                weight * num(p["greeks"]["iv"])
                for weight, p in zip(weights, group)
            ) / total,
        )

    kl, vl = weighted(below)
    kh, vh = weighted(above)

    return (
        vl + (vh - vl) * (c["strike"] - kl) / (kh - kl)
    ) / 100


def bs(s, k, years, vol, side, r=RISK_RATE, q=0.0):
    if min(s, k, years, vol) <= 0:
        return max(0.0, s - k if side == "CE" else k - s)

    def normal_cdf(x):
        return (1 + math.erf(x / math.sqrt(2))) / 2

    d1 = (
        math.log(s / k)
        + (r - q + vol * vol / 2) * years
    ) / (vol * math.sqrt(years))

    d2 = d1 - vol * math.sqrt(years)

    if side == "CE":
        return (
            s * math.exp(-q * years) * normal_cdf(d1)
            - k * math.exp(-r * years) * normal_cdf(d2)
        )

    return (
        k * math.exp(-r * years) * normal_cdf(-d2)
        - s * math.exp(-q * years) * normal_cdf(-d1)
    )


def candidate(c, band, spot, t):
    if not liquid(c):
        return None, "liquidity"

    expiry = close_at(
        datetime.fromisoformat(c["expiry"]).replace(tzinfo=IST)
    )

    if (expiry - t).total_seconds() < 1800:
        return None, "near_expiry"

    vol = peer_iv(c, band)
    if vol is None:
        return None, "insufficient_peer_iv"

    years = (expiry - t).total_seconds() / (365 * 86400)

    div = num(
        DIVIDEND_YIELDS.get(c["symbol"]),
        num(os.getenv("DIVIDEND_YIELD", "0")),
    )

    fair = bs(
        spot,
        c["strike"],
        years,
        vol,
        c["side"],
        q=div,
    )

    if fair < MIN_PREMIUM:
        return None, "tiny_model_value"

    m = c["market"]
    ltp = num(m["ltp"])
    gap = (fair - ltp) / fair

    if fair <= ltp:
        return None, "sell_direction_excluded"

    executable = (
        num(m["ask_price"])
        if gap > 0
        else num(m["bid_price"])
    )
    executable_gap = (fair - executable) / fair

    if MAIN_RULES == "Q" and not MAIN_ALSO_IF:
        executable_too_small = fair + 1e-10 < executable * 1.05
    else:
        executable_too_small = abs(executable_gap) < GAP

    if (
        abs(gap) < GAP
        or gap * executable_gap <= 0
        or executable_too_small
    ):
        return None, "gap_below_threshold"

    if gap >= MAX_GAP:
        return None, "gap_above_band"

    metrics = rule_metrics(c, gap, t)
    metrics["ask_upside_pct"] = (
        (fair - executable) / executable * 100
        if executable > 0
        else None
    )

    flags = evaluate_rules(metrics)
    channel, header = rule_header(flags)

    return {
        "target": round(fair, 4),
        "entry": ltp,
        "direction": "UP" if gap > 0 else "DOWN",
        "gap_pct": gap * 100,
        "executable_gap_pct": executable_gap * 100,
        "peer_iv": vol * 100,
        "entry_oi_lots": num(m.get("oi")) / c["lot"],
        "gap_tier": (
            "EXTENDED_OI" if gap >= BASE_MAX_GAP else "BASE"
        ),
        "rules": flags,
        "rules_passed": "".join(k for k in RULE_ORDER if flags[k]),
        "rules_failed": "".join(
            k for k in RULE_ORDER if not flags[k]
        ),
        "metrics": metrics,
        "channel": channel,
        "header": header,
        "quality_failures": quality_failures(metrics),
        "filter_settings": dict(QUALITY_SETTINGS),
        "entry_ask": executable,
        "fixed_5pct_target": round(executable * 1.05, 6),
        "spot": spot,
        "years": years,
        "r": RISK_RATE,
        "q": div,
    }, None


def obs_value(c, band, spot, t):
    """Compute observation values without the candidate gap gates."""
    try:
        expiry = close_at(
            datetime.fromisoformat(c["expiry"]).replace(tzinfo=IST)
        )
        years = (expiry - t).total_seconds() / (365 * 86400)
        vol = peer_iv(c, band)

        if years <= 0 or vol is None:
            return {}

        div = num(
            DIVIDEND_YIELDS.get(c["symbol"]),
            num(os.getenv("DIVIDEND_YIELD", "0")),
        )

        fair = bs(
            spot,
            c["strike"],
            years,
            vol,
            c["side"],
            q=div,
        )

        m = c["market"]
        ltp = num(m.get("ltp"))

        if fair <= 0:
            return {}

        gap = (fair - ltp) / fair
        executable = (
            num(m.get("ask_price"))
            if gap > 0
            else num(m.get("bid_price"))
        )

        return {
            "peer_iv": round(vol * 100, 3),
            "fair": round(fair, 4),
            "gap_pct": round(gap * 100, 3),
            "xgap_pct": round(
                (fair - executable) / fair * 100,
                3,
            ),
        }

    except (
        ValueError,
        ZeroDivisionError,
        OverflowError,
        KeyError,
        TypeError,
    ):
        return {}


def snapshot_row(
    c,
    band,
    spot,
    t,
    reason,
    value=None,
    source="quote",
):
    m = c.get("market") or {}
    lot = c["lot"]
    bid = num(m.get("bid_price"))
    ask = num(m.get("ask_price"))
    mid = (bid + ask) / 2

    row = {
        "t": iso(t),
        "sym": c["symbol"],
        "exp": c["expiry"],
        "k": c["strike"],
        "side": c["side"],
        "lot": lot,
        "spot": spot,
        "src": source,
        "ltp": num(m.get("ltp")),
        "bid": bid,
        "ask": ask,
        "bid_lots": round(num(m.get("bid_qty")) / lot, 2),
        "ask_lots": round(num(m.get("ask_qty")) / lot, 2),
        "oi_lots": round(num(m.get("oi")) / lot, 1),
        "vol_lots": round(num(m.get("volume")) / lot, 1),
        "spread_pct": (
            round((ask - bid) / mid * 100, 3)
            if mid > 0
            else None
        ),
        "qt": c.get("quote_time"),
        "ltt": c.get("last_trade_time"),
        "age_s": (
            trade_age_seconds(c, t)
            if c.get("last_trade_time")
            else None
        ),
        "greeks": c.get("greeks") or {},
        "reason": reason,
    }

    row.update(obs_value(c, band, spot, t))
    row.update(
        policy=FILTER_POLICY,
        instrument_key=c.get("key"),
        underlying_key=c.get("underlying"),
    )

    if value:
        row.update(
            {
                "chan": value.get("channel"),
                "passed": value.get("rules_passed"),
                "failed": value.get("rules_failed"),
                "quality_failures": value.get("quality_failures"),
                "metrics": value.get("metrics"),
                "entry_ask": value.get("entry_ask"),
                "fixed_5pct_target": value.get("fixed_5pct_target"),
            }
        )
    else:
        fair = row.get("fair")
        gap = (
            (fair - num(m.get("ltp"))) / fair
            if fair
            else 0
        )
        metrics = rule_metrics(c, gap, t)
        metrics["ask_upside_pct"] = (
            (fair - ask) / ask * 100
            if fair and ask > 0
            else None
        )
        row["quality_failures"] = quality_failures(metrics)

    return row


class APIError(RuntimeError):
    pass


class AuthError(APIError):
    pass


class API:
    def __init__(self):
        token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()

        if not token:
            raise AuthError("Missing UPSTOX_ACCESS_TOKEN secret")

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": "Bearer " + token,
                "Accept": "application/json",
            }
        )
        self.last_call = 0.0
        self.interval = max(
            1.2,
            num(os.getenv("REQUEST_INTERVAL", "1.2"), 1.2),
        )

    def get(self, path, params=None):
        for attempt in range(3):
            wait = self.interval - (
                time.monotonic() - self.last_call
            )

            if wait > 0:
                time.sleep(wait)

            self.last_call = time.monotonic()

            try:
                response = self.session.get(
                    "https://api.upstox.com" + path,
                    params=params,
                    timeout=20,
                )
            except requests.RequestException:
                if attempt == 2:
                    raise APIError("Upstox connection failed") from None
                time.sleep(2 ** attempt)
                continue

            if response.status_code in (401, 403):
                raise AuthError(
                    f"Upstox HTTP {response.status_code}; "
                    "check access token/data entitlement"
                )

            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(
                    min(
                        30,
                        max(
                            2 ** attempt,
                            num(
                                response.headers.get("Retry-After"),
                                1,
                            ),
                        ),
                    )
                )
                continue

            if response.status_code != 200:
                raise APIError(
                    f"Upstox HTTP {response.status_code}"
                )

            try:
                body = response.json()
            except ValueError:
                raise APIError(
                    "Invalid Upstox JSON response"
                ) from None

            if body.get("status") != "success":
                raise APIError("Upstox unsuccessful response")

            return body["data"]

        raise APIError("Upstox retry limit")

    def master(self):
        # Broker authorization is never attached to this public request.
        try:
            response = requests.get(MASTER_URL, timeout=45)
            response.raise_for_status()

            raw = (
                gzip.decompress(response.content)
                if response.content[:2] == b"\x1f\x8b"
                else response.content
            )

            rows = json.loads(raw)
            if not isinstance(rows, list):
                raise ValueError()

            return rows

        except (
            requests.RequestException,
            ValueError,
            OSError,
        ):
            raise APIError(
                "Instrument master download failed"
            ) from None

    def chain(self, underlying, metadata):
        rows = self.get(
            "/v2/option/chain",
            {
                "instrument_key": underlying["underlying"],
                "expiry_date": underlying["expiry"],
            },
        )

        result = []

        for row in rows:
            for field, side in (
                ("call_options", "CE"),
                ("put_options", "PE"),
            ):
                obj = row.get(field) or {}
                key = obj.get("instrument_key")
                meta = metadata.get(key)

                if (
                    not meta
                    or meta["underlying"] != underlying["underlying"]
                    or meta["expiry"] != underlying["expiry"]
                ):
                    continue

                result.append(
                    {
                        "key": key,
                        "symbol": underlying["symbol"],
                        "underlying": underlying["underlying"],
                        "expiry": underlying["expiry"],
                        "strike": num(row["strike_price"]),
                        "side": side,
                        "lot": meta["lot"],
                        "spot": num(row.get("underlying_spot_price")),
                        "market": obj.get("market_data") or {},
                        "greeks": obj.get("option_greeks") or {},
                    }
                )

        return result

    def quotes(self, keys):
        result = {}
        keys = list(dict.fromkeys(keys))

        for offset in range(0, len(keys), 100):
            data = self.get(
                "/v2/market-quote/quotes",
                {
                    "instrument_key": ",".join(
                        keys[offset:offset + 100]
                    )
                },
            )

            for q in data.values():
                if q.get("instrument_token"):
                    result[q["instrument_token"]] = q

        return result

    def candles(self, key, date):
        encoded = quote(key, safe="")

        if date == now().date().isoformat():
            path = (
                f"/v3/historical-candle/intraday/"
                f"{encoded}/minutes/1"
            )
        else:
            path = (
                f"/v3/historical-candle/{encoded}/"
                f"minutes/1/{date}/{date}"
            )

        return sorted(
            self.get(path).get("candles", []),
            key=lambda bar: bar[0],
        )


class Scanner:
    def __init__(self, api, directory=STATE_DIR):
        self.api = api
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "reports").mkdir(exist_ok=True)

        self.path = self.directory / "state.json"

        if self.path.exists():
            self.state = json.loads(self.path.read_text())
        else:
            self.state = {
                "version": 1,
                "kind": "stock_s3",
                "days": {},
                "watches": [],
                "outbox": [],
            }

        if self.state.get("kind") != "stock_s3":
            raise RuntimeError(
                "Wrong state folder: use separate s3-stock-state branch"
            )

        self.universe = []
        self.metadata = {}
        self.master_day = None
        self.last_sync = 0.0
        self.last_quotes = float("-inf")

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                self.state,
                indent=2,
                allow_nan=False,
            )
        )
        temporary.replace(self.path)

    def sync(self, force=False):
        self.save()

        if (
            os.getenv("GIT_STATE") != "true"
            or (
                not force
                and time.monotonic() - self.last_sync < 300
            )
        ):
            return

        def git(*args):
            result = subprocess.run(
                ["git", "-C", str(self.directory), *args],
                capture_output=True,
                text=True,
            )
            if result.returncode:
                raise RuntimeError(
                    "Cannot checkpoint state; "
                    "check Actions write permission"
                )
            return result.stdout

        observation_paths = (
            ["observations"]
            if any((self.directory / "observations").glob("*"))
            else []
        )

        git("add", "state.json", "reports", *observation_paths)

        if git("status", "--porcelain").strip():
            git(
                "commit",
                "-m",
                "Save S3 stock research state [skip ci]",
            )

        git("push", "origin", "HEAD:s3-stock-state")
        self.last_sync = time.monotonic()

    def emit(self, id, text):
        lines = text.splitlines()
        chunks = []
        chunk = ""

        for line in lines:
            if len(chunk) + len(line) + 1 > 3500:
                chunks.append(chunk)
                chunk = ""
            chunk += line + "\n"

        if chunk:
            chunks.append(chunk)

        for index, part in enumerate(chunks):
            event_id = f"{id}:{index}"
            if not any(
                message["id"] == event_id
                for message in self.state["outbox"]
            ):
                self.state["outbox"].append(
                    {
                        "id": event_id,
                        "text": part,
                        "sent": False,
                    }
                )

        self.save()

    def flush(self):
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat = os.getenv("TELEGRAM_CHAT_ID")

        if not token or not chat:
            raise RuntimeError("Missing Telegram secrets")

        sent = 0

        for message in self.state["outbox"]:
            if message["sent"]:
                continue

            try:
                response = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": chat,
                        "text": message["text"],
                    },
                    timeout=20,
                )

                data = response.json()

                if not response.ok or not data.get("ok"):
                    if response.status_code == 429:
                        time.sleep(
                            min(
                                30,
                                num(
                                    data.get("parameters", {}).get(
                                        "retry_after"
                                    ),
                                    5,
                                ),
                            )
                        )
                        return

                    raise RuntimeError(
                        f"Telegram HTTP {response.status_code}; "
                        "pending message retained"
                    )

            except (requests.RequestException, ValueError):
                raise RuntimeError(
                    "Telegram connection failed; "
                    "pending message retained"
                ) from None

            message["sent"] = True
            self.save()
            sent += 1

            time.sleep(
                max(
                    3.1,
                    num(
                        os.getenv("TELEGRAM_SEND_INTERVAL", "3.1"),
                        3.1,
                    ),
                )
            )

            if sent >= 5:
                break

    def prepare(self, t):
        date = t.date().isoformat()

        if self.master_day != date:
            self.universe, self.metadata = universe_from_master(
                self.api.master(),
                date,
            )

            if not self.universe:
                raise APIError(
                    "No active NSE stock options found "
                    "in instrument master"
                )

            self.master_day = date

        day = self.state["days"].setdefault(
            date,
            {
                "date": date,
                "cursor": 0,
                "sweeps": 0,
                "symbols": {},
                "counts": {},
                "last_error": None,
            },
        )

        day["universe_count"] = len(self.universe)
        day["filter_policy"] = FILTER_POLICY
        day["filter_settings"] = dict(QUALITY_SETTINGS)

        return day

    def choose_buy_atm(self, verified, chain, spot, t, day):
        atm = atm_strike(chain, spot)
        options = []
        rows = {}

        def mark(c, action):
            if c["key"] in rows:
                rows[c["key"]]["action"] = action

        for c in verified:
            if c["strike"] != atm:
                self.count(day, "non_atm_excluded")

                if OBSERVE_ALL and OBSERVE_BAND:
                    rows[c["key"]] = snapshot_row(
                        c, verified, spot, t, "non_atm"
                    )
                continue

            value, reason = candidate(c, verified, spot, t)

            if OBSERVE_ALL:
                rows[c["key"]] = snapshot_row(
                    c,
                    verified,
                    spot,
                    t,
                    reason or "CANDIDATE",
                    value,
                )

            if reason:
                self.count(day, reason)
                continue

            prior = [
                watch
                for watch in self.state["watches"]
                if watch["day"] == day["date"]
                and watch["key"] == c["key"]
            ]

            # A prior silent observation can later become a MAIN alert.
            if (
                any(
                    watch.get("channel") != "SILENT"
                    for watch in prior
                )
                or (prior and value["channel"] == "SILENT")
            ):
                mark(c, "DUPLICATE_CONTRACT")
                continue

            options.append(
                (
                    c,
                    {
                        **value,
                        "policy": FILTER_POLICY,
                        "atm_at_entry": atm,
                    },
                )
            )

        def flush_rows():
            if rows:
                self.observe(day, list(rows.values()))

        if not options:
            flush_rows()
            return None

        underlying = options[0][0]["underlying"]

        def blocked(channel):
            for watch in self.state["watches"]:
                if (
                    watch["day"] != day["date"]
                    or watch["underlying"] != underlying
                    or watch["status"] != "OPEN"
                ):
                    continue

                if channel == "MAIN" and (
                    watch.get("policy") in LEGACY_POLICIES
                    or (
                        watch.get("policy") == FILTER_POLICY
                        and watch.get("channel") == "MAIN"
                    )
                ):
                    return True

                if (
                    channel == "SILENT"
                    and watch.get("policy") == FILTER_POLICY
                    and watch.get("channel") == "SILENT"
                ):
                    return True

            return False

        options.sort(
            key=lambda item: (
                item[1]["channel"] != "MAIN",
                -len(item[1]["rules_passed"]),
                -item[1]["executable_gap_pct"],
                item[0]["side"],
            )
        )

        chosen = next(
            (
                item
                for item in options
                if not blocked(item[1]["channel"])
            ),
            None,
        )

        if chosen is None:
            for c, _ in options:
                mark(c, "BLOCKED_BY_OPEN_WATCH")

            flush_rows()
            self.count(day, "existing_open_stock_watch")
            return None

        for c, _ in options:
            mark(
                c,
                "SELECTED"
                if c is chosen[0]
                else "OTHER_SIDE_SUPPRESSED",
            )

        flush_rows()

        if len(options) > 1:
            self.count(
                day,
                "other_atm_side_suppressed",
                len(options) - 1,
            )

        return chosen

    def count(self, day, reason, n=1):
        day["counts"][reason] = day["counts"].get(reason, 0) + n

    def observe(self, day, rows):
        """Append accepted and rejected scan observations."""
        if not OBSERVE_ALL or not rows:
            return

        lines = []

        for row in rows:
            try:
                lines.append(
                    json.dumps(
                        row,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                )
            except (TypeError, ValueError):
                self.count(day, "observation_write_error")

        if not lines:
            return

        try:
            folder = self.directory / "observations"
            folder.mkdir(exist_ok=True)

            with open(
                folder / f'{day["date"]}.jsonl',
                "a",
                encoding="utf-8",
            ) as stream:
                stream.write("\n".join(lines) + "\n")

            day["observed_rows"] = (
                day.get("observed_rows", 0) + len(lines)
            )

        except OSError as error:
            day["last_error"] = f"observation log: {error}"

    def record_event(self, day, event):
        """Append compressed raw evidence for the next review."""
        folder = self.directory / "observations"
        folder.mkdir(exist_ok=True)

        try:
            payload = json.dumps(
                {"policy": FILTER_POLICY, **event},
                separators=(",", ":"),
                allow_nan=False,
            )

            with gzip.open(
                folder / f'{day["date"]}.events.jsonl.gz',
                "at",
                encoding="utf-8",
            ) as stream:
                stream.write(payload + "\n")

            day["recorded_events"] = (
                day.get("recorded_events", 0) + 1
            )

        except (OSError, TypeError, ValueError) as error:
            self.count(day, "event_write_error")
            day["last_error"] = (
                f"event log: {type(error).__name__}"
            )

    def observation_summary(self, day):
        if not OBSERVE_ALL:
            return [
                "OBSERVATION LOG | disabled (OBSERVE_ALL=false)"
            ]

        path = (
            self.directory
            / "observations"
            / f'{day["date"]}.jsonl'
        )

        if not path.exists():
            return [
                "OBSERVATION LOG | no snapshots were saved today"
            ]

        rows = []

        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue

        by_contract = {}

        for row in rows:
            key = (
                row["sym"],
                row["exp"],
                row["k"],
                row["side"],
            )
            by_contract.setdefault(key, []).append(row)

        lines = [
            (
                f"OBSERVATION LOG | {len(rows)} snapshots of "
                f"{len(by_contract)} ATM contracts in "
                f'{len({row["sym"] for row in rows})} stocks, '
                "each with bid, ask, depth, OI, volume, last trade, "
                "IV, greeks and model value, saved to "
                f'observations/{day["date"]}.jsonl.gz '
                "(every contract scanned, whether or not "
                "an alert was sent)"
            ),
            "Snapshots by result: " + ", ".join(
                f"{key} {count}"
                for key, count in Counter(
                    row["reason"] for row in rows
                ).most_common()
            ),
        ]

        funnel = Counter()
        later = Counter()

        for sequence in by_contract.values():
            sequence.sort(key=lambda row: row["t"])

            for index, row in enumerate(sequence):
                gap = row.get("gap_pct")
                fair = row.get("fair")

                if (
                    gap is None
                    or not fair
                    or gap < OBS_MIN_GAP * 100
                ):
                    continue

                if row["reason"] != "CANDIDATE":
                    category = row["reason"]
                elif row.get("action") == "SELECTED":
                    category = (
                        "MAIN alert"
                        if row.get("chan") == "MAIN"
                        else "silent record"
                    )
                else:
                    category = (
                        "candidate not recorded ("
                        + str(row.get("action", "unknown")).lower()
                        + ")"
                    )

                funnel[category] += 1

                if any(
                    num(later_row.get("ltp")) >= fair
                    for later_row in sequence[index + 1:]
                ):
                    later[category] += 1

                break

        if funnel:
            lines += [
                "",
                (
                    "FILTER FUNNEL | first sighting of each "
                    "contract valued at least "
                    f"{OBS_MIN_GAP * 100:g}% above its price. "
                    '"Later traded at/above value" is checked '
                    "only at scan times, so it understates real touches"
                ),
            ]

            lines += [
                (
                    f"{category}: {count} contracts, "
                    f"{later[category]} later traded at/above "
                    f"the model value "
                    f"({later[category] / count * 100:.0f}%)"
                )
                for category, count in funnel.most_common(14)
            ]

        return lines

    def compress_observations(self, day):
        for suffix in (".jsonl", ".events.jsonl"):
            path = (
                self.directory
                / "observations"
                / f'{day["date"]}{suffix}'
            )

            if not path.exists():
                continue

            try:
                with open(path, "rb") as source:
                    with gzip.open(str(path) + ".gz", "wb") as dest:
                        dest.write(source.read())

                path.unlink()

            except OSError as error:
                day["last_error"] = (
                    f"observation compress: {error}"
                )

    def add_watch(self, c, value, t, day):
        channel = value.get("channel", "MAIN")

        identity = (
            f'{day["date"]}:{c["key"]}:{channel}'
        ).encode()

        watch_id = hashlib.sha256(identity).hexdigest()[:16]

        if any(
            watch["id"] == watch_id
            for watch in self.state["watches"]
        ):
            return None

        watch = {
            **c,
            **value,
            "channel": channel,
            "id": watch_id,
            "day": day["date"],
            "entered": iso(t),
            "status": "OPEN",
            "last_price": value["entry"],
            "last_observed_at": iso(t),
            "checked_until": iso(t),
            "last_candle_attempt": None,
            "hit_time": None,
            "hit_price": None,
            "hit_evidence": None,
            "hit_candle_close": None,
        }

        self.state["watches"].append(watch)

        self.record_event(
            day,
            {
                "type": "watch_entry",
                "t": iso(t),
                "watch": watch,
            },
        )

        if channel == "SILENT":
            return watch

        if value.get("policy") == FILTER_POLICY:
            label = "BUY-ONLY ATM WATCH"
        elif value["direction"] == "UP":
            label = "BELOW MODEL VALUE"
        else:
            label = "ABOVE MODEL VALUE"

        greeks = c["greeks"]
        market = c["market"]
        header = value.get("header")

        text = (
            f'S3 STOCK OPTIONS | {c["symbol"]} '
            f'{c["strike"]:g} {c["side"]}\n'
        )

        if header:
            text += header + "\n"

        text += (
            f'{label} | expiry {c["expiry"]}\n'
            f'LTP ₹{value["entry"]:.2f} | '
            f'fixed model target ₹{value["target"]:.2f}\n'
            f'Gap {abs(value["gap_pct"]):.1f}% of model value | '
            f'direction {value["direction"]}\n'
            f'Bid ₹{num(market["bid_price"]):.2f} | '
            f'ask ₹{num(market["ask_price"]):.2f}\n'
        )

        if value.get("entry_ask"):
            text += (
                f'Entry ask reference ₹{value["entry_ask"]:.2f} | '
                f'separate +5% study target '
                f'₹{value["fixed_5pct_target"]:.2f}\n'
            )

        text += (
            "Best-level depth: "
            f'bid {num(market["bid_qty"]) / c["lot"]:g} lots | '
            f'ask {num(market["ask_qty"]) / c["lot"]:g} lots\n'
            f'IV {num(greeks.get("iv")):.2f}% | '
            f'peer IV {value["peer_iv"]:.2f}%\n'
            f'Delta {num(greeks.get("delta")):.3f} | '
            f'Gamma {num(greeks.get("gamma")):.5f}\n'
            f'Theta {num(greeks.get("theta")):.2f} | '
            f'Vega {num(greeks.get("vega")):.2f}\n'
            f'OI {num(market["oi"]):g} | '
            f'volume {num(market["volume"]):g} | lot {c["lot"]}\n'
        )

        if value.get("metrics"):
            text += self.value_line(value) + "\n"

        text += (
            f"{t:%H:%M:%S} IST | ID {watch_id}\n"
            "Valuation watch only; no buy/sell execution or stop-loss."
        )

        self.emit("entry-" + watch_id, text)
        return watch

    @staticmethod
    def value_line(value):
        metrics = value["metrics"]
        age = metrics.get("trade_age_s")
        ratio = metrics.get("vol_oi")

        age_text = "n/a" if age is None else f"{age:.1f}s"
        ratio_text = "n/a" if ratio is None else f"{ratio:.2f}"

        return (
            f'Rule values: OI {metrics["oi_lots"]:g} lots | '
            f"last trade {age_text} ago | "
            f"vol/OI {ratio_text} | "
            f'depth {metrics["depth_lots"]:g} lots | '
            f'gamma {metrics["gamma"]:.5f}'
        )

    def finish(self, watch, t, price, evidence, candle_close=None):
        if watch["status"] != "OPEN":
            return

        watch.update(
            status="VALUE_REACHED",
            hit_time=iso(t),
            hit_price=price,
            hit_evidence=evidence,
            hit_candle_close=candle_close,
        )

        day = self.state["days"].get(watch["day"])

        if day:
            self.record_event(
                day,
                {
                    "type": "model_target_touch",
                    "watch_id": watch["id"],
                    "t": iso(t),
                    "price": price,
                    "evidence": evidence,
                    "candle_close": candle_close,
                },
            )

        if watch.get("channel") == "SILENT":
            return

        note = (
            f"\nTouching candle close: ₹{candle_close:.2f} "
            "(not the touch price)"
            if candle_close is not None
            else ""
        )

        text = (
            f'S3 VALUE REACHED | {watch["symbol"]} '
            f'{watch["strike"]:g} {watch["side"]}\n'
        )

        if watch.get("header"):
            text += watch["header"] + "\n"

        text += (
            f'Direction {watch["direction"]} | '
            f'reference ₹{watch["entry"]:.2f}\n'
            f'Fixed target ₹{watch["target"]:.2f}\n'
            f"Touch/reference price ₹{price:.2f} | "
            f"{t:%Y-%m-%d %H:%M:%S} IST\n"
            f"Evidence: {evidence}{note}\n"
            f'ID {watch["id"]}\n'
            "Model-price touch; execution and costs are not verified."
        )

        self.emit("hit-" + watch["id"], text)

    def scan_one(self, day, t):
        index = day["cursor"] % len(self.universe)
        underlying = self.universe[index]
        day.setdefault("sweep_started", iso(t))

        try:
            chain = self.api.chain(underlying, self.metadata)
            checked = now()

            day["symbols"][underlying["symbol"]] = {
                "checked_at": iso(checked),
                "status": "chain_received",
            }

            self.count(day, "chains_received")

            if not chain:
                self.count(day, "empty_chain")
                return

            spot0 = chain[0]["spot"]
            band = select_band(chain, spot0)
            atm0 = atm_strike(chain, spot0)

            def drop(reason):
                self.count(day, reason)

                if (
                    OBSERVE_ALL
                    and dt_time(9, 20)
                    <= checked.time()
                    <= alert_cutoff(checked)
                ):
                    self.observe(
                        day,
                        [
                            snapshot_row(
                                c,
                                band,
                                spot0,
                                checked,
                                reason,
                                source="chain",
                            )
                            for c in chain
                            if c["strike"] == atm0
                        ],
                    )

            if not any(liquid(c) for c in band):
                drop("no_liquid_contracts")
                return

            quotes = self.api.quotes(
                [underlying["underlying"]]
                + [c["key"] for c in band]
            )
            checked = now()

            self.record_event(
                day,
                {
                    "type": "scan_band",
                    "t": iso(checked),
                    "symbol": underlying["symbol"],
                    "underlying": underlying["underlying"],
                    "spot_chain": spot0,
                    "chain_band": band,
                    "quotes": quotes,
                },
            )

            if not (
                dt_time(9, 20)
                <= checked.time()
                <= alert_cutoff(checked)
            ):
                return

            underlying_quote = quotes.get(
                underlying["underlying"], {}
            )

            if not fresh(underlying_quote, checked):
                drop("stale_underlying")
                return

            spot = num(underlying_quote.get("last_price"))

            if spot <= 0:
                drop("invalid_spot")
                return

            desired = select_band(chain, spot)

            if (
                {c["key"] for c in desired}
                != {c["key"] for c in band}
            ):
                drop("atm_shift_during_request")
                return

            verified = []

            for c in band:
                q = quotes.get(c["key"], {})

                if not fresh(q, checked):
                    self.count(
                        day, "stale_option_or_missing_quote"
                    )
                    continue

                updated = quote_contract(c, q)

                if updated:
                    verified.append(updated)

            if OBSERVE_ALL:
                verified_keys = {c["key"] for c in verified}
                atm = atm_strike(chain, spot)

                self.observe(
                    day,
                    [
                        snapshot_row(
                            c,
                            band,
                            spot,
                            checked,
                            "stale_option_or_missing_quote",
                            source="chain",
                        )
                        for c in chain
                        if c["strike"] == atm
                        and c["key"] not in verified_keys
                    ],
                )

            if (checked - t).total_seconds() > 60:
                self.count(day, "chain_quote_delay")

                if OBSERVE_ALL:
                    atm = atm_strike(chain, spot)
                    self.observe(
                        day,
                        [
                            snapshot_row(
                                c,
                                verified,
                                spot,
                                checked,
                                "chain_quote_delay",
                            )
                            for c in verified
                            if c["strike"] == atm
                        ],
                    )

                return

            choice = self.choose_buy_atm(
                verified,
                chain,
                spot,
                checked,
                day,
            )

            if choice:
                c, value = choice

                if self.add_watch(c, value, checked, day):
                    self.count(
                        day,
                        "new_alerts"
                        if value["channel"] == "MAIN"
                        else "new_silent_records",
                    )

        except AuthError:
            raise

        except APIError as error:
            day["symbols"][underlying["symbol"]] = {
                "checked_at": iso(now()),
                "status": "error",
                "reason": str(error),
            }
            self.count(day, "symbol_errors")
            day["last_error"] = (
                f'{underlying["symbol"]}: {error}'
            )

        finally:
            day["cursor"] = (
                index + 1
            ) % len(self.universe)

            if day["cursor"] == 0:
                day["sweeps"] += 1
                day["last_sweep_finished"] = iso(now())
                day["last_sweep_seconds"] = round(
                    (
                        now() - parse(day["sweep_started"])
                    ).total_seconds()
                )
                day["sweep_started"] = iso(now())

            self.save()

    def monitor_quotes(self, t):
        if time.monotonic() - self.last_quotes < 60:
            return

        watches = [
            watch
            for watch in self.state["watches"]
            if watch["day"] == t.date().isoformat()
            and (
                watch["status"] == "OPEN"
                or watch.get("policy") == FILTER_POLICY
            )
        ]

        if watches:
            quotes = self.api.quotes(
                list(
                    dict.fromkeys(
                        watch["key"] for watch in watches
                    )
                )
            )
            observed = now()

            if (
                observed.date() != t.date()
                or observed.time() > stock_close(observed)
            ):
                return

            for watch in watches:
                q = quotes.get(watch["key"], {})
                day = self.state["days"].get(watch["day"])
                is_fresh = fresh(q, observed)

                if day:
                    self.record_event(
                        day,
                        {
                            "type": "monitor_quote",
                            "t": iso(observed),
                            "watch_id": watch["id"],
                            "key": watch["key"],
                            "fresh_trade": is_fresh,
                            "quote": q,
                        },
                    )

                if not is_fresh:
                    continue

                ltp = num(q.get("last_price"))
                if ltp <= 0:
                    continue

                ask_entry = num(watch.get("entry_ask"))
                study_target = num(
                    watch.get("fixed_5pct_target")
                )

                if ask_entry > 0:
                    gain = (ltp / ask_entry - 1) * 100

                    watch["sampled_ltp_max_return_pct"] = max(
                        gain,
                        watch.get(
                            "sampled_ltp_max_return_pct", gain
                        ),
                    )
                    watch["sampled_ltp_min_return_pct"] = min(
                        gain,
                        watch.get(
                            "sampled_ltp_min_return_pct", gain
                        ),
                    )

                if (
                    study_target > 0
                    and ltp >= study_target
                    and not watch.get("fixed_5pct_ltp_touch")
                ):
                    watch["fixed_5pct_ltp_touch"] = {
                        "t": iso(observed),
                        "price": ltp,
                        "evidence": "fresh polled LTP",
                    }

                if watch["status"] != "OPEN":
                    continue

                watch.update(
                    last_price=ltp,
                    last_observed_at=iso(observed),
                )

                hit = (
                    ltp >= watch["target"]
                    if watch["direction"] == "UP"
                    else ltp <= watch["target"]
                )

                if hit:
                    self.finish(
                        watch,
                        observed,
                        ltp,
                        "fresh polled LTP",
                    )

        self.last_quotes = time.monotonic()

    def candle_check(self, watch, t):
        watch["last_candle_attempt"] = iso(t)

        try:
            bars = self.api.candles(
                watch["key"], watch["day"]
            )
        except AuthError:
            raise
        except APIError:
            watch["candle_error"] = True
            return

        watch["candle_error"] = False

        day = self.state["days"].get(watch["day"])
        prior_saved = watch.get("recorded_candle_until")

        completed = [
            bar
            for bar in bars
            if parse(bar[0]).date().isoformat() == watch["day"]
            and parse(bar[0]) + timedelta(minutes=1) <= t
            and (
                not prior_saved
                or parse(bar[0]) > parse(prior_saved)
            )
        ]

        if day and completed:
            self.record_event(
                day,
                {
                    "type": "completed_candles",
                    "t": iso(t),
                    "watch_id": watch["id"],
                    "key": watch["key"],
                    "bars": completed,
                },
            )

            watch["recorded_candle_until"] = max(
                bar[0] for bar in completed
            )

        # Keep later evidence without rewriting a completed outcome.
        if watch["status"] != "OPEN":
            return

        start = (
            parse(watch["entered"]).replace(
                second=0, microsecond=0
            )
            + timedelta(minutes=1)
        )

        cursor = parse(watch["checked_until"])
        valid_minutes = set()

        for bar in bars:
            bar_time = parse(bar[0])

            if not (
                dt_time(9, 15)
                <= bar_time.time()
                < stock_close(bar_time)
            ):
                continue

            if len(bar) < 5 or any(
                num(value) <= 0 for value in bar[1:5]
            ):
                continue

            if (
                num(bar[2])
                < max(num(bar[1]), num(bar[3]), num(bar[4]))
                or num(bar[3])
                > min(num(bar[1]), num(bar[4]))
            ):
                continue

            valid_minutes.add(bar_time)

            if (
                bar_time.date().isoformat() != watch["day"]
                or bar_time < start
                or bar_time <= cursor
                or bar_time + timedelta(minutes=1) > t
            ):
                continue

            watch["checked_until"] = iso(bar_time)

            if (
                bar_time + timedelta(minutes=1)
                >= parse(watch["last_observed_at"])
            ):
                watch.update(
                    last_price=num(bar[4]),
                    last_observed_at=iso(
                        bar_time + timedelta(minutes=1)
                    ),
                )

            hit = (
                num(bar[2]) >= watch["target"]
                if watch["direction"] == "UP"
                else num(bar[3]) <= watch["target"]
            )

            if hit:
                self.finish(
                    watch,
                    bar_time + timedelta(minutes=1),
                    watch["target"],
                    "completed 1m candle high/low; time is candle end",
                    num(bar[4]),
                )
                return

        if (
            t >= close_at(t) + timedelta(minutes=2)
            or watch["day"] < t.date().isoformat()
        ):
            expected = start
            closing = close_at(start)

            while (
                expected < closing
                and expected in valid_minutes
            ):
                expected += timedelta(minutes=1)

            if expected == closing:
                watch["status"] = "NO_TOUCH_IN_MONITORED_BARS"

    def monitor_candles(self, t, budget=1):
        watches = [
            watch
            for watch in self.state["watches"]
            if (
                watch["status"] == "OPEN"
                or (
                    watch.get("policy") == FILTER_POLICY
                    and watch["day"] == t.date().isoformat()
                )
            )
            and (
                not watch["last_candle_attempt"]
                or (
                    t - parse(watch["last_candle_attempt"])
                ).total_seconds() >= 300
            )
        ]

        watches.sort(
            key=lambda watch: watch["last_candle_attempt"] or ""
        )

        for watch in watches[:budget]:
            self.candle_check(watch, t)

    @staticmethod
    def resolved(rows):
        return [
            watch
            for watch in rows
            if watch["status"] in (
                "VALUE_REACHED",
                "NO_TOUCH_IN_MONITORED_BARS",
            )
        ]

    @staticmethod
    def rate(rows):
        done = [
            watch
            for watch in rows
            if watch["status"] in (
                "VALUE_REACHED",
                "NO_TOUCH_IN_MONITORED_BARS",
            )
        ]

        hits = sum(
            watch["status"] == "VALUE_REACHED"
            for watch in done
        )

        misses = [
            (watch["last_price"] / watch["entry"] - 1) * 100
            for watch in done
            if watch["status"] != "VALUE_REACHED"
            and num(watch.get("entry")) > 0
        ]

        text = (
            f"{hits}/{len(done)} ({hits / len(done) * 100:.1f}%)"
            if done
            else "0/0 (n/a)"
        )

        if misses:
            text += (
                f", misses end {sum(misses) / len(misses):+.0f}% "
                "vs entry on average"
            )

        return text

    def rule_lab(self, rows, title):
        lab = [
            watch
            for watch in rows
            if watch.get("policy") == FILTER_POLICY
            and watch.get("rules")
        ]

        lines = [
            title,
            rule_legend(),
            (
                f"Records: {len(lab)}; "
                f"resolved: {len(self.resolved(lab))} "
                "(touch rate counts only resolved records)"
            ),
        ]

        if not self.resolved(lab):
            return lines + ["No resolved records yet."]

        for rule in RULE_ORDER:
            passed = [
                watch for watch in lab
                if watch["rules"].get(rule)
            ]
            failed = [
                watch for watch in lab
                if not watch["rules"].get(rule)
            ]

            lines.append(
                f"{rule} pass {self.rate(passed)} | "
                f"fail {self.rate(failed)}"
            )

        main = [
            watch for watch in lab
            if watch.get("channel") == "MAIN"
        ]

        lines.append(
            f"MAIN channel ({main_rule_text()}): "
            f"{self.rate(main)}"
        )

        for low, high in (
            (GAP * 100, RULE_S_MAX_GAP * 100),
            (RULE_S_MAX_GAP * 100, 15.0),
            (15.0, MAX_GAP * 100),
        ):
            if low >= high:
                continue

            group = [
                watch
                for watch in lab
                if low <= num(watch.get("gap_pct")) < high
            ]

            if group:
                main_group = [
                    watch for watch in group
                    if watch.get("channel") == "MAIN"
                ]
                lines.append(
                    f"Gap {low:g}-{high:g}%: "
                    f"all records {self.rate(group)} | "
                    f"MAIN channel {self.rate(main_group)}"
                )

        for rule in MAIN_RULES:
            others = [
                watch
                for watch in lab
                if all(
                    watch["rules"].get(other)
                    for other in MAIN_RULES
                    if other != rule
                )
            ]

            lines.append(
                f"All main rules except {rule} passed: "
                f"{self.rate(others)}"
            )

        for count in range(len(RULE_ORDER), -1, -1):
            group = [
                watch
                for watch in lab
                if sum(
                    bool(value)
                    for value in watch["rules"].values()
                ) == count
            ]

            if group:
                lines.append(
                    f"{count}/{len(RULE_ORDER)} rules passed: "
                    f"{self.rate(group)}"
                )

        return lines

    def record_line(self, watch):
        line = (
            f'{watch["symbol"]} {watch["strike"]:g} '
            f'{watch["side"]} ({watch["expiry"]}) | '
            f'{watch["direction"]} | {watch["status"]}'
        )

        if watch.get("header"):
            line = watch["header"] + " | " + line

        line += (
            f'\nReference ₹{watch["entry"]:.2f}; '
            f'fixed target ₹{watch["target"]:.2f}'
        )

        if watch.get("metrics"):
            line += "\n" + self.value_line(watch)

        if watch["status"] == "VALUE_REACHED":
            line += (
                f'; touch ₹{watch["hit_price"]:.2f} at '
                f'{parse(watch["hit_time"]):%H:%M:%S} IST '
                f'({watch["hit_evidence"]})'
            )

            if watch["hit_candle_close"] is not None:
                line += (
                    "; touching-candle close "
                    f'₹{watch["hit_candle_close"]:.2f}'
                )

        else:
            gap = (
                (watch["target"] - watch["last_price"])
                / watch["target"]
                * 100
            )

            line += (
                f'; last observed ₹{watch["last_price"]:.2f} at '
                f'{parse(watch["last_observed_at"]):%H:%M:%S}; '
                f"gap {gap:+.1f}%"
            )

        return line

    def report(self, day, t):
        if day.get("reported"):
            return

        rows = [
            watch for watch in self.state["watches"]
            if watch["day"] == day["date"]
        ]

        for watch in rows:
            if watch["status"] == "OPEN":
                watch["status"] = "UNVERIFIED_AT_REPORT"

        counts = Counter(watch["status"] for watch in rows)

        new = [
            watch for watch in rows
            if watch.get("policy") == FILTER_POLICY
        ]

        main = [
            watch for watch in new
            if watch.get("channel") == "MAIN"
        ]
        silent = [
            watch for watch in new
            if watch.get("channel") == "SILENT"
        ]
        legacy = [
            watch for watch in rows
            if watch.get("policy") != FILTER_POLICY
        ]

        def hits(group):
            return sum(
                watch["status"] == "VALUE_REACHED"
                for watch in group
            )

        successful_chains = sum(
            row.get("status") == "chain_received"
            for row in day["symbols"].values()
        )

        if main:
            main_summary = (
                "MAIN ALERTS sent to Telegram "
                f"(all of {main_rule_text()} passed): {len(main)}; "
                f"{hits(main)} targets touched; "
                f"touch rate {hits(main) / len(main) * 100:.1f}%"
            )
        else:
            main_summary = (
                "MAIN ALERTS sent to Telegram "
                f"(all of {main_rule_text()} passed): 0"
            )

        silent_summary = (
            "SILENT RECORDS "
            "(failed at least one main rule; no Telegram alert): "
            f"{len(silent)}; {hits(silent)} targets touched"
        )

        if silent:
            silent_summary += (
                f"; touch rate "
                f"{hits(silent) / len(silent) * 100:.1f}%"
            )

        lines = [
            f'S3 STOCK OPTIONS DAILY REPORT | {day["date"]}',
            main_summary,
            silent_summary,
            (
                f"Legacy watches from earlier rules: {len(legacy)}; "
                "included separately in the full totals below."
            ),
            (
                f'Stocks in universe: {day["universe_count"]}; '
                "stocks with a successful latest chain check: "
                f"{successful_chains}"
            ),
            (
                f'Completed sweeps: {day["sweeps"]}; '
                "last completed sweep: "
                f'{day.get("last_sweep_finished", "none")}'
            ),
            (
                "Last sweep duration: "
                f'{day.get("last_sweep_seconds", "unknown")} seconds'
            ),
            (
                f"All records: {len(rows)} | "
                f'model targets touched: {counts["VALUE_REACHED"]}'
            ),
            (
                "No touch in monitored full-minute bars: "
                f'{counts["NO_TOUCH_IN_MONITORED_BARS"]} | '
                f'unverified: {counts["UNVERIFIED_AT_REPORT"]}'
            ),
            (
                "The partial entry minute is excluded from candle "
                "checks; its unobserved movements are unknown."
            ),
            (
                "A model-value touch is not a profitable buy trade. "
                "DOWN watches describe falling option premiums."
            ),
            "",
        ]

        for name, group in (("MAIN", main), ("SILENT", silent)):
            for side in ("CE", "PE"):
                subset = [
                    watch for watch in group
                    if watch["side"] == side
                ]
                lines.append(
                    f"{name} {side}: "
                    f"{hits(subset)}/{len(subset)} targets touched"
                )

        lines.append("")
        lines += self.rule_lab(rows, "RULE LAB | THIS DAY")

        every = [
            watch for watch in self.state["watches"]
            if watch.get("policy") == FILTER_POLICY
        ]
        days = len({watch["day"] for watch in every})

        if days > 1:
            lines += [""] + self.rule_lab(
                every,
                (
                    f"RULE LAB | ALL DAYS SO FAR "
                    f"({days} days; this report includes today)"
                ),
            )

        lines += [""] + self.observation_summary(day)

        lines += ["", "MAIN ALERTS"]
        lines += (
            [self.record_line(watch) for watch in main]
            or ["none"]
        )

        if silent:
            lines += [
                "",
                "SILENT RECORDS "
                "(header shows each rule: ✓ passed, ✗ failed)",
            ]

            if REPORT_SILENT_LINES:
                lines += [
                    self.record_line(watch)
                    for watch in silent
                ]
            else:
                lines += [
                    "(per-record lines hidden; set "
                    "REPORT_SILENT_LINES=true to list them; "
                    "the saved JSON report has all of them)"
                ]

        if legacy:
            lines += ["", "LEGACY WATCHES"]
            lines += [
                self.record_line(watch)
                for watch in legacy
            ]

        lines += [
            "",
            "WHY CONTRACTS WERE SKIPPED "
            "(checks, not unique contracts)",
        ]

        lines += [
            f"{key}: {value}"
            for key, value in sorted(day["counts"].items())
        ]

        if day.get("last_error"):
            lines.append(
                "Last data issue: " + day["last_error"]
            )

        text = "\n".join(lines)
        folder = self.directory / "reports"

        (folder / f'{day["date"]}.txt').write_text(
            text,
            encoding="utf-8",
        )

        (folder / f'{day["date"]}.json').write_text(
            json.dumps(
                {"day": day, "watches": rows},
                indent=2,
                ensure_ascii=False,
            )
        )

        self.compress_observations(day)
        self.emit("report-" + day["date"], text)
        day["reported"] = True
        self.save()

    def tick(self, t):
        if t.weekday() >= 5:
            return

        day = self.prepare(t)

        self.emit(
            "online-" + FILTER_POLICY + "-" + day["date"],
            (
                f'S3 STOCK OPTIONS ONLINE | {day["date"]} '
                "| QUALITY V5\n"
                "BUY-ONLY ATM: CE or PE, only when its premium "
                "must RISE to the target.\n"
                f"Universe: {len(self.universe)} NSE F&O stocks; "
                "expiry more than 5 calendar days away "
                "(automatic rollover). "
                "ATM ±2 supplies IV peers; only ATM can alert.\n"
                "Q alert filter: "
                f'{RULE_TEXT["Q"]}\n'
                "Broader research candidate gap: "
                f"{GAP * 100:g}% to below {MAX_GAP * 100:g}%. "
                "Candidates failing Q are recorded silently.\n"
                f"{rule_legend()}\n"
                "Alerts 09:20-15:20 IST, Mon-Fri. "
                "Every ATM contract and available peer band scanned "
                "is logged for later filter checks. "
                "One active MAIN and one active SILENT record "
                "per stock. Daily report around 16:15 IST."
            ),
        )

        for date, old in list(self.state["days"].items()):
            if date < day["date"] and not old.get("reported"):
                self.report(old, t)

        if t.time() >= dt_time(16, 15):
            self.report(day, t)
            return

        if not dt_time(9, 20) <= t.time() <= dt_time(16, 15):
            return

        if t.time() <= stock_close(t):
            self.monitor_quotes(t)

        self.monitor_candles(
            now(),
            budget=1 if t.time() <= stock_close(t) else 10,
        )

        if t.time() <= alert_cutoff(t):
            self.scan_one(day, now())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["test", "run", "once"],
        default="run",
    )
    args = parser.parse_args()

    scanner = Scanner(API())
    start = now()

    if args.mode == "test":
        scanner.prepare(start)
        underlying = scanner.universe[0]
        chain = scanner.api.chain(
            underlying,
            scanner.metadata,
        )

        band_count = (
            len(select_band(chain, chain[0]["spot"]))
            if chain
            else 0
        )

        scanner.emit(
            "test-" + iso(start),
            (
                "S3 stock scanner connection OK. "
                f"Discovered {len(scanner.universe)} "
                "stock-option underlyings.\n"
                f'Sample API check {underlying["symbol"]}: '
                f"{len(chain)} contracts in full chain; "
                f"{band_count} IV-support contracts at ATM ±2.\n"
                "New alerts: BUY-ONLY ATM CE or PE.\n"
                f'Q filter: {RULE_TEXT["Q"]}\n'
                "This verifies connectivity, not signal quality."
            ),
        )

        for _ in range(3):
            scanner.flush()

        scanner.sync(force=True)
        return

    if start.time() < dt_time(13, 30):
        end = start.replace(
            hour=14,
            minute=10,
            second=0,
            microsecond=0,
        )
    else:
        end = start.replace(
            hour=16,
            minute=45,
            second=0,
            microsecond=0,
        )

    end = min(end, start + timedelta(hours=5, minutes=15))
    failures = 0

    try:
        while True:
            t = now()

            try:
                scanner.tick(t)
                scanner.flush()
                scanner.sync()
                failures = 0

            except AuthError:
                raise

            except Exception as error:
                failures += 1

                reason = (
                    str(error)
                    if isinstance(error, RuntimeError)
                    else type(error).__name__
                )

                print(f"{t:%H:%M} {reason}", flush=True)

                if failures in (1, 10):
                    scanner.emit(
                        f"error-{t:%Y%m%d%H}-{failures}",
                        "S3 stock scanner issue: " + reason,
                    )

                    try:
                        scanner.flush()
                    except Exception:
                        pass

                scanner.save()

                if failures >= 20:
                    raise RuntimeError(
                        "20 consecutive failures; "
                        "check workflow logs"
                    ) from None

                time.sleep(15)

            if (
                args.mode == "once"
                or now() >= end
                or t.weekday() >= 5
            ):
                break

            current = now()

            time.sleep(
                2
                if dt_time(9, 20)
                <= current.time()
                <= alert_cutoff(current)
                else 15
            )

    finally:
        scanner.sync(force=True)


if __name__ == "__main__":
    main()
