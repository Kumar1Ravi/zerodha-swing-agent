"""MongoDB-backed paper trading persistence for Atlas and serverless deployments."""

from __future__ import annotations

from datetime import date, datetime, timezone
from math import isfinite
from typing import Any

from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

from execution.paper_trader import CostModel, DailySummary, TradeRecord


class MongoPaperTrader:
    def __init__(self, uri: str, *, database_name: str = "paper_trading",
                 starting_capital: float = 10_000, cost_model: CostModel | None = None,
                 client: MongoClient | None = None) -> None:
        if not uri:
            raise ValueError("MongoDB URI is required")
        if not database_name.strip():
            raise ValueError("database_name is required")
        if not isfinite(starting_capital) or starting_capital <= 0:
            raise ValueError("starting_capital must be positive and finite")
        self.client = client or MongoClient(uri, serverSelectionTimeoutMS=8_000)
        self.db = self.client[database_name]
        self.starting_capital = float(starting_capital)
        self.cost_model = cost_model or CostModel()
        self.trades = self.db["trades"]
        self.trades.create_index("signal_key", unique=True, sparse=True)
        self.trades.create_index([("status", 1), ("entry_time", 1)])
        self.trades.create_index([("status", 1), ("exit_time", 1)])
        self.db["counters"].update_one({"_id": "trades"}, {"$setOnInsert": {"value": 0}}, upsert=True)
        self.db["account"].update_one({"_id": "paper"},
            {"$setOnInsert": {"starting_capital": self.starting_capital}}, upsert=True)

    @staticmethod
    def _timestamp(value: datetime | None) -> str:
        value = value or datetime.now(timezone.utc)
        if value.tzinfo is None:
            raise ValueError("timestamps must include timezone information")
        return value.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _record(row: dict[str, Any]) -> TradeRecord:
        return TradeRecord(id=int(row["_id"]), symbol=row["symbol"], side=row["side"],
            quantity=row["quantity"], entry_time=row["entry_time"], entry_price=row["entry_price"],
            stop_loss=row["stop_loss"], target_price=row["target_price"],
            exit_time=row.get("exit_time"), exit_price=row.get("exit_price"),
            exit_reason=row.get("exit_reason"), gross_pnl=row.get("gross_pnl"),
            costs=row.get("costs", 0.0), net_pnl=row.get("net_pnl"), status=row["status"])

    def _trade_costs(self, notional: float, *, is_exit: bool) -> float:
        model = self.cost_model
        total = model.brokerage_inr + notional * (
            model.stt_rate + model.exchange_transaction_rate + model.stamp_duty_rate)
        total += notional * model.exchange_transaction_rate * model.gst_rate
        return total + (model.dp_charge_inr if is_exit else 0.0)

    def _fill_price(self, price: float, *, is_buy: bool) -> float:
        slip = self.cost_model.slippage_percent / 100
        return price * (1 + slip if is_buy else 1 - slip)

    def available_capital(self) -> float:
        account = self.db["account"].find_one({"_id": "paper"})
        closed = list(self.trades.find({"status": "CLOSED"}, {"net_pnl": 1}))
        opened = list(self.trades.find({"status": "OPEN"},
                                      {"quantity": 1, "entry_price": 1, "entry_costs": 1}))
        realized = sum(row.get("net_pnl") or 0.0 for row in closed)
        reserved = sum(row["quantity"] * row["entry_price"] + row.get("entry_costs", 0.0)
                       for row in opened)
        return float(account["starting_capital"] + realized - reserved)

    def open_positions(self) -> list[TradeRecord]:
        rows = self.trades.find({"status": "OPEN"}).sort([("entry_time", 1), ("_id", 1)])
        return [self._record(row) for row in rows]

    def open_position(self, *, symbol: str, quantity: int, entry_price: float,
                      stop_loss: float, target_price: float, signal_key: str,
                      timestamp: datetime | None = None) -> TradeRecord:
        symbol = symbol.strip().upper()
        if not symbol or not signal_key.strip():
            raise ValueError("symbol and signal_key are required")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        for name, value in (("entry_price", entry_price), ("stop_loss", stop_loss),
                            ("target_price", target_price)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if not stop_loss < entry_price < target_price:
            raise ValueError("long trade requires stop_loss < entry_price < target_price")
        filled = self._fill_price(float(entry_price), is_buy=True)
        if filled >= target_price:
            raise ValueError("simulated entry fill is at or above target")
        fee = self._trade_costs(filled * quantity, is_exit=False)
        if filled * quantity + fee > self.available_capital():
            raise ValueError("insufficient paper cash for simulated entry")
        document = {"symbol": symbol, "side": "LONG", "quantity": quantity,
            "entry_time": self._timestamp(timestamp), "entry_price": filled,
            "stop_loss": float(stop_loss), "target_price": float(target_price),
            "entry_costs": fee, "costs": fee, "status": "OPEN", "signal_key": signal_key.strip()}
        counter = self.db["counters"].find_one_and_update({"_id": "trades"},
            {"$inc": {"value": 1}}, return_document=ReturnDocument.AFTER)
        document["_id"] = int(counter["value"])
        try:
            result = self.trades.insert_one(document)
        except DuplicateKeyError as exc:
            raise ValueError("duplicate signal_key; entry already recorded") from exc
        return self._record(document)

    def close_position(self, trade_id: int, *, reference_price: float, reason: str,
                       timestamp: datetime | None = None) -> TradeRecord:
        if isinstance(reference_price, bool) or not isfinite(reference_price) or reference_price <= 0:
            raise ValueError("reference_price must be positive and finite")
        if not reason.strip():
            raise ValueError("exit reason is required")
        row = self.trades.find_one({"_id": trade_id, "status": "OPEN"})
        if row is None:
            raise ValueError("open paper position not found")
        filled = self._fill_price(float(reference_price), is_buy=False)
        exit_cost = self._trade_costs(filled * row["quantity"], is_exit=True)
        gross = (filled - row["entry_price"]) * row["quantity"]
        total_costs = row["entry_costs"] + exit_cost
        updated = self.trades.find_one_and_update(
            {"_id": trade_id, "status": "OPEN"},
            {"$set": {"exit_time": self._timestamp(timestamp), "exit_price": filled,
                "exit_reason": reason.strip(), "exit_costs": exit_cost, "gross_pnl": gross,
                "costs": total_costs, "net_pnl": gross - total_costs, "status": "CLOSED"}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is None:
            raise ValueError("position was closed by another request")
        return self._record(updated)

    def evaluate_exit(self, trade_id: int, *, candle_open: float, candle_high: float,
                      candle_low: float, candle_close: float,
                      timestamp: datetime | None = None) -> TradeRecord | None:
        for name, value in (("candle_open", candle_open), ("candle_high", candle_high),
                            ("candle_low", candle_low), ("candle_close", candle_close)):
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        position = next((p for p in self.open_positions() if p.id == trade_id), None)
        if position is None:
            raise ValueError("open paper position not found")
        if candle_open <= position.stop_loss:
            return self.close_position(trade_id, reference_price=candle_open,
                                      reason="stop_loss_gap", timestamp=timestamp)
        if candle_low <= position.stop_loss:
            return self.close_position(trade_id, reference_price=position.stop_loss,
                                      reason="stop_loss", timestamp=timestamp)
        if candle_high >= position.target_price:
            return self.close_position(trade_id, reference_price=position.target_price,
                                      reason="target", timestamp=timestamp)
        return None

    def daily_summary(self, day: date | str) -> DailySummary:
        day_text = day.isoformat() if isinstance(day, date) else str(day)
        rows = list(self.trades.find({"status": "CLOSED", "exit_time": {"$lte": day_text + "T23:59:59.999999+00:00"}}).sort("exit_time", 1))
        daily = [r for r in rows if r["exit_time"][:10] == day_text]
        gross = sum(r["gross_pnl"] for r in daily)
        costs = sum(r["costs"] for r in daily)
        net = sum(r["net_pnl"] for r in daily)
        wins = sum(r["net_pnl"] > 0 for r in daily)
        equity = peak = self.starting_capital
        drawdown = 0.0
        for row in rows:
            equity += row["net_pnl"]
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
        count = len(daily)
        return DailySummary(day_text, count, gross, costs, net, wins,
                            wins / count if count else 0.0, drawdown, 0.0)

    def daily_risk_pnl(self, day: date | str, *, marks: dict[str, float] | None = None) -> float:
        day_text = day.isoformat() if isinstance(day, date) else str(day)
        rows = self.trades.find({"status": "CLOSED", "exit_time": {"$regex": "^" + day_text}})
        realized = sum(row.get("net_pnl", 0.0) for row in rows)
        unrealized = 0.0
        for position in self.open_positions():
            mark = (marks or {}).get(position.symbol, position.entry_price)
            if not isfinite(mark) or mark <= 0:
                raise ValueError(f"invalid mark price for {position.symbol}")
            unrealized += (mark - position.entry_price) * position.quantity
        return float(realized + unrealized)

    def _connect_rows(self) -> list[dict[str, Any]]:
        return list(self.trades.find().sort([("entry_time", -1), ("_id", -1)]).limit(500))
