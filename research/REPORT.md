# Strategy research report

Run 2026-11-10 07:45 UTC. 15 markets, 4 rules, daily candles (6 years) and 6-hour candles (crypto, 3 years). Costs: 0.1% slippage per side + Trench's 8% fee on wins, x1. Fills at the next bar's open. Paper research, not financial advice.

A pair passes with 15+ trades (5+ out-of-sample), profit factor > 1.3 in both the first 60% and the last 40% of history, and an out-of-sample return above buy-and-hold. It joins the live signals after passing 5 daily runs in a row and leaves after failing 5 in a row. A rule needs 3+ confirmed markets.

| Rule | Timeframe | Markets tested | Passed today | Passing today |
|---|---|---|---|---|
| Trend: close vs EMA50/EMA200, exit on EMA50 cross | 1d | 15 | 3 | BTC, XRP, AVAX |
| Trend: close vs EMA50/EMA200, exit on EMA50 cross | 6h | 10 | 0 | - |
| Breakout: 20-bar high/low entry, 10-bar exit | 1d | 15 | 0 | - |
| Breakout: 20-bar high/low entry, 10-bar exit | 6h | 10 | 0 | - |
| Momentum: price vs 90 bars ago, always in | 1d | 15 | 1 | ETH |
| Momentum: price vs 90 bars ago, always in | 6h | 10 | 0 | - |
| Dip-buy: RSI(2) < 10 above SMA200, exit above SMA5 (long only) | 1d | 15 | 0 | - |
| Dip-buy: RSI(2) < 10 above SMA200, exit above SMA5 (long only) | 6h | 10 | 0 | - |

## Selected for live signals

| Market (Trench) | Rule | TF | Trades | Win rate | PF in / out | Out-of-sample return vs hold | Max drawdown | Passed today |
|---|---|---|---|---|---|---|---|---|
| AVAX | trend_ema50_200 | 1d | 57 | 30% | 1.71 / 1.93 | +50% vs -77% | -57% | yes |
| BTC | trend_ema50_200 | 1d | 74 | 27% | 3.48 / 1.75 | +37% vs +10% | -64% | yes |
| XRP | trend_ema50_200 | 1d | 47 | 15% | 1.42 / 1.83 | +40% vs -62% | -72% | yes |

These are past results with simulated fills. Real fills, holding fees and gaps will differ. Many rule/market pairs were tested, so some passes can still be luck; that's why a rule needs 3+ markets and a 5-day streak.
