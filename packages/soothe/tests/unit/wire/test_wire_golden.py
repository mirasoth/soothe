"""Host-side wire golden contract test.

The golden fixture (``tests/wire_fixtures/wire_golden.json`` at the monorepo
root) is produced by the HOST implementation via ``scripts/gen_wire_golden.py``
and is byte-identical to the copy committed in the soothe-client-python repo
(``tests/fixtures/wire_golden.json``). Both sides pin the fixture's sha256, so
a wire-format change forces a coordinated regen + commit on both sides.

Run on the client side too:
    cd client/python && uv run pytest tests/unit/test_wire_golden.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from soothe.display.card_ledger import card_from_wire_dict, card_to_wire_dict
from soothe.wire.codec import messages_from_wire_dicts, serialize_langchain_message_for_wire
from soothe.wire.protocol import decode_websocket_text, encode_websocket_text

# <root>/packages/soothe/tests/unit/wire/test_wire_golden.py → <root>/tests/wire_fixtures/
FIXTURE = Path(__file__).resolve().parents[5] / "tests" / "wire_fixtures" / "wire_golden.json"
FIXTURE_SHA256 = "ba5a5a408fc2c259b768eae2915289de1820a931da37aad30fdd410d52b16627"

MESSAGE_CASES = ("ai_message_with_tool_calls", "ai_chunk", "human_message", "tool_message")
ENVELOPE_CASES = ("connection_init", "response", "error")


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_is_pinned() -> None:
    """The committed fixture must match the pinned sha256 (and the client copy)."""
    digest = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert digest == FIXTURE_SHA256, (
        "wire golden fixture changed — regenerate via "
        "`uv run python scripts/gen_wire_golden.py`, then copy the file to "
        "client/python/tests/fixtures/wire_golden.json and update FIXTURE_SHA256 "
        "in BOTH host and client contract tests"
    )


def test_response_wire_text_is_stable() -> None:
    doc = _load()
    # Byte-exact decode of the pinned frame text (key order is not semantic;
    # the encode assertion compares parsed payloads, not raw text).
    assert decode_websocket_text(doc["response_wire_text"]) == doc["cases"]["response"]
    assert json.loads(encode_websocket_text(doc["cases"]["response"])) == doc["cases"]["response"]


@pytest.mark.parametrize("case", ENVELOPE_CASES)
def test_envelope_round_trip(case: str) -> None:
    doc = _load()
    envelope = doc["cases"][case]
    assert decode_websocket_text(encode_websocket_text(envelope)) == envelope


def test_batch_round_trip() -> None:
    doc = _load()
    batch = doc["cases"]["batch"]
    text = json.dumps(batch)
    assert decode_websocket_text(text) == batch


@pytest.mark.parametrize("case", MESSAGE_CASES)
def test_message_serde_is_idempotent_and_matches_golden(case: str) -> None:
    """deserialize(golden) → serialize must reproduce the golden wire dict."""
    doc = _load()
    wire = doc["cases"][case]
    (message,) = messages_from_wire_dicts([wire])
    assert serialize_langchain_message_for_wire(message) == wire


def test_ai_tool_calls_survive_round_trip() -> None:
    doc = _load()
    wire = doc["cases"]["ai_message_with_tool_calls"]
    (message,) = messages_from_wire_dicts([wire])
    assert message.tool_calls == [
        {
            "name": "read_file",
            "args": {"path": "docs/README.md", "offset": 0},
            "id": "call_golden_1",
            "type": "tool_call",
        }
    ]


def test_chunk_identity_survives_round_trip() -> None:
    from langchain_core.messages import AIMessageChunk

    doc = _load()
    (message,) = messages_from_wire_dicts([doc["cases"]["ai_chunk"]])
    assert isinstance(message, AIMessageChunk)


def test_card_frame_round_trip() -> None:
    doc = _load()
    frame = doc["cases"]["card_frame"]
    card = card_from_wire_dict(frame)
    assert card_to_wire_dict(card) == frame
