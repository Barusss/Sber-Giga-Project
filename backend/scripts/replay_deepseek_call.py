"""Replay ONE exact DeepSeek request from a private forensic capture.

Default is safe dry-run. --execute makes a paid request. This script never
accepts an API key as a CLI flag and never prints or logs source text.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.ai import diagnostics
from app.ai.deepseek import DeepSeekClient
from app.core.config import settings

JSON_SUFFIX = "\nReturn exactly one valid JSON object. Do not use Markdown fences."


def prepare_request(
    captured: dict, *, max_tokens: int | None = None,
    trim_candidates: int | None = None,
):
    messages = captured.get("messages")
    if not isinstance(messages, list) or len(messages) != 2:
        raise ValueError("Capture must contain exactly two original messages")
    if not isinstance(messages[0], dict) or not isinstance(messages[1], dict):
        raise ValueError("Invalid message shapes")
    system = messages[0].get("content")
    user = messages[1].get("content")
    if not isinstance(system, str) or not isinstance(user, str):
        raise ValueError("Captured message contents must be strings")
    json_mode = captured.get("response_format") == {"type": "json_object"}
    if json_mode:
        if not system.endswith(JSON_SUFFIX):
            raise ValueError("Captured JSON mode system prompt lacks expected suffix")
        system = system[:-len(JSON_SUFFIX)]
    if trim_candidates is not None:
        if trim_candidates < 1:
            raise ValueError("trim_candidates must be >= 1")
        payload = json.loads(user)
        if not isinstance(payload, dict) or not isinstance(payload.get("znp_candidates"), list):
            raise ValueError("--trim-candidates only supports targeted ZnP batches")
        payload["znp_candidates"] = payload["znp_candidates"][:trim_candidates]
        user = json.dumps(payload, ensure_ascii=False)
    cap = max_tokens if max_tokens is not None else captured.get("max_tokens")
    if not isinstance(cap, int) or cap < 1:
        raise ValueError("Missing or invalid max_tokens")
    thinking = captured.get("thinking")
    thinking_type = thinking.get("type") if isinstance(thinking, dict) else None
    if thinking_type not in {"enabled", "disabled"}:
        raise ValueError("Missing thinking mode")
    model = captured.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("Missing model name")
    return {
        "system_prompt": system,
        "user_prompt": user,
        "json_mode": json_mode,
        "model": model,
        "thinking": thinking_type,
        "max_tokens": cap,
    }


def replay(captured: dict, *, max_tokens: int | None = None,
           trim_candidates: int | None = None, execute: bool = False,
           client=None) -> dict:
    spec = prepare_request(
        captured, max_tokens=max_tokens, trim_candidates=trim_candidates,
    )
    summary = {
        "model": spec["model"],
        "thinking": spec["thinking"],
        "max_tokens": spec["max_tokens"],
        "system_chars": len(spec["system_prompt"]),
        "user_chars": len(spec["user_prompt"]),
        "user_sha": diagnostics.sha(spec["user_prompt"]),
        "json_mode": spec["json_mode"],
        "trim_candidates": trim_candidates,
    }
    diagnostics.record("replay.prepared", **summary)
    if not execute:
        return {"executed": False, **summary}
    config = settings.model_copy(update={
        "deepseek_model": spec["model"],
        "deepseek_thinking": spec["thinking"],
        "deepseek_max_tokens": spec["max_tokens"],
    })
    ai = client if client is not None else DeepSeekClient(config=config)
    try:
        if spec["json_mode"]:
            response = ai.complete_json(
                system_prompt=spec["system_prompt"],
                user_prompt=spec["user_prompt"],
            )
        else:
            response = ai.complete(
                system_prompt=spec["system_prompt"],
                user_prompt=spec["user_prompt"],
            )
    finally:
        if client is None:
            ai.close()
    diagnostics.record(
        "replay.finished",
        response_keys=list(response.keys()) if isinstance(response, dict) else None,
    )
    return {"executed": True, **summary}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--out", default="diagnostics", type=Path)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--trim-candidates", type=int, default=None)
    parser.add_argument("--execute", action="store_true",
                        help="Make ONE paid DeepSeek request")
    parser.add_argument("--capture-payloads", action="store_true")
    args = parser.parse_args(argv)

    request = json.loads(args.request.read_text(encoding="utf-8"))
    with diagnostics.diagnostic_run(
        args.out, capture_payloads=args.capture_payloads, strict=True,
    ):
        path = diagnostics.trace_directory()
        try:
            summary = replay(
                request, max_tokens=args.max_tokens,
                trim_candidates=args.trim_candidates, execute=args.execute,
            )
        except BaseException as exc:
            diagnostics.record("replay.error", error_type=type(exc).__name__)
            print(json.dumps({
                "trace_dir": path, "error_type": type(exc).__name__,
            }, ensure_ascii=False))
            raise
        print(json.dumps({"trace_dir": path, **summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
