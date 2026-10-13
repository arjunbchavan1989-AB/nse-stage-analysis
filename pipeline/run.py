#!/usr/bin/env python3
"""
NSE Stock Stage Analysis Pipeline
=================================
Auto-updating daily dashboard porting the 10-sub-stage classification (Steve Jacobs'
adaptation of Stan Weinstein's Stage Analysis, rules as specified by FreedomTradesx)
to the NSE equity market.

Universe filters (user-specified):
- Market cap > 500 crore (₹5,000,000,000)
- Avg daily volume > 70,000 shares (20-day average — assumption, documented)
- Avg 20-day rupee volume > 3 crore (₹30,000,000) — computed as mean(daily close * daily volume) over 20 sessions (unadjusted)

Stage classification rules (FreedomTradesx tweet, Steve Jacobs dashboard design):

Stage 1A — Upward Pivot:
  - 10-week EMA < 20-week EMA (weekly)
  - Price beginning to stabilize / form a base

Stage 1B — Mean Reversion:
  - 10-week EMA > 20-week EMA (weekly)
  - Price recovering from decline/consolidation

Stage 2A — Bullish Trend:
  - Price above 30-week SMA (weekly)
  - 30-week SMA rising (slope > 0 over 5 weeks)
  - Stock near recent highs / forming new highs

Stage 2B — Breakout Confirmation:
  - Price above rising 30-week SMA (weekly)
  - Price reaches a new 20-day high (daily)
  - Ideally above-average volume

Stage 2C — Extended Bullish:
  - Uptrend conditions
  - Price at least 4.6 ATR above the 50-day SMA (daily)
  - Steve Jacobs separate extension badge: ≥7 ATR above SMA50

Stage 3A — Bullish Fade:
  - Price remains above 30-week SMA (weekly)
  - Price has fallen below the 10-week EMA (weekly)

Stage 3B — Fade Confirmation:
  - Price remains above 30-week SMA (weekly)
  - 10-week EMA < 20-week EMA (weekly)

Stage 4A — Bearish Trend:
  - Price below 30-week SMA (weekly)
  - 30-week SMA declining (slope < 0 over 5 weeks)
  - Near recent lows / forming lower lows

Stage 4B — Breakdown Confirmation:
  - Price below declining 30-week SMA (weekly)
  - Price reaches a new 20-day low (daily)

Stage 4C — Extended Bearish:
  - Downtrend conditions
  - Price at least 3 ATR below the 50-day SMA (daily)

Precedence (documented): evaluated top-down 2C > 2B > 4C > 4B > 3A/3B > 2A > 4A > 1B > 1A > Unclassified.
Weekly indicators computed on completed weekly bars; daily classification uses latest daily close vs weekly indicators.
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
CACHE_DIR = HERE / "cache"
DATA_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)

# Configurable assumptions
ATR_PERIOD = 14
RS_LOOKBACK = 63  # ~3 months trading sessions
BULLISH_STAGE_EXT_ATR = 4.6   # FreedomTradesx Stage 2C
EXTENDED_BADGE_ATR = 7.0      # Steve Jacobs "Extended" badge (separate)
BEARISH_STAGE_EXT_ATR = 3.0   # FreedomTradesx Stage 4C
MA_SLOPE_LOOKBACK = 5         # weeks for MA slope
NEW_HIGH_LOW_LOOKBACK = 20    # trading days for 20-day high/low

# Universe filters
MIN_MARKET_CAP = 500e7           # ₹500 crore
MIN_AVG_VOLUME = 70000           # shares
MIN_AVG_RUPEE_VOL = 3e7          # ₹3 crore
MIN_AVG_DAILY_RANGE_PCT = 3.0     # 20-day average daily range % > 3% (user filter)

CRORE = 1e7

BENCHMARK = "^NSEI"  # NIFTY 50


def compute_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """Wilder's ATR."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    return atr


def build_daily_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add daily indicators to a daily OHLCV DataFrame (unadjusted)."""
    df = df.copy()
    df["sma_50d"] = df["close"].rolling(50).mean()
    df["atr_14d"] = compute_atr(df, ATR_PERIOD)
    df["prior_high_20d"] = df["high"].rolling(NEW_HIGH_LOW_LOOKBACK).max().shift(1)
    df["prior_low_20d"] = df["low"].rolling(NEW_HIGH_LOW_LOOKBACK).min().shift(1)
    df["avg_vol_20d"] = df["volume"].rolling(20).mean()
    df["avg_rvol_20d"] = (df["close"] * df["volume"]).rolling(20).mean()
    # Daily range %: (high - low) / previous close × 100 — True Range % proxy
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["daily_range_pct"] = tr / prev_close * 100
    df["avg_range_pct_20d"] = df["daily_range_pct"].rolling(20).mean()
    # Ret returns for RS computation
    df["ret"] = df["close"].pct_change()
    return df


def build_weekly_indicators(df_daily: pd.DataFrame) -> pd.DataFrame:
    """Resample daily OHLCV to weekly bars and compute weekly indicators.

    Uses only completed weekly bars for stable signals; current partial week is
    merged into the current bar only if the trading day is Friday or later in
    the week (the current day counts as the end of the partial week).
    """
    if df_daily.empty or len(df_daily) < 60:
        return pd.DataFrame()

    # Group by week start (Sunday-based weekly periods)
    df_daily = df_daily.copy()
    df_daily["week"] = df_daily.index.to_period("W-SUN").astype("datetime64[ns]")
    w = df_daily.groupby("week").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    # Remove current (possibly incomplete) week unless the last trading day is Friday+
    if not w.empty:
        last_week_start = w.index[-1]
        last_daily = df_daily.index[-1]
        is_last_week_complete = (
            last_daily.weekday() >= 4 and last_daily.normalize() >= last_week_start
        )
        if not is_last_week_complete:
            w = w.iloc[:-1]

    w["ema_10w"] = w["close"].ewm(span=10, adjust=False).mean()
    w["ema_20w"] = w["close"].ewm(span=20, adjust=False).mean()
    w["sma_30w"] = w["close"].rolling(30).mean()
    w["sma_30w_slope"] = w["sma_30w"].diff(MA_SLOPE_LOOKBACK)
    return w


def classify_stage(daily: pd.DataFrame, weekly: pd.DataFrame) -> dict:
    """Classify a stock into a 10-sub-stage using documented precedence.
    Returns dict with sub-stage, all individual rule flags, and ATR-extension metrics.
    """
    if daily.empty or weekly.empty or len(weekly) < 35 or len(daily) < 55:
        return {
            "substage": "UNCLASSIFIED",
            "reason": "insufficient history",
            "flags": {
                "atr_ext_above_sma50": None,
                "atr_ext_below_sma50": None,
                "new_20d_high": False,
                "new_20d_low": False,
            },
            "notes": "insufficient history",
            "steve_extended": False,
        }

    d = daily.iloc[-1]
    w = weekly.iloc[-1]

    flags = {
        # weekly flags
        "price_above_sma30w": bool(d["close"] > w["sma_30w"]),
        "price_below_sma30w": bool(d["close"] < w["sma_30w"]),
        "sma30w_rising": bool(w["sma_30w_slope"] > 0),
        "sma30w_falling": bool(w["sma_30w_slope"] < 0),
        "ema10w_above_ema20w": bool(w["ema_10w"] > w["ema_20w"]),
        "ema10w_below_ema20w": bool(w["ema_10w"] < w["ema_20w"]),
        "price_above_ema10w": bool(d["close"] > w["ema_10w"]),
        "price_below_ema10w": bool(d["close"] < w["ema_10w"]),
        # daily flags
        "new_20d_high": bool(d["high"] >= d["prior_high_20d"]),
        "new_20d_low": bool(d["low"] <= d["prior_low_20d"]),
    }

    atr = float(d["atr_14d"]) if not pd.isna(d["atr_14d"]) else None
    sma50 = float(d["sma_50d"]) if not pd.isna(d["sma_50d"]) else None

    ext_above_atr = None
    ext_below_atr = None
    if atr and atr > 0 and sma50:
        ext_above_atr = (float(d["close"]) - sma50) / atr
        ext_below_atr = (sma50 - float(d["close"])) / atr

    flags["atr_ext_above_sma50"] = ext_above_atr
    flags["atr_ext_below_sma50"] = ext_below_atr

    substage = "UNCLASSIFIED"
    notes = []

    # Precedence-based classification
    if flags["price_above_sma30w"] and flags["sma30w_rising"]:
        # Stage 2 candidate
        if ext_above_atr is not None and ext_above_atr >= BULLISH_STAGE_EXT_ATR:
            substage = "2C"
            notes.append("extended bullish")
        elif flags["new_20d_high"]:
            substage = "2B"
            notes.append("20d high breakout")
        elif flags["price_above_ema10w"]:
            substage = "2A"
            notes.append("bullish trend")
        else:
            # Above 30w SMA + rising, but below 10w EMA → Stage 3A (bullish fade)
            substage = "3A"
            notes.append("bullish fade (below 10w EMA)")
    elif flags["price_above_sma30w"] and flags["ema10w_below_ema20w"]:
        # Price above 30w SMA, 10w EMA < 20w EMA → Stage 3B (fade confirmation)
        substage = "3B"
        notes.append("fade confirmation")
    elif flags["price_above_sma30w"] and flags["ema10w_above_ema20w"] and not flags["price_above_ema10w"]:
        # Above 30w SMA, 10w > 20w, but below 10w EMA → Stage 3A
        substage = "3A"
        notes.append("bullish fade")
    elif flags["price_below_sma30w"] and flags["sma30w_falling"]:
        # Stage 4 candidate
        if ext_below_atr is not None and ext_below_atr >= BEARISH_STAGE_EXT_ATR:
            substage = "4C"
            notes.append("extended bearish")
        elif flags["new_20d_low"]:
            substage = "4B"
            notes.append("20d low breakdown")
        else:
            substage = "4A"
            notes.append("bearish trend")
    elif flags["price_below_sma30w"] and flags["ema10w_below_ema20w"]:
        # Below 30w SMA but not declining trend → Stage 1A (upward pivot forming)
        substage = "1A"
        notes.append("ignored pivot")
    elif flags["price_below_sma30w"] and flags["ema10w_above_ema20w"]:
        # Below 30w SMA, 10w > 20w → mean reversion / recovery
        substage = "1B"
        notes.append("mean reversion")
    elif flags["price_below_sma30w"] and flags["price_above_ema10w"]:
        # Below 30w SMA but above 10w EMA → potential 1B
        substage = "1B"
        notes.append("mean reversion (above 10w EMA)")
    elif flags["price_below_sma30w"] and flags["price_below_ema10w"]:
        # Below 30w SMA, below 10w EMA → 1A
        substage = "1A"
        notes.append("ignored pivot (below 10w EMA)")
    else:
        # Below 30w SMA but SMA is flat → 1A
        substage = "1A"
        notes.append("flat 30w SMA")

    # Steve Jacobs separate extension badge (≥7 ATR above SMA50)
    steve_ext = bool(ext_above_atr is not None and ext_above_atr >= EXTENDED_BADGE_ATR)

    return {
        "substage": substage,
        "notes": "; ".join(notes),
        "flags": flags,
        "steve_extended": steve_ext,
    }


def rs_percentile(stock_daily: pd.DataFrame, benchmark_daily: pd.DataFrame) -> float | None:
    """63-session RS percentile: (stock 63d return − NIFTY 63d return), then percentile
    rank within the full filtered universe.
    """
    if len(stock_daily) < RS_LOOKBACK + 1 or len(benchmark_daily) < RS_LOOKBACK + 1:
        return None
    s = stock_daily["close"].iloc[-1] / stock_daily["close"].iloc[-(RS_LOOKBACK + 1)] - 1
    b = benchmark_daily["close"].iloc[-1] / benchmark_daily["close"].iloc[-(RS_LOOKBACK + 1)] - 1
    return float(s - b)


def get_nse_universe() -> pd.DataFrame:
    """Load the official NSE EQUITY_L.csv universe (EQ series only, exclude ETFs/REITs/InvITs)."""
    csv_path = HERE / "equity_l.csv"
    if not csv_path.exists():
        import urllib.request
        url = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            csv_path.write_bytes(r.read())
    df = pd.read_csv(csv_path)
    df.columns = [c.strip() for c in df.columns]
    # EQ series only (exclude BE/BZ/SME/ETF/REIT/InvIT)
    df = df[df["SERIES"].str.strip() == "EQ"].copy()
    df["SYMBOL"] = df["SYMBOL"].str.strip()
    df = df[["SYMBOL", "NAME OF COMPANY", "DATE OF LISTING", "ISIN NUMBER"]].copy()
    return df


def _download_with_retries(symbols: list[str], max_retries: int = 4) -> pd.DataFrame:
    """Download OHLCV with retry+backoff, tracking failed symbols across attempts."""
    symbols_to_do = list(symbols)
    merged: pd.DataFrame | None = None
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Download attempt {attempt} ({len(symbols_to_do)} symbols)...")
            data = yf.download(
                symbols_to_do,
                period="15mo",
                interval="1d",
                auto_adjust=False,
                group_by="ticker",
                threads=True,
                progress=False,
            )
            if data.empty:
                raise RuntimeError("empty result")
            if merged is None:
                merged = data
            else:
                merged = pd.concat([merged, data], axis=1)
                merged = merged.loc[:, ~merged.columns.duplicated()]
            # Identify failed tickers (all-NaN or missing)
            failed = []
            for t in symbols_to_do:
                try:
                    sub = merged[t] if isinstance(merged.columns, pd.MultiIndex) else merged[[t]]
                    if sub.dropna(how="all").empty:
                        failed.append(t)
                except KeyError:
                    failed.append(t)
            logger.info(f"Attempt {attempt}: {len(symbols_to_do) - len(failed)} ok, {len(failed)} failed")
            if not failed:
                return merged
            symbols_to_do = failed
            time.sleep(min(60 * attempt, 180))
        except Exception as e:
            logger.warning(f"Download attempt {attempt} failed: {e}")
            time.sleep(min(60 * attempt, 180))
    logger.warning(f"Returning partial OHLCV after {max_retries} attempts")
    return merged



def run_pipeline(limit: int | None = None, skip_download: bool = False, dry_run: bool = False) -> None:
    """Main pipeline: fetch universe, download OHLCV, compute stages, emit dashboard JSON."""
    t0 = time.time()
    universe = get_nse_universe()
    if limit:
        universe = universe.head(limit)
    logger.info(f"Universe: {len(universe)} NSE EQ symbols")

    symbols = universe["SYMBOL"].tolist()
    yf_symbols = [f"{s}.NS" for s in symbols]

    # Cache key by date
    today = datetime.now().strftime("%Y-%m-%d")
    cache_path = CACHE_DIR / f"ohlcv_{today}.pkl"
    if not skip_download and not cache_path.exists():
        logger.info("Downloading OHLCV (this may take several minutes, with retries on rate limit)...")
        data = _download_with_retries(yf_symbols)
        if data is None or data.empty:
            logger.error("OHLCV download failed — keeping last good snapshot")
            return
        data.index.name = "date"
        data.to_pickle(cache_path)
    else:
        logger.info("Using cached OHLCV")

    data = pd.read_pickle(cache_path)
    logger.info(f"Loaded OHLCV: {data.shape}")

    # Benchmark (NIFTY 50)
    bench_cache = CACHE_DIR / f"bench_{today}.pkl"
    if not skip_download and not bench_cache.exists():
        bench = yf.download(BENCHMARK, period="15mo", interval="1d", auto_adjust=False, progress=False)
        if isinstance(bench.columns, pd.MultiIndex):
            bench.columns = bench.columns.get_level_values(0)
        bench.to_pickle(bench_cache)
    else:
        bench = pd.read_pickle(bench_cache)

    bench.index.name = "date"
    bench.columns = [c.lower() for c in bench.columns]

    # Build per-symbol frames
    rows = []
    errors = 0
    for sym in symbols:
        yf_sym = f"{sym}.NS"
        try:
            if isinstance(data.columns, pd.MultiIndex):
                df = data.xs(yf_sym, axis=1, level=0).dropna()
            else:
                df = data[yf_sym].dropna()
            if df.empty:
                errors += 1
                continue
            df.columns = [c.lower() for c in df.columns]
            df = df.dropna(subset=["open", "high", "low", "close", "volume"])
            df = df[df["volume"] > 0]
            if len(df) < 60:
                errors += 1
                continue
            rows.append((sym, df))
        except Exception as e:
            errors += 1
            logger.debug(f"Skip {sym}: {e}")
            continue

    logger.info(f"Parsed {len(rows)} symbols with valid data ({errors} skipped)")

    if dry_run:
        logger.info("Dry run complete — no JSON written")
        return

    # Filter universe: mcap proxy (close * shares outstanding — fetched per symbol,
    # but to keep pipeline fast we use shares from yfinance fast_info with a
    # daily batched quote fetch) and volume/rupee-volume filters
    # NOTE: For speed, we compute the universe filters first using only price/volume,
    # then fetch shares outstanding only for the passing set.
    passed_liquidity = []
    for sym, df in rows:
        if len(df) < 20:
            continue
        avg_vol = df["avg_vol_20d"] = df["volume"].rolling(20).mean().iloc[-1] if len(df) >= 20 else None
        avg_rvol = (df["close"] * df["volume"]).rolling(20).mean().iloc[-1] if len(df) >= 20 else None
        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        daily_range_pct = tr / prev_close * 100
        avg_range_pct = daily_range_pct.rolling(20).mean().iloc[-1]
        if (
            avg_vol is None
            or avg_rvol is None
            or avg_range_pct is None
            or pd.isna(avg_vol)
            or pd.isna(avg_rvol)
            or pd.isna(avg_range_pct)
        ):
            continue
        if (
            avg_vol > MIN_AVG_VOLUME
            and avg_rvol > MIN_AVG_RUPEE_VOL
            and avg_range_pct > MIN_AVG_DAILY_RANGE_PCT
        ):
            passed_liquidity.append((sym, df, avg_vol, avg_rvol, avg_range_pct))

    logger.info(f"Liquidity-passed: {len(passed_liquidity)} symbols")

    # Fetch shares outstanding for the liquidity-passed set (batched, using fast_info)
    # This is expensive; cache per-day.
    shares_cache_path = CACHE_DIR / f"shares_{today}.json"
    if shares_cache_path.exists():
        shares = json.loads(shares_cache_path.read_text())
    else:
        shares = {}
        missing_syms = [f"{s}.NS" for s, *_ in passed_liquidity]
        logger.info(f"Fetching shares outstanding for {len(missing_syms)} symbols...")
        for retry in range(1, 4):
            fetched_any = False
            for yfs in missing_syms:
                if yfs in shares:
                    continue
                try:
                    fi = yf.Ticker(yfs).fast_info
                    sh = fi.get("shares")
                    if sh:
                        shares[yfs] = sh
                        fetched_any = True
                except Exception:
                    continue
            if not fetched_any and retry < 3:
                logger.warning(f"Shares fetch retry {retry} (Yahoo rate limit)...")
                time.sleep(60 * retry)
            else:
                break
        shares_cache_path.write_text(json.dumps(shares))

    # Apply market cap filter
    passed_final = []
    for sym, df, avg_vol, avg_rvol, avg_range_pct in passed_liquidity:
        yf_sym = f"{sym}.NS"
        sh = shares.get(yf_sym)
        if not sh:
            continue
        mcap_inr = float(df.iloc[-1]["close"]) * sh
        if mcap_inr <= MIN_MARKET_CAP:
            continue
        passed_final.append((sym, df, avg_vol, avg_rvol, mcap_inr, avg_range_pct))

    logger.info(f"Final universe (mcap + liquidity): {len(passed_final)} symbols")

    # RS scores for all symbols in final universe
    rs_raw = []
    for sym, df, *_ in passed_final:
        r = rs_percentile(df, bench)
        rs_raw.append((sym, r))
    rs_vals = [r for _, r in rs_raw if r is not None]
    # Percentile rank within final universe
    def pct_rank(v):
        if not rs_vals:
            return None
        arr = np.array(rs_vals)
        return float((arr < v).sum() / len(arr) * 100)

    # Classify each symbol
    out_rows = []
    for sym, df, avg_vol, avg_rvol, mcap_inr, avg_range_pct in passed_final:
        daily = build_daily_indicators(df)
        weekly = build_weekly_indicators(df)
        if weekly.empty:
            continue
        classification = classify_stage(daily, weekly)
        rs_r = rs_percentile(df, bench)
        rs_pct = pct_rank(rs_r) if rs_r is not None else None
        d = daily.iloc[-1]
        prev_close = daily.iloc[-2]["close"] if len(daily) >= 2 else None
        chg_pct = ((d["close"] / prev_close) - 1) * 100 if prev_close else 0.0
        vol_ratio = d["volume"] / avg_vol if avg_vol else None

        out_rows.append(
            {
                "symbol": sym,
                "close": round(float(d["close"]), 2),
                "chg_pct": round(float(chg_pct), 2),
                "market_cap_cr": round(mcap_inr / CRORE, 1),
                "avg_vol_20d": int(avg_vol),
                "avg_rvol_20d_cr": round(avg_rvol / CRORE, 2),
                "avg_range_pct_20d": round(float(avg_range_pct), 2),
                "vol_ratio": round(float(vol_ratio), 2) if vol_ratio else None,
                "substage": classification["substage"],
                "steve_extended": classification["steve_extended"],
                "notes": classification["notes"],
                "rs_pct": round(rs_pct, 1) if rs_pct is not None else None,
                "atr_ext_above_sma50": round(classification["flags"]["atr_ext_above_sma50"], 2)
                if classification["flags"]["atr_ext_above_sma50"] is not None
                else None,
                "new_20d_high": classification["flags"]["new_20d_high"],
                "new_20d_low": classification["flags"]["new_20d_low"],
                "last_date": str(daily.index[-1].date()),
            }
        )

    # Fetch earnings dates (optional, best-effort) — use yf.Ticker.get_earnings_dates
    earn_cache = CACHE_DIR / f"earnings_{today}.json"
    if earn_cache.exists():
        earnings = json.loads(earn_cache.read_text())
    else:
        earnings = {}
        try:
            from concurrent.futures import ThreadPoolExecutor
            symbols_to_check = [r["symbol"] for r in out_rows]
            yf_syms = [f"{s}.NS" for s in symbols_to_check]
            tickers = yf.Tickers(" ".join(yf_syms))
            def _get(ts):
                try:
                    ed = ts.get_earnings_dates(limit=2)
                    if ed is not None and not ed.empty:
                        return ts.ticker, str(ed.index[0].date())
                except Exception:
                    pass
                return None
            with ThreadPoolExecutor(max_workers=8) as ex:
                for r in ex.map(_get, list(tickers.tickers.values())):
                    if r:
                        yfs, date = r
                        earnings[yfs.replace(".NS", "")] = date
        except Exception as e:
            logger.warning(f"Earnings fetch failed: {e}")
        earn_cache.write_text(json.dumps(earnings))

    # Add earnings countdown
    for r in out_rows:
        ed = earnings.get(r["symbol"])
        if ed:
            r["earnings_date"] = ed
        else:
            r["earnings_date"] = None

    # Aggregate panel data
    substage_counts = {}
    for r in out_rows:
        s = r["substage"]
        substage_counts[s] = substage_counts.get(s, 0) + 1

    bull = sum(1 for r in out_rows if r["substage"] in ("2A", "2B", "2C"))
    bear = sum(1 for r in out_rows if r["substage"] in ("4A", "4B", "4C"))
    neutral_1 = sum(1 for r in out_rows if r["substage"] in ("1A", "1B"))
    neutral_3 = sum(1 for r in out_rows if r["substage"] in ("3A", "3B"))
    unclassified = sum(1 for r in out_rows if r["substage"] == "UNCLASSIFIED")

    payload = {
        "generated_at": datetime.now().isoformat(),
        "last_trading_date": out_rows[0]["last_date"] if out_rows else None,
        "universe": {
            "nse_eq_total": int(len(symbols)),
            "liquidity_passed": len(passed_liquidity),
            "final_count": len(out_rows),
        },
        "filters": {
            "min_market_cap_cr": 500,
            "min_avg_volume_shares": 70000,
            "min_avg_rupee_vol_cr": 3,
            "min_avg_daily_range_pct": 3,
            "avg_volume_window_days": 20,
        },
        "substage_counts": substage_counts,
        "sentiment": {
            "bullish": bull,
            "bearish": bear,
            "stage1": neutral_1,
            "stage3": neutral_3,
            "unclassified": unclassified,
        },
        "rows": out_rows,
    }

    out_path = DATA_DIR / "latest.json"
    out_path.write_text(json.dumps(payload, indent=1))
    logger.info(f"Wrote {out_path} ({len(out_rows)} rows, {time.time()-t0:.0f}s)")

    # Also write historical snapshot for trend
    hist_path = DATA_DIR / f"snapshots/{today}.json"
    hist_path.parent.mkdir(exist_ok=True)
    hist_path.write_text(json.dumps(payload, indent=1))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="Limit universe size for testing")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if not args.verbose else logging.DEBUG)
    run_pipeline(limit=args.limit, skip_download=args.skip_download, dry_run=args.dry_run)
