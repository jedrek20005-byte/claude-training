from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from bot import portfolio
from bot.config import load_config
from bot.sec_insider import _extract_xml, parse_form4, to_frame
from bot.signals import find_signals

FIXTURE = Path(__file__).parent / "fixtures" / "form4_sample.xml"


@pytest.fixture
def cfg():
    return load_config()


def purchase(ticker, owner, value, filed="2026-10-21", traded="2026-10-20", title="Director",
             officer=False, director=True, price=20.0):
    return {"ticker": ticker, "issuer_cik": "1", "owner_cik": owner, "owner_name": f"Owner {owner}",
            "is_director": director, "is_officer": officer, "is_ten_pct": False, "officer_title": title,
            "trans_date": traded, "filing_date": filed, "code": "P", "acq_disp": "A",
            "shares": value / price, "price": price, "value": value, "accession": owner}


def test_parse_form4_keeps_only_purchases():
    xml = _extract_xml("<SEC-DOCUMENT>\n<XML>\n" + FIXTURE.read_text() + "</XML>")
    rows = parse_form4(xml, date(2026, 10, 21), "acc-1")
    assert len(rows) == 1
    r = rows[0]
    assert r["ticker"] == "EXMP" and r["owner_cik"] == "999001" and r["is_officer"]
    assert r["value"] == pytest.approx(255_000)
    assert r["officer_title"] == "Chief Executive Officer"


def test_cluster_signal(cfg):
    tx = to_frame([purchase("AAA", "1", 60_000), purchase("AAA", "2", 50_000),
                   purchase("BBB", "3", 500_000)])  # BBB: one plain director, no cluster
    s = find_signals(tx, date(2026, 10, 22), cfg)
    assert list(s["ticker"]) == ["AAA"]
    assert s.iloc[0]["n_insiders"] == 2


def test_solo_ceo_signal_and_filters(cfg):
    tx = to_frame([
        purchase("CEO", "1", 300_000, title="President & CEO", officer=True),
        purchase("TEN", "2", 900_000, director=False),                 # 10% owner only: ignored
        purchase("PNY", "3", 300_000, title="CFO", officer=True, price=1.0),  # penny stock
        purchase("OLD", "4", 300_000, title="CEO", officer=True, filed="2026-10-01", traded="2026-09-30"),
        purchase("FUT", "5", 300_000, title="CEO", officer=True, filed="2026-10-25"),  # not known yet
    ])
    s = find_signals(tx, date(2026, 10, 22), cfg)
    assert list(s["ticker"]) == ["CEO"]


def test_entries_split_budget_and_respect_slots(cfg):
    state = portfolio.new_state(5.50)
    sigs = pd.DataFrame({"ticker": ["A", "B", "C", "D", "E"]})
    entries = portfolio.decide_entries(state, sigs, date(2026, 10, 26), cfg)
    assert [e[0] for e in entries] == ["A", "B", "C", "D"]
    assert all(e[1] == pytest.approx(1.37) for e in entries)
    assert sum(e[1] for e in entries) <= 5.50 + 1e-9


def test_entries_stop_below_min_order(cfg):
    state = portfolio.new_state(3.00)  # 3 / 4 slots = 0.75 < $1 minimum
    sigs = pd.DataFrame({"ticker": ["A", "B"]})
    assert portfolio.decide_entries(state, sigs, date(2026, 10, 26), cfg) == []


def test_exits_trailing_stop_and_max_hold(cfg):
    state = portfolio.new_state(100.0)
    portfolio.record_buy(state, "UP", 1.0, 20.0, date(2026, 10, 1))
    portfolio.record_buy(state, "OLD", 1.0, 20.0, date(2026, 7, 1))
    portfolio.update_marks(state, {"UP": 30.0, "OLD": 21.0})
    portfolio.update_marks(state, {"UP": 25.0})  # 16.7% below the 30 peak
    exits = dict(portfolio.decide_exits(state, date(2026, 10, 20), cfg))
    assert "trailing stop" in exits["UP"]
    assert "held" in exits["OLD"]


def test_bookkeeping_and_kill_switch(cfg):
    state = portfolio.new_state(10.0)
    portfolio.record_buy(state, "X", 0.5, 10.0, date(2026, 10, 1))
    assert portfolio.cash(state) == pytest.approx(5.0)
    portfolio.update_marks(state, {"X": 6.0})
    assert portfolio.equity(state) == pytest.approx(8.0)
    assert not portfolio.kill_switch_hit(state, cfg)  # 8.0 > 7.5
    portfolio.update_marks(state, {"X": 2.0})
    assert portfolio.kill_switch_hit(state, cfg)  # 6.0 <= 7.5
    t = portfolio.record_sell(state, "X", 2.0, date(2026, 10, 5), "test")
    assert t["pnl"] == pytest.approx(-4.0)
    assert portfolio.cash(state) == pytest.approx(6.0)


def test_no_rebuy_during_cooldown(cfg):
    state = portfolio.new_state(10.0)
    portfolio.record_buy(state, "X", 0.1, 10.0, date(2026, 10, 1))
    portfolio.record_sell(state, "X", 10.0, date(2026, 10, 10), "test")
    sigs = pd.DataFrame({"ticker": ["X", "Y"]})
    entries = portfolio.decide_entries(state, sigs, date(2026, 10, 20), cfg)
    assert [e[0] for e in entries] == ["Y"]


def test_backtest_simulation_runs(cfg, monkeypatch):
    from backtest import backtest as bt

    days = pd.bdate_range("2026-01-01", "2026-04-30")
    prices = pd.DataFrame({"AAA": [20.0 + 0.05 * i for i in range(len(days))],
                           "SPY": [500.0] * len(days)}, index=days)
    monkeypatch.setattr(bt, "download_prices", lambda tickers, start, end: (prices, prices))
    tx = to_frame([purchase("AAA", "1", 60_000, filed="2026-01-05", traded="2026-01-02"),
                   purchase("AAA", "2", 60_000, filed="2026-01-05", traded="2026-01-02")])
    state, curve = bt.simulate(tx, date(2026, 1, 1), date(2026, 4, 30), cfg, 1000.0)
    trades = state["history"]
    assert trades and trades[0]["symbol"] == "AAA" and trades[0]["entry_date"] == "2026-01-06"
    assert "held" in trades[0]["reason"] and trades[0]["pnl"] > 0
    assert curve["equity"].iloc[-1] > 1000.0


def test_daily_run_with_fake_broker(cfg, monkeypatch, tmp_path):
    from bot import run_daily
    from bot.broker import Fill
    from bot.config import Secrets

    class FakeBroker:
        def __init__(self, *a, **k):
            self.held = {}
        def market_open(self): return True
        def account_summary(self): return "fake"
        def positions(self): return dict(self.held)
        def can_buy(self, s): return True
        def latest_prices(self, syms): return {s: 20.0 for s in syms}
        def buy(self, s, notional, cid):
            self.held[s] = notional / 20.0
            return Fill(qty=notional / 20.0, price=20.0)
        def sell(self, s, qty, cid):
            self.held.pop(s, None)
            return Fill(qty=qty, price=20.0)

    today = date.today()
    tx = to_frame([purchase("AAA", "1", 60_000, filed=today.isoformat(), traded=today.isoformat()),
                   purchase("AAA", "2", 60_000, filed=today.isoformat(), traded=today.isoformat())])
    monkeypatch.setattr(run_daily, "AlpacaBroker", FakeBroker)
    monkeypatch.setattr(run_daily, "recent_purchases", lambda *a, **k: tx)
    monkeypatch.setattr(run_daily, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(run_daily, "load_secrets",
                        lambda: Secrets("k", "s", True, "ua me@example.com", "", "", ""))
    run_daily.run(dry_run=False, send=False)
    import json
    state = json.loads((tmp_path / "state.json").read_text())
    assert list(state["positions"]) == ["AAA"]
    assert state["positions"]["AAA"]["cost"] == pytest.approx(1.37)
