"""Tests for assistant identity prompt helpers."""

from __future__ import annotations

from soothe.prompts import PromptIdentity


def test_normalize_name_defaults_to_soothe() -> None:
    assert PromptIdentity.normalize_name("") == "Soothe"
    assert PromptIdentity.normalize_name("  MyBot  ") == "MyBot"


def test_identity_block_includes_name() -> None:
    block = PromptIdentity.identity_block("Soothe")
    assert "Soothe" in block
    assert "ASSISTANT_IDENTITY" in block


def test_prepend_identity() -> None:
    out = PromptIdentity.prepend_identity("Do the task.", "Soothe")
    assert "Soothe" in out
    assert "Do the task." in out
