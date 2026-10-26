"""Flask dashboard and JSON API suitable for local use and Vercel Functions."""

from __future__ import annotations

import hmac
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template_string, request
from dotenv import load_dotenv

from agent.config import load_config
from agent.risk_manager import RiskLimits, check_trade_allowed, size_long_position
from agent.scanner import scan_symbols
from execution.paper_trader import CostModel, PaperTrader


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
load_dotenv(ROOT / ".env")
app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NSE Swing Paper Trader</title><style>
body{font:16px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#17202a;background:#f7f9fb}
h1{margin-bottom:.2rem}.note{background:#fff4d6;padding:1rem;border-radius:8px}.card{background:white;padding:1rem;border-radius:8px;margin:1rem 0;box-shadow:0 1px 5px #ddd}
input,button{font:inherit;padding:.55rem;margin:.2rem;border:1px solid #ccd;border-radius:5px}button{background:#1259c3;color:white;cursor:pointer}
table{width:100%;border-collapse:collapse;background:white}th,td{text-align:left;padding:.55rem;border-bottom:1px solid #ddd}small{color:#555}
</style></head><body><h1>NSE Swing Trading Agent</h1><p>Paper trading dashboard · no broker orders are sent</p>
<div class="note"><b>Risk notice:</b> simulated and historical results do not predict future returns. Stop-losses cannot guarantee maximum loss because of gaps, slippage, or execution failures.</div>
<section class="card"><h2>Account</h2><p>Available paper cash: <b>₹{{ '%.2f'|format(capital) }}</b> · Open positions: <b>{{ positions|length }}</b></p>
<p>Today's gross P/L ₹{{ '%.2f'|format(summary.gross_pnl) }} · estimated costs ₹{{ '%.2f'|format(summary.costs) }} · net P/L ₹{{ '%.2f'|format(summary.net_pnl) }} · drawdown ₹{{ '%.2f'|format(summary.drawdown) }}</p></section>
<section class="card"><h2>Scan local completed candles</h2><small>CSV source: {{ data_directory }}. No data is bundled. Use an authorized data source.</small>
<form method="post" action="/scan"><input name="symbols" value="RELIANCE, INFY" size="40"><button>Scan</button></form>
{% if signals %}<table><tr><th>Symbol</th><th>Status</th><th>Reason</th><th>Entry</th><th>Stop</th><th>Target</th><th>R:R</th></tr>
{% for signal in signals %}<tr><td>{{ signal.symbol }}</td><td>{{ signal.status }}</td><td>{{ signal.reason }}</td><td>{{ signal.entry_price or '' }}</td><td>{{ signal.stop_loss or '' }}</td><td>{{ signal.target_price or '' }}</td><td>{{ signal.reward_to_risk or '' }}</td></tr>{% endfor %}</table>{% endif %}</section>
<section class="card"><h2>Open paper positions</h2>{% if positions %}<table><tr><th>ID</th><th>Symbol</th><th>Qty</th><th>Entry</th><th>Stop</th><th>Target</th><th>Close</th></tr>
{% for p in positions %}<tr><td>{{ p.id }}</td><td>{{ p.symbol }}</td><td>{{ p.quantity }}</td><td>{{ '%.2f'|format(p.entry_price) }}</td><td>{{ '%.2f'|format(p.stop_loss) }}</td><td>{{ '%.2f'|format(p.target_price) }}</td><td><form method="post" action="/close/{{ p.id }}"><input name="price" type="number" step="0.01" required placeholder="Reference price"><button>Close paper</button></form></td></tr>{% endfor %}</table>{% else %}<p>No open positions.</p>{% endif %}</section>
<section class="card"><h2>Trade history</h2>{% if trades %}<table><tr><th>ID</th><th>Symbol</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Reason</th><th>Net P/L</th></tr>{% for t in trades %}<tr><td>{{ t.id }}</td><td>{{ t.symbol }}</td><td>{{ t.quantity }}</td><td>{{ '%.2f'|format(t.entry_price) }}</td><td>{{ '%.2f'|format(t.exit_price or 0) if t.exit_price else '' }}</td><td>{{ t.exit_reason or t.status }}</td><td>{{ '%.2f'|format(t.net_pnl or 0) if t.net_pnl is not none else '' }}</td></tr>{% endfor %}</table>{% else %}<p>No trades recorded.</p>{% endif %}</section>
<p><small>Costs are configurable estimates; current defaults are zero. Deployment storage uses MongoDB Atlas when MONGODB_URI is configured.</small></p></body></html>"""


def _service():
    config_path = Path(os.getenv("APP_CONFIG_PATH", CONFIG_PATH))
    cfg = load_config(config_path)
    execution = cfg["execution"]
    cost_model = CostModel(
        slippage_percent=execution.get("simulated_slippage_percent", 0),
        brokerage_inr=execution.get("brokerage_inr", 0), stt_rate=execution.get("stt_rate", 0),
        exchange_transaction_rate=execution.get("exchange_transaction_rate", 0),
        gst_rate=execution.get("gst_rate", 0), stamp_duty_rate=execution.get("stamp_duty_rate", 0),
        dp_charge_inr=execution.get("dp_charge_inr", 0),
    )
    db_url = os.getenv("DATABASE_URL", "").strip()
    mongo_uri = os.getenv("MONGODB_URI", "").strip()
    if mongo_uri:
        from execution.mongo_paper_trader import MongoPaperTrader
        trader = MongoPaperTrader(mongo_uri,
            database_name=os.getenv("MONGODB_DATABASE", "paper_trading").strip(),
            starting_capital=cfg["risk"]["starting_capital_inr"], cost_model=cost_model)
    elif db_url:
        from execution.postgres_paper_trader import PostgresPaperTrader
        trader = PostgresPaperTrader(db_url, starting_capital=cfg["risk"]["starting_capital_inr"],
                                      cost_model=cost_model)
    else:
        if os.getenv("VERCEL"):
            raise RuntimeError("MONGODB_URI or DATABASE_URL is required for durable Vercel paper-trading storage")
        db_path = Path(cfg["app"].get("database_path", "data/paper_trades.sqlite3"))
        if not db_path.is_absolute():
            db_path = ROOT / db_path
        trader = PaperTrader(db_path, starting_capital=cfg["risk"]["starting_capital_inr"],
                             cost_model=cost_model)
    risk = cfg["risk"]
    limits = RiskLimits(
        planned_risk_per_trade=risk["planned_risk_per_trade_inr"],
        hard_risk_ceiling_per_trade=risk["hard_risk_ceiling_per_trade_inr"],
        max_open_positions=risk["max_open_positions"], daily_loss_limit=risk["daily_loss_limit_inr"],
        gap_slippage_buffer_percent=risk["gap_slippage_buffer_percent"],
        allow_leverage=risk["allow_leverage"], allow_averaging_down=risk["allow_averaging_down"],
    )
    return cfg, trader, limits


def _scan_config_path() -> Path:
    return Path(os.getenv("APP_CONFIG_PATH", CONFIG_PATH))


def _scan_data_directory(cfg: dict[str, Any]) -> Path:
    directory = Path(cfg["market_data"].get("csv_directory", "data/market"))
    if not directory.is_absolute():
        directory = ROOT / directory
    return directory


def _authorized() -> bool:
    expected = os.getenv("APP_ACCESS_TOKEN", "").strip()
    if not expected:
        return not bool(os.getenv("VERCEL"))
    supplied = request.headers.get("X-App-Token", "") or request.args.get("token", "")
    return hmac.compare_digest(supplied, expected)


@app.before_request
def require_deployment_token():
    if not _authorized():
        return jsonify({"error": "unauthorized; provide X-App-Token"}), 401
    return None


def _record(record: Any) -> dict[str, Any]:
    return {key: getattr(record, key) for key in record.__dataclass_fields__}


def _trade_rows(trader: Any) -> list[dict[str, Any]]:
    if hasattr(trader, "_connect_rows"):
        rows = [dict(row) for row in trader._connect_rows()]
        return [{("id" if key == "_id" else key): value for key, value in row.items()} for row in rows]
    with trader._connect() as db:
        rows = db.execute("SELECT * FROM trades ORDER BY entry_time DESC, id DESC LIMIT 500").fetchall()
    return [dict(row) for row in rows]


@app.get("/")
def dashboard():
    cfg, trader, _ = _service()
    today = date.today().isoformat()
    positions = trader.open_positions()
    return render_template_string(HTML, capital=trader.available_capital(), positions=positions,
        summary=trader.daily_summary(today), trades=_trade_rows(trader), signals=[],
        data_directory=_scan_data_directory(cfg))


@app.post("/scan")
def scan():
    cfg, trader, _ = _service()
    symbols = [s.strip().upper() for s in request.form.get("symbols", "").split(",") if s.strip()]
    signals = scan_symbols(symbols, config_path=_scan_config_path())
    today = date.today().isoformat()
    return render_template_string(HTML, capital=trader.available_capital(),
        positions=trader.open_positions(), summary=trader.daily_summary(today),
        trades=_trade_rows(trader), signals=signals,
        data_directory=_scan_data_directory(cfg))


@app.get("/api/summary")
def api_summary():
    cfg, trader, _ = _service()
    today = date.today().isoformat()
    return jsonify({"mode": "paper", "simulated": True,
        "available_capital": trader.available_capital(),
        "summary": _record(trader.daily_summary(today)),
        "open_positions": [_record(p) for p in trader.open_positions()],
        "trades": _trade_rows(trader),
        "data_provider": cfg["market_data"].get("provider", "none")})


@app.post("/api/scan")
def api_scan():
    payload = request.get_json(silent=True) or {}
    symbols = payload.get("symbols", [])
    if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
        return jsonify({"error": "symbols must be a list of strings"}), 400
    results = scan_symbols([s.strip().upper() for s in symbols], config_path=_scan_config_path())
    return jsonify([_record(s) for s in results])


@app.post("/api/paper-trades")
def api_open_paper_trade():
    payload = request.get_json(silent=True) or {}
    try:
        cfg, trader, limits = _service()
        symbol = str(payload.get("symbol", "")).strip().upper()
        entry = float(payload["entry_price"])
        stop = float(payload["stop_loss"])
        target = float(payload["target_price"])
        key = str(payload["signal_key"])
        quantity_value = payload.get("quantity")
        capital = trader.available_capital()
        sizing = size_long_position(entry_price=entry, stop_loss=stop,
            target_price=target, available_capital=capital, limits=limits)
        quantity = sizing.quantity if quantity_value is None else int(quantity_value)
        if not sizing.accepted or quantity <= 0 or quantity > sizing.quantity:
            return jsonify({"error": sizing.reason if not sizing.accepted else "quantity exceeds risk-sized quantity"}), 400
        allowed, reason = check_trade_allowed(proposed_risk=quantity * sizing.planned_loss_per_share,
            available_capital=capital, required_capital=quantity * entry,
            open_positions=len(trader.open_positions()),
            realized_daily_pnl=trader.daily_risk_pnl(date.today()), limits=limits)
        if not allowed:
            return jsonify({"error": reason}), 409
        opened = trader.open_position(symbol=symbol, quantity=quantity, entry_price=entry,
            stop_loss=stop, target_price=target, signal_key=key,
            timestamp=datetime.now(timezone.utc))
        return jsonify(_record(opened)), 201
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/paper-trades/<int:trade_id>/close")
def api_close_paper_trade(trade_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        _, trader, _ = _service()
        closed = trader.close_position(trade_id, reference_price=float(payload["reference_price"]),
            reason=str(payload.get("reason", "manual_paper_exit")), timestamp=datetime.now(timezone.utc))
        return jsonify(_record(closed))
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/health")
def health():
    return jsonify({"status": "ok", "mode": "paper"})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)
