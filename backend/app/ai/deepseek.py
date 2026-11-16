"""Server-side DeepSeek Chat Completions client shared by modules 2 and 3.

No provider call is made by importing this module. The real API key lives only
in .env / environment variables. Do not expose generic prompt APIs publicly
without authentication and rate limits.
"""

import json
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

from app.core.config import Settings, settings


class DeepSeekError(RuntimeError):
    """Sanitized DeepSeek error: never includes credentials or document text."""


class DeepSeekConnectionError(DeepSeekError):
    """Connection was not established despite safe setup-only retries."""


class DeepSeekNotConfigured(DeepSeekError):
    pass


class DeepSeekAPIError(DeepSeekError):
    pass


class DeepSeekResponseError(DeepSeekError):
    pass


class DeepSeekClient:
    """Minimal reusable client. Pass http_client in tests to avoid real network calls."""

    def __init__(
        self,
        *,
        config: Settings | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._config = config if config is not None else settings
        self._owned_client = http_client is None
        # Reuse pooled TLS connections across all model stages of one run.
        self._http_client = (
            http_client if http_client is not None
            else httpx.Client(timeout=self._config.deepseek_timeout_seconds)
        )

    def close(self) -> None:
        if self._owned_client:
            self._http_client.close()

    def __del__(self):
        # Best-effort cleanup if the caller did not close the owned pool.
        if getattr(self, "_owned_client", False):
            client = getattr(self, "_http_client", None)
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_mode: bool = False,
    ) -> str:
        """Return model text; json_mode asks DeepSeek for one JSON object."""
        key = self._config.deepseek_api_key.get_secret_value().strip()
        if not key:
            raise DeepSeekNotConfigured("Set DEEPSEEK_API_KEY in the root .env file.")

        if json_mode:
            system_prompt = (
                system_prompt.rstrip()
                + "\nReturn exactly one valid JSON object. Do not use Markdown fences."
            )

        body: dict[str, Any] = {
            "model": self._config.deepseek_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "thinking": {"type": self._config.deepseek_thinking},
            "stream": False,
            "max_tokens": self._config.deepseek_max_tokens,
        }
        if self._config.deepseek_thinking == "disabled":
            body["temperature"] = 0
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        url = self._config.deepseek_base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        # Retries are limited to connection establishment failures, before
        # a request can have reached the provider. Never replay ReadTimeout,
        # ReadError, WriteError, RemoteProtocolError, or an HTTP response:
        # provider-side processing/billing could already have occurred.
        for attempt in range(1, 4):
            started = time.monotonic()
            try:
                response = self._http_client.post(
                    url, json=body, headers=headers,
                    timeout=self._config.deepseek_timeout_seconds,
                )
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                logger.warning(
                    "DeepSeek network error: stage=connect type=%s attempt=%d/3 elapsed_ms=%d",
                    type(exc).__name__, attempt, elapsed_ms,
                )
                if attempt == 3:
                    raise DeepSeekConnectionError(
                        f"DeepSeek connection setup failed after {attempt} attempts "
                        f"({type(exc).__name__}); check container network or retry."
                    ) from exc
                time.sleep((0.3, 0.8)[attempt - 1])
            except httpx.TimeoutException as exc:
                logger.warning(
                    "DeepSeek network error: stage=request type=%s attempt=1/1 elapsed_ms=%d",
                    type(exc).__name__, int((time.monotonic() - started) * 1000),
                )
                raise DeepSeekAPIError(
                    f"DeepSeek request timed out ({type(exc).__name__}); "
                    "not retried because delivery is uncertain."
                ) from exc
            except httpx.RequestError as exc:
                logger.warning(
                    "DeepSeek network error: stage=request type=%s attempt=1/1 elapsed_ms=%d",
                    type(exc).__name__, int((time.monotonic() - started) * 1000),
                )
                raise DeepSeekAPIError(
                    f"DeepSeek request transport error ({type(exc).__name__}); "
                    "not retried because delivery is uncertain."
                ) from exc

        if response.status_code >= 400:
            descriptions = {
                401: "DeepSeek API rejected the API key (HTTP 401).",
                402: "DeepSeek account has insufficient balance (HTTP 402).",
                429: "DeepSeek rate limit exceeded (HTTP 429).",
            }
            raise DeepSeekAPIError(
                descriptions.get(
                    response.status_code,
                    f"DeepSeek API returned HTTP {response.status_code}.",
                )
            )

        try:
            result = response.json()
            choice = result["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise DeepSeekResponseError("Malformed DeepSeek API response.") from exc

        if finish_reason == "length":
            raise DeepSeekResponseError(
                "DeepSeek response was truncated; increase DEEPSEEK_MAX_TOKENS."
            )
        if finish_reason in {"content_filter", "insufficient_system_resource", "aborted"}:
            raise DeepSeekResponseError("DeepSeek did not finish the response.")
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekResponseError("DeepSeek returned empty content.")
        return content

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        """Generate and parse one JSON object for facts or requirement extraction."""
        raw = self.complete(
            system_prompt=system_prompt, user_prompt=user_prompt, json_mode=True,
        )
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DeepSeekResponseError("DeepSeek returned invalid JSON.") from exc
        if not isinstance(value, dict):
            raise DeepSeekResponseError("DeepSeek JSON response must be an object.")
        return value
