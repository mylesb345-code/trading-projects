# AI Trading Bot Team (paper mode only)

Four small "roles" work together to paper-trade 10 major coins. Paper trading means fake money: nothing here can spend real money.

**Not financial advice. Paper money only.** Don't connect a real-money account while you're learning.

## How the team works

Every 15 minutes, a trade idea has to pass all four roles in order:

| Role | What it does |
|---|---|
| **Scanner** | Downloads the latest 15-minute price candles for BTC, ETH, SOL, XRP, DOGE, ADA, AVAX, LINK, LTC and BCH from Coinbase. This needs no account. If a coin's data is missing or out of date, it's logged as MISSED instead of traded. |
| **Analyst** | Looks for a trend-and-momentum setup. It says **BUY** only when the 9-candle average (EMA9) has just crossed above the 21-candle average (EMA21) **and** momentum (RSI 14) is between 50 and 70. The stop-loss goes 1.5 × the recent average candle range (ATR) below the price, and the take-profit at twice that distance above it. |
| **Risk Manager** | Approves a trade only if it has a stop-loss and a take-profit. It sizes the trade so that hitting the stop loses at most **2%** of the paper account, fees and slippage included. Other rules: no leverage, at most 3 open trades, one trade per coin, and no new trades for the rest of the day once the account is down **6%** that day. |
| **Executor** | Places the **paper** trade. It refuses to start if anything looks like a real-money setup. |

Open trades are checked every minute. A trade closes at its stop-loss, at its take-profit, or after 48 hours. The bot only buys (no short selling), the same as a normal spot account.

## Two ways to run it

- **Simulated (default, easiest):** no account and no keys. The bot keeps a pretend $10,000 account on your computer and fills trades at the live Coinbase price, plus 0.05% slippage and a 0.25% fee per side. Every line it prints is labeled `PAPER-SIMULATED`.
- **Alpaca paper account:** trades go to Alpaca's paper-trading API, so you can watch them in Alpaca's dashboard. Every line is labeled `PAPER-ALPACA`. The web address is fixed to `https://paper-api.alpaca.markets`, and no setting can point it anywhere else.

Start with Simulated. Switch to Alpaca only if you want trades to show up in a broker's dashboard.

## Setup (about 15 minutes)

### 1. Install Python

- **Mac:** open Terminal and type `python3 --version`. If you see a version number of 3.9 or higher, you're done. If not, download the installer from [python.org/downloads](https://www.python.org/downloads/).
- **Windows:** download the installer from [python.org/downloads](https://www.python.org/downloads/). On its first screen, **tick "Add python.exe to PATH"**, then click Install. Open a new Command Prompt and type `python --version` to check.

The commands below use `python`. On a Mac, type `python3` instead.

### 2. Install dependencies

There aren't any. The bot only uses what comes with Python, so there's nothing to `pip install`.

### 3. Open a terminal in this folder

- **Mac:** in Terminal, type `cd ` (with a space), drag this folder onto the window, and press Return.
- **Windows:** open this folder in File Explorer, click the address bar, type `cmd`, and press Enter.

### 4. Check that everything works

```
python bot_team.py check
```

You should see `PAPER-SIMULATED` and `Market data OK`.

### 5. (Optional) Use an Alpaca paper account

1. Sign up for free at [alpaca.markets](https://alpaca.markets).
2. In the dashboard, switch to **Paper Trading** (top left). Make sure it says Paper.
3. Under **API Keys**, click **Generate New Keys**. A paper key ID starts with **PK**. If yours starts with **AK**, it's a real-money key: don't use it, because the bot will refuse it anyway.
4. In this folder, copy `.env.example` to a new file named exactly `.env`, and paste your key and secret after the `=` signs:
   ```
   PAPER_API_KEY=PK...
   PAPER_API_SECRET=...
   ```
5. In `config.json`, change `"executor": "simulated"` to `"executor": "alpaca"`.
6. Delete `state.json` if it exists (it belongs to the simulated account), then run `python bot_team.py check` again. You should see `PAPER-ALPACA: connected to https://paper-api.alpaca.markets`.

**Keep your keys private.** `.env` is already listed in `.gitignore`, so git won't upload it. Never paste your keys into a chat, a screenshot or the Python files.

### 6. Run it

```
python bot_team.py run
```

Leave the window open. The bot scans every 15 minutes and checks open trades every minute. Check that every trade line says **PAPER**.

The computer has to stay on and awake. If it sleeps, the bot notices when it wakes up and records the gap in `logs/downtime.csv`.

### Stopping

- **Pause:** press `Ctrl+C`. Open paper trades stay open and are picked up the next time you run it.
- **Kill switch:** create an empty file named `STOP` in this folder (Mac: `touch STOP`; Windows: `type nul > STOP`). Within a minute the bot closes every paper position, prints a summary and shuts down. Delete `STOP` to run it again.

### Other commands

| Command | What it does |
|---|---|
| `python bot_team.py check` | Checks settings, keys and market data without trading |
| `python bot_team.py once` | One pass, then stop |
| `python bot_team.py summary` | Results so far, including buy-and-hold |
| `python test_bot_team.py` | Runs the built-in tests (no internet needed) |

## Every file, in plain English

| File | What it is |
|---|---|
| `bot_team.py` | The program you run. It passes each idea through the four roles, writes the logs, and prints the summaries. |
| `roles.py` | The four roles: Scanner, Analyst, Risk Manager and Executor (simulated and Alpaca-paper). |
| `config.json` | Settings: the coins, strategy numbers, risk limits, fees and which executor to use. It must keep `"paper_only": true`, or the bot won't start. |
| `.env.example` | A template for your private keys. Copy it to `.env`; only needed for Alpaca. |
| `.env` | Your private paper keys. You create it. It's never uploaded. |
| `.gitignore` | Tells git never to upload `.env`, your state or your logs. |
| `test_bot_team.py` | Automatic tests for the strategy, the risk rules, the paper-only safety checks, the kill switch and the logs. |
| `state.json` | The bot's memory between runs: open positions, simulated cash and start prices. Created when you first run it. Delete it to start over. |
| `logs/` | Everything the bot did (see below). Created when you first run it. |

## The logs (open them in Excel or Google Sheets)

| File | What's in it |
|---|---|
| `logs/trades.csv` | One row per finished trade. Columns: date/time opened and closed, mode (PAPER-…), asset, entry, exit, position size (`qty`, `notional`), stop-loss, take-profit, fees estimate, slippage estimate, P&L in dollars and %, why it closed, **which roles approved it**, and the Analyst's and Risk Manager's reasons. |
| `logs/decisions.csv` | Every coin, every scan: WAIT, REJECTED (and why), OPENED or MISSED (data errors and stale data). |
| `logs/equity.csv` | The paper account's value at every scan. Max drawdown is calculated from this. |
| `logs/errors.csv` | API errors and anything else that went wrong. |
| `logs/downtime.csv` | Times the bot wasn't running. |
| `logs/summaries.txt` | Every daily summary. |

A summary prints once a day (at midnight UTC), whenever the kill switch is used, and on `python bot_team.py summary`. It shows total trades, win rate, average win and loss, total P&L, max drawdown, the result of simply holding the same 10 coins in equal amounts since the bot started, and how many errors, missed scans and minutes of downtime there were.

## Let it run for 2–4 weeks

One week is only a first look.

- Don't change the rules after every loss.
- Keep every losing trade in the CSV.
- Don't raise the risk because of a winning streak.
- Downtime, API errors and missed trades are logged automatically. Leave them in, because they're part of the result.

## Reading the results

Run `python bot_team.py summary` and compare **Paper account** with **Buy and hold**. If the bot made less than simply holding, the strategy didn't add anything over that period.

Then paste `logs/trades.csv` into Claude or Grok with this prompt:

> Here is my PAPER trading CSV. Explain the results in beginner-friendly language. Compare the bot with buy-and-hold. Identify repeated patterns in the losing trades. Do not change the strategy just to make the past look better. Suggest 3 small improvements I can test separately, one at a time.

## Known limits

- **Paper fills look better than real ones.** Real trading adds latency, wider spreads and partial fills.
- **Simulated prices come from Coinbase; Alpaca paper fills use Alpaca's prices.** The two can differ slightly.
- **Alpaca doesn't list every coin.** Coins it can't trade are logged as SKIPPED.
- **Fees are estimates** (`fee_bps` in `config.json`). Alpaca takes its crypto fee from the coins you receive, so its dashboard can differ from `trades.csv` by a few cents per trade.
- **A stop is checked once a minute**, so a fast move can close a trade a little past its stop, and a loss can be slightly more than 2%.
- **The strategy hasn't been backtested.** It's a common textbook setup, not a proven one. That's what the paper run is for.
- A viral claim like "$100 to $61,873" is unverified and not a normal or expected result.

## Final checklist

- [ ] Ran `python bot_team.py check` successfully
- [ ] (Alpaca only) Paper account created, paper keys (starting with PK) in `.env`, and `.env` listed in `.gitignore`
- [ ] Scanner, Analyst and Risk Manager decisions showing up in `logs/decisions.csv`
- [ ] Every trade labeled PAPER-SIMULATED or PAPER-ALPACA
- [ ] `logs/trades.csv` filling in with every trade, wins and losses
- [ ] Summary shows the buy-and-hold comparison
- [ ] Kill switch tested once (create `STOP`, watch it shut down, delete `STOP`)
- [ ] No real-money credentials anywhere
