import json

import httpx
import pytest

from app.ai.deepseek import (
    DeepSeekAPIError, DeepSeekClient, DeepSeekNotConfigured, DeepSeekResponseError,
)
from app.core.config import Settings


def cfg(**overrides):
    values = {"deepseek_api_key": "fake-for-unit-tests"}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_missing_key_fails_before_network():
    with mock_client(lambda request: pytest.fail("Unexpected HTTP request")) as http:
        ai = DeepSeekClient(config=cfg(deepseek_api_key=""), http_client=http)
        with pytest.raises(DeepSeekNotConfigured, match="DEEPSEEK_API_KEY"):
            ai.complete(system_prompt="system", user_prompt="user")


def test_json_mode_uses_server_side_secret_and_thinking_disabled():
    def handler(request: httpx.Request):
        assert request.url == "https://api.deepseek.com/chat/completions"
        assert request.headers["Authorization"] == "Bearer fake-for-unit-tests"
        payload = json.loads(request.content)
        assert payload["model"] == "deepseek-flash"
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["temperature"] == 0
        assert payload["max_tokens"] == 6000
        assert payload["response_format"] == {"type": "json_object"}
        assert "JSON" in payload["messages"][0]["content"]
        assert payload["stream"] is False
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"facts":[]}'}, "finish_reason": "stop"}]},
        )

    with mock_client(handler) as http:
        ai = DeepSeekClient(config=cfg(), http_client=http)
        assert ai.complete_json(system_prompt="Extract facts.", user_prompt="Документ") == {"facts": []}


def test_plain_response_does_not_require_json():
    def handler(request):
        body = json.loads(request.content)
        assert "response_format" not in body
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "OK"}, "finish_reason": "stop"},
        ]})

    with mock_client(handler) as http:
        assert DeepSeekClient(config=cfg(), http_client=http).complete(
            system_prompt="S", user_prompt="U",
        ) == "OK"


@pytest.mark.parametrize("status", [401, 402, 429, 503])
def test_http_errors_are_sanitized(status):
    with mock_client(lambda request: httpx.Response(
        status, text="should not be forwarded: secret text",
    )) as http:
        with pytest.raises(DeepSeekAPIError) as error:
            DeepSeekClient(config=cfg(), http_client=http).complete(
                system_prompt="S", user_prompt="U",
            )
        assert str(status) in str(error.value)
        assert "secret" not in str(error.value)


def test_truncated_response_raises():
    with mock_client(lambda request: httpx.Response(
        200, json={"choices": [
            {"message": {"content": "partial"}, "finish_reason": "length"},
        ]},
    )) as http:
        with pytest.raises(DeepSeekResponseError, match="truncated"):
            DeepSeekClient(config=cfg(), http_client=http).complete(
                system_prompt="S", user_prompt="U",
            )


def test_invalid_json_and_unexpected_payload_raise():
    with mock_client(lambda request: httpx.Response(
        200, json={"choices": [
            {"message": {"content": "invalid json"}, "finish_reason": "stop"},
        ]},
    )) as http:
        with pytest.raises(DeepSeekResponseError, match="invalid JSON"):
            DeepSeekClient(config=cfg(), http_client=http).complete_json(
                system_prompt="S", user_prompt="U",
            )


def test_matching_json_accepts_observed_search_more_suffix():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"choices": [{
            "message": {"content": '{"links":[{"requirement_id":"r1"}]} ,"search_more":false}'},
            "finish_reason": "stop",
        }]})

    with mock_client(handler) as http:
        result = DeepSeekClient(config=cfg(), http_client=http).complete_json(
            system_prompt="S", user_prompt="U",
        )

    assert result == {"links": [{"requirement_id": "r1"}], "search_more": False}
    assert calls == 1


def test_network_timeout_raises_without_key_leak():
    calls = 0

    def timeout(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timeout")

    with mock_client(timeout) as http:
        with pytest.raises(DeepSeekAPIError, match="timed out") as error:
            DeepSeekClient(config=cfg(), http_client=http).complete(
                system_prompt="S", user_prompt="U",
            )
        assert "fake-for-unit-tests" not in str(error.value)
        assert calls == 2


@pytest.mark.parametrize("exception_type", [httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_transient_response_failure_retries_once_then_uses_valid_response(exception_type):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise exception_type("temporary response failure")
        return httpx.Response(200, json={"choices": [{
            "message": {"content": '{"ok":true}'}, "finish_reason": "stop",
        }]})

    with mock_client(handler) as http:
        result = DeepSeekClient(config=cfg(), http_client=http).complete_json(
            system_prompt="S", user_prompt="U",
        )

    assert result == {"ok": True}
    assert calls == 2


def test_key_is_redacted_in_config_repr():
    assert "fake-for-unit-tests" not in repr(cfg())



def test_transient_connect_error_retries_before_any_response(monkeypatch, caplog):
    from app.ai.deepseek import DeepSeekConnectionError

    monkeypatch.setattr("app.ai.deepseek.time.sleep", lambda seconds: None)
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            raise httpx.ConnectError("private network path should not appear")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"ok":true}'},
                         "finish_reason": "stop"}],
        })

    with mock_client(handler) as http:
        ai = DeepSeekClient(config=cfg(), http_client=http)
        assert ai.complete_json(system_prompt="S", user_prompt="U") == {"ok": True}
    assert len(seen) == 2
    assert "type=ConnectError attempt=1/3" in caplog.text
    assert "private network path" not in caplog.text
    assert "fake-for-unit-tests" not in caplog.text


def test_persistent_connect_error_stops_after_three_attempts(monkeypatch):
    from app.ai.deepseek import DeepSeekConnectionError

    monkeypatch.setattr("app.ai.deepseek.time.sleep", lambda seconds: None)
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("do not leak internal hostname")

    with mock_client(handler) as http:
        ai = DeepSeekClient(config=cfg(), http_client=http)
        with pytest.raises(DeepSeekConnectionError, match="after 3 attempts") as error:
            ai.complete(system_prompt="S", user_prompt="U")
    assert len(calls) == 3
    assert "internal hostname" not in str(error.value)


@pytest.mark.parametrize("exception_type", [
    httpx.ReadError, httpx.WriteError,
])
def test_ambiguous_request_delivery_never_retried(exception_type, monkeypatch):
    monkeypatch.setattr("app.ai.deepseek.time.sleep",
                        lambda seconds: pytest.fail("Unsafe request retry"))
    seen = []

    def handler(request):
        seen.append(request)
        raise exception_type("delivery unknown; should not leak")

    with mock_client(handler) as http:
        with pytest.raises(DeepSeekAPIError):
            DeepSeekClient(config=cfg(), http_client=http).complete(
                system_prompt="S", user_prompt="U",
            )
    assert len(seen) == 1
