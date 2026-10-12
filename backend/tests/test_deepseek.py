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


def test_network_timeout_raises_without_key_leak():
    def timeout(request):
        raise httpx.ReadTimeout("timeout")

    with mock_client(timeout) as http:
        with pytest.raises(DeepSeekAPIError, match="timed out") as error:
            DeepSeekClient(config=cfg(), http_client=http).complete(
                system_prompt="S", user_prompt="U",
            )
        assert "fake-for-unit-tests" not in str(error.value)


def test_key_is_redacted_in_config_repr():
    assert "fake-for-unit-tests" not in repr(cfg())
