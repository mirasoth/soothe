"""Unit tests for the shared tool-approval decision vocabulary (IG-LOOP-018)."""

from __future__ import annotations

from soothe.sloop.clarification.approval_options import (
    APPROVE_LABEL,
    APPROVE_REMEMBER_LABEL,
    REJECT_LABEL,
    decision_options,
    is_approve_answer,
    is_remember_answer,
)


class TestTokens:
    def test_approve_family(self) -> None:
        for token in ("approve", "Approve", "Allow", "yes", "OK", " proceed "):
            assert is_approve_answer(token), token

    def test_remember_tokens_are_approve_family(self) -> None:
        for token in ("Approve & remember", "approve_remember", "always"):
            assert is_approve_answer(token), token

    def test_remember_tokens(self) -> None:
        assert is_remember_answer("Approve & remember")
        assert is_remember_answer("approve_remember")
        assert is_remember_answer("ALWAYS")

    def test_plain_approve_is_not_remember(self) -> None:
        assert not is_remember_answer("approve")
        assert not is_remember_answer("Allow")

    def test_reject_and_prose_are_neither(self) -> None:
        assert not is_approve_answer("reject")
        assert not is_approve_answer("I do not approve")
        assert not is_remember_answer("reject")


class TestDecisionOptions:
    def test_run_command_offers_remember(self) -> None:
        options = decision_options("run_command", {"command": "pytest -xvs"})
        labels = [o["label"] for o in options]
        assert labels == [APPROVE_LABEL, APPROVE_REMEMBER_LABEL, REJECT_LABEL]
        remember = options[1]
        assert "description" in remember

    def test_path_tool_offers_remember(self) -> None:
        options = decision_options("write_file", {"file_path": "/src/a.py"})
        labels = [o["label"] for o in options]
        assert labels == [APPROVE_LABEL, APPROVE_REMEMBER_LABEL, REJECT_LABEL]

    def test_non_rememberable_tool_offers_plain_pair(self) -> None:
        options = decision_options("get_weather", {"city": "SF"})
        labels = [o["label"] for o in options]
        assert labels == [APPROVE_LABEL, REJECT_LABEL]

    def test_path_without_directory_offers_plain_pair(self) -> None:
        options = decision_options("write_file", {"file_path": "notes.md"})
        labels = [o["label"] for o in options]
        assert labels == [APPROVE_LABEL, REJECT_LABEL]
