# S3 NSE stock options — Activity Research V7

Replacement package for toymoney/S3_All_NSE_Stock_Options.
Based on main commit 8ee2b9f54aa1f65a20c2d65d39e5ce93a84eccab.
Research and Telegram alerts only. No broker orders.

Only the active research filter is changed from the V6 package. Broad pricing,
gap and liquidity gates, recording, reports, scheduling, stock-level open-watch
guard, and Telegram behavior are retained. The stock-level guard can still
suppress another contract while an existing main watch in that stock is OPEN.

## Install once

Upload the package files to the same paths in the repository, including
`.github/workflows/stock_s3.yml` and the files inside `tests` and `tools`.
Keep the existing `s3-stock-state` branch and all saved journals.
Do not replace the state branch with this source package.

GitHub repository secrets stay the same:
`UPSTOX_ACCESS_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
Keep the Upstox token current. Enable GitHub Actions with contents-write permission.
The workflow runs offline checks before scanning. Optional connection check:
Actions → S3 stock option research V7 → Run workflow → mode `test`.
Use mode `run` to start an authorized market session manually if needed.
Scheduled runs remain 08:57, 14:07 and 16:17 IST on weekdays.
The 14:07 session continues recording/monitoring, without new R entries.
GitHub scheduled execution can start late; no exact-start guarantee is implied.

## Active selection: rule R

- Current contract volume divided by current OI >=3, using matching units.
- Signal time in IST is strictly before 15:00 (14:59:59 qualifies; 15:00 does not).
- Only the first qualifying MAIN alert per contract per trading day.
  Earlier SILENT observations do not consume that contract's MAIN slot.
  Later repeats are suppressed even after target resolution and process restarts.
- BUY-only CE or PE, ATM at signal time. Existing ATM verification stays.
- Target remains the fixed model value calculated at entry.
- A separate +5% target from entry ask remains recorded for comparison.
- `MAIN_RULES=R`; `MAIN_ALSO_IF` is empty. Q cannot bypass R.

R replaces the previous V6 main-selection route. Delta remains recorded but is not an R gate. It does NOT add the V5 OI>=500,
volume>=100, IV<=28%, spread<=1% or age<=40s limits. Q and all historical
flags remain available in saved records for comparison.

Existing broad eligibility and pricing gates still apply before R:
LTP>=1, OI>=20 lots, volume>=5 lots, bid/ask depth>=1 lot each,
positive ordered bid/ask, spread<=5%, fresh trade under the inherited
180-second check, valid peer IV on both sides, model gap>=5% and <30%,
model value>=5% above entry ask, and expiry eligibility/ATM verification.
These are inherited collection gates, not newly optimized thresholds.
Neighbouring strikes provide IV inputs and are recorded; only ATM can alert.
No change was made to the repository's existing session calendar assumptions.

## Recording and reports

Scanning and silent recording continue until the existing 15:20 IST entry
scan cutoff even after R stops producing main alerts at 15:00. Open-watch outcome
monitoring continues through the existing session close; reports are generated
around 16:15 IST. Existing source scheduling and state checkpoints are retained.

Saved records contain R pass/fail reasons, V5 Q failures, all historical flags,
entry-time volume/OI, Greeks, bid/ask, quantities, freshness, model values and
entry ask. The active settings are saved with the day and each candidate.
Accepted and rejected observations and available neighbouring strikes remain
recorded. Watch-entry, monitoring-quote and completed-candle events remain
recorded; post-target collection is preserved for the active V7 policy.
One active main and one active silent watch per stock and daily contract/channel
dedup remain in place. These selection and dedup rules mean live outcomes may
differ from applying R to previously saved watches.

Reports and `.jsonl.gz` observations continue on `s3-stock-state`.
Do not delete the existing journal or observation folders.

## Historical finding: 25–27 November 2026

R retained 8 of 106 supplied saved watches, all labelled VALUE_REACHED:
25 Nov: DABUR 425 CE (12:50), EICHERMOT 8300 CE (13:04), DMART 3550 CE (13:31).
26 Nov: KFINTECH 960 CE (09:54), BRITANNIA 5150 CE (11:54),
BRITANNIA 5200 CE (12:36).
27 Nov: OBEROIRLTY 1940 CE (10:56), TITAN 4700 CE (13:37).
This is 8 contract observations across 7 stocks. It is a result selected after
examining these days, not an independent validation or executable profit rate.
The labels mix fresh LTP and completed-candle target evidence, and do not
establish target-before-stop, fills, costs or a consistent executable +5% result.
Full raw-entry replay was not used to establish this 8/8 subset.

## Verify and replay

Python 3.11 or newer:

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python tools/replay_v6.py day25.json day26.json day27.json
```

Replay reads each summary's existing watches and outcomes and prints selected
rows as CSV plus daily counts. It never calls the broker or Telegram, does not
modify the source data, and does not calculate fresh outcome labels.
The replay and test filenames containing v6 are retained to overwrite the
previous package files cleanly; their contents implement V7.
The original three summary files are not embedded in the source package.

The package was checked with 79 offline unit tests, including the previous
regression suite, R boundaries, rejection recording, Q isolation and all eight
historical entry-metric fixtures and restart-safe daily contract deduplication. Live Upstox/Telegram connectivity was not tested.
