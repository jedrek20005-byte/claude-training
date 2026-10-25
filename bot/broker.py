"""Thin wrapper around the Alpaca API (orders, positions, prices)."""
import logging
import time
from dataclasses import dataclass

from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

log = logging.getLogger(__name__)

US_EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"}
FINAL = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED,
         OrderStatus.DONE_FOR_DAY}


@dataclass
class Fill:
    qty: float
    price: float


class AlpacaBroker:
    def __init__(self, key: str, secret: str, paper: bool = True):
        self.paper = paper
        self.trading = TradingClient(key, secret, paper=paper)
        self.data = StockHistoricalDataClient(key, secret)

    def market_open(self) -> bool:
        return bool(self.trading.get_clock().is_open)

    def account_summary(self) -> str:
        a = self.trading.get_account()
        return f"status={a.status} equity=${float(a.equity):,.2f} buying_power=${float(a.buying_power):,.2f}"

    def positions(self) -> dict[str, float]:
        return {p.symbol: float(p.qty) for p in self.trading.get_all_positions()}

    def can_buy(self, symbol: str) -> bool:
        """Tradable US stock that supports fractional shares."""
        try:
            a = self.trading.get_asset(symbol)
        except APIError:
            return False
        return bool(a.tradable and a.fractionable and str(a.exchange.value if hasattr(a.exchange, "value")
                                                           else a.exchange) in US_EXCHANGES)

    def latest_prices(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        out = {}
        try:
            trades = self.data.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=list(symbols), feed=DataFeed.IEX))
            out = {s: float(t.price) for s, t in trades.items() if t and t.price}
        except APIError as e:
            log.warning("Price request failed: %s", e)
        return out

    def buy(self, symbol: str, notional: float, client_id: str) -> Fill | None:
        req = MarketOrderRequest(symbol=symbol, notional=round(notional, 2), side=OrderSide.BUY,
                                 time_in_force=TimeInForce.DAY, client_order_id=client_id)
        return self._submit(req)

    def sell(self, symbol: str, qty: float, client_id: str) -> Fill | None:
        held = self.positions().get(symbol, 0.0)
        qty = min(qty, held)
        if qty <= 0:
            log.warning("Nothing to sell for %s (broker holds %s)", symbol, held)
            return None
        req = MarketOrderRequest(symbol=symbol, qty=round(qty, 9), side=OrderSide.SELL,
                                 time_in_force=TimeInForce.DAY, client_order_id=client_id)
        return self._submit(req)

    def _submit(self, req: MarketOrderRequest, timeout: float = 60) -> Fill | None:
        try:
            order = self.trading.submit_order(req)
        except APIError as e:
            log.error("Order rejected for %s: %s", req.symbol, e)
            return None
        deadline = time.monotonic() + timeout
        while order.status not in FINAL and time.monotonic() < deadline:
            time.sleep(2)
            order = self.trading.get_order_by_id(order.id)
        if order.status not in FINAL:
            log.warning("Order for %s not finished after %ss, cancelling the rest", req.symbol, timeout)
            try:
                self.trading.cancel_order_by_id(order.id)
                time.sleep(2)
            except APIError:
                pass
            order = self.trading.get_order_by_id(order.id)
        qty = float(order.filled_qty or 0)
        if qty <= 0:
            log.error("Order for %s ended %s without a fill", req.symbol, order.status)
            return None
        return Fill(qty=qty, price=float(order.filled_avg_price))
