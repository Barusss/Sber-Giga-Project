"""Server-side DeepSeek Chat Completions client shared by modules 2 and 3.

No provider call is made by importing this module. The real API key lives only
in .env / environment variables. Do not expose generic prompt APIs publicly
without authentication and rate limits.
"""

import json
import logging
import time
from typing import Any
from uuid import uuid4

from app.ai import diagnostics

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
            diagnostics.record("provider.preflight_failed", reason="missing_api_key")
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
        call_id = uuid4().hex
        request_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_started = time.monotonic()
        diagnostics.record(
            "provider.request.start",
            call_id=call_id,
            model=self._config.deepseek_model,
            thinking=self._config.deepseek_thinking,
            json_mode=json_mode,
            max_tokens=self._config.deepseek_max_tokens,
            timeout_seconds=self._config.deepseek_timeout_seconds,
            request_bytes=len(request_bytes),
            prompt_chars=len(system_prompt) + len(user_prompt),
            system_chars=len(system_prompt),
            user_chars=len(user_prompt),
            request_sha=diagnostics.sha(request_bytes),
            endpoint_host=httpx.URL(url).host,
        )
        # Body capture contains document text: never includes Authorization.
        diagnostics.capture(call_id, "request", body)
        # Retry connection failures and one observed transient response failure.
        # A timed-out request may already have been billed by the provider.
        # Never replay WriteError, ReadError, or an HTTP response.
        for attempt in range(1, 4):
            started = time.monotonic()
            try:
                diagnostics.record("provider.http.attempt", call_id=call_id, attempt=attempt)
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
                diagnostics.record(
                    "provider.network.error", call_id=call_id, stage="connect",
                    error_type=type(exc).__name__, attempt=attempt,
                    elapsed_ms=elapsed_ms,
                )
                if attempt == 3:
                    raise DeepSeekConnectionError(
                        f"DeepSeek connection setup failed after {attempt} attempts "
                        f"({type(exc).__name__}); check container network or retry."
                    ) from exc
                time.sleep((0.3, 0.8)[attempt - 1])
            except (httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                elapsed_ms = round((time.monotonic() - request_started) * 1000)
                diagnostics.record(
                    "provider.network.error", call_id=call_id, stage="request",
                    error_type=type(exc).__name__, attempt=attempt,
                    elapsed_ms=elapsed_ms, retrying=attempt == 1,
                )
                logger.warning(
                    "DeepSeek network error: stage=request type=%s attempt=%d/2 elapsed_ms=%d",
                    type(exc).__name__, attempt, elapsed_ms,
                )
                if attempt == 1:
                    continue
                reason = "connection closed unexpectedly"
                if isinstance(exc, httpx.ReadTimeout):
                    reason = "timed out"
                raise DeepSeekAPIError(
                    f"DeepSeek request {reason} after {attempt} attempts "
                    f"({type(exc).__name__})."
                ) from exc
            except httpx.TimeoutException as exc:
                logger.warning(
                    "DeepSeek network error: stage=request type=%s attempt=1/1 elapsed_ms=%d",
                    type(exc).__name__, int((time.monotonic() - started) * 1000),
                )
                diagnostics.record(
                    "provider.network.error", call_id=call_id, stage="request",
                    error_type=type(exc).__name__, attempt=attempt,
                    elapsed_ms=round((time.monotonic() - request_started) * 1000),
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
                diagnostics.record(
                    "provider.network.error", call_id=call_id, stage="request",
                    error_type=type(exc).__name__, attempt=attempt,
                    elapsed_ms=round((time.monotonic() - request_started) * 1000),
                )
                raise DeepSeekAPIError(
                    f"DeepSeek request transport error ({type(exc).__name__}); "
                    "not retried because delivery is uncertain."
                ) from exc

        diagnostics.record(
            "provider.http.response", call_id=call_id, status_code=response.status_code,
            attempts=attempt, elapsed_ms=round((time.monotonic() - request_started) * 1000),
            request_id=(response.headers.get("x-request-id") or "")[:128],
            response_bytes=len(response.content),
            response_sha=diagnostics.sha(response.content),
        )
        if response.status_code >= 400:
            diagnostics.record("provider.http.error", call_id=call_id, status_code=response.status_code)
            # Error payload may contain document data; capture only when opted in.
            diagnostics.capture(call_id, "response", {
                "status_code": response.status_code, "body": response.text,
            })
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
            diagnostics.record(
                "provider.response.malformed", call_id=call_id,
                error_type=type(exc).__name__,
            )
            diagnostics.capture(call_id, "response", {"body": response.text})
            raise DeepSeekResponseError("Malformed DeepSeek API response.") from exc

        usage = result.get("usage") if isinstance(result, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        message = choice.get("message") if isinstance(choice, dict) else None
        message = message if isinstance(message, dict) else {}
        diagnostics.record(
            "provider.response.parsed", call_id=call_id,
            finish_reason=finish_reason,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            completion_details=usage.get("completion_tokens_details"),
            content_chars=len(content) if isinstance(content, str) else None,
            content_sha=diagnostics.sha(content) if isinstance(content, str) else None,
            reasoning_chars=len(message.get("reasoning_content") or ""),
        )
        diagnostics.capture(call_id, "response", result)

        if finish_reason == "length":
            diagnostics.record(
                "provider.response.rejected", call_id=call_id, code="finish_reason_length",
                max_tokens=self._config.deepseek_max_tokens,
                completion_tokens=usage.get("completion_tokens"),
            )
            raise DeepSeekResponseError(
                "DeepSeek response was truncated; increase DEEPSEEK_MAX_TOKENS."
            )
        if finish_reason in {"content_filter", "insufficient_system_resource", "aborted"}:
            diagnostics.record(
                "provider.response.rejected", call_id=call_id,
                code="finish_reason_incomplete", finish_reason=finish_reason,
            )
            raise DeepSeekResponseError("DeepSeek did not finish the response.")
        if not isinstance(content, str) or not content.strip():
            diagnostics.record("provider.response.rejected", call_id=call_id, code="empty_content")
            raise DeepSeekResponseError("DeepSeek returned empty content.")
        diagnostics.record("provider.response.accepted", call_id=call_id)
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
            value = None
            try:
                links_object, end = json.JSONDecoder().raw_decode(raw)
                suffix = raw[end:].strip()
                if (isinstance(links_object, dict)
                        and set(links_object) == {"links"}
                        and isinstance(links_object["links"], list)
                        and suffix.startswith(",")):
                    search_object = json.loads("{" + suffix[1:])
                    if (isinstance(search_object, dict)
                            and set(search_object) == {"search_more"}
                            and isinstance(search_object["search_more"], bool)):
                        links_object["search_more"] = search_object["search_more"]
                        value = links_object
            except json.JSONDecodeError:
                pass
            if value is None:
                diagnostics.record(
                    "provider.json.invalid", raw_sha=diagnostics.sha(raw),
                    raw_chars=len(raw), error_line=exc.lineno, error_column=exc.colno,
                    error_position=exc.pos,
                )
                raise DeepSeekResponseError("DeepSeek returned invalid JSON.") from exc
            diagnostics.record(
                "provider.json.repaired", code="search_more_outside_object",
                raw_sha=diagnostics.sha(raw), raw_chars=len(raw),
            )
        if not isinstance(value, dict):
            diagnostics.record("provider.json.invalid_type", type=type(value).__name__)
            raise DeepSeekResponseError("DeepSeek JSON response must be an object.")
        diagnostics.record(
            "provider.json.valid", key_count=len(value), keys=sorted(value.keys())[:20],
        )
        return value
