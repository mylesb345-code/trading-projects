# Market Sphere (paper mode only)

Scans 24 markets every day, scores each one from 0.00 to 1.00, ranks them, and paper-trades only the #1 setup. It has no broker connection and no code that can place a real order.

**Read this first:** the viral "$200 → $4,523 in 48 hours" claim is unverified. This tool has not been backtested on real data yet: the cloud computer it was built on can't reach price sites. Run the backtest (step 3) before you trust anything it says.

## What's inside (the guide's five parts)

| Part | What it does here |
|---|---|
| Market scanner | Daily candles for 10 cryptos (Coinbase, falling back to Yahoo) plus gold, silver, copper, platinum, WTI, Brent, natural gas, 4 US index futures and 3 forex pairs (Yahoo). |
| Order-flow reader | For the cryptos only: two Coinbase order-book snapshots taken 15 seconds apart (bid/ask imbalance within 0.5% of the price, biggest resting orders) plus who was aggressive in the last 200 trades. It gives a 0–100 score, a BULLISH/BEARISH/NEUTRAL bias, the key levels (the biggest bid and ask walls), whether those walls **held or were pulled** between snapshots, whether liquidity is appearing or disappearing, and the main risk. **Shown on the board, not part of the score**: there's no free order-flow history, so it can't be backtested, and a wall that held for 15 seconds can still be pulled. |
| Gamma mapper | **Off.** There's no reliable free options data for these markets, so it always reads `UNCLEAR (no options data)` instead of making levels up. |
| Scoring engine | Five parts, each 0–1, weighted into one score (weights in `config.json`). A missing part counts as 0. |
| Paper executor | Only the #1 market, only if its score is at least 0.70, one position at a time. |
| Leaderboard + sphere | A ranked table in the terminal, plus `logs/sphere.html`: every market as a point on a turning 3D sphere, with #1 highlighted. |

### How the score works

Direction comes from the trend: **LONG** if the 50-day average is above the 200-day average, otherwise **SHORT**.

- **Trend (35%)**: price, 20-day, 50-day and 200-day averages lined up in that direction, and how fast the 50-day is moving.
- **Range position (20%)**: for a long, how close price is to its 55-day high (for a short, to its 55-day low).
- **Volatility (15%)**: full marks when today's volatility is in the middle of its past year; it fades toward the extremes, so wild markets are skipped.
- **Volume (15%)**: recent volume compared with normal. Yahoo has no volume for forex, so forex always loses this 15%.
- **Not overextended (15%)**: full marks within 1 ATR of the 20-day average, falling to 0 at 3 ATR.

### Paper risk rules

- Risk at most **1%** of the paper account per trade, fees and slippage included.
- **No leverage**: a position is never bigger than the account.
- The stop sits **2 ATR** from the setup price and the target is **2R**. The trade closes at the stop, the target, or after **20 days**.
- If the stop and target are both touched in the same candle, the stop is counted (the cautious choice).
- A setup found on a daily close fills at the **next daily open**. If that open is already past the stop, the trade is blocked.
- After a **3%** losing day, no new entries until the next day.
- Fees are 0.10% and slippage 0.05% per side. The live paper account and the backtest run the exact same code.

## Setup

1. **Install Python 3** from [python.org](https://www.python.org/downloads/). On Windows, tick "Add python.exe to PATH". There are no extra packages to install.
2. **Copy this folder** to your computer and open a terminal in it.
3. **Backtest first:** `python sphere.py backtest` (add `--years 5` for longer). It prints trades, win rate, average R, profit factor, max drawdown, Sharpe and return, for the full period, the first 70% and the last 30%. The results go to `logs/backtest_trades.csv` and `logs/backtest_summary.json`.
4. **See today's board:** `python sphere.py scan`. Under the table, **Top setup** gives the #1 market's direction, entry zone, stop, target and why it ranked first. For crypto, a second line gives the order-flow read. Each run also writes `logs/sphere.html`; double-click it to open the sphere in your browser.
5. **Run the paper account:** `python sphere.py run`. Run it once a day after 1 AM ET, or leave `python sphere.py run --loop` going (it checks every hour). Each run only processes daily closes it hasn't seen yet.
6. **Phone alerts (optional):** install the free ntfy app and subscribe to a hard-to-guess channel name. Copy `.env.example` to `.env`, put the name after `NTFY_TOPIC=`, then run `python sphere.py test-alert`.

## Files

- `sphere.py`: the program you run (scan, run, backtest, test-alert), the leaderboard and the sphere view.
- `marketdata.py`: downloads candles and the order-flow snapshot.
- `scoring.py`: indicators and the 0–1 score.
- `paper.py`: the paper executor, risk rules and backtest statistics.
- `config.json`: markets, weights, threshold and risk settings. It refuses to run unless `"paper_only": true`.
- `.env.example`: template for your private settings. Your real `.env` never leaves your computer.
- `test_sphere.py`: tests (`python test_sphere.py`).

Each run writes to the `logs/` folder (import any of these into Google Sheets):
- `scores.csv`: every market's score and parts, every day.
- `sphere.html`: the latest leaderboard as a 3D sphere (open in a browser).
- `events.csv`: every setup, fill, skip, exit and halt, with the reason.
- `trades.csv`: each closed paper trade with entry, stop, target, size, P&L, R, why it was taken and why it closed.

`paper_state.json` is the paper account. Delete it to start over.

## Known limits

- **Untested on real data.** The tests use made-up prices. Run the backtest first.
- **Downloads retry automatically.** A dropped connection, a timeout or a rate limit is retried twice with a growing pause before a market is marked `ERROR`.
- **Yahoo isn't an official API**, so it can rate-limit or change without notice. Problem markets show as `ERROR` on the board and are left out.
- **Check the dates on your first scan.** Futures and forex candles are dated in their exchange's time zone; I couldn't confirm this against live Yahoo data.
- **The weights weren't tuned, and that's on purpose.** Don't adjust them until one period looks perfect; that's overfitting. Change one thing at a time and re-run the backtest.
- **Paper fills look better than real ones.** Real trading adds latency, wider spreads and partial fills.

Not financial advice.
