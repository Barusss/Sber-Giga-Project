"""Opt-in, file-based forensic traces for TU/ZnP and DeepSeek.

Only explicit numeric metadata, hashes and fixed labels belong in events.jsonl.
Original document/prompt/response text is saved *only* with capture_payloads.
Never capture API keys or Authorization headers.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TraceContext:
    run_id: str
    directory: Path
    capture_payloads: bool
    strict: bool


_run: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar(
    "deepseek_diagnostics_run", default=None,
)
_phase: contextvars.ContextVar[str] = contextvars.ContextVar(
    "deepseek_diagnostics_phase", default="unknown",
)
_subject: contextvars.ContextVar[str] = contextvars.ContextVar(
    "deepseek_diagnostics_subject", default="",
)


def sha(text: str | bytes) -> str:
    payload = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(payload).hexdigest()[:16]


def _secure_directory(path: Path) -> Path:
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Keep existing user-provided shared directories intact, but do not make
    # newly written evidence readable by other users.
    return path


def _append_json(path: Path, data: dict[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")


def _context() -> TraceContext | None:
    active = _run.get()
    if active is not None:
        return active
    base = os.environ.get("DEEPSEEK_TRACE_DIR", "").strip()
    if not base:
        return None
    return TraceContext(
        run_id=f"process-{os.getpid()}",
        directory=_secure_directory(Path(base)) / f"process-{os.getpid()}",
        capture_payloads=os.environ.get("DEEPSEEK_TRACE_CAPTURE_PAYLOADS") == "1",
        strict=False,
    )


def enabled() -> bool:
    return _run.get() is not None or bool(os.environ.get("DEEPSEEK_TRACE_DIR", "").strip())


def current_phase() -> str:
    return _phase.get()


def current_subject() -> str:
    return _subject.get()


def record(event_name: str, **metadata: Any) -> None:
    """Emit one machine-readable event; NEVER pass raw prompts or credentials."""
    ctx = _context()
    if ctx is None:
        return
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "monotonic_ns": time.monotonic_ns(),
        "run_id": ctx.run_id,
        "pid": os.getpid(),
        "phase": _phase.get(),
        "subject": sha(_subject.get()) if _subject.get() else None,
        "event": event_name,
        **metadata,
    }
    try:
        _secure_directory(ctx.directory)
        _append_json(ctx.directory / "events.jsonl", event)
    except OSError:
        if ctx.strict:
            raise
        logger.exception("Unable to write DeepSeek diagnostic metadata")


def capture(call_id: str, kind: str, content: Any) -> None:
    """Store original input/output ONLY for explicit local sensitive capture.

    Contains proprietary document text. Never check this directory into git.
    """
    ctx = _context()
    if ctx is None or not ctx.capture_payloads:
        return
    if kind not in {"request", "response"}:
        raise ValueError("Invalid capture kind")
    safe_id = "".join(ch for ch in call_id if ch.isalnum() or ch == "-")[:64]
    directory = _secure_directory(ctx.directory / "private")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(directory / f"{safe_id}-{kind}.json"), flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(content, handle, ensure_ascii=False, default=str, indent=2)
    except OSError:
        if ctx.strict:
            raise
        logger.exception("Unable to write private DeepSeek diagnostic payload")


@contextmanager
def phase_scope(name: str, subject: str = "") -> Iterator[None]:
    phase_token = _phase.set(name)
    subject_token = _subject.set(subject)
    try:
        yield
    finally:
        _subject.reset(subject_token)
        _phase.reset(phase_token)


@contextmanager
def diagnostic_run(
    directory: str | Path | None = None,
    *,
    capture_payloads: bool = False,
    strict: bool = False,
) -> Iterator[str | None]:
    """Start a run with isolated output; no files or overhead when disabled."""
    base = directory or os.environ.get("DEEPSEEK_TRACE_DIR", "").strip()
    if not base:
        yield None
        return
    run_id = uuid4().hex
    path = _secure_directory(Path(base) / run_id)
    ctx = TraceContext(run_id, path, capture_payloads, strict)
    token = _run.set(ctx)
    started = time.monotonic()
    try:
        record("run.start", capture_payloads=capture_payloads)
        yield run_id
    except BaseException as exc:
        record(
            "run.error", error_type=type(exc).__name__,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )
        raise
    else:
        record("run.finish", elapsed_ms=round((time.monotonic() - started) * 1000))
    finally:
        _run.reset(token)


def trace_directory() -> str | None:
    ctx = _context()
    return str(ctx.directory) if ctx is not None else None


def trace_comparison(func):
    """Opt-in trace root for background/API comparisons on their worker thread."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        with diagnostic_run(
            capture_payloads=os.environ.get("DEEPSEEK_TRACE_CAPTURE_PAYLOADS") == "1",
        ):
            payload = args[0] if args else kwargs.get("payload")
            record(
                "comparison.entry",
                test_mode=bool(getattr(payload, "test_mode", False)),
                tu_document_sha=sha(str(getattr(payload, "tu_document_id", ""))),
                znp_document_sha=sha(str(getattr(payload, "znp_document_id", ""))),
            )
            return func(*args, **kwargs)
    return wrapped
