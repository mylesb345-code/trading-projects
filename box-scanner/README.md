# Box-theory swing scanner (stocks + crypto, Alpaca paper)

Scans US stocks and crypto for **box theory** setups around the clock, alerts you, and can optionally trade them on your Alpaca **paper** account.

- **darvas_breakout** (default): buy when price closes above a confirmed Darvas box on rising volume, in an uptrend.
- **range_reversion**: buy a confirmed bounce off the bottom quarter of a long trading range, and exit in the top quarter.

It only ever connects to Alpaca's **paper** API. It exits on start-up if a key, URL or account looks live.

## Setup

```bash
cd ~/trading-projects/box-scanner
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # already done on your Mac
cp .env.example .env        # then paste your Alpaca PAPER keys into .env
.venv/bin/python main.py check
```

`check` confirms the keys, that the account is a paper account (number starts with `PA`), that stock and crypto data download, and which alert channels are on.

## Commands

| Command | What it does |
|---|---|
| `.venv/bin/python main.py check` | Verify keys, paper account, data and alerts |
| `.venv/bin/python main.py test-alert` | Send a test message to every enabled alert channel |
| `.venv/bin/python main.py scan crypto` | One crypto scan now (daily + 4-hour) |
| `.venv/bin/python main.py scan stocks` | One stock scan now. The first run downloads history for ~1,500 stocks and takes a while; after that only new candles are fetched. |
| `.venv/bin/python main.py run` | Run 24/7 on the schedule |
| `.venv/bin/python main.py report` | Win/loss report for the paper account, also sent to your alert channels |
| `.venv/bin/python backtest.py` | Backtest both strategies (~2.5 years, 50 stocks, 20 crypto pairs) |
| `.venv/bin/python -m unittest discover -s tests -t .` | Run the tests |

## Schedule (`main.py run`)

| When | What |
|---|---|
| Every 4 hours at :02 UTC (00:02, 04:02, …) | Crypto scan on 4-hour candles |
| 00:02 UTC daily | Crypto scan on daily candles |
| 9:40 AM New York, Mon–Fri | Stock scan on trading days (holidays skipped), after the opening rush. It reads yesterday's completed candles, re-checks each setup at the live price (skipping gaps through the stop or target, moves of 3%+ past the signal, and reward:risk under 1.5), then places orders. |
| Every 15 minutes | Heartbeat line in the log |

A crash on one symbol is logged and skipped. A crash in a whole scan is logged, and the process keeps running.

## Run it 24/7

Your Mac only scans while it's awake. For around the clock, use an always-on machine or a small cloud server (a $5–6/month VPS is plenty).

**systemd (Linux server):** copy the folder to the server, create `.venv` and `.env` there, then:

```bash
sudo cp box-scanner.service /etc/systemd/system/      # edit User= and the paths inside first
sudo systemctl enable --now box-scanner
journalctl -u box-scanner -f                          # watch it
```

It restarts automatically if it crashes or the server reboots.

**Docker:**

```bash
docker build -t box-scanner .
docker run -d --restart unless-stopped --env-file .env -v "$PWD/cache:/app/cache" \
  -v "$PWD/state:/app/state" -v "$PWD/logs:/app/logs" --name box-scanner box-scanner
```

## Files

| File | Purpose |
|---|---|
| `config.yaml` | Every setting, each one commented |
| `.env` / `.env.example` | Keys and alert secrets (`.env` is git-ignored and never printed or logged) |
| `main.py` | Commands |
| `scheduler.py` | The 24/7 schedule (APScheduler) |
| `scanner.py` | One scan: universe → closed bars → signals → rank → de-duplicate → alert → orders |
| `strategies/box_theory.py` | The box logic (pure functions, shared by the live scan and the backtest) |
| `data.py` | Alpaca bars, the SQLite candle cache, the closed-candle rule, universe filters |
| `broker.py` | Paper orders: stock brackets, crypto entries, closing positions |
| `risk.py` | Position sizing and the entry checks |
| `alerts.py` | Discord, Telegram, email and ntfy |
| `store.py` | SQLite log of signals (de-duplication), every order event, and crypto stops/targets |
| `backtest.py` | The backtest |
| `tests/` | Unit tests on synthetic prices |
| `cache/`, `state/`, `logs/`, `backtest_results/` | Created at run time (git-ignored) |

## The rules in plain English

**Darvas box**
- **Top:** a high that's the highest of the last 50 bars, with no higher high in the 3+ bars since.
- **Bottom:** after the top, the lowest low that the next 3 bars don't undercut.
- **Valid box:** every bar since the top stays between top and bottom, it has lasted at least 5 bars, and its height is 3–15% for stocks or 3–25% for crypto.
- **Buy:** the previous close was inside the box, the latest close is 0.2%+ above the top, volume is 1.5×+ its 20-bar average, and close > SMA50 > SMA200.
- **Stop:** box bottom × 0.995. **Target:** top + 2 × box height. A close below the bottom is an **exit** (and a short, for stocks only, if `allow_short: true`).

**Range reversion**
- **Range:** the high and low of the last 252 bars for stocks, or 365 for crypto.
- **Buy:** the bar dips into the bottom 25% and closes above both its open and the previous close, still inside the bottom zone. It never buys the middle.
- **Exit:** at the top 25%. **Stop:** range low × 0.99.

**Only closed candles.** Each bar is stamped with its close time, and nothing still forming is ever used.

**Ranking and de-duplication.** Signals are ranked by volume ratio, then tighter box, then reward:risk. The same symbol + box + direction never alerts or trades twice. In signals-only mode, an exit is alerted only for symbols the bot alerted a buy on.

**paper_auto.** Before every order it re-reads equity, positions and open orders, and skips symbols already held or ordered. It respects the 10-position cap, the 3-per-scan cap and the 3% daily loss stop.
- **Size:** 1% of equity at risk between entry and stop, capped at 10% of equity.
- **Stocks:** whole shares, as a bracket order (entry + stop-loss + take-profit, good till canceled).
- **Crypto:** fractional, as a plain market entry, because Alpaca has no crypto brackets. The bot watches the stop and target itself on every crypto scan.
- Every request, response and error is saved to the `orders` table in `state/scanner.sqlite` and to the log.

## Before switching to paper_auto

Run `backtest.py` and read `backtest_results/summary.txt`. Any test with fewer than 30 trades is flagged as too few to judge. Backtests model one position per symbol and include 0.1% slippage per side plus crypto fees. Real fills will differ. Not financial advice.

## On GitHub (how it runs for you)

- `.github/workflows/box-scanner.yml`: the stock scan and orders, 9:40 AM New York on trading days.
- `.github/workflows/box-report.yml`: the win/loss report to your phone, weekdays after the close.

It only manages positions it opened (logged in `state/scanner.sqlite`), never ones you opened by hand. It also sells anything it holds that its rules no longer allow, such as a leveraged, inverse or volatility fund.
