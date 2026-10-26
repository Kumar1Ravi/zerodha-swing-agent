"""SQLite-backed paper execution; never sends orders to a broker."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import isfinite
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CostModel:
    slippage_percent: float = 0.10
    brokerage_inr: float = 0.0
    stt_rate: float = 0.0
    exchange_transaction_rate: float = 0.0
    gst_rate: float = 0.0
    stamp_duty_rate: float = 0.0
    dp_charge_inr: float = 0.0

    def __post_init__(self) -> None:
        for name in ("slippage_percent", "brokerage_inr", "stt_rate", "exchange_transaction_rate",
                     "gst_rate", "stamp_duty_rate", "dp_charge_inr"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class TradeRecord:
    id: int
    symbol: str
    side: str
    quantity: int
    entry_time: str
    entry_price: float
    stop_loss: float
    target_price: float
    exit_time: str | None
    exit_price: float | None
    exit_reason: str | None
    gross_pnl: float | None
    costs: float
    net_pnl: float | None
    status: str


@dataclass(frozen=True, slots=True)
class DailySummary:
    day: str
    closed_trades: int
    gross_pnl: float
    costs: float
    net_pnl: float
    wins: int
    win_rate: float
    drawdown: float
    unrealized_pnl: float = 0.0


class PaperTrader:
    """Persist simulated cash-market trades in a local SQLite database."""

    def __init__(self, database_path: str | Path, *, starting_capital: float = 10_000,
                 cost_model: CostModel | None = None) -> None:
        if not isfinite(starting_capital) or starting_capital <= 0:
            raise ValueError("starting_capital must be positive and finite")
        self.database_path = str(database_path)
        self.starting_capital = float(starting_capital)
        self.cost_model = cost_model or CostModel()
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    starting_capital REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK (side = 'LONG'),
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    entry_time TEXT NOT NULL,
                    entry_price REAL NOT NULL CHECK (entry_price > 0),
                    stop_loss REAL NOT NULL CHECK (stop_loss > 0),
                    target_price REAL NOT NULL CHECK (target_price > entry_price),
                    entry_costs REAL NOT NULL DEFAULT 0,
                    exit_time TEXT,
                    exit_price REAL,
                    exit_reason TEXT,
                    exit_costs REAL NOT NULL DEFAULT 0,
                    gross_pnl REAL,
                    costs REAL NOT NULL DEFAULT 0,
                    net_pnl REAL,
                    status TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
                    signal_key TEXT UNIQUE
                );
                CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
                CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades(exit_time);
            """)
            db.execute("INSERT OR IGNORE INTO account(id, starting_capital) VALUES (1, ?)",
                       (self.starting_capital,))

    @staticmethod
    def _timestamp(value: datetime | None) -> str:
        value = value or datetime.now(timezone.utc)
        if value.tzinfo is None:
            raise ValueError("timestamps must include timezone information")
        return value.astimezone(timezone.utc).isoformat()

    def _trade_costs(self, notional: float, *, is_exit: bool) -> float:
        model = self.cost_model
        charges = model.brokerage_inr
        charges += notional * (model.stt_rate + model.exchange_transaction_rate + model.stamp_duty_rate)
        charges += notional * model.exchange_transaction_rate * model.gst_rate
        if is_exit:
            charges += model.dp_charge_inr
        return charges

    def _fill_price(self, reference_price: float, *, is_buy: bool) -> float:
        slip = self.cost_model.slippage_percent / 100
        return reference_price * (1 + slip if is_buy else 1 - slip)

    def available_capital(self) -> float:
        """Cash remaining after open positions and closed-trade net P/L."""
        with self._connect() as db:
            account = db.execute("SELECT starting_capital FROM account WHERE id=1").fetchone()
            open_cost = db.execute("SELECT COALESCE(SUM(quantity * entry_price + entry_costs), 0) FROM trades WHERE status='OPEN'").fetchone()[0]
            realized = db.execute("SELECT COALESCE(SUM(net_pnl), 0) FROM trades WHERE status='CLOSED'").fetchone()[0]
        return float(account[0] + realized - open_cost)

    def open_positions(self) -> list[TradeRecord]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM trades WHERE status='OPEN' ORDER BY entry_time, id").fetchall()
        return [self._record(row) for row in rows]

    def open_position(self, *, symbol: str, quantity: int, entry_price: float,
                      stop_loss: float, target_price: float, signal_key: str,
                      timestamp: datetime | None = None) -> TradeRecord:
        symbol = symbol.strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        for name, value in (("entry_price", entry_price), ("stop_loss", stop_loss), ("target_price", target_price)):
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if not stop_loss < entry_price < target_price:
            raise ValueError("long trade requires stop_loss < entry_price < target_price")
        if not signal_key.strip():
            raise ValueError("signal_key is required for duplicate prevention")
        filled = self._fill_price(float(entry_price), is_buy=True)
        if filled >= target_price:
            raise ValueError("simulated entry fill is at or above target")
        notional = filled * quantity
        entry_cost = self._trade_costs(notional, is_exit=False)
        required = notional + entry_cost
        if required > self.available_capital():
            raise ValueError("insufficient paper cash for simulated entry")
        ts = self._timestamp(timestamp)
        with self._connect() as db:
            try:
                cursor = db.execute("""
                    INSERT INTO trades(symbol, side, quantity, entry_time, entry_price,
                        stop_loss, target_price, entry_costs, costs, status, signal_key)
                    VALUES (?, 'LONG', ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
                """, (symbol, quantity, ts, filled, stop_loss, target_price, entry_cost,
                      entry_cost, signal_key.strip()))
            except sqlite3.IntegrityError as exc:
                if "signal_key" in str(exc):
                    raise ValueError("duplicate signal_key; entry already recorded") from exc
                raise
            row = db.execute("SELECT * FROM trades WHERE id=?", (cursor.lastrowid,)).fetchone()
        return self._record(row)

    def close_position(self, trade_id: int, *, reference_price: float, reason: str,
                       timestamp: datetime | None = None) -> TradeRecord:
        if not isfinite(reference_price) or reference_price <= 0:
            raise ValueError("reference_price must be positive and finite")
        if not reason.strip():
            raise ValueError("exit reason is required")
        with self._connect() as db:
            row = db.execute("SELECT * FROM trades WHERE id=? AND status='OPEN'", (trade_id,)).fetchone()
            if row is None:
                raise ValueError("open paper position not found")
            filled = self._fill_price(float(reference_price), is_buy=False)
            notional = filled * row["quantity"]
            exit_cost = self._trade_costs(notional, is_exit=True)
            gross = (filled - row["entry_price"]) * row["quantity"]
            costs = row["entry_costs"] + exit_cost
            net = gross - costs
            db.execute("""
                UPDATE trades SET exit_time=?, exit_price=?, exit_reason=?, exit_costs=?,
                    gross_pnl=?, costs=?, net_pnl=?, status='CLOSED'
                WHERE id=? AND status='OPEN'
            """, (self._timestamp(timestamp), filled, reason.strip(), exit_cost, gross,
                  costs, net, trade_id))
            updated = db.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        return self._record(updated)

    def evaluate_exit(self, trade_id: int, *, candle_open: float, candle_high: float,
                      candle_low: float, candle_close: float,
                      timestamp: datetime | None = None) -> TradeRecord | None:
        """Apply stop/target to a completed candle; if both touch, assume stop first.

        For a gap below the stop, fill from the candle open (then slippage), which
        models the fact a stop is not a guaranteed loss cap.
        """
        for name, price in (("candle_open", candle_open), ("candle_high", candle_high),
                            ("candle_low", candle_low), ("candle_close", candle_close)):
            if not isfinite(price) or price <= 0:
                raise ValueError(f"{name} must be positive and finite")
        for position in self.open_positions():
            if position.id != trade_id:
                continue
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
        raise ValueError("open paper position not found")

    def daily_summary(self, day: date | str) -> DailySummary:
        day_text = day.isoformat() if isinstance(day, date) else str(day)
        with self._connect() as db:
            rows = db.execute("""
                SELECT exit_time, gross_pnl, costs, net_pnl FROM trades
                WHERE status='CLOSED' AND substr(exit_time, 1, 10) <= ?
                ORDER BY exit_time, id
            """, (day_text,)).fetchall()
        history = [row for row in rows if row["exit_time"][:10] == day_text]
        gross = sum(row["gross_pnl"] for row in history)
        costs = sum(row["costs"] for row in history)
        net = sum(row["net_pnl"] for row in history)
        wins = sum(row["net_pnl"] > 0 for row in history)
        equity = peak = self.starting_capital
        max_drawdown = 0.0
        for row in rows:
            equity += row["net_pnl"]
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
        count = len(history)
        return DailySummary(day_text, count, gross, costs, net, wins,
                            wins / count if count else 0.0, max_drawdown, 0.0)

    def daily_risk_pnl(self, day: date | str, *, marks: dict[str, float] | None = None) -> float:
        """Realized net P/L for a day plus marked unrealized P/L for open positions.

        Marks are optional and keyed by symbol. Missing marks use entry price,
        which is neutral but should not be interpreted as current exposure value.
        """
        day_text = day.isoformat() if isinstance(day, date) else str(day)
        with self._connect() as db:
            realized = db.execute("""
                SELECT COALESCE(SUM(net_pnl), 0) FROM trades
                WHERE status='CLOSED' AND substr(exit_time, 1, 10)=?
            """, (day_text,)).fetchone()[0]
        unrealized = 0.0
        for position in self.open_positions():
            mark = (marks or {}).get(position.symbol, position.entry_price)
            if not isfinite(mark) or mark <= 0:
                raise ValueError(f"invalid mark price for {position.symbol}")
            unrealized += (mark - position.entry_price) * position.quantity
        return float(realized + unrealized)

    @staticmethod
    def _record(row: sqlite3.Row) -> TradeRecord:
        return TradeRecord(
            id=row["id"], symbol=row["symbol"], side=row["side"], quantity=row["quantity"],
            entry_time=row["entry_time"], entry_price=row["entry_price"],
            stop_loss=row["stop_loss"], target_price=row["target_price"],
            exit_time=row["exit_time"], exit_price=row["exit_price"],
            exit_reason=row["exit_reason"], gross_pnl=row["gross_pnl"],
            costs=row["costs"], net_pnl=row["net_pnl"], status=row["status"],
        )
