# Hand-off prompt: Nifty options data pipeline, backtests and live Kite collector

Paste everything below the line into the other assistant (GPT or similar) as the first message. It describes what was built
in the previous working session (2026-11-04 to 2026-11-10), why, what is known to work, what is not, and what to do next.
Facts marked **(verified)** were checked against real data or real runs; **(simulated)** means tested only against a fake Kite;
**(not run)** means written but never executed against the real API.

---

## 0. Your role and ground rules

You are continuing an existing Python project for a retail trader in India (Windows 10, PowerShell, Python 3.14, pandas, PyYAML,
`kiteconnect` 5.2.2, `requests`). The project downloads Nifty index / futures / weekly options data (Dhan API for history, Zerodha
Kite Connect for live and far strikes), builds a credit-spread backtester, and now runs a continuous live data collector.

Rules that matter:
1. **Never print, log, commit or ask for secrets.** Credentials live in `data_download.env` / `.env` / environment variables
   (`DHAN_CLIENT_ID`, `DHAN_PIN`, `DHAN_TOTP_SECRET`, `KITE_API_KEY`, `KITE_API_SECRET`) and token files (`.dhan_token.json`,
   `.kite_token.json`). All are git-ignored. Keep them that way.
2. **You probably cannot reach api.dhan.co / api.kite.trade from your sandbox** (the previous assistant got
   `SSLError CERTIFICATE_VERIFY_FAILED`). Do not disable certificate verification. Write code, test it with fakes (see the fake Kite in
   section 8), and give the user exact PowerShell commands to run on their PC; ask them to paste output back.
3. Windows/PowerShell: scripts in the current folder need `.\` (`.\script-data\run_live_collector.bat`); `&&` is not available in Windows PowerShell 5.1;
   use `python -c`/files rather than here-strings with backslashes (a `\r` in an edit once turned into a carriage return).
4. Report results honestly, including failures. Backtest numbers below are **in-sample** (settings were chosen on the same data); do not present
   them as expected live performance.
5. Prefer small, verified edits. Read a file before editing it. Keep config in YAML, not in code.

## 1. Repositories and folders

| Path | Purpose |
|---|---|
| `C:\Trading-Application\first-project` | Code + history: downloaders, backtester, history data (`data_daily/`, `data_hourly/`, `data_minute/`, `data_5min/`), configs, results. Git branch `testing-with-open-interest-0`. |
| `C:\Trading-Application\first-project-data` | **This repo.** Live collector (`script-data/live_collector.py`), its configs (`configurations/live/`), output (`data/`), logs (`logs/`), state (`state/`), `README.md`, this file. |

`first-project/readme.MD` documents the history pipeline (credentials, `update_data.py`, per-dataset downloads, known data issues, backtests).
`first-project-data/README.md` documents the live collector.

## 2. History pipeline (first-project)

| Script | What it does |
|---|---|
| `download_data.py` | Downloads index (Dhan intraday charts + NIFTYBEES volume proxy), options (Dhan `/v2/charts/rollingoption`, ATM-relative strikes, 2-day windows), futures (Kite), daily candles, Kite far strikes (`--only kite_options`). Resumable per strike/side (`*_parts/`). `--days N`, `--probe-strikes`, `--login`. |
| `dhan_auth.py` | Fully automatic Dhan token from client id + PIN + TOTP secret (stdlib RFC 6238, cache `.dhan_token.json`, auto-refresh mid-run). **(verified)** |
| `update_data.py` | One command to top up everything: `--only hourly|daily|minute|five`, `--minute`, `--five-minute`, `--futures`, `--backfill-months N`, `--dry-run`. The 5-minute step is `make_5min.py`, not a download. |
| `make_5min.py` | Builds `data_5min/nifty_5m.csv` and `nifty_options_5m.csv` from the 1-minute files (bars stamped at start, 09:15-15:29 only). **(verified)** |
| `repair_hourly.py` | Repairs hourly files from the 1-minute files: removes junk Saturday days, rebuilds missing/short index days, rebuilds expiry-1 option days stamped :20 instead of :15. Run after every hourly update. **(verified)** |
| `strategy.py` | Backtester + HTML report (credit spreads). See section 4. |
| `run_updates.bat` | Scheduled-task wrapper for `update_data.py`, logs to `logs\`. |

### Data inventory (as of 2026-11-10, **verified** with consistency checks)

| Data | File(s) | Range | Notes |
|---|---|---|---|
| Daily index | `data_daily/nifty_1d.csv` | 2021-11-08 -> 2026-11-06 | |
| Daily options | `data_daily/nifty_options_1d.csv` | 2021-11-15 -> 2026-11-06 | Built from the hourly option files (Dhan has no daily option interval). Expiry codes 1 and 2. |
| Hourly index | `data_hourly/nifty_1h.csv` | 2021-11-15 -> 2026-11-09 | 7 bars/day (09:15..15:15). Exact aggregate of the 1-minute bars except the last bar's close (Dhan: official 15:30 close). |
| Hourly options | `nifty_options_1h.csv` (expiry 1, ATM+-10), `nifty_options_1h_exp2.csv` (expiry 2, ATM+-3) | 2021-11-15 -> 2026-11-09 | **exp2 still has 16 days stamped :20 (2021-11-15..12-01, 2026-11-03..06)** that cannot be rebuilt (no 1-minute source). |
| 1-minute index | `data_minute/nifty_1m.csv` | 2020-11-17 -> 2026-11-09 | Includes Muhurat evening/Sunday sessions and a few filler bars outside 09:15-15:29. |
| 1-minute options | `data_minute/nifty_options_1m.csv` (~23M rows) | 2020-11-17 -> 2026-11-09 | ATM+-10 from Dhan; ATM+-20 on recent days from Kite (`data_minute/kite_wide/`, no IV). 9-11% of rows have IV<=0; 197 rows OI=0. |
| 5-minute index/options | `data_5min/nifty_5m.csv`, `nifty_options_5m.csv` | 2020-11-17 -> 2026-11-09 | Built from 1-minute. 110,706 index bars; 4.65M option rows. |
| Futures | none yet in history folders | | Kite only; `config_download_5min.yaml` has a `futures` section (not run). |

### Dhan / Kite facts learned (important)
- Dhan rolling options serve **only ATM+-10 for index options at every age** (probed 2026-11-08); later expiries only ATM+-3. Do not try `extra_strikes`.
- Dhan returns at most 2 days per options request; a full 5-year options download is ~38,000 requests (hours); everything resumes per strike.
- Dhan's **5-minute option candles are stamped at the bar END minute** (the 09:20 bar holds 09:16-09:20, first bar of the day is 1 minute; bars exist at 15:30 and 15:35),
  while Dhan's index candles are stamped at the start. Joined on time they would misalign by ~4 minutes and 57% of option bars differ from the 1-minute aggregate.
  **Therefore the 5-minute files are built from the 1-minute data, not downloaded.** **(verified)**
- Dhan's oldest 1-minute index window (5 years back) contains filler bars (before 09:15, after 15:30, weekends); `_decode_epoch` was made tolerant and drops them. **(verified by the user's successful run)**
- Dhan hourly files: re-running with a short `data.days` **overwrites** the file with the shorter history (hourly configs are now `days: 1822`); closed Saturdays appear as junk sessions;
  the first and newest days of a run can be bucketed at :20 with a stray 08:20 bar. `repair_hourly.py` handles index and expiry 1.
- Kite historical API serves **only live contracts** (expired weekly options/futures are gone). Rate limit 3 requests/s. Daily login required (token expires ~6 AM IST); no official automated login.
- Weekly Nifty expiry is **Tuesday**, moved earlier when Tuesday is a holiday (2026-11-09 Monday, because 2026-11-10 was a holiday; the ATM call decays to ~0 on 11-09 in the data).
- After an expiry, premiums jump; on Wed-Fri early in the expiry week no hedge cheaper than Rs 10 exists inside ATM+-10.
- `to_csv` of tz-aware timestamps for 20M+ rows takes 15+ minutes and a Ctrl+C mid-write truncated `nifty_options_1m.csv` once (recovered by re-running the options step). `_write_csv_safe` now formats each distinct timestamp once and writes to a temp file then `os.replace` (4x faster, interruption-safe). **(verified)**

## 3. Credentials and login flow
- Dhan: `data_download.env` (git-ignored) with the three TOTP variables; `python dhan_auth.py` to test. Fully automatic.
- Kite: `KITE_API_KEY`, `KITE_API_SECRET`; daily `python download_data.py --login` (history repo) or `python script-data/live_collector.py --login` (this repo; writes its own `.kite_token.json`).

## 4. Backtester (`strategy.py`) — what exists

Credit spreads on Nifty weekly options. Key mechanics (all **verified** by runs):
- One position at a time. Signals use the previous bar; entries fill at the current bar's open. A trade closed at a bar's close cannot reopen on the same bar.
- `spread.reverse_on_flip`: a signal against the open trade closes it (at the next open, reason `TrendChange`) and opens the new direction. `spread.flip_confirm_bars: N` requires the opposite signal on N consecutive bars.
- Exits: stop-loss (`sl_pct`, % of credit), target (`tp_pct`; **`100` = effectively no target**, only a fully decayed spread triggers it), `TrendChange`, `EdgeExit` (short strike `edge_exit_steps` strikes ITM, about to leave the ATM+-10 data window),
  expiry (last bar of expiry day), `NoData`, and **`session.exit_eod: true` = flat at the day's last bar** (this was silently ignored for spreads before; fixed).
- Direction logic `direction.logic: skew_mom`: equal-weight z-scores of (a) IV skew = PE IV(offset -2) - CE IV(offset +2) vs its 5-day mean and (b) intraday momentum (close vs day open). Long if score > threshold, short if < -threshold. Windows default to 5/10 days of bars
  (`skew_norm_bars`, `scale_bars`), so they scale with bar size.
- Short-leg selection modes: `otm_premium`, `itm_premium`, `oi_wall`, `itm_steps`. Hedge: far OTM leg cheaper than `hedge_max_premium`.
- Bar size handling: `_native_minutes()` detects the file's bar size; if it equals `resample.timeframe` no resampling happens; if `resample.enabled: false` bars/day comes from the data (7 hourly). Configs: set `data.output_csv` / `dhan.output_csv` to the files to trade.
- Not yet inside `strategy.py`: cleaning of out-of-session bars, zero-OI and IV<=0 rows (an external cleaned-data harness exists only as throwaway scripts).

### Configs and results (in-sample; capital 1.9M INR, fixed 650 qty = 10 lots of 65; slippage 5 bps, costs 3 bps)

Minute files resampled to 5 min unless stated. `v8` = sell ITM (premium 100-140), hedge < Rs 10, SL 100%, TP 20%, edge exit 8.

| Config (`configurations/`) | Change | Period | Trades | CAGR | PF | Max DD | Sharpe |
|---|---|---|---|---|---|---|---|
| `config_skew_mom_v8_itm` | baseline | 2022-11 -> 2026-11 | 1477 | 18.2% | 1.13 | -59% | 0.82 |
| `..._v10_eod_tp30` | flat at close, TP 30% | same | 1301 | 23.8% | 1.19 | -45% | 1.16 |
| `..._v11_confirm3` | v10 + flip needs 3 bars | same | 1093 | 23.3% | 1.19 | -34% | 1.16 |
| `..._v12_no_eod` | v11 holding overnight | same | 988 | 24.0% | 1.20 | -52% | 0.99 |
| `..._v13_no_tp` | v12 without target (SL kept) | same | 565 | 30.7% | 1.38 | -40% | 1.35 |
| `..._v14_no_tp_no_sl` | v12 without target or stop | same | 554 | 33.0% | 1.44 | -42% | 1.36 |
| `..._v11_5min` | v11 on `data_5min/` | 2020-11 -> 2026-11 | 1650 | 19.9% | 1.19 | -22.8% | 1.31 |
| `..._v11_hourly` | v11 on hourly files, TP removed | 2021-11 -> 2026-11 | 616 | 5.4% | 1.06 | -41% | 0.39 |

Findings: overnight trades lost money (-0.8M vs +2.6M intraday on v8); TrendChange exits were the largest loser (20% win rate); profit of v13/v14 comes from trades held to expiry and ~18 of those use `NoData` exits priced at the last quote (least reliable part);
confirm=3 looks like a lucky point (4 and 6 worse, 2 similar); 2023-24 are flat/negative in nearly every variant; hourly bars have no real edge (an untested variant, hold overnight + no target, reached 13.2% CAGR / Sharpe 0.81).
Earlier minute-data work: v3-v7 (OI-only direction, IV filter was tested then **removed**, deep ITM v6 judged unreliable because strikes drop out of the ATM+-10 window, v9 `itm_steps`).

## 5. Live collector (this repo) — `script-data/live_collector.py`

Runs continuously (`python script-data/live_collector.py`, or `.\script-data\run_live_collector.bat` which restarts it after any exit). `--login`, `--once`, `--dry-run`, `--backfill-only`, `--config-dir`.

**Config (`configurations/live/`)**: `live_common.yaml` (kite limits/token file, `output.root: data`, session 09:15-15:30, poll 60 s + 5 s delay, `holidays`, `extra_days`, logging, lock port, instrument definitions,
`backfill`, `state_file`) plus one file per basket: `live_minute.yaml`, `live_5minute.yaml`, `live_hourly.yaml`, `live_daily.yaml` (`enabled`, `folder`, `kite_interval`, `bar_minutes`, `instruments`, `overrides`, optional `start_date`).

**Behaviour (all simulated, partly confirmed by the user's real run)**
- Baskets/folders: `data/data_minute`, `data_5minute`, `data_hourly`, `data_daily`. Per-day files `nifty_<yyyymmdd>.csv`, `nifty_fut_<yyyymmdd>.csv`, `nifty_options_<yyyymmdd>.csv`; daily basket `*_all.csv`. Same columns as the history files; options `iv` empty; index volume = NIFTYBEES volume.
- Each run fetches from the last saved bar to now and keeps only closed bars, so restarts/outages self-heal and duplicates are impossible (dedupe keys include `expiry_code` for options). Atomic temp-file writes.
- A basket runs only when a new bar of its size has closed (09:15-anchored; the short last hourly bar closes at 15:30). Daily basket runs after the close.
- Options: expiries collected = `options.expiries` (now codes 1, 2, 3 = current week, next, week after) at **ATM+-15** (`strike_min/max`, per-entry overrides allowed). `strike_offset` is computed per bar from the index close and ATM rounding to 50.
  `every`/`phase` per expiry refresh it only every N-th run (used in `live_minute.yaml`: code 1 every minute, codes 2 and 3 every 4th minute, 2 minutes apart) to respect Kite's 3 req/s (3 x 62 contracts = 186 requests = ~66 s otherwise).
- **Top-up at every start and once per later day** from `backfill.start_date` (2026-11-01): finds days whose files are missing/incomplete (bar count per day, per expiry code, and strike window reaches the configured edges) and downloads them.
  Index/futures: any day back to the start date (futures valid only after the previous monthly expiry). Options: which live contract held expiry code k on past day D is `live_expiries[k - 1 - n]` with `n = floor((first_live_expiry - D).days / 7)`; entries before the live list are unavailable (expired), logged once and remembered in `state/collector_state.json`.
  Assumes weekly cadence; a holiday shift can make it wrong by one expiry (worked for 2026-11-04..09). Holidays are detected (weekday with no candles; or no candle 15 minutes into the live session) and skipped.
- Token handling: a missing/rejected token is logged once; run `--login` in another window and the loop picks it up. Single instance via a local TCP port. Log: `logs/live_collector.log` (daily rotation, 60 days), one line per cycle.
- Request budget (`--dry-run` prints it): 1-minute basket ~96 requests/run average, 5-minute and hourly ~189 each, so a minute where several baskets are due can exceed 60 s; late cycles only delay the next one (nothing lost).

**What actually happened on 2026-11-10 (real run, user-supplied log)**: Kite login worked, instruments loaded (10,442 NSE, 41,665 NFO), index/futures top-up for 2-9 Nov succeeded (+2,320 index rows in the minute basket, etc.), the Muhurat Sunday 2026-11-08 session was also saved, 
and options for 2-9 Nov were unavailable because the weekly contract had expired 2026-11-09. Today (Tue 11-10) was a market holiday: cycles logged `nothing due`. The expiries-1/2/3 and ATM+-15 changes were made **after** that run and have **not** been run against real Kite.

## 6. Open items / suggested next steps (priority order)
1. **Run the live collector on a real trading day (Wed 2026-11-11 or later)** and verify against Dhan data: row counts per bar (expect 62 + 62 + 62 for codes 1/2/3 on the 5-minute/hourly baskets, code 1 every minute), no gaps, `strike_offset` range -15..15, log lines, request budget vs 60 s.
   Check that `historical_data` with `oi=True` works for option contracts and that Kite returns candles with tz-aware IST dates (code assumes both).
2. Decide how live files feed the backtester/strategy: merge `data/data_*` daily files into the history CSVs (same schema, but note codes 2/3 and ATM+-15 vs Dhan's +-10/+-3), or point the strategy at them. Not built.
3. Fill the option gap 2-9 Nov (and any weeks the collector misses) from Dhan: `python update_data.py --minute`, then `python repair_hourly.py`, `python make_5min.py`.
4. Run the Kite futures history (`config_download_5min.yaml --only futures`; **not run**) and, if wanted, 1-minute futures.
5. Forward-test the strategy: fit on 2020-11..2026-04, test untouched on 2026-05..11 (v11/v13 settings were selected in-sample). Add position sizing by volatility/risk (single stop-loss ~4% of capital), test "carry overnight only if in profit" (`session.overnight: winners`), re-check `NoData` exits in v13/v14.
6. Move data cleaning (out-of-session bars, OI=0, IV<=0) into `strategy.py`; handle expiry-2/3 options for the strategy (the minute files only hold expiry 1).
7. Hourly strategy: current results are weak; test hold-overnight/no-target hourly variant before relying on it.
8. Robustness: holiday calendar for NSE (list in `schedule.holidays`), alerting when the Kite token is missing at 09:10, packaging the collector as a scheduled task (Windows Task Scheduler starting `run_live_collector.bat` at ~09:00 after the daily login).

## 7. Commands the user runs (PowerShell)

```powershell
# history repo (C:\Trading-Application\first-project)
python update_data.py --minute --five-minute --futures      # needs `python download_data.py --login` first for the Kite steps
python repair_hourly.py ; python make_5min.py
python strategy.py --config configurations/config_skew_mom_v11_5min.yaml
python download_data.py --config configurations/config_download_5min.yaml --only futures

# this repo (C:\Trading-Application\first-project-data)
python script-data/live_collector.py --login        # once a day before 09:15
python script-data/live_collector.py --dry-run
.\script-data\run_live_collector.bat
```

## 8. How to test without network
`script-data/live_collector.py` takes its Kite client from `KiteSession`; the previous session tested with a `FakeKite` (methods `profile()`, `instruments(exchange)`, `historical_data(token, from, to, interval, continuous, oi)`,
returning candle dicts with tz-aware IST `date`) and a `FakeSession`, a temp output folder, and calls `Collector.run_cycle(now)` with chosen datetimes (start-up top-up, restart, token loss, holiday, next day). Re-create that harness before changing the collector.
Fake instruments need: NSE `NIFTY 50` and `NIFTYBEES`; NFO option rows (`instrument_token, tradingsymbol, name, expiry(date), strike, instrument_type CE/PE`) and `FUT` rows.

## 9. Style of work the user prefers
Short direct answers; plain-language summaries first, details in tables; explicit commands to copy; admit what was not run. The user is a trader, comfortable with PowerShell but not with Python internals.
