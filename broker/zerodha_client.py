"""Minimal Kite Connect authentication and read-only account adapter.

No order-placement, modification, or cancellation methods are implemented.
Secrets are read from environment variables and are never logged.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests


API_ROOT = "https://api.kite.trade"
LOGIN_ROOT = "https://kite.zerodha.com/connect/login"


class KiteApiError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class KiteCredentials:
    api_key: str
    api_secret: str
    access_token: str | None = None

    @classmethod
    def from_env(cls) -> "KiteCredentials":
        key = os.getenv("KITE_API_KEY", "").strip()
        secret = os.getenv("KITE_API_SECRET", "").strip()
        token = os.getenv("KITE_ACCESS_TOKEN", "").strip() or None
        if not key or not secret:
            raise ValueError("Set KITE_API_KEY and KITE_API_SECRET in the environment")
        return cls(key, secret, token)


class ZerodhaReadOnlyClient:
    """Kite Connect read-only requests implemented with requests."""

    def __init__(self, credentials: KiteCredentials, *, timeout_seconds: float = 10,
                 session: requests.Session | None = None) -> None:
        self.credentials = credentials
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    def login_url(self) -> str:
        return f"{LOGIN_ROOT}?{urlencode({'v': 3, 'api_key': self.credentials.api_key})}"

    def exchange_request_token(self, request_token: str) -> dict[str, Any]:
        if not request_token.strip():
            raise ValueError("request_token is required")
        checksum = hashlib.sha256(
            f"{self.credentials.api_key}{request_token}{self.credentials.api_secret}".encode("utf-8")
        ).hexdigest()
        response = self.session.post(f"{API_ROOT}/session/token", data={
            "api_key": self.credentials.api_key,
            "request_token": request_token,
            "checksum": checksum,
        }, headers={"X-Kite-Version": "3"}, timeout=self.timeout_seconds)
        payload = self._decode(response)
        data = payload.get("data", {})
        if not data.get("access_token"):
            raise KiteApiError("Kite session response did not include an access token")
        return data

    def profile(self) -> dict[str, Any]:
        return self._get("/user/profile")

    def positions(self) -> dict[str, Any]:
        return self._get("/portfolio/positions")

    def holdings(self) -> list[dict[str, Any]]:
        return self._get("/portfolio/holdings")

    def margins(self, segment: str = "equity") -> dict[str, Any]:
        if segment not in {"equity", "commodity"}:
            raise ValueError("segment must be 'equity' or 'commodity'")
        return self._get(f"/user/margins/{segment}")

    def _get(self, endpoint: str) -> Any:
        token = self.credentials.access_token
        if not token:
            raise ValueError("Set KITE_ACCESS_TOKEN in the environment after completing Kite login")
        response = self.session.get(f"{API_ROOT}{endpoint}", headers={
            "X-Kite-Version": "3",
            "Authorization": f"token {self.credentials.api_key}:{token}",
        }, timeout=self.timeout_seconds)
        return self._decode(response).get("data")

    @staticmethod
    def _decode(response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise KiteApiError(f"Kite returned non-JSON response (HTTP {response.status_code})") from exc
        if response.status_code >= 400 or payload.get("status") != "success":
            message = payload.get("message", "request failed")
            code = payload.get("error_type", "KiteApiError")
            raise KiteApiError(f"{code}: {message}")
        return payload
