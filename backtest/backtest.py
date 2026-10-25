"""Backtest the insider-buying strategy on past data.

Insider data: SEC "Insider Transactions Data Sets" (free, quarterly).
Prices: Yahoo Finance via yfinance (free).

Usage:
  python -m backtest.backtest --start 2022-01-01 --end 2025-06-30

Limitation: Yahoo only has prices for stocks that still trade, so companies that
went bankrupt or were delisted are skipped. That makes results look better than reality.
"""
import argparse
import io
import logging
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

from bot import portfolio
from bot.config import DATA_DIR, ROOT, load_config, load_secrets
from bot.sec_insider import COLUMNS, SEC, SecClient
from bot.signals import find_signals

log = logging.getLogger("backtest")
QUARTER_URL = SEC + "/files/structureddata/data/insider-transactions-data-sets/{y}q{q}_form345.zip"


def _dates(col: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(col, format="%d-%b-%Y", errors="coerce")
    return parsed.fillna(pd.to_datetime(col, errors="coerce"))


def load_quarter(client: SecClient, y: int, q: int, cache_dir: Path) -> pd.DataFrame:
    cache = cache_dir / f"{y}q{q}_purchases.pkl"
    if cache.exists():
        return pd.read_pickle(cache)
    log.info("Downloading SEC insider data set %dQ%d ...", y, q)
    raw = client.get(QUARTER_URL.format(y=y, q=q), binary=True)
    if raw is None:
        log.warning("No SEC data set for %dQ%d yet", y, q)
        return pd.DataFrame(columns=COLUMNS)
    read = dict(sep="\t", dtype=str, keep_default_na=False, on_bad_lines="skip")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        sub = pd.read_csv(z.open("SUBMISSION.tsv"), **read)
        own = pd.read_csv(z.open("REPORTINGOWNER.tsv"), **read)
        nd = pd.read_csv(z.open("NONDERIV_TRANS.tsv"), **read)

    sub = sub[sub["DOCUMENT_TYPE"] == "4"]
    nd = nd[nd["TRANS_CODE"] == "P"]
    own = own.drop_duplicates("ACCESSION_NUMBER")  # first owner only, as in live parsing
    df = nd.merge(sub, on="ACCESSION_NUMBER").merge(own, on="ACCESSION_NUMBER")
    rel = df["RPTOWNER_RELATIONSHIP"].str.lower()
    shares = pd.to_numeric(df["TRANS_SHARES"], errors="coerce").fillna(0)
    price = pd.to_numeric(df["TRANS_PRICEPERSHARE"], errors="coerce").fillna(0)
    out = pd.DataFrame({
        "ticker": df["ISSUERTRADINGSYMBOL"].str.upper().str.strip(),
        "issuer_cik": df["ISSUERCIK"].str.lstrip("0"),
        "owner_cik": df["RPTOWNERCIK"].str.lstrip("0"),
        "owner_name": df["RPTOWNERNAME"],
        "is_director": rel.str.contains("director"),
        "is_officer": rel.str.contains("officer"),
        "is_ten_pct": rel.str.contains("tenpercent"),
        "officer_title": df["RPTOWNER_TITLE"],
        "trans_date": _dates(df["TRANS_DATE"]),
        "filing_date": _dates(df["FILING_DATE"]),
        "code": "P",
        "acq_disp": df["TRANS_ACQUIRED_DISP_CD"].replace("", "A"),
        "shares": shares,
        "price": price,
        "value": shares * price,
        "accession": df["ACCESSION_NUMBER"],
    }).dropna(subset=["trans_date", "filing_date"])
    cache.parent.mkdir(parents=True, exist_ok=True)
    out.to_pickle(cache)
    return out


def quarters(start: date, end: date):
    y, q = start.year, (start.month - 1) // 3 + 1
    while (y, q) <= (end.year, (end.month - 1) // 3 + 1):
        yield y, q
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)


def download_prices(tickers: list[str], start: date, end: date) -> tuple[pd.DataFrame, pd.DataFrame]:
    opens, closes = [], []
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        log.info("Prices %d-%d of %d", i + 1, i + len(chunk), len(tickers))
        px = yf.download(chunk, start=start, end=end + timedelta(days=1), auto_adjust=True,
                         progress=False, group_by="column", threads=True)
        if px.empty:
            continue
        o, c = px["Open"], px["Close"]
        if isinstance(o, pd.Series):  # single ticker
            o, c = o.to_frame(chunk[0]), c.to_frame(chunk[0])
        opens.append(o)
        closes.append(c)
    return pd.concat(opens, axis=1), pd.concat(closes, axis=1)


def simulate(tx: pd.DataFrame, start: date, end: date, cfg: dict, capital: float):
    # Signals known each morning = filings up to the previous calendar day.
    days = pd.bdate_range(start, end)
    log.info("Computing signals for %d days ...", len(days))
    sigs = {d: find_signals(tx, d - timedelta(days=1), cfg) for d in days}
    tickers = sorted({t for s in sigs.values() for t in s["ticker"]} | {"SPY"})
    opens, closes = download_prices(tickers, start - timedelta(days=5), end)
    trading_days = [d for d in days if d in closes.index]

    slip = cfg["backtest_slippage"]
    state = portfolio.new_state(capital)
    curve = []
    for d in trading_days:
        today = d.date()
        o, c = opens.loc[d], closes.loc[d]

        # Morning: exits decided on yesterday's close, executed at today's open.
        if not state["halted"] and portfolio.kill_switch_hit(state, cfg):
            state["halted"] = True
            exits = [(s, "kill switch") for s in state["positions"]]
        else:
            exits = portfolio.decide_exits(state, today, cfg)
        for sym, reason in exits:
            px = o.get(sym)
            px = px if pd.notna(px) and px > 0 else state["positions"][sym]["last_price"]
            portfolio.record_sell(state, sym, px * (1 - slip), today, reason)

        tradable = sigs[d][[pd.notna(o.get(t)) and o.get(t, 0) >= cfg["signals"]["min_price"]
                            for t in sigs[d]["ticker"]]] if len(sigs[d]) else sigs[d]
        for sym, notional, _ in portfolio.decide_entries(state, tradable, today, cfg):
            px = o[sym] * (1 + slip)
            portfolio.record_buy(state, sym, notional / px, px, today)

        # Evening: mark to close.
        portfolio.update_marks(state, {s: c.get(s) for s in state["positions"] if pd.notna(c.get(s))})
        curve.append({"date": d, "equity": portfolio.equity(state), "spy": c.get("SPY")})
    return state, pd.DataFrame(curve).set_index("date")


def summarize(state: dict, curve: pd.DataFrame, capital: float) -> str:
    eq = curve["equity"]
    years = max((curve.index[-1] - curve.index[0]).days / 365.25, 1e-9)
    total = eq.iloc[-1] / capital - 1
    dd = (eq / eq.cummax() - 1).min()
    spy = curve["spy"].dropna()
    spy_total = spy.iloc[-1] / spy.iloc[0] - 1 if len(spy) > 1 else float("nan")
    trades = state["history"]
    wins = [t for t in trades if t["pnl"] > 0]
    avg = sum(t["return_pct"] for t in trades) / len(trades) if trades else 0.0
    return "\n".join([
        f"Period:            {curve.index[0].date()} -> {curve.index[-1].date()}",
        f"Strategy return:   {total:+.1%}  (per year {(1 + total) ** (1 / years) - 1:+.1%})",
        f"S&P 500 (SPY):     {spy_total:+.1%}",
        f"Worst drawdown:    {dd:.1%}",
        f"Closed trades:     {len(trades)}, winners {len(wins)} ({len(wins) / max(len(trades), 1):.0%}), "
        f"average {avg:+.1%} per trade",
        f"Still open:        {len(state['positions'])}",
        f"Kill switch hit:   {state['halted']}",
    ])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default=(date.today() - timedelta(days=120)).isoformat())
    ap.add_argument("--capital", type=float, default=10_000.0,
                    help="simulated money; results in %% are the same for any amount")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    cfg = load_config()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    client = SecClient(load_secrets().sec_user_agent)
    cache = DATA_DIR / "cache" / "datasets"
    tx = pd.concat([load_quarter(client, y, q, cache)
                    for y, q in quarters(start - timedelta(days=30), end)], ignore_index=True)
    log.info("%d insider purchases loaded", len(tx))

    state, curve = simulate(tx, start, end, cfg, args.capital)
    out = ROOT / "backtest" / "results"
    out.mkdir(parents=True, exist_ok=True)
    curve.to_csv(out / "equity_curve.csv")
    pd.DataFrame(state["history"]).to_csv(out / "trades.csv", index=False)
    summary = summarize(state, curve, args.capital)
    (out / "summary.txt").write_text(summary, encoding="utf-8")
    print("\n" + summary + f"\n\nDetails saved in {out}")


if __name__ == "__main__":
    main()
