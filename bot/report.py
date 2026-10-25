"""Daily report text and Gmail sending."""
import smtplib
from email.message import EmailMessage

from bot import portfolio


def build_report(state: dict, actions: list[str], signals, notes: list[str], paper: bool) -> tuple[str, str]:
    eq = portfolio.equity(state)
    start = state["start_capital"]
    change = eq - start
    mode = "PAPER" if paper else "REAL MONEY"
    subject = f"[Trading bot {mode}] ${eq:.2f} ({change / start:+.1%} since start)"

    lines = [f"Mode: {mode}",
             f"Bot money: ${eq:.2f} (start ${start:.2f}, {change:+.2f} USD, {change / start:+.1%})",
             f"Cash: ${portfolio.cash(state):.2f}   Realized P&L: {state['realized_pnl']:+.2f} USD"]
    if state["halted"]:
        lines.append("!!! KILL SWITCH ACTIVE - the bot has stopped trading. See README to reset. !!!")

    lines += ["", "Today's actions:"] + ([f"  - {a}" for a in actions] or ["  (none)"])

    lines += ["", "Open positions:"]
    for sym, p in state["positions"].items():
        ret = p["last_price"] / p["entry_price"] - 1
        lines.append(f"  {sym:6} since {p['entry_date']}  buy {p['entry_price']:.2f}  now {p['last_price']:.2f}"
                     f"  ({ret:+.1%})  value ${p['qty'] * p['last_price']:.2f}")
    if not state["positions"]:
        lines.append("  (none)")

    closed = state["history"]
    if closed:
        wins = sum(1 for t in closed if t["pnl"] > 0)
        lines += ["", f"Closed trades: {len(closed)}, winners: {wins} ({wins / len(closed):.0%})"]
        for t in closed[-5:]:
            lines.append(f"  {t['symbol']:6} {t['entry_date']} -> {t['exit_date']}  {t['return_pct']:+.1%}  ({t['reason']})")

    lines += ["", "Top insider-buying signals today:"]
    if signals is not None and len(signals):
        for r in signals.head(8).to_dict("records"):
            lines.append(f"  {r['ticker']:6} score {r['score']:.1f}  {r['n_insiders']} insider(s)  "
                         f"${r['total_value']:,.0f}{'  CEO/CFO' if r['ceo_cfo'] else ''}")
    else:
        lines.append("  (none)")

    if notes:
        lines += ["", "Notes:"] + [f"  - {n}" for n in notes]
    return subject, "\n".join(lines)


def send_email(address: str, app_password: str, to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = address, to, subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(address, app_password)
        s.send_message(msg)
