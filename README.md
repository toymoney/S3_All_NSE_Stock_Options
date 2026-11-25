# S3-only NSE stock-option scanner

Python + Upstox + Telegram + GitHub Actions. Only S3 is included: no S1, S2, Kronos, training or orders.

## V4 RULE LAB (supersedes the filter descriptions below where they differ)

Every buy-direction ATM candidate that passes the hard gates is scored on nine rules. Only a candidate that passes **all** `MAIN_RULES` (default `TOAVEI`) is sent to Telegram (MAIN). A second route also sends it as MAIN when rule **P** passes on its own (`MAIN_ALSO_IF`, default `P`; see below). Every other candidate is recorded silently (SILENT): it is monitored for the target exactly like a MAIN alert, no Telegram message is sent when it is created or when it touches, and its outcome appears in the daily report.

| Letter | Rule | Default |
|---|---|---|
| T | **Required.** Tier OI: a gap below 17% needs no extra OI (the 20-lot floor still applies); gap 17% to below 30% needs OI >= 2,000 lots | `EXTENDED_MIN_OI_LOTS` 2000 |
| D | Best bid AND best ask each >= 3 lots (scored and recorded; NOT required by default) | `MIN_DEPTH_LOTS` 3 |
| O | **Required.** OI >= 500 lots | `RULE_MIN_OI_LOTS` 500 |
| A | **Required.** Last trade at most 10 seconds old at quote time | `RULE_MAX_TRADE_AGE` 10 |
| V | **Required.** Volume / OI <= 3 | `RULE_MAX_VOL_OI` 3 |
| G | Gamma >= 0.006 (scored and recorded; NOT required by default, see below) | `RULE_MIN_GAMMA` 0.006 |
| E | **Required.** Absolute delta >= 0.35 (works for CE and PE; the premium must follow the stock) | `RULE_MIN_ABS_DELTA` 0.35 |
| I | **Required.** Implied volatility <= 28% (high-IV contracts missed more often) | `RULE_MAX_IV` 28 |
| P | **Extra route to MAIN.** Gap 15% to below 17% AND best bid and best ask each >= 3 lots. Passing P alone is enough to send a MAIN alert | `MAIN_ALSO_IF` `P` (empty switches the route off) |

Hard gates that still reject a candidate outright (nothing is recorded): buy direction only (model target above the premium, never a sell), no CE/PE restriction, ATM strike only, gap `MIN_VALUE_GAP` (default **5%**, lowered from 15%) to below 30% of model value, ask-based gap at least `MIN_VALUE_GAP`, spread <= 5%, base liquidity (OI >= 20 lots, volume >= 5 lots, at least 1 lot on each side), valid peer IV, expiry more than 5 calendar days away, at least 30 minutes to expiry, and a quote no older than 180 seconds. Missing data (no last-trade time, no gamma, no depth) fails the rule instead of passing it.

**Header on every record.** `MAIN | T✓ D✓ O✓ A✓ V✓ G✓ E✓ I✓ P✓ | 9/9 rules passed`, `MAIN via P | T✓ D✓ O✗ A✓ V✓ G✓ E✓ I✗ P✓ | 7/9 rules passed` (sent because of the P route), or `SILENT | T✓ D✗ O✓ A✓ V✓ G✓ E✓ I✗ P✗ | 6/9 rules passed` (recorded only, nothing sent). The same header is stored in the journal (`header`, `rules`, `rules_passed`, `rules_failed`, `channel`) together with the raw `metrics` (OI lots, volume lots, volume/OI, bid and ask lots, depth lots, last-trade age, gamma, delta, IV, theta, vega, spread %, gap %). Because the raw values are kept, any threshold can be re-tested later without re-running the scan.

**Selection.** At most one active MAIN and one active SILENT record per stock. A SILENT record does not block a MAIN alert and the reverse. If several ATM candidates exist, MAIN comes first, then the most rules passed, then the larger ask-based gap. A contract already alerted as MAIN (or by an earlier policy) is never repeated that day; a contract recorded only as SILENT can still become a MAIN alert later in the day if it then passes every main rule.

**Automatic rule comparison (Telegram only).** The daily report (about 16:15 IST) contains a RULE LAB block: for each rule, the touch rate when it passed versus failed, the average end move of the misses against entry, the result when all main rules passed, the result when all main rules passed except one (shows what each rule adds), and the result by number of rules passed. From the second day on a second block repeats the same table over all days so far. Nothing needs to be opened or operated by hand. `REPORT_SILENT_LINES=false` hides the one-per-record SILENT lines from the Telegram text (the saved JSON report on the state branch always has them).

**Gap band lowered to 5%.** The first scanned day on the next-month expiry (25 Nov, 09:56-12:27 IST) showed only 4 ATM contracts with a gap of 15% or more, out of 4,958 snapshots, because a contract with about a month to expiry rarely drifts that far from model value. The minimum gap is now 5% (`MIN_VALUE_GAP`); the upper limit stays at 30%. The ask-based gap must also be at least 5%. In the 5-15% band the required rules (O, A, V, E, I) still apply, T passes automatically, and the P route does not apply (it keeps its own 15% lower edge, `RULE_P_MIN_GAP`). Of 23 contracts first seen in the 5-10% band that morning, 3 would have passed all five required rules. A 5% target is thin: the spread is typically 1-2% and brokerage and charges are extra, so a touch in this band is worth much less than one at 15%. The daily rule comparison now shows touch rates by gap band (5-10%, 10-15%, 15-30%) so the bands can be compared after a few days.

**The P route (gap 15-17% + depth).** On the 20 November data this combination alone gave 11 alerts, 11 reached target. Adding it as an extra route on top of the required rules gives 26 alerts, 26 reached (5 more than the required rules alone). Requiring it in addition (`MAIN_RULES: 'TOAVEIP'`, `MAIN_ALSO_IF: ''`) gives only 6 alerts and removes the whole 17-30% band. The 5 extra alerts from the route would have failed some required rules (one had a last trade 69 seconds old, one a volume/OI of 12.6) and still reached target, so the route admits alerts the other rules would stop; depth was not a separator of hits from misses in the wider data, so treat the 11 of 11 as a small sample. The rule comparison shows MAIN via P separately in the headers; if that route starts missing, set `MAIN_ALSO_IF` to empty.

**Changing which rules gate the main alert later, with no code change.** Set `MAIN_RULES` in the workflow env, for example `MAIN_RULES: 'TOAVEIG'` to require gamma again, `MAIN_RULES: 'TDOAVEI'` to require depth again, or `MAIN_RULES: 'TOAVE'` to drop the IV rule. Thresholds can be changed with the `RULE_*` variables. `MIN_DEPTH_LOTS` cannot go below 3 and `EXTENDED_MIN_OI_LOTS` cannot go below 2000.

**Why gamma is not required by default.** Absolute gamma mostly measures the stock price (correlation with 1/spot was 0.88 on 20 November) and the time left to expiry. Because this bot always trades an expiry more than 5 calendar days away, gamma is structurally lower than on 20 November (about 14% of alerts would pass 0.006 instead of 56%), so the filter would mostly keep cheap stocks. A price- and expiry-independent version of gamma did not separate touches from misses at all. G is therefore scored on every record, so the rule lab shows whether it ever helps, but it does not gate the main alert unless you add it to `MAIN_RULES`.

**Important limits.** The other thresholds were tuned on one day (20 November), when the scanner was using the 23 November expiry. A touch rate is not a profit and says nothing about fills or stop-loss. Old watches stay labelled as legacy.

**Why E and I were added.** On the 20 November data, 9 of the 70 alerts passing only O, A and V missed target. Six of the nine were PE buys, they had higher IV (about 29-30% against 22% for hits) and several had weak delta. IV <= 28 and absolute delta >= 0.35 together removed all nine (39 of 39 reached target); with tier added and depth not required, 21 of 21. These cut-offs were chosen after seeing those misses on a single day, so some of that is fitting, and IV levels on the December expiry may differ from the November contracts. Both rules are scored on every record so the rule comparison will show whether they hold.

Verification: 62 offline tests pass, including each rule failing on its own, boundary values, missing data, silent and main not blocking each other, no Telegram message for silent records, and the rule-lab report counts. A simulated scan produced one MAIN alert (one Telegram message) and one SILENT record (no message). Authenticated Upstox, Telegram and GitHub scheduling were not tested here.

## OBSERVATION LOG (every ATM contract scanned, alert or not)

To judge whether the filters are too tight, too loose or about right, the bot now saves a snapshot of **every ATM CE and PE it looks at on every visit to a stock**, including all the ones that fail a gate and send nothing. Alerts and silent records are unchanged; this is a separate log.

Each snapshot row holds: time, stock, expiry, strike, side, lot, stock price; LTP, best bid and ask price, bid and ask lots, spread %, OI lots, volume lots, quote time, last-trade time and age; **all Greeks the API returns** (IV, delta, gamma, theta, vega and any others); peer IV, model value, gap % and ask/bid-based gap % (computed even when the contract is rejected); the reason it was rejected (`liquidity`, `gap_below_threshold`, `gap_above_band`, `insufficient_peer_iv`, `stale_option_or_missing_quote`, `no_liquid_contracts`, `stale_underlying`, ...) or `CANDIDATE`; for candidates, which rules passed and failed and the action (`SELECTED`, `DUPLICATE_CONTRACT`, `BLOCKED_BY_OPEN_WATCH`, `OTHER_SIDE_SUPPRESSED`). Rows from stocks that stopped early (no liquid contract, stale stock quote) use the option chain's own bid, ask and Greeks and are marked `src: chain`.

Files: `observations/<date>.jsonl` while the day runs, compressed to `observations/<date>.jsonl.gz` at the report, committed to the `s3-stock-state` branch with the journal. Expect roughly 6,000-7,000 rows a day, about 3 MB raw and about 0.5 MB compressed (measured on synthetic rows of the real size, about 490 bytes each); 5 days is a few MB. No extra API calls are made.

**Daily Telegram summary.** The report adds an OBSERVATION LOG block (snapshot, contract and stock counts, snapshots by result) and a FILTER FUNNEL: for the first sighting of each contract whose model value was at least 10% above its price, how many were stopped by each gate, how many became alerts, and how many of them later traded at or above the model value. That last check uses only the prices seen at later scans, so it understates real touches; use it to compare gates with each other, not as a touch rate.

Settings: `OBSERVE_ALL` (default `true`, `false` turns the log off), `OBSERVE_BAND` (default `false`; `true` also logs the non-ATM peer contracts, about 5x the rows), `OBSERVE_FUNNEL_MIN_GAP` (default `0.10`).

Limits: rows exist only for contracts the bot actually fetched. A stock whose chain request failed, or that was outside 09:20-15:20, has no row. Nifty VIX, sector and VWAP are not recorded. The price path after entry is still not stored.

## Coverage

Discovers all NSE stock-option underlyings from the current instrument master. The public master checked during development contained 218 eligible underlyings; the count can change. Indices and ordinary shares without listed options are excluded.

For each stock: nearest listed expiry more than five calendar days away. Only the actual ATM strike may produce a new alert, on CE or PE, and only when the model target is ABOVE the current premium. Starting LTP gap must be `MIN_VALUE_GAP` (now 5%) inclusive to 30% exclusive of model value. The 17% to below 30% tier additionally requires OI >=2,000 lots; gaps below 17% retain the 20-lot OI floor. The ask-based gap must still be at least 15%. Both best bid and best ask must display at least 3 lots each. Falling-premium targets are excluded.

ATM ±2 still supplies up to 10 contracts for IV estimation; neighbouring strikes do not produce alerts. If ATM is missing, stale, illiquid, or has inadequate peer IV, skip that stock rather than substitute another strike. ATM ties choose the higher strike.

If both ATM CE and PE qualify in the same scan, select the larger ask-based gap (CE wins an exact tie). Keep one active BUY_ATM_GAP_DEPTH_OI_V3 watch per stock to avoid conflicting/stacked alerts. After it resolves, a different contract can qualify later; the same contract is still limited to once per day. This is not a one-stock-per-day cap. Selection uses information available at that check, not future outcomes.

The journal records policy BUY_ATM_GAP_DEPTH_OI_V3, exact gap, current spot and ATM strike at entry. The daily report separately counts new-policy alerts, CE/PE results and one-percentage-point gap bands. Historical watches remain explicitly labelled as legacy in the totals. No historical outcomes are rewritten.

These are OPTION premium estimates, not fundamental fair values of stock shares.

## Updating an existing installation

Replace `stock_s3_bot.py`, `tests/test_stock_s3.py` and `README.md` from this package. Workflow and secrets need no changes. A running job uses its checked-out code; cancel it and start a new `run` job to apply immediately, or wait for the next scheduled run. Existing recorded watches continue to be monitored, including those outside the newly narrowed band. New watches use buy-only ATM selection. Stop the current job before uploading; start a fresh run after committing the files, or wait for the next schedule.

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
- New watches: 09:20–15:20 IST. Quote monitoring until 15:40, automatically changing to 15:45 on 7 December 2026; completed-candle checks until the report around 16:15.
- GitHub schedules can start late or be missed. Each process is limited to 5h15. This is not an exchange-grade live service.
- No paid server/model dependency. Uses standard public-repository Actions runners and your broker account's API entitlement; check account-specific data restrictions. Review failed runs and keep your repository active.
- No exchange holiday calendar bundled. Freshness checks block yesterday's quotes, but a weekday holiday can generate an empty report.

## Example messages — illustrative prices only

```text
S3 STOCK OPTIONS | SAMPLE 100 CE
BUY-ONLY ATM WATCH | expiry YYYY-MM-DD
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

A CE at ₹10 with model target ₹12 can qualify. A PE at ₹12 with target ₹10 is excluded. A PE at ₹10 with target ₹12 can qualify instead if it meets all checks. If both sides qualify to rise, only the selected side is alerted.

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

## First filter comparison: supplied 20 November report

The original report contains 339 watches with 251 recorded touches (74.0%). Removing DOWN watches leaves 230 UP watches with 156 recorded touches (67.8%): CE 136/184 (73.9%), PE 20/46 (43.5%). This removes 109 alerts, or 32.2%. These are historical reported touch counts, not costed returns or a replay of the new combined policy.

The pasted report does not give spot/ATM at entry or all candidate snapshots. Therefore the exact historical buy-only + ATM + active-stock-selection result is NOT established. Supply `state.json` or `reports/2026-11-20.json` from the s3-stock-state branch for further reconstruction; historical coverage may still limit a full replay. Do not infer ATM from the daily closing stock price.

Post-close candle bars are now excluded and quote requests finishing after the applicable session close cannot create hits. Existing stored hits are not automatically corrected without a data audit.

## Model and filters

Black–Scholes uses current underlying spot, strike, exact time to expiry-day stock-derivatives close (15:40 before 7 December 2026; 15:45 thereafter), a risk-free rate, dividend yield and neighbouring-strike IV. The candidate's own IV is excluded to avoid merely reproducing its market price. Up to two liquid same-side strikes below and two above are weighted using OI and spread, then interpolated. No supported peer on BOTH sides means no alert; band edges can be skipped. No extrapolation is used.

Delta, Gamma, Theta and Vega are displayed broker sensitivities, not independent ingredients added together to produce a guaranteed actual value. OI supplies liquidity checks and peer weights. This is a relative-value estimate.

Defaults: 6% annual risk-free rate, zero dividend yield unless configured. Dividends, ex-dates, corporate actions, events, skew and stock-option settlement effects can create misleading apparent gaps. There is no corporate-event calendar. Configure dividend assumptions where known. Stock-option delivery/margin obligations are outside this paper scanner.

The target is FIXED when the watch is created. A later touch is a touch of that original estimate, not proof that the option is then fairly priced.

| Filter | Default |
|---|---:|
| Minimum gap relative to model value | 5% (`MIN_VALUE_GAP`; was 15% until 25 Nov 2026) |
| Maximum bid–ask spread / midpoint | 5% |
| Minimum premium and model value | ₹1 |
| Minimum OI | 20 lots |
| Minimum volume | 5 lots |
| Best-level displayed size on each side for new alerts | 3 lots |
| Base starting-gap upper boundary | 17% |
| Maximum starting gap (exclusive) | 30% |
| OI for starting gaps >=17% | 2,000 lots |
| Maximum last-trade age | 180 seconds |
| Minimum remaining expiry time | 30 minutes |

Both LTP and ask must pass the positive gap filter; targets must exceed the premium. This checks quote quality; it does not simulate fills. Missing/stale quotes or peer IV are skipped. Calls are paced at least 1.2 seconds apart, with bounded retries and quote batches of 100.

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
BASE_MAX_VALUE_GAP: '0.17'
MAX_VALUE_GAP: '0.30'
EXTENDED_MIN_OI_LOTS: '2000'
MIN_DEPTH_LOTS: '3'
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

Release verification: 42 offline tests passed for discovery, strike bands, IV interpolation, filters, quote freshness, gap direction, duplicates, candle evidence, incomplete coverage, persistence and reports. Public instrument-master retrieval and active stock-option discovery checked. Authenticated quotes/candles, Telegram and GitHub scheduling have NOT been tested with your credentials here; run the connection check and observe the first paper session.

Official references:
- https://upstox.com/developer/api-documentation/instruments/
- https://upstox.com/developer/api-documentation/get-pc-option-chain/
- https://upstox.com/developer/api-documentation/get-full-market-quote/
- https://upstox.com/developer/api-documentation/rate-limiting/

## Gap and depth update — 20 November 2026

The earlier buy-only ATM, CE/PE ranking and active-stock rules remain. Previous BUY_ATM_V1 open watches also block new watches for that stock until resolved. Old watches keep their original targets and policy labels; reports separate the new policy. The 3-lot test applies to new alert candidates; IV peers retain the existing 1-lot liquidity floor. Depth means displayed quantity at the best price divided by the contract lot size, not summed five-level depth. Missing depth is rejected.

The original base-tier research used 15% <= starting gap <17%. Exactly 17% now enters the extended tier and requires OI >=2,000 lots. This narrows alerts but does not establish a future win rate. No authenticated live API checks were performed for this update.

## Automatic expiry rollover and session timing

At five calendar days before the actual listed monthly expiry (inclusive), new scans switch to the next listed expiry more than five days away. Example: 24 November expiry switches to 29 December on 19 November. Calendar days include weekends and holidays. Dates come from the current master, so shifted expiry dates are respected. If no eligible later expiry exists, skip the stock; do not fall back to the expiring contract. Existing watches retain their expiry and target and continue monitoring. All ATM, gap and depth rules remain.

Stock derivatives close at 15:40 for 3 August–6 December 2026 and 15:45 from 7 December 2026; earlier sessions use 15:30. Valuation time, quote checks, candle boundaries and coverage checks share this schedule. New alerts continue through 15:20 IST, with no 30-minute-before-close cutoff. The report remains around 16:15 IST. The five-calendar-day rollover avoids the expiry delivery-margin period.

Sources: NSE CAS FAQs (October 2026 and CAS 2.0 November 2026); NSE/FAOP/77356 dated 6 November 2026; Upstox physical-settlement announcement.

## 20 November depth comparison

Buy-only historical watches, before ATM-only and active-stock suppression. Both best bid and best ask >=3 lots. Starting gap bands are lower-inclusive, upper-exclusive.

|Gap %|Alerts|Touches|Non-touches|Touch rate|
|---|---:|---:|---:|---:|
|15–17|11|11|0|100%|
|17–20|11|7|4|63.6%|
|20–25|14|8|6|57.1%|
|25–30|2|1|1|50%|
|15–30 total|38|27|11|71.1%|

The extended tier is now enabled: 17% to below 30% with OI >=2,000 lots.

## Final OI extension

New policy BUY_ATM_GAP_DEPTH_OI_V3 accepts 15% <= gap <17%, OR 17% <= gap <30% with OI >=2,000 lots. Both require >=3 lots at best bid and best ask and all existing ATM, liquidity, freshness, ask-gap and expiry checks. OI units are divided by contract lot size; 2,000 lots is not 2,000 units. Exactly 30% is excluded. Prior V1 and V2 open stock watches still block new simultaneous watches. The journal stores entry_oi_lots and gap_tier.

If you previously set MAX_VALUE_GAP explicitly in your workflow, change it to 0.30; BASE_MAX_VALUE_GAP is 0.17. The bundled workflow uses code defaults. Secrets and schedule do not change. Historical 18/18 counts precede ATM and active-watch suppression.
