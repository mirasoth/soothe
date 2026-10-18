#!/usr/bin/env python3
"""Generate the wire golden fixture shared by host and client contract tests.

Canonical producer is the HOST implementation (``soothe.wire`` + ``soothe.display``).
The same JSON file is committed to both the monorepo (``tests/wire_fixtures/``)
and the soothe-client-python repo (``tests/fixtures/``); each side's test
asserts the fixture's sha256 against a shared constant, so updating the wire
format forces a coordinated update on both sides.

Usage:
    uv run python scripts/gen_wire_golden.py [--check]

``--check`` re-runs the generator and fails when the committed fixture drifted
(only unstable fields would cause that — treat a failure as a red flag).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from soothe.display.card_ledger import card_to_wire_dict
from soothe.display.transcript_types import MessageData, ToolStatus
from soothe.display.transcript_types import MessageType as MsgType
from soothe.wire.codec import serialize_langchain_message_for_wire
from soothe.wire.protocol import encode_websocket_text

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "wire_fixtures" / "wire_golden.json"

FIXTURE_SCHEMA = "soothe.wire.golden/1"


def build_cases() -> dict[str, dict]:
    """Produce every golden case from the host implementation."""
    ai_with_tools = AIMessage(
        content="Reading the ledger now.",
        tool_calls=[
            {
                "name": "read_file",
                "args": {"path": "docs/README.md", "offset": 0},
                "id": "call_golden_1",
            }
        ],
        id="golden-ai-1",
    )
    ai_chunk = AIMessageChunk(content="delta text ", id="golden-chunk-1")
    human = HumanMessage(content="Summarize the plan", id="golden-human-1")
    tool = ToolMessage(
        content="# Title\nBody",
        tool_call_id="call_golden_1",
        name="read_file",
        id="golden-tool-1",
    )

    card = MessageData(
        type=MsgType.TOOL,
        content="",
        id="card_golden_1",
        tool_name="read_file",
        tool_args={"path": "docs/README.md"},
        tool_status=ToolStatus.SUCCESS,
        tool_output="# Title\nBody",
        timestamp="2026-10-13T00:00:00Z",
    )

    stream_update = {
        "type": "soothe.stream.tool_call.update",
        "tool_call_id": "s1:s:read_file:0",
        "tool_name": "read_file",
        "args": {"path": "docs/README.md"},
    }

    return {
        "connection_init": {
            "type": "connection_init",
            "id": "golden-init",
            "payload": {
                "client_name": "golden",
                "client_version": "0.0.0",
                "loop_id": "loop_golden",
            },
        },
        "response": {
            "type": "response",
            "id": "golden-resp",
            "payload": {"ok": True, "result": {"value": 42}},
        },
        "error": {
            "type": "error",
            "id": "golden-err",
            "payload": {"code": "not_found", "message": "loop missing"},
        },
        "batch": [
            {"type": "ping", "id": "golden-ping", "payload": {}},
            {"type": "pong", "id": "golden-pong", "payload": {}},
        ],
        "ai_message_with_tool_calls": serialize_langchain_message_for_wire(ai_with_tools),
        "ai_chunk": serialize_langchain_message_for_wire(ai_chunk),
        "human_message": serialize_langchain_message_for_wire(human),
        "tool_message": serialize_langchain_message_for_wire(tool),
        "card_frame": card_to_wire_dict(card),
        "stream_tool_update": stream_update,
    }


def main() -> int:
    cases = build_cases()
    wire_text = encode_websocket_text(cases["response"])
    doc = {
        "schema": FIXTURE_SCHEMA,
        "note": "Golden wire payloads; canonical producer is the host soothe.wire/soothe.display. "
        "The sha256 of this file is pinned in both the host and client contract tests.",
        "response_wire_text": wire_text,
        "cases": cases,
    }
    text = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

    if "--check" in sys.argv:
        current = FIXTURE_PATH.read_text(encoding="utf-8")
        if hashlib.sha256(current.encode("utf-8")).hexdigest() != digest:
            print("FAILED: committed wire golden fixture drifted from generator output")
            print("  expected sha256:", digest)
            print("  Regenerate with: uv run python scripts/gen_wire_golden.py")
            return 1
        print(f"OK: wire golden fixture matches generator (sha256={digest})")
        return 0

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(text, encoding="utf-8")
    print(f"wrote {FIXTURE_PATH}")
    print(f"sha256={digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
