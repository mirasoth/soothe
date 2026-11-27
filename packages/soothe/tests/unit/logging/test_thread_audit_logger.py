"""ThreadAuditLogger persistence of loop-tagged assistant output (resume parity).

When the loop emits ``loop_assistant_messages_chunk`` for ``plan_direct`` or
``goal_completion`` (LEDGER_DIRECT strategy), the text never reaches the
LangGraph checkpoint. ``ThreadAuditLogger`` must persist it as a
``kind=conversation, role=assistant`` row so ``LoopCardManager._derive_cards``
→ ``card_binder.collect_cognition_card_replay`` can surface it on resume.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from soothe.logging.audit_store import SqliteAuditStore
from soothe.logging.thread_audit_logger import ThreadAuditLogger


@pytest.fixture()
def audit_store(tmp_path: Path) -> Any:
    store = SqliteAuditStore(db_path=tmp_path / "audit.db")
    yield store
    store.close()


def _rows(store: Any, thread_id: str, limit: int = 100) -> list[dict[str, Any]]:
    return store.fetch_recent(thread_id, limit=limit)


def _make_logger(store: Any, thread_id: str) -> ThreadAuditLogger:
    return ThreadAuditLogger(thread_id=thread_id, store=store)


def test_plan_direct_phase_persists_as_conversation_row(audit_store: Any) -> None:
    logger = _make_logger(audit_store, "t1")
    msg = AIMessage(content="I will complete this goal directly: do thing")
    msg.phase = "plan_direct"

    logger.log(namespace=(), mode="messages", data=(msg, {}))
    logger.flush()

    conv = [r for r in _rows(audit_store, "t1") if r.get("kind") == "conversation"]
    assert len(conv) == 1
    assert conv[0]["role"] == "assistant"
    assert conv[0]["phase"] == "plan_direct"
    assert conv[0]["text"].startswith("I will complete this goal directly")


def test_goal_completion_phase_persists(audit_store: Any) -> None:
    logger = _make_logger(audit_store, "t2")
    msg = AIMessage(content="Result\n\n| col |\nTotal: 42")
    msg.phase = "goal_completion"

    logger.log(namespace=(), mode="messages", data=(msg, {}))
    logger.flush()

    conv = [
        r
        for r in _rows(audit_store, "t2")
        if r.get("kind") == "conversation" and r.get("role") == "assistant"
    ]
    assert len(conv) == 1
    assert conv[0]["phase"] == "goal_completion"
    assert "Total: 42" in conv[0]["text"]


def test_streaming_goal_completion_chunks_accumulate_until_terminal(audit_store: Any) -> None:
    """Terminal ``goal_completion`` stream frames persist one assembled row."""
    from soothe.ux.loop_stream import GOAL_COMPLETION_STREAM_TERMINAL_FIELD

    logger = _make_logger(audit_store, "t3")
    chunk_a = AIMessageChunk(content="There are ", phase="goal_completion")
    chunk_b = AIMessageChunk(
        content="3632 files.",
        phase="goal_completion",
        **{GOAL_COMPLETION_STREAM_TERMINAL_FIELD: True},
    )

    logger.log(namespace=(), mode="messages", data=(chunk_a, {}))
    logger.log(namespace=(), mode="messages", data=(chunk_b, {}))
    logger.flush()

    conv = [r for r in _rows(audit_store, "t3") if r.get("kind") == "conversation"]
    assert len(conv) == 1
    assert conv[0]["phase"] == "goal_completion"
    assert conv[0]["text"] == "There are 3632 files."


def test_non_terminal_goal_completion_chunk_is_not_persisted(audit_store: Any) -> None:
    logger = _make_logger(audit_store, "t3b")
    chunk = AIMessageChunk(content="partial token ")
    chunk.phase = "goal_completion"

    logger.log(namespace=(), mode="messages", data=(chunk, {}))
    logger.flush()

    conv = [r for r in _rows(audit_store, "t3b") if r.get("kind") == "conversation"]
    assert conv == []


def test_ai_message_without_loop_phase_is_not_persisted_as_conversation(
    audit_store: Any,
) -> None:
    logger = _make_logger(audit_store, "t4")
    msg = AIMessage(content="generic assistant text")
    # No phase → CoreAgent output we do not want to mirror into the activity log.

    logger.log(namespace=(), mode="messages", data=(msg, {}))
    logger.flush()

    rows = _rows(audit_store, "t4")
    assert [r for r in rows if r.get("kind") == "conversation"] == []


def test_tail_records_stay_buffered_without_explicit_flush(audit_store: Any) -> None:
    """Tail records stick in the buffer if no further write triggers the flush.

    The buffer flushes on (a) >=100 records OR (b) >=1 second since last flush
    AND a new write happens. After a loop ends, no new writes arrive on that
    thread — so the goal_completion conversation row and the trailing
    ``log_assistant_response`` row both stay in memory unless the caller
    explicitly invokes ``flush()`` (which is exactly what the engine's
    stream-finally block now does).
    """
    logger = _make_logger(audit_store, "tail")

    # Seed the buffer and flush deterministically so it starts empty for the
    # tail writes.
    logger._write_record({"timestamp": "t1", "kind": "event", "data": {"x": 1}})
    logger._write_record({"timestamp": "t2", "kind": "event", "data": {"x": 2}})
    logger._flush_buffer()  # type: ignore[attr-defined]

    # Tail writes: arrive within the 1s interval, no further writes follow.
    msg = AIMessage(content="FINAL ANSWER")
    msg.phase = "goal_completion"
    logger.log(namespace=(), mode="messages", data=(msg, {}))
    logger.log_assistant_response("FINAL ANSWER")

    # Snapshot the store WITHOUT calling flush(): the tail rows must still be
    # in memory.
    conv_rows = [r for r in _rows(audit_store, "tail") if r.get("kind") == "conversation"]
    assert conv_rows == [], "tail records must be stuck in buffer before flush()"
    assert len(logger._buffer) == 2  # type: ignore[attr-defined]

    # The engine's flush call lands the tail records in the store.
    logger.flush()
    conv_rows_after = [r for r in _rows(audit_store, "tail") if r.get("kind") == "conversation"]
    assert len(conv_rows_after) == 2
    assert conv_rows_after[0]["phase"] == "goal_completion"
    assert conv_rows_after[0]["text"] == "FINAL ANSWER"


def test_phase_tagged_row_does_not_get_overwritten_by_legacy_concat(audit_store: Any) -> None:
    """Document the resume duality the engine fix relies on.

    When a phase-tagged ``goal_completion`` row exists, the engine must SKIP
    the legacy ``log_assistant_response("".join(full_response))`` call,
    because that legacy row historically concatenates plan_direct text +
    ToolMessage outputs + goal_completion fragments into one malformed
    untyped assistant card. This test asserts that the two writes are
    distinguishable in the store: the phase-tagged row carries ``phase``,
    the legacy row does not — so the engine's
    ``phase_tagged_assistant_written`` flag can suppress one without
    affecting the other.
    """
    logger = _make_logger(audit_store, "dual")
    msg = AIMessage(content="## Result\n```\n42 files\n```")
    msg.phase = "goal_completion"

    # 1) Phase-tagged write (the new path)
    logger.log(namespace=(), mode="messages", data=(msg, {}))

    # 2) Legacy concat write (the bug the engine flag suppresses)
    logger.log_assistant_response(
        "I will do thing"  # plan_direct
        "raw tool output 1"
        "raw tool output 2"
        "## Result\n```\n42 files\n```"  # goal_completion fragment
    )

    logger.flush()
    assistant_rows = [
        r
        for r in _rows(audit_store, "dual")
        if r.get("kind") == "conversation" and r.get("role") == "assistant"
    ]
    assert len(assistant_rows) == 2
    tagged = [r for r in assistant_rows if r.get("phase") == "goal_completion"]
    legacy = [r for r in assistant_rows if not r.get("phase")]
    assert len(tagged) == 1
    assert len(legacy) == 1
    # The phase-tagged row is clean, the legacy is the jammed-together text.
    assert tagged[0]["text"] == "## Result\n```\n42 files\n```"
    assert "raw tool output" in legacy[0]["text"]


def test_ai_message_with_tool_calls_logs_tool_call_only_not_conversation(
    audit_store: Any,
) -> None:
    logger = _make_logger(audit_store, "t5")
    msg = AIMessage(
        content="calling tool",
        tool_calls=[{"id": "tc1", "name": "run_command", "args": {"command": "ls"}}],
    )
    msg.phase = "goal_completion"  # would otherwise trigger the conversation path

    logger.log(namespace=(), mode="messages", data=(msg, {}))
    logger.flush()

    rows = _rows(audit_store, "t5")
    assert any(r.get("kind") == "tool_call" for r in rows)
    # tool-bearing assistant messages are not user-visible final text, so no
    # conversation row should be written even when the phase matches.
    assert not any(r.get("kind") == "conversation" for r in rows)


def test_tool_call_args_are_stored_in_full(audit_store: Any) -> None:
    """Full tool args are stored untruncated (500-char preview is gone)."""
    logger = _make_logger(audit_store, "args-full")
    long_command = "echo " + "x" * 5000
    msg = AIMessage(
        content="calling tool",
        tool_calls=[
            {"id": "tc1", "name": "run_command", "args": {"command": long_command}},
        ],
    )

    logger.log(namespace=(), mode="messages", data=(msg, {}))
    logger.flush()

    rows = [r for r in _rows(audit_store, "args-full") if r.get("kind") == "tool_call"]
    assert len(rows) == 1
    assert rows[0]["args"] == {"command": long_command}
    assert rows[0]["args_truncated"] is False


def test_tool_call_args_over_cap_collapse_to_truncated_preview(audit_store: Any) -> None:
    """Args above the byte cap collapse to a preview with the truncated marker."""
    logger = _make_logger(audit_store, "args-cap")
    huge_blob = "y" * (512 * 1024)  # 512 KiB > 256 KiB cap
    msg = AIMessage(
        content="calling tool",
        tool_calls=[{"id": "tc1", "name": "ingest", "args": {"blob": huge_blob}}],
    )

    logger.log(namespace=(), mode="messages", data=(msg, {}))
    logger.flush()

    rows = [r for r in _rows(audit_store, "args-cap") if r.get("kind") == "tool_call"]
    assert len(rows) == 1
    assert rows[0]["args_truncated"] is True
    assert "preview" in rows[0]["args"]
    assert huge_blob not in str(rows[0]["args"])


def test_tool_result_content_is_truncated_at_2000_chars(audit_store: Any) -> None:
    logger = _make_logger(audit_store, "result-trunc")
    tool_msg = ToolMessage(content="z" * 5000, name="read_file", tool_call_id="tc1")

    logger.log(namespace=(), mode="messages", data=(tool_msg, {}))
    logger.flush()

    rows = [r for r in _rows(audit_store, "result-trunc") if r.get("kind") == "tool_result"]
    assert len(rows) == 1
    assert len(rows[0]["content"]) == 2000
    assert rows[0]["tool_name"] == "read_file"


def test_thinking_is_captured_from_additional_kwargs(audit_store: Any) -> None:
    logger = _make_logger(audit_store, "thinking")
    msg = AIMessage(
        content="",
        additional_kwargs={"reasoning_content": "I should check the file first."},
    )

    logger.log(namespace=(), mode="messages", data=(msg, {}))
    logger.flush()

    rows = [r for r in _rows(audit_store, "thinking") if r.get("kind") == "thinking"]
    assert len(rows) == 1
    assert rows[0]["text"] == "I should check the file first."
    assert rows[0]["source"] == "additional_kwargs.reasoning_content"


def test_missing_reasoning_content_writes_no_thinking_row(audit_store: Any) -> None:
    logger = _make_logger(audit_store, "no-thinking")
    msg = AIMessage(content="plain answer")

    logger.log(namespace=(), mode="messages", data=(msg, {}))
    logger.flush()

    assert [r for r in _rows(audit_store, "no-thinking") if r.get("kind") == "thinking"] == []


def test_store_failure_is_fail_soft(audit_store: Any) -> None:
    """A broken store must not raise out of the writer (audit never kills a loop)."""

    class ExplodingStore:
        def append(self, thread_id: str, records: list[dict[str, Any]]) -> None:
            raise RuntimeError("store down")

    logger = ThreadAuditLogger(thread_id="boom", store=ExplodingStore())  # type: ignore[arg-type]
    logger.log_user_input("hello")  # buffered, no raise

    logger.flush()  # must not raise
    assert len(logger._buffer) == 1  # record retained for the next successful flush  # type: ignore[attr-defined]
