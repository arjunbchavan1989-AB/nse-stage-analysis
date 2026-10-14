# NSE Stock Stage Analysis Dashboard

Auto-updating daily dashboard that classifies NSE equities into 10 Weinstein sub-stages
(1A/1B, 2A/2B/2C, 3A/3B, 4A/4B/4C) using the Steve Jacobs adaptation of Stan
Weinstein's Stage Analysis.

## Architecture

- `pipeline/` — Python pipeline that fetches NSE universe + OHLCV, applies universe
  filters, computes weekly/daily indicators, classifies into 10 sub-stages, and emits
  `web/data/latest.json`.
- `web/` — Self-contained static HTML/JS dashboard (dark theme, Chart.js, no build step).
- `.github/workflows/daily-update.yml` — GitHub Actions cron that runs the pipeline
  daily and deploys to GitHub Pages.

## Universe filters

- Market cap > ₹500 crore (close × shares outstanding)
- 20-day average daily volume > 70,000 shares
- 20-day average rupee volume > ₹3 crore (close × volume, unadjusted)
- 20-day average daily range % > 3% (True Range / previous close, 20-day average)
- NSE EQ series only (excludes BE/BZ, ETFs, REITs, InvITs)

## Stage rules (10 sub-stages)

| Sub-stage | Rule (weekly indicators) | Rule (daily indicators) |
|---|---|---|
| 1A — Upward Pivot | 10w EMA < 20w EMA, price stabilizing | — |
| 1B — Mean Reversion | 10w EMA > 20w EMA | — |
| 2A — Bullish Trend | Price > rising 30w SMA | — |
| 2B — Breakout Confirm | Price > rising 30w SMA | New 20-day high |
| 2C — Extended Bullish | Price > rising 30w SMA | ≥4.6 ATR above 50d SMA |
| 3A — Bullish Fade | Price > 30w SMA | Below 10w EMA |
| 3B — Fade Confirm | Price > 30w SMA, 10w EMA < 20w EMA | — |
| 4A — Bearish Trend | Price < falling 30w SMA | — |
| 4B — Breakdown Confirm | Price < falling 30w SMA | New 20-day low |
| 4C — Extended Bearish | Price < falling 30w SMA | ≥3 ATR below 50d SMA |

Additional Steve Jacobs badge: "Extended" — ≥7 ATR above SMA50 (separate from 2C).

RS score = 63-session return minus NIFTY MidSmallcap 400 return, aligned on common
trading dates and percentile-ranked within the
final universe (shown as circular badge 0–100).

Benchmark closes come from the exact Nifty MidSmallcap 400 price-index row in
NSE's daily index archive, not an ETF or the 50:50 variant. If sufficient current
benchmark history is unavailable, the pipeline fails rather than substituting an
unrelated index. The small benchmark CSV is persisted in `pipeline/data/`.

Each stage column has a `Copy TV` button. It copies the symbols currently visible
in that column, deduplicated and comma-separated as `NSE:SYMBOL`. Paste the copied
text into TradingView's Add symbols dialog; the dashboard cannot paste into another
browser tab automatically. Empty columns have a disabled button.

## Set up on your own GitHub account (one-time)

1. **Create a new repo** on GitHub.com (any name, e.g. `nse-stage-analysis`).

2. **Unzip the downloaded project** (or `git init` + copy files):

   ```bash
   unzip nse-stage-analysis-dashboard.zip -d nse-stage-analysis
   cd nse-stage-analysis
   ```

3. **Push to GitHub**:

   ```bash
   git init
   git add .
   git commit -m "Initial commit: NSE stage analysis dashboard"
   git branch -M main
   git remote add origin https://github.com/<your-username>/nse-stage-analysis.git
   git push -u origin main
   ```

4. **Enable GitHub Pages** in your repo:
   - Go to **Settings → Pages**
   - Under **Build and deployment → Source**, select **"GitHub Actions"**
   - This lets the workflow deploy the `gh-pages` branch via `peaceiris/actions-gh-pages`.

   > Note: Alternatively you can pick "Deploy from branch" and select `gh-pages` — either works, but "GitHub Actions" is preferred since our workflow already pushes to `gh-pages`.

5. **Verify the daily cron works**:
   - Go to **Actions → Daily Update Stage Analysis Dashboard → Run workflow** and click "Run workflow" to trigger a manual run now.
   - After it succeeds, visit `https://<your-username>.github.io/nse-stage-analysis/` to see the live dashboard.
   - The workflow is scheduled every weekday at **7:30 PM IST** (14:00 UTC) after NSE close.

6. **(Optional) View historical snapshots**: `dashboard/web/data/history/` stores one JSON per day for trend/backtest use.

## Local development

```bash
cd dashboard/pipeline
pip install -r requirements.txt
python run.py            # full run (~2-3 min, downloads fresh data)
python run.py --limit 50 --skip-download  # quick test with cache
```

Then serve the `dashboard/web/` folder statically (e.g. `python -m http.server`).

## How the daily auto-update works

1. GitHub Actions is scheduled at 14:00 UTC (7:30 PM IST) every weekday. This is the requested start time, not a guaranteed completion time; GitHub may delay scheduled jobs.
2. The pipeline fetches the NSE universe, applies the 4 filters, classifies all stocks, writes `latest.json`.
3. The workflow copies it to `dashboard/web/data/`, commits it to `main`, and pushes `dashboard/web/` to the `gh-pages` branch.
4. GitHub Pages serves `gh-pages` at your public URL — refreshed daily, no server needed.
5. Kell and EP are also scheduled at 19:30 IST, but wait for this evening's upstream batch in public GitHub `main`: Stage → Kell → EP. All three publish the same completed price session. No cross-repository dispatch token is required.

On weekends/holidays the last actual completed session remains the EOD date.
Snapshots include a timezone-aware generation timestamp and `refresh_batch`.
Missing prices, under-95% same-session coverage, or disagreement with the dated NSE
index archive stop publication. The last good live dashboard remains available,
but the failed workflow is red rather than falsely marking old data as updated.
If refreshing manually, run Stage first, then Kell and EP; their waiting steps
can also be started while Stage runs. UI-only downstream redeploys bypass waiting.

## Limitations / Assumptions

- Yahoo Finance (yfinance) remains the equity data source; the previously paused NSE equity-source migration is not resumed. Rate limits are handled with retries/backoff; failures do not republish old data as a successful refresh.
- ATR = daily Wilder ATR(14). MA slope = 5-week difference. Weekly bars = completed weeks (current partial week excluded).
- Steve Jacobs' "Buyable / Reduce / Restore / Sell" font-color statuses are not specified in the source material; this dashboard instead uses the sub-stage color coding (Stage 2 green, Stage 4 red, etc.).
- Earnings badges use Yahoo's next earnings date where available (many NSE symbols lack coverage — shown as no badge).
