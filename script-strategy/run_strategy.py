"""
Run the first-project strategy (entries and exits) on the data saved by the live collector.

The trading logic is NOT copied: strategy.py of the first-project repo is imported (live_data.strategy_py in the
config), so signals, spread selection, stop-loss, trend-change flips, expiry and end-of-day exits are exactly the ones
backtested there. This script only (1) reads the collector's per-day files, (2) adds the IV that Kite does not
provide, (3) feeds the strategy and (4) writes trades / equity / report and prints the latest state.

    python script-strategy/run_strategy.py                                  # default config below
    python script-strategy/run_strategy.py --config configurations/strategy/config_skew_mom_5min.yaml
    python script-strategy/run_strategy.py --folder data/data_hourly --config <a config with resample off>
    python script-strategy/run_strategy.py --config configurations/strategy/config_skew_mom_1min_next_week.yaml     # 1-minute bars
    python script-strategy/run_strategy.py --folder data/data_minute --timeframe 5min   # 1-minute files, traded on 5-minute bars

Paths are relative to the root of this repo (the parent of script-strategy/).
"""
from __future__ import annotations

import argparse
import copy
import csv
import re
import importlib.util
import logging
import math
import os
import sys
import time
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

HERE = Path(__file__).resolve().parent.parent          # repo root
DEFAULT_CONFIG = "configurations/strategy/config_skew_mom_5min.yaml"
log = logging.getLogger("run_strategy")


def rel(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else HERE / p


def load_strategy(path: str):
    """Import strategy.py (first-project) as a module."""
    p = rel(os.environ.get("STRATEGY_PY") or path).resolve()
    if not p.exists():
        raise SystemExit(f"strategy.py not found at {p}. Set live_data.strategy_py in the config (or env STRATEGY_PY).")
    sys.path.insert(0, str(p.parent))                   # so its own imports/relative files resolve
    spec = importlib.util.spec_from_file_location("strategy_logic", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    log.info(f"Trading logic: {p}")
    return mod


# ───────────────────────────── data ─────────────────────────────

def _day_of(f: Path) -> date:
    return datetime.strptime(f.stem.split("_")[-1], "%Y%m%d").date()


def _files(folder: Path, prefix: str, ld: dict) -> list[Path]:
    lo = date.fromisoformat(str(ld["start_date"])) if ld.get("start_date") else date.min
    hi = date.fromisoformat(str(ld["end_date"])) if ld.get("end_date") else date.max
    out = []
    for f in sorted(folder.glob(f"{prefix}_????????.csv")):
        try:
            if lo <= _day_of(f) <= hi:
                out.append(f)
        except ValueError:
            pass
    if ld.get("lookback_days") and out:               # only the newest N calendar days (a live run does not need 5 years)
        newest = _day_of(out[-1])
        out = [f for f in out if (newest - _day_of(f)).days <= int(ld["lookback_days"])]
    return out


def _read(files: list[Path]) -> pd.DataFrame:
    frames = [pd.read_csv(f) for f in files]
    df = pd.concat(frames, ignore_index=True)
    # the collector writes "+05:30" stamps; read them as IST
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True).dt.tz_convert("Asia/Kolkata")
    return df


def load_index(folder: Path, ld: dict) -> pd.DataFrame:
    fs = _files(folder, "nifty", ld)
    if not fs:
        raise SystemExit(f"no nifty_<date>.csv files in {folder}")
    df = _read(fs).drop_duplicates("datetime", keep="last").set_index("datetime").sort_index()
    return df


def load_options(folder: Path, ld: dict) -> pd.DataFrame:
    fs = _files(folder, "nifty_options", ld)
    if not fs:
        raise SystemExit(f"no nifty_options_<date>.csv files in {folder}")
    want = str(ld.get("expiry_code", "nearest")).lower()
    frames, seen = [], set()
    for f in fs:                                      # keep only the traded contract while reading: 5 years of 1-minute options are huge
        o = pd.read_csv(f)
        seen |= set(int(c) for c in o["expiry_code"].unique())
        o = o[o["expiry_code"] == (o["expiry_code"].min() if want == "nearest" else int(want))]
        if not o.empty:
            frames.append(o)
    if not frames:
        raise SystemExit(f"expiry_code {want} is not in the option files read (codes present: {sorted(seen)}). The collector cannot fetch "
                         f"already-expired contracts, so early days hold only the later codes; use expiry_code: nearest, or a start_date "
                         f"from the first day that has it.")
    o = pd.concat(frames, ignore_index=True)
    o["datetime"] = pd.to_datetime(o["datetime"], utc=True).dt.tz_convert("Asia/Kolkata")
    o = o.drop_duplicates(["datetime", "option_type", "strike", "expiry_code"], keep="last")
    return o.dropna(subset=["strike", "open", "high", "low", "close"]).reset_index(drop=True)


# ───────────────────────── expiry dates and IV ─────────────────────────

def expiry_dates(first: date, last: date, weekday: int, shifts: dict) -> list[date]:
    """Weekly expiries between first and last + 5 weeks: every `weekday`, moved by the holiday shifts."""
    sh = {date.fromisoformat(str(k)): date.fromisoformat(str(v)) for k, v in (shifts or {}).items()}
    d = first - timedelta(days=7)
    out = []
    while d <= last + timedelta(days=35):
        if d.weekday() == weekday:
            out.append(sh.get(d, d))
        d += timedelta(days=1)
    return sorted(set(out))


_erf = np.frompyfunc(math.erf, 1, 1)


def _ncdf(x: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + _erf(x / math.sqrt(2.0)).astype(float))


def _bs_price(s, k, t, r, sig, is_call):
    sq = sig * np.sqrt(t)
    d1 = (np.log(s / k) + (r + 0.5 * sig * sig) * t) / sq
    d2 = d1 - sq
    call = s * _ncdf(d1) - k * np.exp(-r * t) * _ncdf(d2)
    put = k * np.exp(-r * t) * _ncdf(-d2) - s * _ncdf(-d1)
    return np.where(is_call, call, put)


def implied_vol(price, s, k, t, r, is_call) -> np.ndarray:
    """Black-Scholes IV in percent by bisection (NaN where the price is below intrinsic or time is up)."""
    price, s, k, t, is_call = (np.asarray(a, dtype=float) for a in (price, s, k, t, is_call))
    is_call = is_call > 0.5
    lo, hi = np.full(price.shape, 0.01), np.full(price.shape, 5.0)
    for _ in range(45):
        mid = (lo + hi) / 2
        too_high = _bs_price(s, k, t, r, mid, is_call) > price
        hi = np.where(too_high, mid, hi)
        lo = np.where(too_high, lo, mid)
    iv = (lo + hi) / 2 * 100
    intrinsic = np.where(is_call, np.maximum(s - k * np.exp(-r * t), 0), np.maximum(k * np.exp(-r * t) - s, 0))
    bad = ~(t > 0) | ~(price > intrinsic + 0.05) | ~(price > 0) | (iv >= 499)
    return np.where(bad, np.nan, iv)


def add_iv(o: pd.DataFrame, ld: dict) -> pd.DataFrame:
    """Fill `iv` for the strike offsets the strategy reads. Expiry of a row = the expiry_code-th weekly expiry on/after its day."""
    o = o.copy()
    if "iv" not in o.columns:
        o["iv"] = np.nan
    days = o["datetime"].dt.date
    exps = expiry_dates(days.min(), days.max(), int(ld.get("expiry_weekday", 1)), ld.get("holiday_shifts"))
    m = o["iv"].isna() | (o["iv"] <= 0)
    m &= o["strike_offset"].isin([int(x) for x in ld.get("iv_strike_offsets", [-2, 0, 2])])
    if not m.any():
        return o
    x = o[m]
    exp_arr = np.array(exps, dtype="datetime64[D]")
    d_arr = np.array(x["datetime"].dt.date.values, dtype="datetime64[D]")
    first_idx = np.searchsorted(exp_arr, d_arr, side="left")             # first expiry on/after the day
    idx = first_idx + (x["expiry_code"].astype(int).values - 1)
    ok = idx < len(exp_arr)
    exp_dt = pd.Series(pd.to_datetime(exp_arr[np.minimum(idx, len(exp_arr) - 1)]).tz_localize("Asia/Kolkata") + pd.Timedelta(hours=15, minutes=30), index=x.index)
    t = (exp_dt - x["datetime"]).dt.total_seconds().values / (365.0 * 86400.0)
    iv = implied_vol(x["close"].values, x["spot"].values, x["strike"].values, np.where(ok, t, 0.0),
                     float(ld.get("risk_free_rate", 0.065)), (x["option_type"] == "CE").values)
    o.loc[x.index, "iv"] = iv
    log.info(f"IV solved for {int(np.isfinite(iv).sum())} of {len(x)} option rows (offsets {ld.get('iv_strike_offsets', [-2, 0, 2])})")
    return o


# ───────────────────────────── run ─────────────────────────────

def pick_expiry(o: pd.DataFrame, code) -> pd.DataFrame:
    have = sorted(o["expiry_code"].unique())
    day = o["datetime"].dt.date
    if str(code).lower() == "nearest":
        low = o.groupby(day)["expiry_code"].transform("min")
        log.info(f"Trading the nearest expiry code present on each day (codes in the data: {[int(c) for c in have]})")
        return o[o["expiry_code"] == low].reset_index(drop=True)
    code = int(code)
    if code not in have:
        raise SystemExit(f"expiry_code {code} is not in the data (codes present: {have}). "
                         f"The collector cannot fetch already-expired contracts, so early days hold only the later codes; "
                         f"use live_data.expiry_code: nearest or start_date from the first day that has code {code}.")
    days_with = o.loc[o["expiry_code"] == code, "datetime"].dt.date.nunique()
    log.info(f"Trading expiry_code {code}: {days_with} of {day.nunique()} days have it")
    return o[o["expiry_code"] == code].reset_index(drop=True)


def setup(config_path: str, timeframe: str | None = None):
    """Load the config and the strategy module (and install the hedge selection the config asks for)."""
    cfg = yaml.safe_load(open(rel(config_path), encoding="utf-8"))
    ld = cfg["live_data"]
    if timeframe:                                        # e.g. 1-minute files traded on 5-minute bars
        cfg["resample"] = {"enabled": True, "timeframe": timeframe}
    S = load_strategy(ld["strategy_py"])
    target = (cfg.get("spread") or {}).get("hedge_target_premium")
    if (cfg.get("spread") or {}).get("hedge_pick") == "lowest":      # hedge = the cheapest available strike beyond the short leg
        def pick_hedge(idx, ts, opt_type, short_k, cap, floor):
            chain = idx["chain"].get((ts, opt_type))
            if chain is None:
                return None
            strikes, closes = chain
            beyond = strikes < short_k if opt_type == "PE" else strikes > short_k
            ok = beyond & (closes < cap) & (closes >= floor)
            if not ok.any():
                return None
            k = np.flatnonzero(ok)
            return float(strikes[k[np.argmin(closes[k])]])
        S.pick_hedge = pick_hedge
        log.info(f"Hedge selection: lowest premium available (spread.hedge_pick), within {cfg['spread']['hedge_min_premium']}-{cfg['spread']['hedge_max_premium']}")
    elif target:                                         # hedge = the available strike whose premium is nearest the target
        _orig = S.pick_hedge

        def pick_hedge(idx, ts, opt_type, short_k, cap, floor, _t=float(target)):
            chain = idx["chain"].get((ts, opt_type))
            if chain is None:
                return None
            strikes, closes = chain
            beyond = strikes < short_k if opt_type == "PE" else strikes > short_k
            ok = beyond & (closes < cap) & (closes >= floor)
            if not ok.any():
                return None
            k = np.flatnonzero(ok)
            return float(strikes[k[np.argmin(np.abs(closes[k] - _t))]])
        S.pick_hedge = pick_hedge
        log.info(f"Hedge selection: premium nearest Rs {target} (spread.hedge_target_premium), within {cfg['spread']['hedge_min_premium']}-{cfg['spread']['hedge_max_premium']}")
    return cfg, S


def evaluate(cfg0: dict, S, fdir: Path) -> dict:
    """Read the data folder and run the strategy over it. Returns signals, trades, equity and the position still open."""
    cfg = copy.deepcopy(cfg0)
    ld = cfg["live_data"]
    df = load_index(fdir, ld)
    opts = load_options(fdir, ld)
    log.debug(f"Index bars {len(df)} ({df.index.min():%Y-%m-%d %H:%M} -> {df.index.max():%Y-%m-%d %H:%M}); option rows {len(opts)}")

    opts = pick_expiry(opts, ld.get("expiry_code", "nearest"))     # before resampling: one contract per strike
    code_by_day = opts.groupby(opts["datetime"].dt.date)["expiry_code"].first().to_dict()

    # bar-size handling: identical to strategy.main()
    if not S._is_daily(cfg) and "bars_per_day" not in cfg["backtest"] and not cfg["resample"]["enabled"]:
        cfg["backtest"]["bars_per_day"] = float(df.groupby(df.index.date).size().median())
    native = S._native_minutes(df)
    if cfg["resample"]["enabled"] and native == S._tf_minutes(cfg["resample"]["timeframe"]):
        log.debug(f"Data is already {native}-minute bars: no resampling")
    elif cfg["resample"]["enabled"]:
        tf = cfg["resample"]["timeframe"]
        df, opts = S.resample_bars(df, tf), S.resample_options(opts, tf)

    opts = add_iv(opts, ld)
    opts["datetime"] = pd.to_datetime(opts["datetime"])
    # The collector refreshes some expiries only every few minutes, so the newest index bars can lack option data (no IV skew,
    # no prices to fill at). Evaluate only up to the last bar that has options.
    last_opt = opts["datetime"].max()
    lag = int((df.index.max() - last_opt).total_seconds() // 60)
    if lag > 0:
        df = df[df.index <= last_opt]
        log.debug(f"index bars after {last_opt:%H:%M} have no option data yet ({lag} min): not evaluated")

    flow = S.compute_oi_flow(opts, cfg)
    df = df.join(flow, how="left")
    df[["call_doi", "put_doi"]] = df[["call_doi", "put_doi"]].fillna(0.0)
    df = df.join(opts[opts["strike_offset"] == 0].groupby("datetime")["iv"].mean().rename("iv_atm"))
    iv = opts[opts["iv"] > 0].pivot_table(index="datetime", columns=["option_type", "strike_offset"], values="iv")
    if ("PE", -2) in iv.columns and ("CE", 2) in iv.columns:
        df = df.join((iv[("PE", -2)] - iv[("CE", 2)]).rename("skew"))
    else:
        raise RuntimeError("no IV at strike offsets PE -2 / CE +2 (needed by skew_mom): check the option files and the "
                           "live_data settings (iv_strike_offsets, expiry_weekday, holiday_shifts)")
    idx = S.build_option_index(opts, cfg["session"].get("expiry_roll_ratio", 3.0))

    n_need = int(cfg["direction"].get("min_history", 0))
    if len(df) < n_need + 5:
        log.warning(f"Only {len(df)} bars of data but direction.min_history is {n_need}: few or no signals until more data is collected")

    sig = S.build_signals(df, cfg)
    trades, eq = S.run_backtest(sig, cfg, idx)
    open_pos = trades.attrs.get("open_position")
    rejects = trades.attrs.get("oi_rejects") or {}
    exps = expiry_dates(df.index.min().date(), df.index.max().date(), int(ld.get("expiry_weekday", 1)), ld.get("holiday_shifts"))
    if len(trades):
        trades = trades.copy()
        trades.insert(list(trades.columns).index("spread") + 1 if "spread" in trades.columns else len(trades.columns), "expiry",
                      [contract_expiry(t, code_by_day, ld, exps) for t in trades["entry_time"]])
    if open_pos:
        open_pos = dict(open_pos, expiry_date=contract_expiry(open_pos["entry_time"], code_by_day, ld, exps))
    return {"cfg": cfg, "df": df, "sig": sig, "trades": trades, "eq": eq,
            "rejects": rejects, "open": open_pos, "lag": max(lag, 0)}


def contract_expiry(entry_ts, code_by_day: dict, ld: dict, exps: list):
    """Expiry date of the contract a trade was opened on: the code-th weekly expiry on or after the entry day (code of that day's data)."""
    d = pd.Timestamp(entry_ts).date()
    code = code_by_day.get(d)
    if code is None or not exps:
        return ""
    i = int(np.searchsorted(np.array(exps, dtype="datetime64[D]"), np.datetime64(d), side="left")) + int(code) - 1
    return f"{exps[i]:%Y-%m-%d}" if i < len(exps) else ""


def state_text(sig: pd.DataFrame) -> str:
    last = sig.iloc[-1]
    state = "LONG (bullish: put credit spread)" if last.get("long_signal") else \
            "SHORT (bearish: call credit spread)" if last.get("short_signal") else "no directional signal"
    return f"close {last['close']:.2f}, score {last.get('score', float('nan')):.2f} -> {state}"


def score_text(sig: pd.DataFrame, cfg: dict) -> str:
    """The pieces of the directional score of the latest bar (skew_mom: IV-skew z-score + intraday-momentum z-score)."""
    l = sig.iloc[-1]
    if "skew_z" not in sig.columns:
        return f"score {l.get('score', float('nan')):+.2f}"
    thr = float(cfg["direction"]["threshold"])
    use = cfg["direction"].get("use", ["skew", "open_mom"])
    f = lambda v, d=2: "n/a" if v != v else f"{v:+.{d}f}"
    parts = []
    if "skew" in use:
        parts.append(f"skew z {f(l['skew_z'])} [put-call IV skew {f(l['skew'])} vs norm {f(l['skew_norm'])}, deviation {f(l['skew_dev'])}]")
    if "open_mom" in use:
        parts.append(f"momentum z {f(l['mom_z'])} [close vs day open {f(l['open_mom'] * 100)}%]")
    call = "LONG" if l["score"] > thr else "SHORT" if l["score"] < -thr else "no call"
    return f"score {l['score']:+.2f} = " + " + ".join(parts) + f"  | needs beyond +-{thr:.2f} -> {call}"


def main(config_path: str, folder: str | None, open_report: bool, timeframe: str | None = None, args=None) -> None:
    """One run over everything collected so far: trades, equity, signals and an HTML report."""
    cfg, S = setup(config_path, timeframe)
    if args is not None:
        apply_overrides(cfg, args)
    fdir = rel(folder or cfg["live_data"]["folder"])
    log.info(f"Data folder: {fdir}")
    res = evaluate(cfg, S, fdir)
    cfg, df, sig, trades, eq = res["cfg"], res["df"], res["sig"], res["trades"], res["eq"]
    log.info(f"Index bars {len(df)} ({df.index.min():%Y-%m-%d %H:%M} -> {df.index.max():%Y-%m-%d %H:%M})")
    log.info(f"Signals skipped at entry: {res['rejects'] or 'none'}")
    metrics = S.compute_metrics(trades, eq, cfg["backtest"]["initial_capital"], S._bars_per_day(cfg))
    metrics = {k: round(float(v), 2) if isinstance(v, (float, np.floating)) else v for k, v in metrics.items()}
    trades, eq = trades.round(2), eq.round(2)

    out = rel(cfg.get("results_dir", "results/run"))
    out.mkdir(parents=True, exist_ok=True)
    S.save_csv(trades, out / "trades.csv", index=False)
    S.save_csv(eq, out / "equity_curve.csv")
    sig_cols = [c for c in ("close", "score", "skew_z", "mom_z", "long_signal", "short_signal", "skew", "skew_norm", "open_mom", "iv_atm")
                if c in sig.columns]
    S.save_csv(sig[sig_cols].round(4), out / "signals.csv")

    log.info("-- SUMMARY --")
    for k, v in metrics.items():
        log.info(f"  {k:>15}: {v:.2f}" if isinstance(v, float) else f"  {k:>15}: {v}")
    log.info(f"Trades: {len(trades)}   results: {out}")
    log.info(f"Latest bar {sig.index[-1]:%Y-%m-%d %H:%M}: {state_text(sig)}"
             + (f" [option data {res['lag']} min behind the index]" if res["lag"] else ""))
    log.info("  " + score_text(sig, cfg))
    if res["open"]:
        log.info("Open position:\n" + "\n".join(describe_position(res["open"])))
    if not trades.empty:
        cols = [c for c in ("entry_time", "exit_time", "spread", "expiry", "reason", "pnl") if c in trades.columns]
        log.info("Last trades:\n" + trades[cols].tail(5).to_string(index=False))

        rep = cfg.get("report", {})
        rp = S.write_html_report(trades, eq, metrics, cfg, config_path, (df.index.min(), df.index.max()),
                                 str(rel(rep.get("html", out / "report.html"))), price=df)
        log.info(f"HTML report: {Path(rp).resolve()}")
        if open_report or (rep.get("open", False) and not NO_OPEN[0]):
            webbrowser.open(Path(rp).resolve().as_uri())


# ───────────────────────────── continuous run + trade log ─────────────────────────────

LOG_COLS = ["logged_at", "event", "note", "trade_id", "data_time", "strategy", "spread", "expiry", "legs", "qty", "entry_credit",
            "exit_cost", "stop_cost", "target_cost", "pnl", "pnl_pct_capital", "reason", "entry_spot", "exit_spot", "score"]


def _legs_text(legs: list) -> str:
    return " | ".join(f"{'SELL' if lg['short'] else 'BUY'} {lg['strike']:.0f}{lg['type']} @{lg['entry']:.2f}"
                      + (f" (now {lg['last']:.2f})" if "last" in lg else "") for lg in legs)


def describe_position(pos: dict) -> list:
    mtm = (pos["p0"] - pos["last_p"]) * pos["qty"]
    return [f"  {pos['strategy']}  since {pos['entry_time']:%Y-%m-%d %H:%M}   spot at entry {pos['entry_spot']:.2f}   expiry {pos.get('expiry_date') or 'n/a'}",
            f"  {_legs_text(pos['legs'])}",
            f"  credit {pos['p0']:.2f}/share, cost to close now {pos['last_p']:.2f}, stop at {pos['sl']:.2f}, "
            f"target at {pos['tp']:.2f}, qty {pos['qty']}",
            f"  unrealised P&L Rs {mtm:,.0f}"]


def _row_from_position(pos: dict, event: str, note: str) -> dict:
    sc = pos.get("score")
    return {"event": event, "note": note, "trade_id": f"{pos['entry_time']:%Y-%m-%d %H:%M}",
            "data_time": f"{pos['entry_time']:%Y-%m-%d %H:%M}", "strategy": pos["strategy"], "spread": pos["label"],
            "expiry": pos.get("expiry_date", ""),
            "legs": _legs_text([{k: lg[k] for k in ("short", "strike", "type", "entry")} for lg in pos["legs"]]),
            "qty": pos["qty"], "entry_credit": round(pos["p0"], 2), "stop_cost": round(pos["sl"], 2),
            "target_cost": round(pos["tp"], 2), "entry_spot": round(float(pos["entry_spot"]), 2),
            "score": round(float(sc), 2) if sc is not None and sc == sc else ""}


def _row_from_trade(t, capital: float, event: str, note: str) -> dict:
    return {"event": event, "note": note, "trade_id": f"{pd.Timestamp(t['entry_time']):%Y-%m-%d %H:%M}",
            "data_time": f"{pd.Timestamp(t['exit_time']):%Y-%m-%d %H:%M}", "strategy": t.get("strategy", ""),
            "spread": t.get("spread", ""), "expiry": t.get("expiry", ""), "legs": t.get("legs", ""), "qty": t.get("qty", ""),
            "entry_credit": round(float(t["entry"]), 2), "exit_cost": round(float(t["exit"]), 2),
            "pnl": round(float(t["pnl"]), 2), "pnl_pct_capital": round(float(t["pnl"]) / capital * 100, 2),
            "reason": t.get("reason", ""), "entry_spot": t.get("entry_spot", ""), "exit_spot": t.get("exit_spot", ""),
            "score": t.get("direction_entry", "")}


def _open_row_from_trade(t, cfg: dict, note: str) -> dict:
    """OPEN event for a trade the strategy reports as already closed (it opened and closed between two checks, or is still
    open and only closed provisionally at the last bar)."""
    sp, e = cfg["spread"], float(t["entry"])
    tid = f"{pd.Timestamp(t['entry_time']):%Y-%m-%d %H:%M}"
    tp = float(sp.get("tp_pct", 100))
    return {"event": "OPEN", "note": note, "trade_id": tid, "data_time": tid, "strategy": t.get("strategy", ""),
            "spread": t.get("spread", ""), "expiry": t.get("expiry", ""), "legs": re.sub(r"->[0-9.]+", "", str(t.get("legs", ""))),
            "qty": t.get("qty", ""),
            "entry_credit": round(e, 2), "stop_cost": round(e * (1 + float(sp["sl_pct"]) / 100), 2),
            "target_cost": round(e * (1 - tp / 100), 2) if tp < 100 else "no target",
            "entry_spot": t.get("entry_spot", ""), "score": t.get("direction_entry", "")}


class TradeLog:
    """Append-only trade_log CSV (OPEN and CLOSE events). On restart it reads what is already logged, so nothing is logged twice."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.seen = set()
        if path.exists() and path.stat().st_size:
            old = pd.read_csv(path, dtype=str)
            self.seen = set(zip(old["trade_id"], old["event"]))

    def add(self, row: dict) -> None:
        row = {c: row.get(c, "") for c in LOG_COLS}
        row["logged_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new = not self.path.exists() or self.path.stat().st_size == 0
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=LOG_COLS)
            if new:
                w.writeheader()
            w.writerow(row)
        self.seen.add((row["trade_id"], row["event"]))
        lines = [f"TRADE {row['event']}  {row['trade_id']}  {row['strategy']}  {row['spread']}  (expiry {row['expiry'] or 'n/a'})",
                 f"    legs: {row['legs']}"]
        if row["event"] == "CLOSE":
            lines.append(f"    qty {row['qty']}   credit {row['entry_credit']}   exit cost {row['exit_cost']}   "
                         f"P&L Rs {float(row['pnl']):,.0f} ({row['pnl_pct_capital']}% of capital)   reason {row['reason']}")
        else:
            lines.append(f"    qty {row['qty']}   credit {row['entry_credit']}   stop at cost {row['stop_cost']}   "
                         f"target at cost {row['target_cost']}   spot {row['entry_spot']}")
        if row["note"]:
            lines.append(f"    note: {row['note']}")
        log.info("\n" + "\n".join(lines))


def latest_stamp(fdir: Path):
    """Newest index bar saved (last line of the newest nifty_<date>.csv): changes whenever the collector adds a bar."""
    fs = sorted(fdir.glob("nifty_????????.csv"))
    if not fs:
        return None
    try:
        with open(fs[-1], "rb") as f:
            f.seek(0, 2)
            f.seek(max(f.tell() - 400, 0))
            last = f.read().decode("utf-8", "ignore").strip().splitlines()[-1]
        return f"{fs[-1].name}|{last.split(',')[0]}"
    except OSError:
        return None


NO_OPEN = [False]


def apply_overrides(cfg: dict, a) -> None:
    NO_OPEN[0] = bool(getattr(a, "no_open", False))
    ld = cfg["live_data"]
    if a.expiry_code:
        ld["expiry_code"] = int(a.expiry_code) if a.expiry_code.isdigit() else a.expiry_code
    if a.start:
        ld["start_date"] = a.start
    if a.end:
        ld["end_date"] = a.end
    if a.lookback_days:
        ld["lookback_days"] = a.lookback_days


def live(config_path: str, folder, timeframe, poll, args=None) -> None:
    """Run continuously: whenever the collector saves a new bar, re-run the strategy on everything and report what changed
    (trade opened, trade closed with P&L and reason) plus a one-line status per bar. Trades go to the trade-log CSV."""
    cfg, S = setup(config_path, timeframe)
    if args is not None:
        apply_overrides(cfg, args)
    lc = cfg.get("live") or {}
    cfg["live_data"].setdefault("lookback_days", int(lc.get("lookback_days", 20)))   # live: recent days only (fast on 1-minute data)
    poll = int(poll or lc.get("poll_seconds", 15))
    fdir = rel(folder or cfg["live_data"]["folder"])
    tl = TradeLog(rel(lc.get("trade_log", str(Path(cfg.get("results_dir", "results/run")) / "trade_log.csv"))))
    capital = float(cfg["backtest"]["initial_capital"])
    end_t = datetime.strptime(str(cfg["session"]["end"]), "%H:%M").time()
    logs = rel("logs")
    logs.mkdir(exist_ok=True)
    fh = logging.FileHandler(logs / "run_strategy_live.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(fh)
    log.info(f"LIVE strategy run: config {config_path}, data {fdir}, trade log {tl.path}, polling every {poll}s. Ctrl+C stops.")
    first, last_stamp = True, None
    try:
        while True:
            stamp = latest_stamp(fdir)
            if stamp is not None and stamp != last_stamp:
                last_stamp = stamp
                try:
                    res = evaluate(cfg, S, fdir)
                    sig, trades, pos = res["sig"], res["trades"].round(2), res["open"]
                    last_bar = sig.index[-1]
                    prov = None
                    for _, t in trades.iterrows():                      # closed trades: log OPEN (if new) and CLOSE
                        tid = f"{pd.Timestamp(t['entry_time']):%Y-%m-%d %H:%M}"
                        # The strategy closes whatever is open at the LAST bar of the data (EOD / Expiry / NoData). Mid-day that is
                        # just "no more data yet", not a real exit: the position is still open, so it is not logged as closed. (An Expiry exit at
                        # the last bar is confirmed by the next bar: if the next day's data shows it closed there, it is logged then.)
                        reason = t.get("reason")
                        if pd.Timestamp(t["exit_time"]) == last_bar and (
                                reason in ("Expiry", "NoData") or (reason == "EOD" and last_bar.time() < end_t)):
                            prov = t
                            if (tid, "OPEN") not in tl.seen:
                                tl.add(_open_row_from_trade(t, cfg, "open at start-up" if first else ""))
                            continue
                        if first and not lc.get("log_history", False):   # history before the first pass is only the baseline
                            tl.seen |= {(tid, "OPEN"), (tid, "CLOSE")}
                            continue
                        note = "history" if first else ""
                        if (tid, "OPEN") not in tl.seen:
                            tl.add(_open_row_from_trade(t, cfg, note))
                        if (tid, "CLOSE") not in tl.seen:
                            tl.add(_row_from_trade(t, capital, "CLOSE", note))
                    if pos:
                        tid = f"{pos['entry_time']:%Y-%m-%d %H:%M}"
                        if (tid, "OPEN") not in tl.seen:
                            tl.add(_row_from_position(pos, "OPEN", "open at start-up" if first else ""))
                    status = f"{sig.index[-1]:%Y-%m-%d %H:%M}  {state_text(sig)}"
                    if res["lag"]:
                        status += f" [option data {res['lag']} min behind the index]"
                    if prov is not None:
                        status += (f" | OPEN {prov.get('spread', '')} exp {prov.get('expiry', 'n/a')}: credit {float(prov['entry']):.2f}, cost to close {float(prov['exit']):.2f}, "
                                   f"unrealised Rs {float(prov['pnl']):,.0f}")
                    elif pos:
                        mtm = (pos["p0"] - pos["last_p"]) * pos["qty"]
                        status += (f" | OPEN {pos['label']} exp {pos.get('expiry_date') or 'n/a'}: cost to close {pos['last_p']:.2f} (stop {pos['sl']:.2f}), "
                                   f"unrealised Rs {mtm:,.0f}")
                    else:
                        status += " | flat" + (f" (skipped at entry so far: {res['rejects']})" if res["rejects"] else "")
                    log.info(status)
                    log.info("    " + score_text(sig, cfg))
                    first = False
                except Exception as e:                                   # noqa: BLE001  (a bad cycle must not stop the loop)
                    log.exception(f"strategy cycle failed: {type(e).__name__}: {e}")
            time.sleep(poll)
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--folder", help="data folder (overrides live_data.folder), e.g. data/data_hourly")
    ap.add_argument("--timeframe", help="trade on bars of this size, built from the files' bars (e.g. 5min from data/data_minute); "
                                        "omit to trade the files' own bars")
    ap.add_argument("--live", action="store_true", help="run continuously on new data and log trades (live: section of the config)")
    ap.add_argument("--poll", type=int, help="seconds between checks for new bars in --live mode (default live.poll_seconds or 15)")
    ap.add_argument("--expiry-code", help="1, 2, 3 or nearest (overrides live_data.expiry_code)")
    ap.add_argument("--start", help="first day to use, YYYY-MM-DD (overrides live_data.start_date)")
    ap.add_argument("--end", help="last day to use, YYYY-MM-DD (overrides live_data.end_date)")
    ap.add_argument("--lookback-days", type=int, help="use only the newest N calendar days of files (live mode default: live.lookback_days or 20)")
    ap.add_argument("--open", action="store_true", help="open the HTML report in the browser (the config's report.open is true by default)")
    ap.add_argument("--no-open", action="store_true", help="do not open the report in the browser")
    a = ap.parse_args()
    a.open = a.open and not a.no_open
    if a.live:
        live(a.config, a.folder, a.timeframe, a.poll, a)
    else:
        main(a.config, a.folder, a.open, a.timeframe, a)
