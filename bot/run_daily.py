"""Daily run: scan insider buys, sell what should be sold, buy new signals, email a report.

Usage:
  python -m bot.run_daily            normal run (what the scheduled task does)
  python -m bot.run_daily --dry-run  show what it would do; no orders, nothing saved
  python -m bot.run_daily --check    test Alpaca keys and send a test email
"""
import argparse
import json
import logging
import sys
import traceback
from datetime import date

from bot import portfolio
from bot.broker import AlpacaBroker
from bot.config import DATA_DIR, LOG_DIR, STATE_FILE, load_config, load_secrets
from bot.report import build_report, send_email
from bot.sec_insider import SecClient, recent_purchases
from bot.signals import find_signals

log = logging.getLogger("bot")


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


def load_state(cfg: dict) -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return portfolio.new_state(cfg["capital_usd"])


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def reconcile(state: dict, held: dict[str, float], today: date, notes: list[str]) -> None:
    """Drop positions the broker no longer has (e.g. sold by hand in the Alpaca app)."""
    for sym in list(state["positions"]):
        if held.get(sym, 0) <= 0:
            p = state["positions"][sym]
            portfolio.record_sell(state, sym, p["last_price"], today, "missing at broker")
            notes.append(f"{sym} was in the bot's records but not at Alpaca; marked as sold at last price.")


def sell(broker, state, sym, reason, today, actions, dry_run) -> None:
    p = state["positions"][sym]
    if dry_run:
        actions.append(f"WOULD SELL {sym} ({reason})")
        return
    fill = broker.sell(sym, p["qty"], f"ct-{today:%Y%m%d}-{sym}-sell")
    if fill:
        # A sell capped by the broker's holding still closes the bot's record.
        t = portfolio.record_sell(state, sym, fill.price, today, reason)
        actions.append(f"SOLD {sym} @ {fill.price:.2f}: {t['return_pct']:+.1%} ({reason})")
    else:
        actions.append(f"FAILED to sell {sym} ({reason}); will retry next run")


def run(dry_run: bool, send: bool) -> None:
    cfg = load_config()
    sec = load_secrets()
    today = date.today()
    state = load_state(cfg)
    actions, notes = [], []

    broker = AlpacaBroker(sec.alpaca_key, sec.alpaca_secret, paper=sec.alpaca_paper)
    is_open = broker.market_open()
    log.info("Alpaca %s account: %s, market open: %s",
             "paper" if sec.alpaca_paper else "LIVE", broker.account_summary(), is_open)

    log.info("Scanning SEC insider filings for the last %d days...", cfg["scan_days"])
    tx = recent_purchases(SecClient(sec.sec_user_agent), cfg["scan_days"], DATA_DIR / "cache", today)
    signals = find_signals(tx, today, cfg)
    log.info("%d insider purchases, %d signals", len(tx), len(signals))

    if not dry_run:
        reconcile(state, broker.positions(), today, notes)
    portfolio.update_marks(state, broker.latest_prices(list(state["positions"])))

    if not is_open:
        notes.append("Market closed (weekend/holiday/outside hours): no orders placed.")
    elif state["halted"]:
        notes.append("Kill switch active: no orders placed.")
    elif portfolio.kill_switch_hit(state, cfg):
        state["halted"] = True
        notes.append(f"Kill switch triggered: bot money fell {cfg['kill_switch_drawdown']:.0%} below start. Selling everything.")
        for sym in list(state["positions"]):
            sell(broker, state, sym, "kill switch", today, actions, dry_run)
    else:
        for sym, reason in portfolio.decide_exits(state, today, cfg):
            sell(broker, state, sym, reason, today, actions, dry_run)

        candidates = signals[[broker.can_buy(t) for t in signals["ticker"]]] if len(signals) else signals
        prices = broker.latest_prices(list(candidates["ticker"])) if len(candidates) else {}
        candidates = candidates[[prices.get(t, 0) >= cfg["signals"]["min_price"] for t in candidates["ticker"]]] \
            if len(candidates) else candidates
        for sym, notional, row in portfolio.decide_entries(state, candidates, today, cfg):
            why = f"{row['n_insiders']} insider(s) bought ${row['total_value']:,.0f}: {row['insiders']}"
            if dry_run:
                actions.append(f"WOULD BUY {sym} for ${notional:.2f} ({why})")
                continue
            fill = broker.buy(sym, notional, f"ct-{today:%Y%m%d}-{sym}-buy")
            if fill:
                portfolio.record_buy(state, sym, fill.qty, fill.price, today, why)
                actions.append(f"BOUGHT {sym} ${fill.qty * fill.price:.2f} @ {fill.price:.2f} ({why})")
            else:
                actions.append(f"FAILED to buy {sym}")

    state["last_run"] = today.isoformat()
    if not dry_run:
        save_state(state)

    subject, body = build_report(state, actions, signals, notes, sec.alpaca_paper)
    log.info("%s\n%s", subject, body)
    if send and sec.gmail_address and sec.gmail_app_password and sec.report_to:
        send_email(sec.gmail_address, sec.gmail_app_password, sec.report_to, subject, body)
        log.info("Report emailed to %s", sec.report_to)


def check() -> None:
    sec = load_secrets()
    broker = AlpacaBroker(sec.alpaca_key, sec.alpaca_secret, paper=sec.alpaca_paper)
    print("Alpaca OK:", "paper" if sec.alpaca_paper else "LIVE", broker.account_summary())
    print("Market open now:", broker.market_open())
    if sec.gmail_address and sec.gmail_app_password and sec.report_to:
        send_email(sec.gmail_address, sec.gmail_app_password, sec.report_to,
                   "[Trading bot] Test email", "If you can read this, email reports work.")
        print("Test email sent to", sec.report_to)
    else:
        print("Email not configured (GMAIL_ADDRESS / GMAIL_APP_PASSWORD / REPORT_TO in .env)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no orders, nothing saved")
    ap.add_argument("--check", action="store_true", help="test Alpaca keys and email")
    ap.add_argument("--no-email", action="store_true")
    args = ap.parse_args()
    setup_logging()
    if args.check:
        check()
        return
    try:
        run(dry_run=args.dry_run, send=not args.no_email and not args.dry_run)
    except Exception:
        log.exception("Bot run failed")
        try:
            sec = load_secrets()
            if not args.dry_run and sec.gmail_address and sec.gmail_app_password and sec.report_to:
                send_email(sec.gmail_address, sec.gmail_app_password, sec.report_to,
                           "[Trading bot] ERROR - run failed", traceback.format_exc())
        except Exception:
            log.exception("Could not send the error email either")
        sys.exit(1)


if __name__ == "__main__":
    main()
