"""Provider-specific webhook acknowledgement semantics."""

import pytest

from app.services.webhook_response import validate_business_response


@pytest.mark.parametrize(
    ("provider", "body"),
    [
        ("wecom", {"errcode": 0}),
        ("dingtalk", {"errcode": 0}),
        ("feishu", {"code": 0}),
        ("feishu", {"StatusCode": 0}),
        ("feishu", {"code": None, "StatusCode": 0}),
    ],
)
def test_numeric_zero_is_success(provider, body):
    assert validate_business_response(provider, body)[0]


@pytest.mark.parametrize(
    ("provider", "body"),
    [
        ("wecom", {"errcode": False}),
        ("wecom", {"errcode": True}),
        ("dingtalk", {"errcode": False}),
        ("feishu", {"code": False}),
        ("feishu", {"StatusCode": True}),
        ("wecom", {"ok": True}),
        ("feishu", [0]),
        ("unknown", {"success": True}),
    ],
)
def test_invalid_business_acknowledgement_is_failure(provider, body):
    assert not validate_business_response(provider, body)[0]


def test_raw_provider_does_not_require_json_response(monkeypatch):
    from app.services import alert_service

    class EmptyResponse:
        status_code = 204
        text = ""

        def json(self):
            raise ValueError("empty response")

    monkeypatch.setattr(alert_service.httpx, "post", lambda *_args, **_kwargs: EmptyResponse())
    alert_service._push("https://example.test/hook", "raw", "message")
