# Swing trading bot (BTC)

This bot checks the market around the clock, follows the one rule that passed its backtest, paper-trades every signal, and sends an alert to your phone the moment the rule says to enter, exit or flip.

## What it does

- Pulls BTC, ETH and SOL daily prices from Coinbase. If Coinbase is down it uses Kraken.
- Follows the rule that passed testing on BTC daily candles (35 trades over 3 years, profit factor 3.16, profitable in 5 of 6 test periods):
  - **LONG** when the daily close is above the 50-day average (EMA50) and EMA50 is above the 200-day average (EMA200). Exit on a daily close below EMA50.
  - **SHORT** when the daily close is below EMA50 and EMA50 is below EMA200. Exit on a daily close above EMA50.
- Paper-trades every signal and writes each finished trade to `journal.csv`, after Trench's 8% fee on winning trades.
- Sends trade alerts to your phone, plus one status message a day after the daily close (8 PM ET, or 7 PM ET in winter).
- Tells you if it can't get prices for 3 checks in a row.

## What it does not do

- **It does not click Trench for you.** Trench.io has no API, so there's nothing for a bot to connect to. When an alert comes in, you place the trade yourself; it takes about a minute.
- **ETH and SOL are paper-only.** The rule was only tested on BTC. The bot tracks the other two quietly so you can see later whether the rule works on them. Don't trade them yet.
- **It's not a same-day money maker.** Trades last from a day to a few months. The test had 11 wins and 24 losses: the losses are small, and a few big wins carry the results.

## Step 1: phone alerts (free)

1. Install **ntfy** from the App Store or Google Play. It's free and needs no account.
2. The first time the bot runs, it prints a channel name like `swing-bot-1a2b3c4d5e6f` and saves it in `config.json`.
3. In the ntfy app, tap **+** and subscribe to exactly that name.
4. Run `python bot.py --test-alert`. A "Test alert" should arrive on your phone.

## Step 2: run it

On a computer with Python 3 installed (no extra packages needed):

```
python bot.py --loop
```

This checks every 15 minutes for as long as the computer stays on and awake. `python bot.py` runs a single check, which you can use with a scheduler.

To run it 24/7 without your computer, use GitHub Actions (below).

## Running it on GitHub Actions (24/7, free)

The file `.github/workflows/trading-bot.yml` (in the folder above this one) runs `python3 trading-bot/bot.py` once an hour on GitHub's computers, so your Mac can be off. After each run it saves `state.json` and `journal.csv` back into the repo, which is how the bot remembers its position.

- Keep the repo **private**. Your alert channel name is in `config.json`, and anyone who knows it can read your alerts.
- Hourly runs use about 720 of the 2,000 free minutes a private repo gets each month.
- To check it's working, open the repo on github.com → **Actions** → **trading-bot**. Each run is listed there; a green tick means it worked. **Run workflow** starts a check right away.
- To pause it, open **Actions** → **trading-bot** → **…** → **Disable workflow**.

## Volatility clock: when BTC moves the most

`volclock.py` runs right after the bot each hour. It looks at the last 90 days of hourly BTC prices and works out how far BTC typically moves within each hour of the day (New York time).

- **Big-move alert:** if the hour that just finished moved at least **2.5×** its normal amount for that time of day, and at least **1%** from high to low, your phone gets "BTC big move UP/DOWN" with the size of the move. It's a heads-up, not a trade signal. The swing rule still only acts on daily closes.
- **Sunday report:** once a week, your phone gets the busiest and quietest hours and days.
- `python volclock.py --report` sends the report right away.

Settings (optional, in `config.json`): `vol_spike_ratio`, `vol_spike_min_pct`, `vol_lookback_days`, `vol_timezone`.

**Read this:** the hours when BTC moves the most are also the hours when it can move furthest against you. Busy hours mean more risk as well as more opportunity, especially with a multiplier.

## Mirroring alerts in Trench's demo mode

When a BTC alert arrives, do the same thing in Trench's **demo** account, then add a row to `trench_demo.csv` (it opens in Excel, Numbers or Google Sheets):

| Column | What to write |
|---|---|
| alert_date | The date of the alert |
| alert | GO LONG, GO SHORT, EXIT or FLIP |
| trench_side | Up or Down |
| multiplier | The multiplier you picked. Use x1 to match the bot (`paper_multiplier` in `config.json`). If Trench's demo has no x1, set `paper_multiplier` to the same number. |
| stake | Demo money you put in |
| trench_entry_price | The price Trench showed when you opened |
| close_date, trench_exit_price, trench_result_usd | Fill these in when the exit alert comes |
| notes | Anything odd: a late alert, a price gap, a demo-mode quirk |

After a few trades, compare `trench_demo.csv` with the bot's `journal.csv`. Any differences show what Trench's real prices, fees and multiplier would have done to the paper results.

## What to do when an alert arrives

| Alert | On Trench |
|---|---|
| BTC GO LONG | Open BTC **Up** |
| BTC GO SHORT | Open BTC **Down** |
| BTC EXIT | Close your BTC position |
| FLIP | Close your position, then open the other direction |

- **Use x1.** The backtest used no leverage. At x10, a 10% move against you wipes out the trade, and BTC has moved that much within a single trade before.
- **Only stake money you can afford to lose.** Not financial advice.

## Known limits

- The backtest's open long started 2026-09-12 at $77,294. The bot's own average math puts the start of that signal on 2026-09-09. Around a crossover, the bot can be a few days early or late compared with the backtest.
- I haven't confirmed whether Trench charges a fee for holding a position for days or weeks. Check before you hold.
- Past results don't promise future ones. The edge was concentrated in strong uptrends.

## Files

- `bot.py`: the bot
- `config.json`: settings
- `state.json`: what the bot remembers between checks (created on first run)
- `journal.csv`: paper trade results (created after the first finished trade)
- `test_bot.py`: tests (`python test_bot.py`)
