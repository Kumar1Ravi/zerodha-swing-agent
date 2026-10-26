"""Postgres-backed PaperTrader for serverless deployments such as Vercel + Neon."""

from __future__ import annotations

from datetime import date, datetime, timezone
from math import isfinite
from typing import Any

import psycopg
from psycopg.rows import dict_row

from execution.paper_trader import CostModel, DailySummary, TradeRecord


class PostgresPaperTrader:
    def __init__(self, database_url: str, *, starting_capital: float = 10_000,
                 cost_model: CostModel | None = None) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        if not isfinite(starting_capital) or starting_capital <= 0:
            raise ValueError("starting_capital must be positive and finite")
        self.database_url = database_url
        self.starting_capital = float(starting_capital)
        self.cost_model = cost_model or CostModel()
        self._initialize()

    def _connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=8)

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS paper_account (
                    id SMALLINT PRIMARY KEY CHECK (id=1), starting_capital DOUBLE PRECISION NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id BIGSERIAL PRIMARY KEY, symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK (side='LONG'), quantity INTEGER NOT NULL CHECK (quantity>0),
                    entry_time TEXT NOT NULL, entry_price DOUBLE PRECISION NOT NULL CHECK (entry_price>0),
                    stop_loss DOUBLE PRECISION NOT NULL CHECK (stop_loss>0),
                    target_price DOUBLE PRECISION NOT NULL CHECK (target_price>entry_price),
                    entry_costs DOUBLE PRECISION NOT NULL DEFAULT 0,
                    exit_time TEXT, exit_price DOUBLE PRECISION, exit_reason TEXT,
                    exit_costs DOUBLE PRECISION NOT NULL DEFAULT 0,
                    gross_pnl DOUBLE PRECISION, costs DOUBLE PRECISION NOT NULL DEFAULT 0,
                    net_pnl DOUBLE PRECISION, status TEXT NOT NULL CHECK (status IN ('OPEN','CLOSED')),
                    signal_key TEXT UNIQUE
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS paper_trades_status_idx ON paper_trades(status)")
            db.execute("INSERT INTO paper_account(id,starting_capital) VALUES (1,%s) ON CONFLICT(id) DO NOTHING",
                       (self.starting_capital,))

    @staticmethod
    def _timestamp(value: datetime | None) -> str:
        value = value or datetime.now(timezone.utc)
        if value.tzinfo is None:
            raise ValueError("timestamps must include timezone information")
        return value.astimezone(timezone.utc).isoformat()

    def _trade_costs(self, notional: float, *, is_exit: bool) -> float:
        model = self.cost_model
        total = model.brokerage_inr + notional * (model.stt_rate + model.exchange_transaction_rate + model.stamp_duty_rate)
        total += notional * model.exchange_transaction_rate * model.gst_rate
        return total + (model.dp_charge_inr if is_exit else 0)

    def _fill_price(self, reference_price: float, *, is_buy: bool) -> float:
        slip = self.cost_model.slippage_percent / 100
        return reference_price * (1 + slip if is_buy else 1 - slip)

    def available_capital(self) -> float:
        with self._connect() as db:
            row = db.execute("""
                SELECT a.starting_capital
                  + COALESCE((SELECT SUM(net_pnl) FROM paper_trades WHERE status='CLOSED'),0)
                  - COALESCE((SELECT SUM(quantity*entry_price+entry_costs) FROM paper_trades WHERE status='OPEN'),0) AS cash
                FROM paper_account a WHERE a.id=1
            """).fetchone()
        return float(row["cash"])

    @staticmethod
    def _record(row: dict[str, Any]) -> TradeRecord:
        return TradeRecord(id=row["id"], symbol=row["symbol"], side=row["side"], quantity=row["quantity"],
            entry_time=row["entry_time"], entry_price=row["entry_price"], stop_loss=row["stop_loss"],
            target_price=row["target_price"], exit_time=row["exit_time"], exit_price=row["exit_price"],
            exit_reason=row["exit_reason"], gross_pnl=row["gross_pnl"], costs=row["costs"],
            net_pnl=row["net_pnl"], status=row["status"])

    def open_positions(self) -> list[TradeRecord]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM paper_trades WHERE status='OPEN' ORDER BY entry_time,id").fetchall()
        return [self._record(row) for row in rows]

    def open_position(self, *, symbol: str, quantity: int, entry_price: float,
                      stop_loss: float, target_price: float, signal_key: str,
                      timestamp: datetime | None = None) -> TradeRecord:
        symbol = symbol.strip().upper()
        if not symbol or not signal_key.strip():
            raise ValueError("symbol and signal_key are required")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        for name, value in (("entry_price", entry_price), ("stop_loss", stop_loss), ("target_price", target_price)):
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if not stop_loss < entry_price < target_price:
            raise ValueError("long trade requires stop_loss < entry_price < target_price")
        filled = self._fill_price(float(entry_price), is_buy=True)
        if filled >= target_price:
            raise ValueError("simulated entry fill is at or above target")
        notional = filled * quantity
        fee = self._trade_costs(notional, is_exit=False)
        if notional + fee > self.available_capital():
            raise ValueError("insufficient paper cash for simulated entry")
        try:
            with self._connect() as db:
                row = db.execute("""
                    INSERT INTO paper_trades(symbol,side,quantity,entry_time,entry_price,stop_loss,target_price,
                                             entry_costs,costs,status,signal_key)
                    VALUES (%s,'LONG',%s,%s,%s,%s,%s,%s,%s,'OPEN',%s) RETURNING *
                """, (symbol, quantity, self._timestamp(timestamp), filled, stop_loss, target_price,
                      fee, fee, signal_key.strip())).fetchone()
        except psycopg.errors.UniqueViolation as exc:
            raise ValueError("duplicate signal_key; entry already recorded") from exc
        return self._record(row)

    def close_position(self, trade_id: int, *, reference_price: float, reason: str,
                       timestamp: datetime | None = None) -> TradeRecord:
        if not isfinite(reference_price) or reference_price <= 0 or not reason.strip():
            raise ValueError("valid reference_price and exit reason are required")
        with self._connect() as db:
            row = db.execute("SELECT * FROM paper_trades WHERE id=%s AND status='OPEN' FOR UPDATE",
                             (trade_id,)).fetchone()
            if row is None:
                raise ValueError("open paper position not found")
            filled = self._fill_price(float(reference_price), is_buy=False)
            exit_fee = self._trade_costs(filled * row["quantity"], is_exit=True)
            gross = (filled - row["entry_price"]) * row["quantity"]
            total_cost = row["entry_costs"] + exit_fee
            updated = db.execute("""
                UPDATE paper_trades SET exit_time=%s,exit_price=%s,exit_reason=%s,exit_costs=%s,
                    gross_pnl=%s,costs=%s,net_pnl=%s,status='CLOSED'
                WHERE id=%s AND status='OPEN' RETURNING *
            """, (self._timestamp(timestamp), filled, reason.strip(), exit_fee,
                  gross, total_cost, gross-total_cost, trade_id)).fetchone()
        return self._record(updated)

    def evaluate_exit(self, trade_id: int, *, candle_open: float, candle_high: float,
                      candle_low: float, candle_close: float,
                      timestamp: datetime | None = None) -> TradeRecord | None:
        for name, price in (("candle_open", candle_open), ("candle_high", candle_high),
                            ("candle_low", candle_low), ("candle_close", candle_close)):
            if not isfinite(price) or price <= 0:
                raise ValueError(f"{name} must be positive and finite")
        position = next((p for p in self.open_positions() if p.id == trade_id), None)
        if position is None:
            raise ValueError("open paper position not found")
        if candle_open <= position.stop_loss:
            return self.close_position(trade_id, reference_price=candle_open, reason="stop_loss_gap", timestamp=timestamp)
        if candle_low <= position.stop_loss:
            return self.close_position(trade_id, reference_price=position.stop_loss, reason="stop_loss", timestamp=timestamp)
        if candle_high >= position.target_price:
            return self.close_position(trade_id, reference_price=position.target_price, reason="target", timestamp=timestamp)
        return None

    def daily_summary(self, day: date | str) -> DailySummary:
        day_text = day.isoformat() if isinstance(day, date) else str(day)
        with self._connect() as db:
            rows = db.execute("""
                SELECT exit_time,gross_pnl,costs,net_pnl FROM paper_trades
                WHERE status='CLOSED' AND exit_time <= %s ORDER BY exit_time,id
            """, (day_text + "T23:59:59.999999+00:00",)).fetchall()
        daily = [r for r in rows if r["exit_time"][:10] == day_text]
        gross = sum(r["gross_pnl"] for r in daily)
        fee = sum(r["costs"] for r in daily)
        net = sum(r["net_pnl"] for r in daily)
        wins = sum(r["net_pnl"] > 0 for r in daily)
        equity = peak = self.starting_capital
        drawdown = 0.0
        for row in rows:
            equity += row["net_pnl"]
            peak = max(peak, equity)
            drawdown = max(drawdown, peak-equity)
        count = len(daily)
        return DailySummary(day_text,count,gross,fee,net,wins,wins/count if count else 0.0,drawdown,0.0)

    def daily_risk_pnl(self, day: date | str, *, marks: dict[str, float] | None = None) -> float:
        day_text = day.isoformat() if isinstance(day, date) else str(day)
        with self._connect() as db:
            realized = db.execute("""
                SELECT COALESCE(SUM(net_pnl),0) AS pnl FROM paper_trades
                WHERE status='CLOSED' AND exit_time LIKE %s
            """, (day_text + "%",)).fetchone()["pnl"]
        unrealized = 0.0
        for position in self.open_positions():
            mark = (marks or {}).get(position.symbol, position.entry_price)
            if not isfinite(mark) or mark <= 0:
                raise ValueError(f"invalid mark price for {position.symbol}")
            unrealized += (mark-position.entry_price)*position.quantity
        return float(realized+unrealized)

    def _connect_rows(self):
        with self._connect() as db:
            rows = db.execute("SELECT * FROM paper_trades ORDER BY entry_time DESC,id DESC LIMIT 500").fetchall()
        return rows
