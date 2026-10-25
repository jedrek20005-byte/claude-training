"""The bot's own book-keeping and buy/sell decisions.

The bot tracks its own small budget (config `capital_usd`) separately from the
broker account, so a paper account with $100,000 still behaves like 20 PLN.
All functions here are pure logic, shared by the live bot and the backtest.
"""
import math
from datetime import date, timedelta


def new_state(capital: float) -> dict:
    return {"start_capital": capital, "realized_pnl": 0.0, "positions": {}, "history": [], "halted": False}


def cash(state: dict) -> float:
    invested = sum(p["cost"] for p in state["positions"].values())
    return state["start_capital"] + state["realized_pnl"] - invested


def equity(state: dict) -> float:
    return cash(state) + sum(p["qty"] * p["last_price"] for p in state["positions"].values())


def update_marks(state: dict, prices: dict) -> None:
    """Record the latest price and the highest price seen since buying."""
    for sym, p in state["positions"].items():
        price = prices.get(sym)
        if price and price > 0:
            p["last_price"] = float(price)
            p["peak_price"] = max(p["peak_price"], float(price))


def decide_exits(state: dict, today: date, cfg: dict) -> list[tuple[str, str]]:
    ex = cfg["exits"]
    out = []
    for sym, p in state["positions"].items():
        held = (today - date.fromisoformat(p["entry_date"])).days
        if held >= ex["max_hold_days"]:
            out.append((sym, f"held {held} days"))
        elif p["last_price"] <= p["peak_price"] * (1 - ex["trailing_stop_pct"]):
            out.append((sym, f"trailing stop ({p['last_price']:.2f} vs peak {p['peak_price']:.2f})"))
        elif ex.get("take_profit_pct") and p["last_price"] >= p["entry_price"] * (1 + ex["take_profit_pct"]):
            out.append((sym, "take profit"))
    return out


def kill_switch_hit(state: dict, cfg: dict) -> bool:
    return equity(state) <= state["start_capital"] * (1 - cfg["kill_switch_drawdown"])


def decide_entries(state: dict, signals, today: date, cfg: dict) -> list[tuple[str, float, dict]]:
    """Which signals to buy and how many dollars each (equal-weight slots)."""
    slots = cfg["max_positions"] - len(state["positions"])
    if slots <= 0 or state["halted"] or signals is None or len(signals) == 0:
        return []
    target = equity(state) / cfg["max_positions"]
    available = cash(state)
    cooldown = timedelta(days=cfg["exits"]["rebuy_cooldown_days"])
    recently_sold = {t["symbol"] for t in state["history"]
                     if today - date.fromisoformat(t["exit_date"]) < cooldown}
    out = []
    for row in signals.to_dict("records"):
        sym = row["ticker"]
        if sym in state["positions"] or sym in recently_sold:
            continue
        notional = math.floor(min(target, available) * 100) / 100  # never round up past the cash
        if notional < cfg["min_order_usd"]:
            break
        out.append((sym, notional, row))
        available -= notional
        if len(out) >= slots:
            break
    return out


def record_buy(state: dict, sym: str, qty: float, price: float, today: date, note: str = "") -> None:
    state["positions"][sym] = {
        "qty": qty, "cost": qty * price, "entry_price": price, "entry_date": today.isoformat(),
        "peak_price": price, "last_price": price, "note": note,
    }


def record_sell(state: dict, sym: str, price: float, today: date, reason: str) -> dict:
    p = state["positions"].pop(sym)
    proceeds = p["qty"] * price
    pnl = proceeds - p["cost"]
    state["realized_pnl"] += pnl
    trade = {
        "symbol": sym, "qty": p["qty"], "entry_date": p["entry_date"], "entry_price": p["entry_price"],
        "exit_date": today.isoformat(), "exit_price": price, "pnl": pnl,
        "return_pct": pnl / p["cost"] if p["cost"] else 0.0, "reason": reason,
    }
    state["history"].append(trade)
    return trade
