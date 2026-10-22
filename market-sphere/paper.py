"""Paper-only executor and risk rules. There is no broker connection and no code path that sends an order anywhere.

Rules: only the #1 market, only above the score threshold, one position at a time, at most 1% of the paper account
at risk (fees and slippage included), no leverage, and no new entries for the rest of a day that lost 3%.
A setup found on a daily close fills at the next daily open, the same way in live paper mode and in the backtest.
"""
import math
from datetime import date as Date


def new_state(cfg):
    equity = float(cfg["starting_equity"])
    return {"equity": equity, "position": None, "pending": None, "day": None, "day_start_equity": equity,
            "day_realized": 0.0, "halted_day": None, "next_id": 1, "closed": [], "curve": [], "last_date": None,
            "logged_trades": 0}


def days_between(a, b):
    return (Date.fromisoformat(b) - Date.fromisoformat(a)).days


class PaperEngine:
    def __init__(self, cfg, state=None, log=None):
        self.cfg = cfg
        self.state = state if state is not None else new_state(cfg)
        self.log = log or (lambda kind, day, symbol, text: None)
        self.slip = cfg["slippage_bps"] / 10000
        self.fee = cfg["fee_bps"] / 10000

    def step(self, day, bars, scores, context=None):
        """Advance one day. bars: {symbol: candle} for markets with a candle that day; scores: {symbol: score}."""
        st = self.state
        if st["day"] != day:
            st["day"], st["day_start_equity"], st["day_realized"] = day, st["equity"], 0.0
        if st["pending"] and st["pending"]["symbol"] in bars:
            self._fill(st["pending"], bars[st["pending"]["symbol"]], day)
        if st["position"] and st["position"]["symbol"] in bars:
            self._manage(st["position"], bars[st["position"]["symbol"]], day)
        self._consider_entry(day, scores, context or {})
        st["curve"].append([day, round(self.marked_equity(), 2)])
        st["last_date"] = day

    def marked_equity(self):
        st, pos = self.state, self.state["position"]
        if not pos:
            return st["equity"]
        sign = 1 if pos["direction"] == "LONG" else -1
        return st["equity"] + sign * (pos["last_price"] - pos["entry"]) * pos["units"] - pos["entry_fee"]

    def _fill(self, order, candle, day):
        st, cfg = self.state, self.cfg
        st["pending"] = None
        sign = 1 if order["direction"] == "LONG" else -1
        entry = candle[1] * (1 + sign * self.slip)
        stop = order["stop"]
        per_unit = (entry - stop) * sign
        if per_unit <= 0:
            self.log("BLOCKED", day, order["symbol"], f"opened at {candle[1]:.6g}, already past the stop {stop:.6g}")
            return
        stop_fill = stop * (1 - sign * self.slip)
        cost_per_unit = (entry - stop_fill) * sign + (entry + stop_fill) * self.fee
        units = min(st["equity"] * cfg["risk_per_trade"] / cost_per_unit, st["equity"] / entry)
        pos = dict(order, id=st["next_id"], entry_date=day, entry=entry, units=units,
                   target=entry + sign * cfg["target_r"] * per_unit, risk=units * cost_per_unit,
                   entry_fee=units * entry * self.fee, last_price=candle[4])
        st["next_id"] += 1
        st["position"] = pos
        self.log("FILL", day, pos["symbol"],
                 f"#{pos['id']} {pos['direction']} {units:.6g} at {entry:.6g} | stop {stop:.6g} | "
                 f"target {pos['target']:.6g} | risking ${pos['risk']:,.2f}")

    def _manage(self, pos, candle, day):
        _, open_, high, low, close, _ = candle
        long = pos["direction"] == "LONG"
        stop, target = pos["stop"], pos["target"]
        price = reason = None
        if (low <= stop) if long else (high >= stop):  # stop first when both are touched in one candle
            price, reason = (min(open_, stop) if long else max(open_, stop)), "stop"
        elif (high >= target) if long else (low <= target):
            price, reason = (max(open_, target) if long else min(open_, target)), "target"
        elif days_between(pos["entry_date"], day) >= self.cfg["max_hold_days"]:
            price, reason = close, "time limit"
        if price is None:
            pos["last_price"] = close
        else:
            self._close(pos, price, reason, day)

    def _close(self, pos, price, reason, day):
        st, cfg = self.state, self.cfg
        sign = 1 if pos["direction"] == "LONG" else -1
        exit_ = price * (1 - sign * self.slip)
        fees = pos["units"] * (pos["entry"] + exit_) * self.fee
        pnl = sign * (exit_ - pos["entry"]) * pos["units"] - fees
        st["equity"] += pnl
        st["day_realized"] += pnl
        trade = {k: pos.get(k, "") for k in ("id", "symbol", "direction", "signal_date", "entry_date", "entry", "stop",
                                             "target", "units", "risk", "score", "rank", "why", "orderflow", "gamma")}
        trade.update(exit_date=day, exit=exit_, pnl=pnl, r=pnl / pos["risk"], reason=reason,
                     result="WIN" if pnl > 0 else "LOSS")
        st["closed"].append(trade)
        st["position"] = None
        self.log("EXIT", day, pos["symbol"], f"#{pos['id']} {reason} at {exit_:.6g} | P&L ${pnl:,.2f} ({trade['r']:+.2f}R) "
                                            f"| paper account ${st['equity']:,.2f}")
        if st["day_realized"] <= -cfg["daily_loss_limit"] * st["day_start_equity"]:
            st["halted_day"] = day
            self.log("HALT", day, "", f"down ${-st['day_realized']:,.2f} today, past the "
                                      f"{cfg['daily_loss_limit']:.0%} daily limit; no new entries until tomorrow")

    def _consider_entry(self, day, scores, context):
        st, cfg = self.state, self.cfg
        if st["position"] or st["pending"]:
            return self.log("SKIP", day, "", "a paper trade is already open or waiting to fill")
        if st["halted_day"] == day:
            return self.log("SKIP", day, "", "daily loss limit hit; no new entries today")
        ranked = sorted((s for s in scores.values() if s), key=lambda s: (-s["score"], s["symbol"]))
        if not ranked:
            return self.log("SKIP", day, "", "no market had a fresh candle with enough history")
        top = ranked[0]
        if top["score"] < cfg["min_score"]:
            return self.log("SKIP", day, top["symbol"],
                            f"top score {top['score']:.2f} is below the {cfg['min_score']:.2f} threshold")
        extra = context.get(top["symbol"], {})
        st["pending"] = {"symbol": top["symbol"], "direction": top["direction"], "stop": top["stop"],
                         "signal_date": day, "signal_close": top["close"], "score": top["score"], "rank": 1,
                         "why": top.get("why", ""), "orderflow": extra.get("orderflow", ""),
                         "gamma": extra.get("gamma", "")}
        self.log("SETUP", day, top["symbol"], f"ranked #1 at {top['score']:.2f}: {top['direction']} at the next open, "
                                              f"stop {top['stop']:.6g} ({top.get('why', '')})")


def summarize(trades, curve, start_equity):
    """Backtest statistics. curve is [[date, marked equity], ...]."""
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win, gross_loss = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in losses)
    peak, max_dd = None, 0.0
    for _, eq in curve:
        peak = eq if peak is None else max(peak, eq)
        max_dd = min(max_dd, eq / peak - 1)
    rets = [b / a - 1 for (_, a), (_, b) in zip(curve, curve[1:]) if a]
    mean = sum(rets) / len(rets) if rets else 0.0
    std = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) if len(rets) > 1 else 0.0
    end = curve[-1][1] if curve else start_equity

    def avg(xs):
        return sum(xs) / len(xs) if xs else None

    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else None,
        "avg_win_r": avg([t["r"] for t in wins]),
        "avg_loss_r": avg([t["r"] for t in losses]),
        "avg_r": avg([t["r"] for t in trades]),
        "profit_factor": gross_win / gross_loss if gross_loss else None,
        "max_drawdown": max_dd,
        "sharpe": mean / std * math.sqrt(365) if std else None,
        "total_return": end / start_equity - 1,
        "end_equity": end,
    }
