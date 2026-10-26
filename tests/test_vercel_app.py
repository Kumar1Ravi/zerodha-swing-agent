from datetime import date, datetime, timedelta, timezone

import pytest

import app as webapp
from execution.paper_trader import PaperTrader


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    database = tmp_path / "web.sqlite3"
    config = {
        "app": {"mode": "paper", "database_path": str(database)},
        "risk": {"starting_capital_inr": 10_000, "planned_risk_per_trade_inr": 200,
                 "hard_risk_ceiling_per_trade_inr": 300, "max_open_positions": 1,
                 "daily_loss_limit_inr": 300, "gap_slippage_buffer_percent": 0,
                 "allow_leverage": False, "allow_averaging_down": False},
        "strategy": {}, "execution": {"simulated_slippage_percent": 0},
        "market_data": {"provider": "csv", "csv_directory": str(tmp_path / "market"),
                         "max_data_age_minutes": 30},
    }
    config_path = tmp_path / "config.yaml"
    import yaml
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    webapp.app.config.update(TESTING=True)
    return webapp.app.test_client(), config_path, database


def test_dashboard_and_summary_api(client):
    http, _, _ = client
    assert http.get("/").status_code == 200
    response = http.get("/api/summary")
    assert response.status_code == 200
    assert response.json["mode"] == "paper"
    assert response.json["available_capital"] == 10_000


def test_paper_trade_api_checks_risk_and_closes(client):
    http, _, _ = client
    response = http.post("/api/paper-trades", json={"symbol": "ABC", "entry_price": 100,
        "stop_loss": 95, "target_price": 110, "signal_key": "s1"})
    assert response.status_code == 201
    trade_id = response.json["id"]
    close = http.post(f"/api/paper-trades/{trade_id}/close",
                      json={"reference_price": 110, "reason": "target"})
    assert close.status_code == 200
    assert close.json["status"] == "CLOSED"


def test_deployment_requires_access_token_and_database(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    response = webapp.app.test_client().get("/api/summary")
    assert response.status_code == 401


def test_vercel_protection_gates_dashboard_while_json_api_keeps_token(client, monkeypatch):
    http, _, _ = client
    service = webapp._service()
    monkeypatch.setattr(webapp, "_service", lambda: service)
    monkeypatch.setenv("VERCEL", "1")
    assert http.get("/").status_code == 200
    assert http.get("/api/summary").status_code == 401


def test_entry_api_rejects_over_risk_quantity(client):
    http, _, _ = client
    response = http.post("/api/paper-trades", json={"symbol": "ABC", "entry_price": 100,
        "stop_loss": 95, "target_price": 110, "quantity": 41, "signal_key": "large"})
    assert response.status_code == 400
