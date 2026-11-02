# Moving-average crossover bot (Alpaca paper trading)

Buys when the 20-day moving average crosses **above** the 50-day, and sells when it crosses **below**, comparing the last two completed trading days. It only trades on your Alpaca **paper** account.

## Files

| File | What it is |
|---|---|
| `config.json` | Your settings: `symbol` (AAPL), `fast_ma` (20), `slow_ma` (50), `trade_qty` (1 share), `history_days` (100) |
| `.env.example` | Template for your Alpaca paper keys. Copy it to `.env`. |
| `.env` | Your keys (you create it). It's never uploaded to GitHub. |
| `bot.py` | The bot |
| `test_bot.py` | Offline tests: no internet, no keys, no orders |
| `trades.csv` | Every run's decision and any order placed (created on first run) |
| `state.json` | Remembers the last crossover it acted on, so a re-run never doubles an order |

## One-time setup

1. In Alpaca, switch to **Paper Trading** → **API Keys** → **Generate New Keys**. Paper key IDs start with **PK**.
2. Copy `.env.example` to `.env` in this folder and paste the two keys after the `=` signs.

The packages are already installed in `.venv`. If you ever need to reinstall them: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.

## Run it

```
cd ~/trading-projects/stock-bot
.venv/bin/python bot.py --dry     # show the decision, place nothing
.venv/bin/python bot.py           # place the order if there's a crossover
```

Run it once a day **after 4:15 PM ET**, when the day's close is final. Before then, it ignores today's still-forming bar.

## Rules it follows

- **BUY** `trade_qty` shares when the 20-day was at or below the 50-day yesterday and is above it today.
- **SELL** up to `trade_qty` shares when the 20-day was at or above the 50-day yesterday and is below it today. It only sells shares you already own, and never sells short.
- **No crossover means no trade.** A trend that's merely continuing isn't a signal.
- **Each crossover is acted on once,** however many times you run it.
- **Paper only:** the bot is hard-wired to Alpaca's paper API and refuses keys that don't start with PK.
- Orders are market orders, good for the day. Placed after hours, they fill at the next market open.

Moving-average crossovers lag the price and get whipsawed in sideways markets. Test it on paper before trusting it. Not financial advice.
