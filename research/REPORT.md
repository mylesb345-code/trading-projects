# Strategy research report

Run 2026-10-22 21:11 UTC. 15 markets, 4 rules, daily candles (6 years) and 6-hour candles (crypto, 3 years). Costs: 0.1% slippage per side + Trench's 8% fee on wins, x1. Fills at the next bar's open. Paper research, not financial advice.

A pair passes with 15+ trades (5+ out-of-sample), profit factor > 1.3 in both the first 60% and the last 40% of history, and an out-of-sample return above buy-and-hold. A rule is selected only if it passes on 3+ markets.

| Rule | Timeframe | Markets tested | Passed | Passing markets | Selected |
|---|---|---|---|---|---|
| Trend: close vs EMA50/EMA200, exit on EMA50 cross | 1d | 15 | 3 | BTC, XRP, AVAX | YES |
| Trend: close vs EMA50/EMA200, exit on EMA50 cross | 6h | 10 | 0 | - | no |
| Breakout: 20-bar high/low entry, 10-bar exit | 1d | 15 | 0 | - | no |
| Breakout: 20-bar high/low entry, 10-bar exit | 6h | 10 | 0 | - | no |
| Momentum: price vs 90 bars ago, always in | 1d | 15 | 1 | ETH | no |
| Momentum: price vs 90 bars ago, always in | 6h | 10 | 0 | - | no |
| Dip-buy: RSI(2) < 10 above SMA200, exit above SMA5 (long only) | 1d | 15 | 0 | - | no |
| Dip-buy: RSI(2) < 10 above SMA200, exit above SMA5 (long only) | 6h | 10 | 0 | - | no |

## Selected for live signals

| Market (Trench) | Rule | TF | Trades | Win rate | PF in / out | Out-of-sample return vs hold | Max drawdown |
|---|---|---|---|---|---|---|---|
| BTC | trend_ema50_200 | 1d | 71 | 27% | 4.00 / 2.02 | +52% vs +23% | -64% |
| XRP | trend_ema50_200 | 1d | 47 | 15% | 1.42 / 1.83 | +12% vs -56% | -72% |
| AVAX | trend_ema50_200 | 1d | 56 | 30% | 1.79 / 2.40 | +48% vs -73% | -57% |

These are past results with simulated fills. Real fills, holding fees and gaps will differ. Many rule/market pairs were tested, so some passes can still be luck; that's why a rule needs 3+ markets to be selected.
