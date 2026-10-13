"""Recalculate RS on the existing snapshot without changing universe or stages."""
import json
from pathlib import Path
import pandas as pd
import yfinance as yf
from run import BENCHMARK, CACHE_DIR, load_benchmark, rs_percentile


def main():
    root = Path(__file__).resolve().parents[1]
    path = root / "web/data/latest.json"
    payload = json.loads(path.read_text())
    as_of = payload["last_trading_date"]
    benchmark = load_benchmark(as_of)
    prices = pd.read_pickle(CACHE_DIR / f"ohlcv_{as_of}.pkl")
    missing = []
    for row in payload["rows"]:
        symbol = row["symbol"] + ".NS"
        if symbol not in prices.columns.get_level_values(0) or prices[symbol].dropna().empty:
            missing.append(symbol)
    if missing:
        fill = yf.download(missing, start=str(benchmark.index.min().date()),
                           end=str((pd.Timestamp(as_of) + pd.Timedelta(days=1)).date()),
                           group_by="ticker", auto_adjust=False, progress=False, threads=4)
        prices = fill.combine_first(prices)
    scores = {}
    for row in payload["rows"]:
        try:
            stock = prices[row["symbol"] + ".NS"].dropna()
            stock.columns = stock.columns.str.lower()
            stock = stock.loc[stock.index <= pd.Timestamp(as_of)]
            scores[row["symbol"]] = rs_percentile(stock, benchmark)
        except KeyError:
            scores[row["symbol"]] = None
    values = [v for v in scores.values() if v is not None]
    for row in payload["rows"]:
        value = scores[row["symbol"]]
        row["rs_excess_pct"] = round(value * 100, 2) if value is not None else None
        row["rs_pct"] = round(sum(v < value for v in values) / len(values) * 100, 1) if value is not None else None
    payload["rs_benchmark"] = BENCHMARK
    path.write_text(json.dumps(payload, indent=1))
    (root / "pipeline/data/latest.json").write_text(json.dumps(payload, indent=1))
    print(f"Updated benchmark for {len(values)} stocks; unchanged universe and stage classifications.")


if __name__ == "__main__":
    main()
