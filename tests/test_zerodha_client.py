import hashlib

import pytest

from broker.zerodha_client import KiteApiError, KiteCredentials, ZerodhaReadOnlyClient


class Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class Session:
    def __init__(self, response=None):
        self.response = response or Response({"status": "success", "data": {"ok": True}})
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append(("POST", args, kwargs))
        return self.response

    def get(self, *args, **kwargs):
        self.calls.append(("GET", args, kwargs))
        return self.response


def test_official_login_url_and_token_checksum():
    session = Session(Response({"status": "success", "data": {"access_token": "secret-token"}}))
    client = ZerodhaReadOnlyClient(KiteCredentials("key", "secret"), session=session)
    assert "api_key=key" in client.login_url()
    result = client.exchange_request_token("request")
    assert result["access_token"] == "secret-token"
    payload = session.calls[0][2]["data"]
    assert payload["checksum"] == hashlib.sha256(b"keyrequestsecret").hexdigest()


def test_read_only_methods_only_use_get():
    session = Session(Response({"status": "success", "data": []}))
    client = ZerodhaReadOnlyClient(KiteCredentials("key", "secret", "token"), session=session)
    client.profile()
    client.positions()
    client.holdings()
    client.margins()
    assert [call[0] for call in session.calls] == ["GET"] * 4
    assert all("Authorization" in call[2]["headers"] for call in session.calls)


def test_api_failure_is_reported_without_exposing_tokens():
    session = Session(Response({"status": "error", "error_type": "TokenException",
                               "message": "expired"}))
    session.response.status_code = 403
    client = ZerodhaReadOnlyClient(KiteCredentials("key", "secret", "sensitive"), session=session)
    with pytest.raises(KiteApiError, match="expired") as exc:
        client.profile()
    assert "sensitive" not in str(exc.value)
