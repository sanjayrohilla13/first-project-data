# Live collector: Kite data every minute

All commands run from this folder (`first-project-data`) in PowerShell.
Runs continuously, and each minute downloads every candle that has just closed for all baskets (1-minute, 5-minute, hourly,
daily) and all instruments (Nifty index, front-month futures, options of the current, next and next-next weekly expiry at ATM+-25, `expiry_code` 1/2/3), straight from Kite Connect.
```powershell
python script-data/live_collector.py --login     # Kite login, once a day before the open (token expires ~6 AM). A running collector picks it up
python script-data/live_collector.py --dry-run   # plan: baskets, output folders, requests per cycle
python script-data/live_collector.py             # run until Ctrl+C (sleeps outside 09:15-15:30 and on weekends)
python script-data/live_collector.py --once      # one cycle now (also downloads the day so far), then exit
.\script-data\run_live_collector.bat             # same, restarted automatically if it ever exits (PowerShell needs the .\ in front)
```
- **Start date and top-up (every run):** at start-up, and once on each later day, the collector looks at every basket's files from
  `backfill.start_date` (default `2026-11-01`, in `live_common.yaml`; a basket file can set its own) up to yesterday, downloads
  each day that is missing or incomplete, and only then goes live. Complete days are never fetched again, so restarting is always safe.
  `python script-data/live_collector.py --backfill-only` does just the top-up and exits.
  - Kite only serves **live contracts**, so for options/futures it can only fill days whose contract has not expired yet. Today
    (Tuesday 10 Nov 2026, expiry day) it can still fill 4-9 Nov options, but not 2-3 Nov (that weekly contract has expired); each
    unavailable day is logged once and remembered in `state/collector_state.json`. **Start the collector before the weekly expiry
    passes, ideally every day**: a day not collected while its contract is live is lost to Kite for good (the Dhan history downloads
    can fill it).
  - Example (10 Nov 2026): Tuesday was a market holiday, so the weekly options expired on Monday 9 Nov. From then on Kite lists only
    the 17 Nov contract, so the options of 2-9 Nov cannot be downloaded from Kite (the log says so once per basket); index and futures
    were filled and option collection starts with the next trading day. Take those days from the Dhan history (`first-project`).
  - Market holidays are detected automatically (a weekday with no candles, or no candle 15 minutes into the live session) and skipped from then on; you can also list them in
    `schedule.holidays`.
- **Options window:** `options.strike_min/strike_max` in `live_common.yaml` (now -15/+15) for each entry of `options.expiries`
  (`{code: 1}`, `{code: 2}`, `{code: 3}`; each can override the window). Widening the window later is picked up automatically:
  the top-up refetches days whose strikes do not reach the new edges, and today's new contracts are filled from the open.
  Three expiries x 102 contracts is 306 requests (~110 s), more than a one-minute cycle allows, so `live_minute.yaml` fetches
  expiry 2 (the one the strategy trades) every minute and expiries 1 and 3 only every 6th minute (`every: 6`, staggered with `phase`);
  each fetch catches up on every bar since the last, so nothing is lost, those two expiries just arrive up to 6 minutes late. The 5-minute and hourly baskets fetch all three every run.
- **Config** (`configurations/live/`): `live_common.yaml` (Kite limits, output folder, session times, holidays, logging, what each
  instrument kind is, strike range) and one file per basket: `live_minute.yaml`, `live_5minute.yaml`, `live_hourly.yaml`,
  `live_daily.yaml` (`enabled`, `folder`, `kite_interval`, `bar_minutes`, which kinds, `overrides`). Add a basket by copying a file
  and listing it under `basket_files`. Set `schedule.holidays` for market holidays.
- **Output:** `output.root` (default `data`, i.e. this folder's `data/`) / the basket folder (`data_minute`,
  `data_5minute`, `data_hourly`, `data_daily`): `nifty_<yyyymmdd>.csv`, `nifty_fut_<yyyymmdd>.csv`, `nifty_options_<yyyymmdd>.csv`
  (daily basket: `nifty_all.csv`, `nifty_fut_all.csv`), with the same columns as the history files. Options have `iv` empty (Kite has none).
- **Log:** `logs/live_collector.log` (in this folder), one line per cycle (rows added per basket and kind, requests, seconds, errors), rolled over each
  midnight and kept for 60 days. Warnings name the instrument that failed.
- **Safe to stop/restart/lose network:** every run fetches from the last saved bar to now and keeps only closed candles, so gaps fill by
  themselves and nothing is duplicated. Only one collector can run (a local port is held).
- **Limits:** Kite allows 3 historical requests a second. A normal minute is ~45 requests (~16 s); a minute where all baskets are due
  (10:15, 11:15, ...) is ~135 (~48 s). For wider strikes (-20..+20) or a second expiry, raise `strike_min/max` / `expiries` and expect
  proportionally longer cycles. The collector writes its own daily files; it does not touch the large history files.
- Kite only serves live contracts, so options/futures come from the current expiry. This does not replace the Dhan history downloads.

- **Credentials:** `KITE_API_KEY` and `KITE_API_SECRET` in the environment (see the `first-project` readme, "Kite"). The daily token is kept in
  `.kite_token.json` in THIS folder (git-ignored), written by `python script-data/live_collector.py --login`; it is separate from the one in `first-project`.
- **Config location:** `configurations/live/`. `--config-dir` points at another folder.

## Running the strategy on the collected data (`script-strategy/`)

```
python script-strategy/run_strategy.py                       # uses configurations/strategy/config_skew_mom_5min.yaml
python script-strategy/run_strategy.py --config configurations/strategy/config_skew_mom_5min.yaml --open
```

`script-strategy/run_strategy.py` imports the trading logic from `first-project/strategy.py` (path in `live_data.strategy_py`),
so entries/exits are exactly the backtested ones. It reads `data/data_5minute` (per-day files), picks the expiry
(`live_data.expiry_code`: 1/2/3 or `nearest`), solves the IV Kite does not provide (ATM and strikes +-2) and writes
`results/skew_mom_5min/` (trades.csv, equity_curve.csv, signals.csv, report.html). It also logs the latest bar's signal.

Limits: the strategy picks a short leg with premium 100-140 and a hedge under Rs 10, which exists only for the CURRENT-week
expiry (code 1). Until the collector has days with code 1 (from the first trading day after 10 Nov), the data holds only codes
2/3 and the default config places no trades (the signals are still computed). Tune `spread.short_premium` / `hedge_max_premium`
if you want to trade the next-week contract.

## Start-up sequence

On every start (`python script-data/live_collector.py` or the .bat) the collector: (1) checks today's Kite token; if it is missing or
rejected it shows the login URL and asks for the `request_token` (paste the token or the whole redirect URL; Enter skips, and
the collector then waits for `--login` in another window); (2) backfills everything missing since the last download (days
and strikes that are missing or incomplete, from `backfill.start_date`), even when the market is closed; (3) goes live,
one cycle per minute. The token is not re-asked during the run: if Kite rejects it later the log says so and a `--login` in
another window is picked up automatically.

### Continuous strategy run and trade log

```
python script-strategy/run_strategy.py --config configurations/strategy/config_skew_mom_v9_itm_steps.yaml --live
```

Run it next to the collector. Whenever the collector saves a new bar (checked every `live.poll_seconds`, default 15), the strategy is
re-run on all the data and the screen shows a one-line status per bar (latest signal, open position and its unrealised P&L) plus a block for
every new trade: `TRADE OPEN` (legs, prices, quantity, credit, stop and target) and `TRADE CLOSE` (exit cost, P&L, % of capital, reason).
The same events are appended to the trade log CSV (`live.trade_log`, default `results/<name>/trade_log.csv`) and to `logs/run_strategy_live.log`.

- Restart-safe: the log is read at start, so nothing is logged twice. Trades already in the data when the run starts are not logged unless
  `live.log_history: true`; a position that is open at start-up is logged as OPEN ("open at start-up").
- Entries and exits are the backtested ones, so a trade is detected when the bar it fills on has been saved (one bar after the signal bar).
  This is a signal/paper log, not an order engine: nothing is sent to a broker.
- The strategy closes whatever is open at the last bar of the data; during a live run that is not a real exit, so such a trade is shown as
  still open. An expiry exit is confirmed (and logged) when the next bar arrives.

### Running the strategy on 1-minute data

```
python script-strategy/run_strategy.py --config configurations/strategy/config_skew_mom_v9_itm_steps_1min.yaml --live           # continuous
python script-strategy/run_strategy.py --config configurations/strategy/config_skew_mom_v9_itm_steps_1min.yaml --start 2026-11-04   # one run
python script-strategy/run_strategy.py --config configurations/strategy/config_skew_mom_v9_itm_steps_1min.yaml --expiry-code 1 --start 2025-01-01 --end 2025-03-31   # history
```

`--expiry-code`, `--start`, `--end` and `--lookback-days` override the `live_data` settings. Only the traded expiry is read from the option files
(years of 1-minute options are tens of millions of rows), and a `--live` run uses just the newest `live.lookback_days` (default 20) days, so each
re-run takes seconds. A quarter of history (61 days, 23k bars) takes about 35 s; the full 5 years is long, run it in slices.

## Commands (quick reference)

Run from `C:\Trading-Application\first-project-data` in PowerShell.

```
# --- data collector (Kite) ---
python script-data/live_collector.py --login            # Kite login only (it also asks for the login by itself at start-up when the token is invalid)
.\script-data\run_live_collector.bat                    # continuous: token check -> backfill -> live every minute; restarts itself after any exit
python script-data/live_collector.py                    # same without the automatic restart
python script-data/live_collector.py --backfill-only    # just fill the missing history, then exit
python script-data/live_collector.py --once             # one cycle now, then exit
python script-data/live_collector.py --dry-run          # show the baskets, files and requests per cycle

# --- strategy on the collected data: continuous, with screen output and trade log ---
python script-strategy/run_strategy.py --config configurations/strategy/config_skew_mom_v9_itm_steps_1min.yaml --live    # 1-minute bars
python script-strategy/run_strategy.py --config configurations/strategy/config_skew_mom_v9_itm_steps.yaml --live         # 5-minute bars built from the 1-minute files

# --- strategy: one run over the data (trades.csv, equity_curve.csv, signals.csv, report.html in results/) ---
python script-strategy/run_strategy.py --config configurations/strategy/config_skew_mom_v9_itm_steps_1min.yaml --start 2026-11-04
python script-strategy/run_strategy.py --config configurations/strategy/config_skew_mom_v9_itm_steps_1min.yaml --expiry-code 1 --start 2025-01-01 --end 2025-03-31
python script-strategy/run_strategy.py --config configurations/strategy/config_skew_mom_5min_next_week.yaml --folder data/data_minute --timeframe 5min
```

Stop any of them with Ctrl+C. The trade log of a live run is `results/<config name>/trade_log.csv`; the collector's log is `logs/live_collector.log`,
the strategy's `logs/run_strategy_live.log`.
