# Live collector: Kite data every minute

All commands run from this folder (`first-project-data`) in PowerShell.
Runs continuously, and each minute downloads every candle that has just closed for all baskets (1-minute, 5-minute, hourly,
daily) and all instruments (Nifty index, front-month futures, options of the current, next and next-next weekly expiry at ATM+-15, `expiry_code` 1/2/3), straight from Kite Connect.
```powershell
python live_collector.py --login     # Kite login, once a day before the open (token expires ~6 AM). A running collector picks it up
python live_collector.py --dry-run   # plan: baskets, output folders, requests per cycle
python live_collector.py             # run until Ctrl+C (sleeps outside 09:15-15:30 and on weekends)
python live_collector.py --once      # one cycle now (also downloads the day so far), then exit
.\run_live_collector.bat             # same, restarted automatically if it ever exits (PowerShell needs the .\ in front)
```
- **Start date and top-up (every run):** at start-up, and once on each later day, the collector looks at every basket's files from
  `backfill.start_date` (default `2026-11-01`, in `live_common.yaml`; a basket file can set its own) up to yesterday, downloads
  each day that is missing or incomplete, and only then goes live. Complete days are never fetched again, so restarting is always safe.
  `python live_collector.py --backfill-only` does just the top-up and exits.
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
  Three expiries x 62 contracts is 186 requests (~66 s), more than a one-minute cycle allows, so `live_minute.yaml` refreshes
  expiries 2 and 3 only every 4th minute (`every: 4`, staggered with `phase`); each fetch catches up on every bar since the last,
  so nothing is lost, those two expiries just arrive up to 4 minutes late. The 5-minute and hourly baskets fetch all three every run.
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
  `.kite_token.json` in THIS folder (git-ignored), written by `python live_collector.py --login`; it is separate from the one in `first-project`.
- **Config location:** `configurations/live/`. `--config-dir` points at another folder.
