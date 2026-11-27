"""Thread audit trace writer (replaces the JSONL conversation log).

Buffers records and appends them to the process-wide audit store
(`audit_store.py`). Fail-soft by design: audit failures never break the loop.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from soothe.logging.audit_store import AuditStoreProtocol, get_audit_store, now_ts
from soothe.utils.text import truncate_text

logger = logging.getLogger(__name__)

_RESULT_CONTENT_LIMIT = 2000
_MAX_ARG_BYTES = 256 * 1024
_ARG_PREVIEW_LIMIT = 1000
_MESSAGE_TUPLE_LENGTH = 2
_BUFFER_FLUSH_THRESHOLD = 100
_BUFFER_FLUSH_INTERVAL_SECONDS = 1.0


def _extract_thinking(msg: Any) -> str:
    """Extract reasoning text from `additional_kwargs["reasoning_content"]`."""
    ak = getattr(msg, "additional_kwargs", None) or {}
    if not isinstance(ak, dict):
        return ""
    return str(ak.get("reasoning_content") or "").strip()


def _capped_args(args: Any) -> tuple[Any, bool]:
    """Serialize tool-call args; over-cap args collapse to a text preview."""
    try:
        serialized = json.dumps(args, default=str)
    except (TypeError, ValueError):
        text = str(args)
        return ({"preview": text} if len(text.encode("utf-8")) > _MAX_ARG_BYTES else text, True)
    if len(serialized.encode("utf-8")) <= _MAX_ARG_BYTES:
        return (args, False)
    return (
        {"preview": truncate_text(serialized, limit=_ARG_PREVIEW_LIMIT, marker="…", strip=False)},
        True,
    )


class ThreadAuditLogger:
    """Append-only audit trace writer for stream and conversation records."""

    def __init__(
        self,
        thread_id: str | int | None = None,
        *,
        store: AuditStoreProtocol | None = None,
        retention_days: int = 30,
        max_size_mb: int = 100,
    ) -> None:
        """Initialize the audit writer for one thread."""
        self._thread_id = str(thread_id or "default")
        self._store = store
        self._retention_days = retention_days
        self._max_size_mb = max_size_mb
        self._buffer: list[dict[str, Any]] = []
        self._last_flush_time: float = time.time()
        self._goal_completion_chunk_accum: list[str] = []

    def log(
        self,
        namespace: tuple[str, ...],
        mode: str,
        data: Any,
    ) -> None:
        """Log a stream chunk: custom events and tool-related messages."""
        if mode == "custom" and isinstance(data, dict):
            from soothe.ux import classify_event_to_tier

            record: dict[str, Any] = {
                "timestamp": now_ts(),
                "kind": "event",
                "namespace": list(namespace),
                "classification": classify_event_to_tier(data.get("type", ""), namespace),
                "data": data,
            }
            self._write_record(record)
        elif (
            mode == "messages"
            and isinstance(data, (tuple, list))
            and len(data) == _MESSAGE_TUPLE_LENGTH
        ):
            self._log_message_event(namespace, data)

    def log_user_input(self, text: str) -> None:
        """Log a user turn for later thread review."""
        cleaned = text.strip()
        if not cleaned:
            return
        self._write_record(
            {
                "timestamp": now_ts(),
                "kind": "conversation",
                "role": "user",
                "text": cleaned,
            }
        )

    def log_assistant_response(self, text: str) -> None:
        """Log an assistant turn for later thread review.

        Args:
            text: Final assistant response text.
        """
        cleaned = text.strip()
        if not cleaned:
            return
        self._write_record(
            {
                "timestamp": now_ts(),
                "kind": "conversation",
                "role": "assistant",
                "text": cleaned,
            }
        )

    def flush(self) -> None:
        """Flush buffered records to the audit store."""
        self._flush_buffer()

    def _log_message_event(
        self,
        namespace: tuple[str, ...],
        data: Any,
    ) -> None:
        """Log tool calls / tool results / loop assistant output from messages-mode chunks."""
        try:
            from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

            from soothe.ux.loop_stream import assistant_output_phase

            msg, _metadata = data
            if isinstance(msg, ToolMessage):
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                self._write_record(
                    {
                        "timestamp": now_ts(),
                        "kind": "tool_result",
                        "namespace": list(namespace),
                        "tool_name": getattr(msg, "name", "unknown"),
                        "content": truncate_text(
                            content, limit=_RESULT_CONTENT_LIMIT, marker="...", strip=False
                        ),
                    }
                )
            elif isinstance(msg, AIMessageChunk):
                from soothe.ux.loop_stream import is_goal_completion_stream_terminal

                phase = assistant_output_phase(msg)
                if phase != "goal_completion":
                    return
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                if content:
                    self._goal_completion_chunk_accum.append(content)
                if is_goal_completion_stream_terminal(msg):
                    cleaned = "".join(self._goal_completion_chunk_accum).strip()
                    self._goal_completion_chunk_accum.clear()
                    if cleaned:
                        self._write_record(
                            {
                                "timestamp": now_ts(),
                                "kind": "conversation",
                                "role": "assistant",
                                "text": cleaned,
                                "phase": "goal_completion",
                                "namespace": list(namespace),
                            }
                        )
            elif isinstance(msg, AIMessage):
                tool_calls = getattr(msg, "tool_calls", None) or []
                for tc in tool_calls:
                    if isinstance(tc, dict) and tc.get("name"):
                        args, args_truncated = _capped_args(tc.get("args", {}))
                        self._write_record(
                            {
                                "timestamp": now_ts(),
                                "kind": "tool_call",
                                "namespace": list(namespace),
                                "tool_name": tc["name"],
                                "args": args,
                                "args_truncated": args_truncated,
                            }
                        )

                thinking = _extract_thinking(msg)
                if thinking:
                    self._write_record(
                        {
                            "timestamp": now_ts(),
                            "kind": "thinking",
                            "namespace": list(namespace),
                            "text": thinking,
                            "source": "additional_kwargs.reasoning_content",
                        }
                    )

                phase = assistant_output_phase(msg)
                if phase and not tool_calls:
                    content = msg.content if isinstance(msg.content, str) else str(msg.content)
                    cleaned = content.strip()
                    if cleaned:
                        self._write_record(
                            {
                                "timestamp": now_ts(),
                                "kind": "conversation",
                                "role": "assistant",
                                "text": cleaned,
                                "phase": phase,
                                "namespace": list(namespace),
                            }
                        )
        except Exception:
            logger.debug("Failed to log message event", exc_info=True)

    def _write_record(self, record: dict[str, Any]) -> None:
        """Buffer a record; flush on threshold or interval (fail-soft)."""
        try:
            self._buffer.append(record)
            current_time = time.time()
            if (
                len(self._buffer) >= _BUFFER_FLUSH_THRESHOLD
                or current_time - self._last_flush_time >= _BUFFER_FLUSH_INTERVAL_SECONDS
            ):
                self._flush_buffer()
        except Exception:
            logger.debug("Audit write failed", exc_info=True)

    def _flush_buffer(self) -> None:
        """Append buffered records to the audit store (fail-soft)."""
        if not self._buffer:
            return
        try:
            self._audit_store.append(self._thread_id, self._buffer)
            self._buffer.clear()
            self._last_flush_time = time.time()
        except Exception:
            logger.debug("Audit flush failed", exc_info=True)

    @property
    def _audit_store(self) -> AuditStoreProtocol:
        return self._store or get_audit_store()
