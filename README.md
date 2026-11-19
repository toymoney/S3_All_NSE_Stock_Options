# S3-only NSE stock-option scanner

Python + Upstox + Telegram + GitHub Actions. Only S3 is included: no S1, S2, Kronos, training or orders.

## Coverage

Discovers all NSE stock-option underlyings from the current instrument master. The public master checked during development contained 218 eligible underlyings; the count can change. Indices and ordinary shares without listed options are excluded.

For each stock: nearest active expiry, ATM and two actual listed strikes each side, both CE and PE (maximum 10 contracts). Real lot sizes and strike spacing are used. The minimum value gap is 15% of model value; there is no upper cutoff. Both above-value and below-value watches qualify. One alert per contract per observed session, with repeated sweeps throughout the session. No daily alert-count cap.

These are OPTION premium estimates, not fundamental fair values of stock shares.

## Updating an existing installation

Replace `stock_s3_bot.py`, `tests/test_stock_s3.py` and `README.md` from this package. Workflow and secrets need no changes. A running job uses its checked-out code; cancel it and start a new `run` job to apply immediately, or wait for the next scheduled run. Existing recorded watches continue to be monitored, including those outside the newly narrowed band. New watches use ATM ±2 only.

## GitHub setup

1. Create a new public repository, for example `stock-s3-alerts`. Extract this ZIP and upload its contents at repository root. Do not upload the ZIP as the program.
2. Include `stock_s3_bot.py`, `requirements.txt`, `tests/test_stock_s3.py` and `.github/workflows/stock_s3.yml`.
3. To create the hidden workflow folder on GitHub: **Add file → Create new file**, enter `.github/workflows/stock_s3.yml`, paste the supplied YAML and commit. The slash-separated name creates the folders. Use the same approach for `tests/test_stock_s3.py` if needed.
4. **Settings → Secrets and variables → Actions → New repository secret**: add `UPSTOX_ACCESS_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Reuse your existing valid values. Never paste them into code, commits, issues or screenshots. Renew the broker token when it expires; this package does not automate broker login.
5. **Settings → Actions → General → Workflow permissions → Read and write permissions**. Records are saved on a separate `s3-stock-state` branch.
6. To pause S1 and S2, open your OLD Nifty repository → Actions → old Nifty workflow → three-dot menu → **Disable workflow**. Installing this separate project does not disable the old bot automatically.
7. New repository → **Actions → S3 stock option alerts → Run workflow → mode: test**. This checks the master, one option chain and Telegram. It does not validate live signals or all API endpoints.
8. After the test succeeds, choose **mode: run** for today's remaining session, or wait for the next weekday schedule. `once` performs one tick, not a full-universe sweep. Avoid repeatedly starting jobs: they queue to protect the journal.

Keep the workflow on the default branch. Do not edit/delete the journal while a job is running.

## Schedule and operating limits

- Weekday workflow starts: 08:57, 14:07 and 16:17 IST.
- Morning process ends around 14:10; the afternoon job queues behind it and continues until 16:45. The third job is report recovery.
- New watches: 09:20–15:00 IST. Quote monitoring until 15:30; completed-candle checks until the report around 16:15.
- GitHub schedules can start late or be missed. Each process is limited to 5h15. This is not an exchange-grade live service.
- No paid server/model dependency. Uses standard public-repository Actions runners and your broker account's API entitlement; check account-specific data restrictions. Review failed runs and keep your repository active.
- No exchange holiday calendar bundled. Freshness checks block yesterday's quotes, but a weekday holiday can generate an empty report.

## Example messages — illustrative prices only

```text
S3 STOCK OPTIONS | SAMPLE 100 CE
BELOW MODEL VALUE | expiry YYYY-MM-DD
LTP ₹20.00 | fixed model target ₹25.00
Gap 20.0% of model value | direction UP
Bid ₹19.90 | ask ₹20.10
IV ... | peer IV ...
Delta ... | Gamma ...
Theta ... | Vega ...
OI ... | volume ... | lot ...
10:12:30 IST | ID ...
Valuation watch only; no buy/sell execution or stop-loss.
```

An ABOVE MODEL VALUE watch could show ₹30 premium, ₹25 target, direction DOWN. That means convergence requires the option premium to fall. It is not a buy signal or an instruction to short the option.

```text
S3 VALUE REACHED | SAMPLE 100 CE
Direction UP | reference ₹20.00
Fixed target ₹25.00
Touch/reference price ₹25.00 | ... IST
Evidence: completed 1m candle high/low; time is candle end
Touching candle close: ₹23.00 (not the touch price)
Model-price convergence; not a guaranteed trade fill or profit.
```

Daily header: `S3 STOCK OPTIONS DAILY REPORT`. Lists every watch, touch evidence or last observed gap, stocks covered, sweep duration, missing data and rejection counts. Long reports are split across Telegram messages.

## Model and filters

Black–Scholes uses current underlying spot, strike, exact time to expiry-day 15:30 IST, a risk-free rate, dividend yield and neighbouring-strike IV. The candidate's own IV is excluded to avoid merely reproducing its market price. Up to two liquid same-side strikes below and two above are weighted using OI and spread, then interpolated. No supported peer on BOTH sides means no alert; band edges can be skipped. No extrapolation is used.

Delta, Gamma, Theta and Vega are displayed broker sensitivities, not independent ingredients added together to produce a guaranteed actual value. OI supplies liquidity checks and peer weights. This is a relative-value estimate.

Defaults: 6% annual risk-free rate, zero dividend yield unless configured. Dividends, ex-dates, corporate actions, events, skew and stock-option settlement effects can create misleading apparent gaps. There is no corporate-event calendar. Configure dividend assumptions where known. Stock-option delivery/margin obligations are outside this paper scanner.

The target is FIXED when the watch is created. A later touch is a touch of that original estimate, not proof that the option is then fairly priced.

| Filter | Default |
|---|---:|
| Minimum gap relative to model value | 15% |
| Maximum bid–ask spread / midpoint | 5% |
| Minimum premium and model value | ₹1 |
| Minimum OI | 20 lots |
| Minimum volume | 5 lots |
| Displayed size on each side | 1 lot |
| Maximum last-trade age | 180 seconds |
| Minimum remaining expiry time | 30 minutes |

Both LTP and the relevant quote side must pass the gap filter: ask for UP, bid for DOWN. This checks quote quality; it does not simulate fills. Missing/stale quotes or peer IV are skipped. Calls are paced at least 1.2 seconds apart, with bounded retries and quote batches of 100.

A full sweep takes several minutes, potentially considerably longer with many watches. Quote checks are attempted about once per minute; API and processing delays extend this. Candle checks rotate across open watches. Telegram sends at most five queued messages per loop, at least 3.1 seconds apart, so heavy alert volume can delay delivery and scanning. Coverage and elapsed sweep time are reported.

## Honest outcomes and persistence

- Fresh LTP crossings or completed one-minute high/low candles confirm a touch. Candle-end time is an interval label, not exact tick time. A candle touch is not a guaranteed executable fill.
- The partial entry minute is excluded from candle checks to avoid counting pre-alert touches. Unobserved movements within that minute remain unknown.
- `NO_TOUCH_IN_MONITORED_BARS` requires complete minute coverage from the next minute through the closing bar. It makes no claim about the excluded partial entry minute.
- Missing final coverage becomes `UNVERIFIED_AT_REPORT`. Interrupted previous sessions are reported as unverified on recovery; missed alerts/outcomes are not invented.
- Normal retries deduplicate event IDs. A crash or ambiguous Telegram response can still cause a rare duplicate.
- State is committed about every five minutes and at orderly exit; abrupt termination can lose work since the last checkpoint.
- The `s3-stock-state` branch holds watch records, Telegram message text and reports. **These are public in a public repository.** Credentials are read from secrets and are not journalled.
- Reports are `reports/YYYY-MM-DD.txt` and `.json` on the state branch. Watches close for that day's report, without automatic multi-day carryover.

## Optional workflow environment variables

```yaml
MIN_VALUE_GAP: '0.15'
MAX_SPREAD: '0.05'
MIN_OI_LOTS: '20'
MIN_VOLUME_LOTS: '5'
MIN_PREMIUM: '1'
FRESH_SECONDS: '180'
RISK_FREE_RATE: '0.06'
DIVIDEND_YIELD: '0'
DIVIDEND_YIELDS_JSON: '{"YOUR_SYMBOL":0.015}'
```

Dividend yields are annual decimals (0.015 = 1.5%); use real exchange symbols and verified assumptions. The example is syntax only. Looser filters increase stale/illiquid alerts.

## Local use and validation

Python 3.11 or newer, with the same three secrets in environment variables:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python stock_s3_bot.py --mode test
python stock_s3_bot.py --mode run
```

Local state defaults to `state_s3/`. Git checkpointing is off unless `GIT_STATE=true`.

Release verification: 28 offline tests passed for discovery, strike bands, IV interpolation, filters, quote freshness, gap direction, duplicates, candle evidence, incomplete coverage, persistence and reports. Public instrument-master retrieval and active stock-option discovery checked. Authenticated quotes/candles, Telegram and GitHub scheduling have NOT been tested with your credentials here; run the connection check and observe the first paper session.

Official references:
- https://upstox.com/developer/api-documentation/instruments/
- https://upstox.com/developer/api-documentation/get-pc-option-chain/
- https://upstox.com/developer/api-documentation/get-full-market-quote/
- https://upstox.com/developer/api-documentation/rate-limiting/
