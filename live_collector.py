"""
Continuous data collector for Zerodha Kite Connect: every minute it downloads whatever candles have closed, for every
basket (1-minute, 5-minute, hourly, daily) and every instrument kind (Nifty index, Nifty futures, Nifty weekly options),
appends them to CSV files and writes a log.

    python live_collector.py --login        # Kite login, once a day before the market opens (access tokens expire ~6 AM)
    python live_collector.py                # run until stopped (Ctrl+C); sleeps outside market hours
    python live_collector.py --once         # a single cycle now, then exit (also downloads the day so far)
    python live_collector.py --dry-run      # show baskets, files and the request budget, then exit

Settings: configurations/live/live_common.yaml (Kite, schedule, logging, instruments, output folder) plus one file per
basket next to it (live_minute.yaml, live_5minute.yaml, live_hourly.yaml, live_daily.yaml).

How it stays correct
  * Each cycle fetches from the last saved bar of every series to now and keeps only CLOSED bars, so a missed cycle, a
    restart or a network drop is filled by the next one (no gaps, no half-built candles, no duplicates).
  * A basket only runs when a new bar of its size has closed (5-minute basket: 09:20, 09:25, ...; hourly: 10:15, ... and
    the 15:30 close for the last 15:15 bar; daily: after the close).
  * Options: the contracts within ATM +- N strikes of the current index price for the nearest expiry(ies); strike_offset
    is worked out per bar from the index close, like the Dhan files. iv is empty (Kite has none).
  * A lost token is picked up again without restarting: run `python live_collector.py --login` in another window.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import math
import os
import socket
import sys
import time
from copy import deepcopy
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

HERE = Path(__file__).resolve().parent
CONFIG_DIR = HERE / "configurations" / "live"
IST = ZoneInfo("Asia/Kolkata")
TZ = "Asia/Kolkata"
log = logging.getLogger("live_collector")

INDEX_COLS = ["datetime", "open", "high", "low", "close", "volume"]
FUT_COLS = INDEX_COLS + ["oi"]
OPTION_COLS = ["datetime", "option_type", "strike_offset", "strike", "spot", "open", "high", "low", "close",
               "volume", "oi", "iv", "expiry_flag", "expiry_code"]
FILE_STEM = {"index": "nifty", "futures": "nifty_fut", "options": "nifty_options"}
KEYS = {"index": ["datetime"], "futures": ["datetime"], "options": ["datetime", "option_type", "strike", "expiry_code"]}


# ─────────────────────────────── configuration ───────────────────────────────
def _hhmm(s: str) -> dtime:
    h, m = str(s).split(":")
    return dtime(int(h), int(m))


def load_config(config_dir: Path) -> dict:
    common = yaml.safe_load((config_dir / "live_common.yaml").read_text(encoding="utf-8"))
    baskets = []
    for name in common.get("basket_files", []):
        b = yaml.safe_load((config_dir / name).read_text(encoding="utf-8"))
        if not b.get("enabled", True):
            continue
        for k in ("name", "folder", "kite_interval", "bar_minutes", "instruments"):
            if k not in b:
                raise SystemExit(f"{name}: missing '{k}'")
        ins = deepcopy(common["instruments"])
        for kind, over in (b.get("overrides") or {}).items():
            ins.setdefault(kind, {}).update(over)
        b["settings"] = ins
        b["kinds"] = [k for k in ("index", "futures", "options") if b["instruments"].get(k)]
        if "options" in b["kinds"] and "index" not in b["kinds"]:
            raise SystemExit(f"{name}: options need the index kind too (it gives the spot price for the strikes)")
        baskets.append(b)
    common["baskets"] = baskets
    bf = common.setdefault("backfill", {})
    bf.setdefault("enabled", True)
    bf.setdefault("start_date", "2026-11-01")
    bf.setdefault("chunk_days", {"minute": 55, "5minute": 95, "60minute": 380, "day": 1900})
    bf.setdefault("expiry_weekday", 1)
    common["session_start"] = _hhmm(common["schedule"]["session_start"])
    common["session_end"] = _hhmm(common["schedule"]["session_end"])
    return common


def setup_logging(cfg: dict) -> None:
    lc = cfg["logging"]
    d = Path(lc.get("dir", "logs"))
    d = d if d.is_absolute() else HERE / d
    d.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(lc.get("level", "INFO")).upper(), logging.INFO))
    fh = logging.handlers.TimedRotatingFileHandler(d / lc.get("file", "live_collector.log"), when="midnight",
                                                   backupCount=int(lc.get("keep_days", 60)), encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if lc.get("console", True):
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        root.addHandler(ch)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("kiteconnect").setLevel(logging.WARNING)


# ─────────────────────────────────── Kite ───────────────────────────────────
class KiteSession:
    """Kite client built from the token file; a bad or missing token is re-read on the next cycle."""

    def __init__(self, kcfg: dict):
        self.path = Path(kcfg["token_file"])
        self.path = self.path if self.path.is_absolute() else HERE / self.path
        self.client = None
        self._token = None

    def _read_token(self) -> str | None:
        env = os.environ.get("KITE_ACCESS_TOKEN")
        if env:
            return env
        try:
            c = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return c.get("access_token") if c.get("date") == datetime.now(IST).strftime("%Y-%m-%d") else None

    def get(self):
        """A ready Kite client, or None when there is no valid token for today."""
        tok = self._read_token()
        if tok is None:
            self.client = None
            return None
        if self.client is not None and tok == self._token:
            return self.client
        key = os.environ.get("KITE_API_KEY")
        if not key:
            raise SystemExit("KITE_API_KEY is not set (see the readme: Kite credentials).")
        from kiteconnect import KiteConnect
        k = KiteConnect(api_key=key)
        k.set_access_token(tok)
        try:
            k.profile()
        except Exception as e:                                   # noqa: BLE001 (any failure = unusable token)
            log.error(f"Kite rejected the access token ({type(e).__name__}: {e}). "
                      "Run: python live_collector.py --login")
            self.client = None
            return None
        self.client, self._token = k, tok
        log.info("Kite session ready")
        return k

    def invalidate(self) -> None:
        self.client = None


def kite_login(kcfg: dict) -> None:
    from kiteconnect import KiteConnect
    key, secret = os.environ.get("KITE_API_KEY"), os.environ.get("KITE_API_SECRET")
    if not (key and secret):
        raise SystemExit("Set KITE_API_KEY and KITE_API_SECRET first (see the readme: Kite credentials).")
    k = KiteConnect(api_key=key)
    print(f"1. Open and log in:\n   {k.login_url()}")
    print("2. After login you are redirected to a URL containing ?request_token=XXXX")
    rt = input("3. Paste the request_token here: ").strip()
    s = k.generate_session(rt, api_secret=secret)
    p = Path(kcfg["token_file"])
    p = p if p.is_absolute() else HERE / p
    p.write_text(json.dumps({"date": datetime.now(IST).strftime("%Y-%m-%d"), "access_token": s["access_token"]}))
    print(f"Access token saved to {p} (valid for today only). The running collector picks it up by itself.")


class Throttle:
    def __init__(self, per_second: float):
        self.gap = 1.0 / max(per_second, 0.1)
        self.last = 0.0

    def wait(self) -> None:
        d = self.last + self.gap - time.monotonic()
        if d > 0:
            time.sleep(d)
        self.last = time.monotonic()


class TokenLost(Exception):
    pass


# ─────────────────────────────────── bar maths ───────────────────────────────
def session_bounds(cfg: dict, day: date) -> tuple[datetime, datetime]:
    return datetime.combine(day, cfg["session_start"]), datetime.combine(day, cfg["session_end"])


def latest_closed_start(cfg: dict, now: datetime, bar: int) -> datetime | None:
    """Start time of the newest intraday bar that has closed by `now` (09:15-anchored; the last bar may be short)."""
    op, cl = session_bounds(cfg, now.date())
    total = int((cl - op).total_seconds() // 60)
    elapsed = int((min(now, cl) - op).total_seconds() // 60)
    if elapsed < 0:
        return None
    full = elapsed // bar
    if now >= cl and total % bar:
        full = math.ceil(total / bar)          # the short final bar (15:15-15:30 for hourly) closes with the session
    if full <= 0:
        return None
    return op + timedelta(minutes=(full - 1) * bar)


def drop_open_bars(df: pd.DataFrame, cfg: dict, now: datetime, bar: int) -> pd.DataFrame:
    """Keep only bars that have finished (bar end <= now; the last bar of the day ends at the session close)."""
    if df.empty:
        return df
    start = df["datetime"]
    end = start + pd.Timedelta(minutes=bar)
    close = start.dt.normalize() + pd.Timedelta(hours=cfg["session_end"].hour, minutes=cfg["session_end"].minute)
    return df[(end.where(end <= close, close) <= pd.Timestamp(now)).to_numpy()]


def build_option_rows(df: pd.DataFrame, spot: pd.Series, step: int, lo: int, hi: int, flag: str, code: int) -> pd.DataFrame:
    """Contract candles -> rows like the Dhan files: ATM from the index close of each bar, offset = strikes from ATM."""
    df = df.copy()
    df["spot"] = df["datetime"].map(spot)
    df = df.dropna(subset=["spot"])
    atm = (df["spot"] / step).round() * step
    df["strike_offset"] = ((df["strike"] - atm) / step).round().astype(int)
    df = df[(df["strike_offset"] >= lo) & (df["strike_offset"] <= hi)]
    df["iv"] = float("nan")                                  # Kite has no implied volatility
    df["expiry_flag"], df["expiry_code"] = flag, int(code)
    return df


def expiry_specs(oc: dict) -> list[dict]:
    """options.expiries -> [{code, lo, hi}]. An entry is a number (1 = current week, 2 = next week, 3 = the one after) or
    {code: 2, strike_min: -3, strike_max: 3, every: 4, phase: 0}; missing strike limits fall back to the options section's.
    every/phase spread the load: that expiry is refreshed on every `every`-th run of the basket (counted from the start,
    shifted by `phase`), fetching everything since its last saved bar, so no bar is lost, it only arrives a few runs later."""
    out = []
    for e in oc.get("expiries", [1]):
        d = dict(e) if isinstance(e, dict) else {"code": int(e)}
        out.append({"code": int(d["code"]),
                    "lo": int(d.get("strike_min", oc["strike_min"])), "hi": int(d.get("strike_max", oc["strike_max"])),
                    "every": max(int(d.get("every", 1)), 1), "phase": int(d.get("phase", 0))})
    return out


def prev_monthly_expiry(e: date, weekday: int = 1) -> date:
    """The monthly expiry before `e`: the last `weekday` (Tuesday = 1) of the previous month."""
    d = e.replace(day=1) - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


# ─────────────────────────────────── storage ───────────────────────────────
class Store:
    def __init__(self, root: str):
        r = Path(root)
        self.root = r if r.is_absolute() else HERE / r

    def path(self, folder: str, kind: str, day: date | None) -> Path:
        d = self.root / folder
        d.mkdir(parents=True, exist_ok=True)
        return d / (f"{FILE_STEM[kind]}_{day:%Y%m%d}.csv" if day else f"{FILE_STEM[kind]}_all.csv")

    def append(self, folder: str, kind: str, df: pd.DataFrame, daily: bool) -> int:
        """Merge `df` (naive IST datetimes) into the per-day files. Returns the number of new rows."""
        if df.empty:
            return 0
        cols = {"index": INDEX_COLS, "futures": FUT_COLS, "options": OPTION_COLS}[kind]
        out = df[cols].copy()
        out["datetime"] = out["datetime"].dt.tz_localize(TZ).map(lambda t: t.isoformat(sep=" "))
        keys = KEYS[kind]
        new_rows = 0
        groups = [(None, out)] if daily else [(d, g) for d, g in out.groupby(pd.to_datetime(out["datetime"].str[:10]).dt.date)]
        for day, g in groups:
            p = self.path(folder, kind, day)
            old = pd.read_csv(p, dtype={"datetime": str}) if p.exists() and p.stat().st_size else None
            merged = pd.concat([old, g], ignore_index=True) if old is not None else g
            merged = merged.drop_duplicates(keys, keep="last").sort_values(keys).reset_index(drop=True)
            new_rows += len(merged) - (len(old) if old is not None else 0)
            tmp = p.with_name(p.name + ".tmp")
            merged.to_csv(tmp, index=False)
            os.replace(tmp, p)
        return new_rows

    def read_day(self, folder: str, kind: str, day: date | None) -> pd.DataFrame | None:
        p = self.path(folder, kind, day)
        if not (p.exists() and p.stat().st_size):
            return None
        d = pd.read_csv(p, dtype={"datetime": str})
        d["datetime"] = pd.to_datetime(d["datetime"].str[:19])           # naive IST
        return d


# ─────────────────────────────────── collector ───────────────────────────────
class Collector:
    def __init__(self, cfg: dict, session: KiteSession):
        self.cfg, self.session = cfg, session
        self.store = Store(cfg["output"]["root"])
        self.throttle = Throttle(cfg["kite"].get("max_requests_per_second", 2.8))
        self.requests = 0
        self.errors: list[str] = []
        self.last: dict[tuple, pd.Timestamp] = {}          # last saved bar per series
        self._since: dict[str, datetime] = {}               # per basket: start of this cycle's index window (catch-up range)
        self.spot: dict[str, pd.Series] = {}               # per basket: index close by bar start, for the option strikes
        self._inst_day: date | None = None
        self.nse = self.nfo = None
        self.warned_empty: date | None = None
        sf = Path(cfg.get("state_file", "state/collector_state.json"))
        self.state_path = sf if sf.is_absolute() else HERE / sf
        self._state: dict | None = None
        self._topup_day: date | None = None
        self._opt_runs: dict[tuple, int] = {}              # runs of each (basket, expiry code), for the refresh rhythm

    # ---- instruments (refreshed once a day) ----
    def instruments(self, kite, day: date) -> None:
        if self._inst_day == day:
            return
        self.nse = pd.DataFrame(self._call(kite.instruments, "NSE"))
        self.nfo = pd.DataFrame(self._call(kite.instruments, "NFO"))
        self.nfo["expiry"] = pd.to_datetime(self.nfo["expiry"]).dt.date
        self._inst_day = day
        log.info(f"Instruments loaded: {len(self.nse)} NSE, {len(self.nfo)} NFO")

    def nse_token(self, symbol: str) -> int:
        r = self.nse[self.nse["tradingsymbol"] == symbol]
        if r.empty:
            raise RuntimeError(f"{symbol} not found among NSE instruments")
        return int(r.iloc[0]["instrument_token"])

    def _call(self, fn, *a, **kw):
        """A Kite call with throttle and retries; raises TokenLost when the token is rejected."""
        from kiteconnect.exceptions import TokenException
        retries, wait = int(self.cfg["kite"].get("retries", 3)), float(self.cfg["kite"].get("retry_wait_seconds", 2))
        for attempt in range(1, retries + 1):
            self.throttle.wait()
            self.requests += 1
            try:
                return fn(*a, **kw)
            except TokenException as e:
                raise TokenLost(str(e)) from e
            except Exception as e:                              # noqa: BLE001
                if attempt == retries:
                    raise
                log.debug(f"retry {attempt}/{retries} after {type(e).__name__}: {e}")
                time.sleep(wait * 2 ** (attempt - 1))

    def history(self, kite, token: int, start: datetime, end: datetime, interval: str, oi: bool = False) -> pd.DataFrame:
        c = self._call(kite.historical_data, token, start, end, interval, False, oi)
        if not c:
            return pd.DataFrame()
        df = pd.DataFrame(c).rename(columns={"date": "datetime"})
        dt = pd.to_datetime(df["datetime"])
        df["datetime"] = (dt.dt.tz_convert(TZ) if dt.dt.tz is not None else dt.dt.tz_localize(TZ)).dt.tz_localize(None)
        if "oi" not in df.columns:
            df["oi"] = 0
        return df.sort_values("datetime").reset_index(drop=True)

    # ---- window of a series ----
    def _from(self, key: tuple, basket: dict, now: datetime, daily: bool) -> datetime:
        if daily:
            return datetime.combine(now.date() - timedelta(days=int(basket.get("lookback_days", 7))), dtime(0, 0))
        last = self.last.get(key)
        if last is not None and last.date() == now.date():
            return (last + pd.Timedelta(minutes=basket["bar_minutes"])).to_pydatetime()
        return session_bounds(self.cfg, now.date())[0]

    # ---- one basket ----
    def run_basket(self, kite, basket: dict, now: datetime) -> dict:
        bar, daily = int(basket["bar_minutes"]), basket["kite_interval"] == "day"
        folder, iv = basket["folder"], basket["kite_interval"]
        _, close = session_bounds(self.cfg, now.date())
        if daily:
            done = self.last.get((basket["name"], "index"))
            if now < close or (done is not None and done.date() >= now.date()):
                return {}
        elif latest_closed_start(self.cfg, now, bar) is None:
            return {}
        added: dict[str, int] = {}
        st = basket["settings"]

        # --- index (also gives the spot for the option strikes) ---
        spot = self.spot.setdefault(basket["name"], pd.Series(dtype=float))
        if "index" in basket["kinds"] or "options" in basket["kinds"]:
            ic = st["index"]
            key = (basket["name"], "index")
            if key not in self.last:
                self._warm(basket, "index", now, daily)
            frm = self._from(key, basket, now, daily)
            self._since[basket["name"]] = frm
            if daily or latest_closed_start(self.cfg, now, bar) >= pd.Timestamp(frm):
                try:
                    px = self.history(kite, self.nse_token(ic["symbol"]), frm, now, iv)
                    px = px if daily else drop_open_bars(px, self.cfg, now, bar)   # daily: only run after the close
                    if not px.empty and ic.get("volume_proxy"):
                        pv = self.history(kite, self.nse_token(ic["volume_proxy"]), frm, now, iv)
                        px["volume"] = px["datetime"].map(pv.set_index("datetime")["volume"]).fillna(0).astype(float) \
                            if not pv.empty else 0.0
                    elif not px.empty:
                        px["volume"] = 0.0
                    if not px.empty:
                        spot = pd.concat([spot, px.set_index("datetime")["close"]])
                        spot = spot[~spot.index.duplicated(keep="last")].sort_index()
                        self.spot[basket["name"]] = spot
                        self.last[key] = px["datetime"].max()
                        if "index" in basket["kinds"]:
                            added["index"] = self.store.append(folder, "index", px, daily)
                except TokenLost:
                    raise
                except Exception as e:                           # noqa: BLE001
                    self.errors.append(f"{basket['name']}/index: {type(e).__name__}: {e}")

        # --- futures ---
        if "futures" in basket["kinds"]:
            try:
                added["futures"] = self._futures(kite, basket, now, daily)
            except TokenLost:
                raise
            except Exception as e:                               # noqa: BLE001
                self.errors.append(f"{basket['name']}/futures: {type(e).__name__}: {e}")

        # --- options ---
        if "options" in basket["kinds"] and not daily:
            try:
                added["options"] = self._options(kite, basket, now)
            except TokenLost:
                raise
            except Exception as e:                               # noqa: BLE001
                self.errors.append(f"{basket['name']}/options: {type(e).__name__}: {e}")
        return added

    def _warm(self, basket: dict, kind: str, now: datetime, daily: bool) -> None:
        """After a restart pick up where the files stop (today's file only; older days are never re-fetched)."""
        d = self.store.read_day(basket["folder"], "index" if kind == "index" else kind, None if daily else now.date())
        if d is None or d.empty:
            return
        self.last[(basket["name"], kind)] = d["datetime"].max()
        if kind == "index":
            self.spot[basket["name"]] = d.set_index("datetime")["close"]

    def _futures(self, kite, basket: dict, now: datetime, daily: bool) -> int:
        fc = basket["settings"]["futures"]
        key = (basket["name"], "futures")
        if key not in self.last:
            self._warm(basket, "futures", now, daily)
        f = self.nfo[(self.nfo["name"] == fc["name"]) & (self.nfo["instrument_type"] == "FUT")
                     & (self.nfo["expiry"] >= now.date())].sort_values("expiry")
        if f.empty:
            raise RuntimeError(f"no active {fc['name']} futures")
        row = f.iloc[1 if fc.get("contract") == "next" and len(f) > 1 else 0]
        frm = self._from(key, basket, now, daily)
        bar = int(basket["bar_minutes"])
        if not daily and latest_closed_start(self.cfg, now, bar) < pd.Timestamp(frm):
            return 0
        df = self.history(kite, int(row["instrument_token"]), frm, now, basket["kite_interval"], oi=True)
        df = df if daily else drop_open_bars(df, self.cfg, now, bar)
        if df.empty:
            return 0
        self.last[key] = df["datetime"].max()
        return self.store.append(basket["folder"], "futures", df, daily)

    def _options(self, kite, basket: dict, now: datetime) -> int:
        oc = basket["settings"]["options"]
        spot = self.spot.get(basket["name"])
        if spot is None or spot.empty:
            return 0
        bar, iv, step = int(basket["bar_minutes"]), basket["kite_interval"], int(oc["strike_step"])
        since = self._since.get(basket["name"])
        win = spot[spot.index >= pd.Timestamp(since)] if since is not None else spot.tail(1)
        if win.empty:
            win = spot.tail(1)
        atm_lo, atm_hi = round(float(win.min()) / step) * step, round(float(win.max()) / step) * step   # covers a catch-up
        opts = self.nfo[(self.nfo["name"] == oc["name"]) & self.nfo["instrument_type"].isin(["CE", "PE"])
                        & (self.nfo["expiry"] >= now.date())]
        expiries = sorted(opts["expiry"].unique())
        total = 0
        for sp in expiry_specs(oc):
            code, lo, hi = sp["code"], sp["lo"], sp["hi"]
            if code > len(expiries):
                continue
            seen = (basket["name"], code)
            n = self._opt_runs.get(seen, -1) + 1
            self._opt_runs[seen] = n
            if n > 0 and (n - sp["phase"]) % sp["every"] != 0:     # not this run's turn (the first run always fetches)
                continue
            exp = expiries[code - 1]
            sub = opts[(opts["expiry"] == exp) & (opts["strike"] >= atm_lo + lo * step)
                       & (opts["strike"] <= atm_hi + hi * step)]
            frames = []
            for r in sub.itertuples():
                key = (basket["name"], "opt", int(r.instrument_token))
                if key not in self.last:
                    self._warm_option(basket, key, r, code, now)
                frm = self._from(key, basket, now, False)
                if latest_closed_start(self.cfg, now, bar) < pd.Timestamp(frm):
                    continue
                try:
                    b = self.history(kite, int(r.instrument_token), frm, now, iv, oi=True)
                except TokenLost:
                    raise
                except Exception as e:                           # noqa: BLE001
                    self.errors.append(f"{basket['name']}/{r.tradingsymbol}: {type(e).__name__}: {e}")
                    continue
                b = drop_open_bars(b, self.cfg, now, bar)
                if b.empty:
                    continue
                self.last[key] = b["datetime"].max()
                frames.append(b.assign(option_type=r.instrument_type, strike=float(r.strike)))
            if not frames:
                continue
            df = build_option_rows(pd.concat(frames, ignore_index=True), spot, step, lo, hi,
                                   oc.get("expiry_flag", "WEEK"), code)
            total += self.store.append(basket["folder"], "options", df, False)
        return total

    def _warm_option(self, basket: dict, key: tuple, r, code: int, now: datetime) -> None:
        d = self.store.read_day(basket["folder"], "options", now.date())
        if d is None:
            return
        m = d[(d["option_type"] == r.instrument_type) & (d["strike"] == float(r.strike)) & (d["expiry_code"] == code)]
        if not m.empty:
            self.last[key] = m["datetime"].max()

    # ───────── top-up: everything missing between backfill.start_date and today ─────────
    def _load_state(self) -> dict:
        if self._state is None:
            try:
                self._state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._state = {}
            self._state.setdefault("no_data_days", [])        # market days for which Kite returned no candles (holidays)
            self._state.setdefault("unavailable", {})         # series/days Kite cannot serve (expired contracts), logged once
        return self._state

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self._load_state(), indent=1), encoding="utf-8")
        except OSError as e:
            log.warning(f"could not save {self.state_path}: {e}")

    def _market_days(self, start: date, end_excl: date) -> list[date]:
        bad = set(self._load_state()["no_data_days"])
        out, d = [], start
        while d < end_excl:
            if is_market_day(self.cfg, d) and f"{d}" not in bad:
                out.append(d)
            d += timedelta(days=1)
        return out

    def _expected_bars(self, bar: int) -> int:
        op, cl = session_bounds(self.cfg, date(2000, 1, 3))
        return math.ceil((cl - op).total_seconds() / 60 / bar)

    def _missing_days(self, basket: dict, kind: str, days: list[date]) -> list[date]:
        need, out = self._expected_bars(int(basket["bar_minutes"])), []
        for d in days:
            df = self.store.read_day(basket["folder"], kind, d)
            if df is None or df["datetime"].nunique() < need:
                out.append(d)
        return out

    @staticmethod
    def _runs(days: list[date], market_days: list[date]) -> list[list[date]]:
        """Group days that follow each other in the market-day calendar (weekends and holidays do not break a run)."""
        pos = {d: i for i, d in enumerate(market_days)}
        runs: list[list[date]] = []
        for d in sorted(days):
            if runs and pos.get(d, -9) == pos.get(runs[-1][-1], -99) + 1:
                runs[-1].append(d)
            else:
                runs.append([d])
        return runs

    def history_range(self, kite, token: int, start: datetime, end: datetime, interval: str, oi: bool = False) -> pd.DataFrame:
        """A long window split into requests Kite accepts (backfill.chunk_days per interval)."""
        limit = int(self.cfg["backfill"]["chunk_days"].get(interval, 55))
        frames, cur = [], start
        while cur < end:
            nxt = min(cur + timedelta(days=limit), end)
            df = self.history(kite, token, cur, nxt, interval, oi)
            if not df.empty:
                frames.append(df)
            cur = nxt
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames).drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)

    def _mark_no_data(self, d: date) -> None:
        st = self._load_state()
        if f"{d}" not in st["no_data_days"]:
            st["no_data_days"].append(f"{d}")
            log.info(f"top-up: Kite has no candles for {d} (market holiday?); it is skipped from now on "
                     f"(list in {self.state_path.name}, or add it to schedule.holidays)")

    def _note_unavailable(self, key: str, days: list[date], why: str) -> None:
        st = self._load_state()["unavailable"]
        new = [f"{d}" for d in days if f"{d}" not in st.get(key, [])]
        if new:
            st[key] = sorted(set(st.get(key, [])) | set(new))
            log.warning(f"top-up {key}: cannot download {new[0]}..{new[-1]} ({len(new)} days): {why}")

    def _front_future(self, basket: dict, now: datetime):
        fc = basket["settings"]["futures"]
        f = self.nfo[(self.nfo["name"] == fc["name"]) & (self.nfo["instrument_type"] == "FUT")
                     & (self.nfo["expiry"] >= now.date())].sort_values("expiry")
        if f.empty:
            raise RuntimeError(f"no active {fc['name']} futures")
        return f.iloc[1 if fc.get("contract") == "next" and len(f) > 1 else 0], f.iloc[0]["expiry"]

    def topup(self, kite, now: datetime) -> None:
        """Download every day since backfill.start_date that a basket's files do not cover completely.
        Runs at start-up and once on each later day; the live loop then continues from the files."""
        bf = self.cfg["backfill"]
        if not bf.get("enabled", True):
            self._topup_day = now.date()
            return
        self.instruments(kite, now.date())
        t0, r0 = time.monotonic(), self.requests
        for b in self.cfg["baskets"]:
            start = date.fromisoformat(str(b.get("start_date") or bf["start_date"]))
            try:
                if b["kite_interval"] == "day":
                    self._topup_daily(kite, b, start, now)
                else:
                    self._topup_basket(kite, b, start, now)
            except TokenLost:
                raise
            except Exception as e:                                # noqa: BLE001
                log.warning(f"top-up {b['name']} failed: {type(e).__name__}: {e}")
        self._save_state()
        self._topup_day = now.date()
        log.info(f"top-up finished: {self.requests - r0} requests in {time.monotonic() - t0:.0f}s")

    def _topup_basket(self, kite, b: dict, start: date, now: datetime) -> None:
        days = self._market_days(start, now.date())               # completed days only; today belongs to the live loop
        for kind in ("index", "futures", "options"):
            if kind not in b["kinds"]:
                continue
            if kind == "options":
                n = self._fill_options(kite, b, days, now)
                if n:
                    log.info(f"top-up {b['name']}/options: +{n} rows")
                continue
            gone = set(self._load_state()["unavailable"].get(f"{b['name']}/{kind}", []))   # expired contracts: nothing to fetch
            miss = [d for d in self._missing_days(b, kind, days) if f"{d}" not in gone]
            if not miss:
                continue
            log.info(f"top-up {b['name']}/{kind}: {len(miss)} incomplete day(s) {miss[0]}..{miss[-1]}")
            n = {"index": self._fill_index, "futures": self._fill_futures}[kind](kite, b, miss, days, now)
            log.info(f"top-up {b['name']}/{kind}: +{n} rows")

    def _fill_index(self, kite, b: dict, miss: list[date], days: list[date], now: datetime) -> int:
        ic, iv, bar = b["settings"]["index"], b["kite_interval"], int(b["bar_minutes"])
        tok = self.nse_token(ic["symbol"])
        vtok = self.nse_token(ic["volume_proxy"]) if ic.get("volume_proxy") else None
        total = 0
        for run in self._runs(miss, days):
            frm = datetime.combine(run[0], self.cfg["session_start"])
            to = datetime.combine(run[-1], self.cfg["session_end"])
            px = drop_open_bars(self.history_range(kite, tok, frm, to, iv), self.cfg, now, bar)
            if px.empty:
                if len(run) == 1:
                    self._mark_no_data(run[0])
                else:
                    log.warning(f"top-up {b['name']}/index {run[0]}..{run[-1]}: Kite returned nothing")
                continue
            if vtok:
                pv = self.history_range(kite, vtok, frm, to, iv)
                px["volume"] = px["datetime"].map(pv.set_index("datetime")["volume"]).fillna(0).astype(float) if not pv.empty else 0.0
            else:
                px["volume"] = 0.0
            have = set(px["datetime"].dt.date)
            for d in run:
                if d not in have:
                    self._mark_no_data(d)
            total += self.store.append(b["folder"], "index", px, False)
        return total

    def _fill_futures(self, kite, b: dict, miss: list[date], days: list[date], now: datetime) -> int:
        row, front_exp = self._front_future(b, now)
        # the live front contract was the front contract only after the PREVIOUS monthly expiry
        first_ok = prev_monthly_expiry(front_exp, int(self.cfg["backfill"]["expiry_weekday"]))
        ok = [d for d in miss if d > first_ok]
        if len(ok) < len(miss):
            self._note_unavailable(f"{b['name']}/futures", [d for d in miss if d <= first_ok],
                                   f"those days traded a contract that has expired (Kite serves live contracts only; "
                                   f"{row['tradingsymbol']} became the front month after {first_ok})")
        total = 0
        for run in self._runs(ok, days):
            frm = datetime.combine(run[0], self.cfg["session_start"])
            to = datetime.combine(run[-1], self.cfg["session_end"])
            df = drop_open_bars(self.history_range(kite, int(row["instrument_token"]), frm, to, b["kite_interval"], oi=True),
                                self.cfg, now, int(b["bar_minutes"]))
            total += self.store.append(b["folder"], "futures", df, False)
        return total

    def _fill_options(self, kite, b: dict, days: list[date], now: datetime) -> int:
        """Option days (per expiry code) that are missing or incomplete, fetched from the contracts that are still live.
        Which contract held code k on a past day D: the live expiries start n = floor(days to the first live expiry / 7)
        weeks after D's own nearest expiry, so code k is the live list's entry k - 1 - n (n weekly contracts have expired
        since). Entries before the start of the live list are gone for good (Kite serves live contracts only)."""
        oc, iv, bar = b["settings"]["options"], b["kite_interval"], int(b["bar_minutes"])
        step, specs = int(oc["strike_step"]), {sp["code"]: sp for sp in expiry_specs(oc)}
        opts = self.nfo[(self.nfo["name"] == oc["name"]) & self.nfo["instrument_type"].isin(["CE", "PE"])
                        & (self.nfo["expiry"] >= now.date())]
        expiries = sorted(opts["expiry"].unique())
        if not expiries:
            return 0
        need = self._expected_bars(bar)
        groups: dict[tuple, list[date]] = {}
        gone: dict[int, list[date]] = {}
        for d in days:
            df = self.store.read_day(b["folder"], "options", d)
            have = df.groupby("expiry_code")["datetime"].nunique().to_dict() if df is not None else {}
            if df is not None:                       # a window that was widened later (e.g. ATM+-3 -> +-15) is incomplete
                for code, sp_ in specs.items():
                    x = df.loc[df["expiry_code"] == code, "strike_offset"]
                    if len(x) and (x.min() > sp_["lo"] or x.max() < sp_["hi"]):
                        have[code] = 0
            n = (expiries[0] - d).days // 7
            for code in specs:
                idx = code - 1 - n
                if idx < 0:
                    gone.setdefault(code, []).append(d)
                elif idx < len(expiries) and have.get(code, 0) < need:
                    groups.setdefault((code, expiries[idx]), []).append(d)
        for code, ds in gone.items():
            self._note_unavailable(f"{b['name']}/options/code{code}", ds,
                                   f"expiry {code} of those days has already expired (the nearest live expiry is {expiries[0]}) "
                                   f"and Kite only serves live contracts; take those days from the Dhan history download")
        if not groups:
            return 0
        frames_idx = [self.store.read_day(b["folder"], "index", d) for d in sorted({d for v in groups.values() for d in v})]
        frames_idx = [f for f in frames_idx if f is not None]
        if not frames_idx:
            return 0
        spot_all = pd.concat(frames_idx).drop_duplicates("datetime").set_index("datetime")["close"].sort_index()
        total = 0
        for (code, exp), ds in sorted(groups.items()):
            lo, hi, dset = specs[code]["lo"], specs[code]["hi"], set(ds)
            sp = spot_all[[t.date() in dset for t in spot_all.index]]
            if sp.empty:
                continue
            atm_lo, atm_hi = round(float(sp.min()) / step) * step, round(float(sp.max()) / step) * step
            sub = opts[(opts["expiry"] == exp) & (opts["strike"] >= atm_lo + lo * step) & (opts["strike"] <= atm_hi + hi * step)]
            log.info(f"top-up {b['name']}/options code {code} (expiry {exp}): {len(ds)} day(s) {ds[0]}..{ds[-1]}, "
                     f"{len(sub)} contracts")
            frm = datetime.combine(ds[0], self.cfg["session_start"])
            to = datetime.combine(ds[-1], self.cfg["session_end"])
            frames = []
            for r in sub.itertuples():
                try:
                    bars = self.history_range(kite, int(r.instrument_token), frm, to, iv, oi=True)
                except TokenLost:
                    raise
                except Exception as e:                            # noqa: BLE001
                    self.errors.append(f"{b['name']}/{r.tradingsymbol}: {type(e).__name__}: {e}")
                    continue
                bars = drop_open_bars(bars, self.cfg, now, bar)
                bars = bars[bars["datetime"].dt.date.isin(dset)]
                if not bars.empty:
                    frames.append(bars.assign(option_type=r.instrument_type, strike=float(r.strike)))
            if frames:
                df = build_option_rows(pd.concat(frames, ignore_index=True), spot_all, step, lo, hi,
                                       oc.get("expiry_flag", "WEEK"), code)
                total += self.store.append(b["folder"], "options", df, False)
        for e in self.errors[:8]:
            log.warning(e)
        self.errors = []
        return total

    def _topup_daily(self, kite, b: dict, start: date, now: datetime) -> None:
        """Daily candles: one request per kind for the whole span (only completed days are kept)."""
        iv = b["kite_interval"]
        _, close = session_bounds(self.cfg, now.date())
        last_day = now.date() if now >= close else now.date() - timedelta(days=1)
        frm, to = datetime.combine(start, dtime(0, 0)), now
        if "index" in b["kinds"]:
            ic = b["settings"]["index"]
            px = self.history_range(kite, self.nse_token(ic["symbol"]), frm, to, iv)
            px = px[px["datetime"].dt.date <= last_day] if not px.empty else px
            if not px.empty and ic.get("volume_proxy"):
                pv = self.history_range(kite, self.nse_token(ic["volume_proxy"]), frm, to, iv)
                px["volume"] = px["datetime"].map(pv.set_index("datetime")["volume"]).fillna(0).astype(float) if not pv.empty else 0.0
            elif not px.empty:
                px["volume"] = 0.0
            log.info(f"top-up {b['name']}/index: +{self.store.append(b['folder'], 'index', px, True)} rows")
        if "futures" in b["kinds"]:
            row, front_exp = self._front_future(b, now)
            first_ok = prev_monthly_expiry(front_exp, int(self.cfg["backfill"]["expiry_weekday"]))
            df = self.history_range(kite, int(row["instrument_token"]), frm, to, iv, oi=True)
            if not df.empty:
                df = df[(df["datetime"].dt.date <= last_day) & (df["datetime"].dt.date > first_ok)]
            log.info(f"top-up {b['name']}/futures: +{self.store.append(b['folder'], 'futures', df, True)} rows")

    def _check_holiday(self, now: datetime) -> None:
        """No index candle at all 15 minutes into the session, and no request failed: the market is closed today.
        The collector then sleeps until the next session instead of polling an empty day (nothing is saved to the
        state file: tomorrow's top-up records the day as a holiday)."""
        op, cl = session_bounds(self.cfg, now.date())
        if not (op + timedelta(minutes=15) <= now <= cl) or self.errors or f"{now.date()}" in self.cfg["schedule"]["holidays"]:
            return
        if any(k[1] == "index" and v.date() == now.date() for k, v in self.last.items()):
            return
        self.cfg["schedule"]["holidays"].append(f"{now.date()}")
        log.warning(f"No candles from Kite for {now.date()} by {now:%H:%M}: treating today as a market holiday and sleeping "
                    f"until the next session (add the date to schedule.holidays to make it permanent; restart the collector "
                    f"if the market is in fact open)")

    # ---- one cycle ----
    def run_cycle(self, now: datetime) -> str:
        self.requests, self.errors = 0, []
        t0 = time.monotonic()
        kite = self.session.get()
        if kite is None:
            return "no valid Kite token: run  python live_collector.py --login"
        try:
            self.instruments(kite, now.date())
            if self._topup_day != now.date():
                self.topup(kite, now)                          # at start-up and once on each later day
            parts = []
            for b in self.cfg["baskets"]:
                added = self.run_basket(kite, b, now)
                if any(added.values()):
                    parts.append(f"{b['name']}: " + " ".join(f"{k}+{v}" for k, v in added.items() if v))
        except TokenLost as e:
            self.session.invalidate()
            return f"Kite token rejected ({e}): run  python live_collector.py --login"
        self._check_holiday(now)
        msg = " | ".join(parts) if parts else "nothing due"
        msg += f" | {self.requests} requests {time.monotonic() - t0:.1f}s"
        if self.errors:
            for e in self.errors[:8]:
                log.warning(e)
            msg += f" | {len(self.errors)} errors"
        return msg


# ─────────────────────────────────── scheduling ───────────────────────────────
def is_market_day(cfg: dict, day: date) -> bool:
    s = cfg["schedule"]
    if f"{day:%Y-%m-%d}" in [str(x) for x in s.get("extra_days", [])]:
        return True
    return day.weekday() in s.get("weekdays", [0, 1, 2, 3, 4]) and f"{day:%Y-%m-%d}" not in [str(x) for x in s.get("holidays", [])]


def next_active_start(cfg: dict, now: datetime) -> datetime:
    s = cfg["schedule"]
    pre = timedelta(minutes=int(s.get("start_before_open_minutes", 1)))
    d = now.date()
    for _ in range(14):
        op, _cl = session_bounds(cfg, d)
        if is_market_day(cfg, d) and op - pre > now:
            return op - pre
        d += timedelta(days=1)
    return now + timedelta(hours=12)


def in_active_window(cfg: dict, now: datetime) -> bool:
    s = cfg["schedule"]
    if not is_market_day(cfg, now.date()):
        return False
    op, cl = session_bounds(cfg, now.date())
    return op - timedelta(minutes=int(s.get("start_before_open_minutes", 1))) <= now <= \
        cl + timedelta(minutes=int(s.get("final_pass_after_close_minutes", 2)))


def now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def sleep_until(target: datetime, stop_flag) -> None:
    while not stop_flag() and now_ist() < target:
        time.sleep(min(1.0, max((target - now_ist()).total_seconds(), 0.05)))


def _requests_per_run(b: dict) -> int:
    st = b["settings"]
    n = 0
    if "index" in b["kinds"] or "options" in b["kinds"]:
        n += 2 if st["index"].get("volume_proxy") else 1
    if "futures" in b["kinds"]:
        n += 1
    if "options" in b["kinds"] and b["kite_interval"] != "day":
        o = st["options"]
        n += round(sum((sp["hi"] - sp["lo"] + 1) * 2 / sp["every"] for sp in expiry_specs(o)))
    return n


def describe(cfg: dict) -> None:
    root = Path(cfg["output"]["root"])
    rps = float(cfg["kite"]["max_requests_per_second"])
    print(f"Output root: {root}")
    for b in cfg["baskets"]:
        n = _requests_per_run(b)
        print(f"  {b['name']:12} bar {b['bar_minutes']:>4} min  kinds {','.join(b['kinds']):22} ~{n:>3} requests per run "
              f"(~{n / rps:.0f}s)  -> {root / b['folder']}")
    busiest = sum(_requests_per_run(b) for b in cfg["baskets"] if b["kite_interval"] != "day")
    print(f"Session {cfg['session_start']:%H:%M}-{cfg['session_end']:%H:%M}, a cycle every {cfg['schedule']['poll_seconds']}s "
          f"(+{cfg['schedule']['delay_seconds']}s). Kite allows 3 requests/s: a normal minute (1-minute basket) needs ~"
          f"{_requests_per_run(cfg['baskets'][0]) / rps:.0f}s; a minute where every basket is due (e.g. 10:15) up to ~{busiest / rps:.0f}s. "
          f"A cycle that runs long only delays the next one: nothing is skipped, because each run fetches from the last saved bar.")


def lock_port(port: int):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        raise SystemExit(f"Another live_collector is already running (port {port} is held). Only one may run.")
    return s


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config-dir", default=str(CONFIG_DIR), help="folder with live_common.yaml and the basket files")
    ap.add_argument("--login", action="store_true", help="Kite login (once a day)")
    ap.add_argument("--once", action="store_true", help="run one cycle now and exit")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    ap.add_argument("--backfill-only", action="store_true", help="top up the missing history since backfill.start_date, then exit")
    a = ap.parse_args()
    cfg = load_config(Path(a.config_dir))
    if a.login:
        kite_login(cfg["kite"])
        return
    if a.dry_run:
        describe(cfg)
        return
    setup_logging(cfg)
    session = KiteSession(cfg["kite"])
    col = Collector(cfg, session)
    if a.backfill_only:
        k = session.get()
        if k is None:
            raise SystemExit("no valid Kite token: run  python live_collector.py --login")
        col.topup(k, now_ist())
        return
    if a.once:
        log.info("single cycle: " + col.run_cycle(now_ist()))
        return

    _lock = lock_port(int(cfg.get("instance_lock_port", 48731)))      # noqa: F841  (held until the process ends)
    stop = {"now": False}
    try:
        import signal
        signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("now", True))
    except (ValueError, AttributeError):
        pass
    sch = cfg["schedule"]
    log.info(f"Live collector started: baskets {[b['name'] for b in cfg['baskets']]}, output {cfg['output']['root']}")
    last_token_msg = None
    while not stop["now"]:
        now = now_ist()
        if not in_active_window(cfg, now):
            nxt = next_active_start(cfg, now)
            log.info(f"Market closed: sleeping until {nxt:%a %Y-%m-%d %H:%M}")
            sleep_until(nxt, lambda: stop["now"])
            continue
        # the next cycle time: the next minute boundary plus the delay
        poll = int(sch.get("poll_seconds", 60))
        base = now.replace(second=0, microsecond=0)
        target = base + timedelta(seconds=int(sch.get("delay_seconds", 5)))
        if target <= now:
            target = base + timedelta(seconds=poll + int(sch.get("delay_seconds", 5)))
        sleep_until(target, lambda: stop["now"])
        if stop["now"]:
            break
        now = now_ist()
        try:
            msg = col.run_cycle(now)
        except SystemExit:
            raise
        except Exception as e:                                           # noqa: BLE001  (never let the loop die)
            log.exception(f"cycle failed: {type(e).__name__}: {e}")
            msg = None
        if msg is not None:
            if msg.startswith(("no valid", "Kite token")):
                if msg != last_token_msg:
                    log.error(msg)
                last_token_msg = msg
            else:
                last_token_msg = None
                if msg != "nothing due" or log.isEnabledFor(logging.DEBUG):
                    log.info(f"{now:%H:%M:%S} {msg}")
    log.info("Live collector stopped")


if __name__ == "__main__":
    main()
